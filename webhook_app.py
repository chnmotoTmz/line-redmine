import google.generativeai as genai
from google.generativeai.types import GenerationConfig, Tool
import json
import asyncio
import uvicorn
import sys
import os
import httpx
import dotenv
from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager
# LINE Bot SDK v3の正しいインポート
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage as V3TextMessage
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
# 正しいインポートパスに修正
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timezone, timedelta
import traceback  # traceback をインポートに追加

# --- 設定の読み込みと検証 ---
dotenv.load_dotenv()

# 環境変数を定数として定義
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
REDMINE_URL = os.environ.get("REDMINE_URL")
REDMINE_PUBLIC_URL = os.environ.get("REDMINE_PUBLIC_URL", REDMINE_URL)  # 追加: 公開用URL
REDMINE_API_KEY = os.environ.get("REDMINE_API_KEY")
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
WEBHOOK_PORT = os.environ.get("WEBHOOK_PORT", "8000")

# 必要な環境変数のチェック
if not all([GOOGLE_API_KEY, REDMINE_URL, REDMINE_API_KEY, LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET]):
    print("CRITICAL: Required environment variables are missing")
    sys.exit(1)

# --- LINE Bot v3の設定 ---
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- グローバル変数の準備 ---
PRIORITY_IDS = {}
scheduler = AsyncIOScheduler(timezone="Asia/Tokyo") # タイムゾーンを指定

# --- Lifespan Events ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global PRIORITY_IDS
    print("=== Verifying API connections on startup ===")
    
    # 1. Google APIキーの有効性を最終チェック
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        genai.GenerativeModel("gemini-2.5-flash").generate_content("Hello")
        print("✓ Google API key is valid.")
    except Exception as e:
        print(f"✗ CRITICAL: Google API key is invalid. Error: {e}")
        sys.exit(1)

    # 2. Redmineから優先度IDを最終チェック
    print("Fetching Redmine priority IDs directly...", flush=True)
    result = list_issue_priorities()

    if result.get("error"):
        print(f"✗ CRITICAL: Failed to fetch priority IDs. Error: {result['error']}")
        sys.exit(1)
    
    priorities = result.get("body", {}).get("issue_priorities", [])
    if not priorities:
        print(f"✗ CRITICAL: 'issue_priorities' not found in response: {result}")
        sys.exit(1)

    priority_map = {p["name"]: p["id"] for p in priorities}
    print(f"✓ Successfully fetched priority IDs: {priority_map}")
    
    # '通常' と '急いで' の存在を確認
    if "通常" not in priority_map or "急いで" not in priority_map:
        print("✗ CRITICAL: Could not find '通常' or '急いで' in Redmine priorities.")
        sys.exit(1)
    
    PRIORITY_IDS = priority_map
    print("\n=== Application startup successful! ===")

    # スケジューラのジョブ追加と開始
    scheduler.add_job(check_and_notify_overdue_tickets, CronTrigger(hour=8, minute=0, timezone="Asia/Tokyo"))
    if not scheduler.running:
         scheduler.start()
         print("Scheduler started.")
    else:
        print("Scheduler already running.")
    
    yield
    
    # Shutdown
    if scheduler.running:
        scheduler.shutdown()
        print("Scheduler stopped.")

app = FastAPI(lifespan=lifespan)

# --- Redmineツール ---
def redmine_request(path: str, method: str = 'get', data: dict = None):
    url = f"{REDMINE_URL}{path}"
    headers = {'X-Redmine-API-Key': REDMINE_API_KEY, 'Content-Type': 'application/json'}
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.request(method=method.lower(), url=url, json=data, headers=headers)
            response.raise_for_status()
            return {"status_code": response.status_code, "body": response.json() if response.status_code != 204 else None, "error": ""}
    except httpx.HTTPStatusError as e:
        return {"status_code": e.response.status_code, "body": e.response.text, "error": f"HTTP Error: {e}"}
    except Exception as e:
        return {"status_code": 0, "body": None, "error": f"Unexpected Error: {e}"}

def create_issue(project_id: int, subject: str, description: str, priority_id: int):
    issue_data = {"issue": {"project_id": project_id, "subject": subject, "description": description, "priority_id": priority_id}}
    return redmine_request(path="/issues.json", method="post", data=issue_data)

def list_issue_priorities():
    return redmine_request(path="/enumerations/issue_priorities.json", method="get")

# --- プッシュ通知機能 ---
async def check_and_notify_overdue_tickets():
    """
    Redmineの未完了チケット（期限切れまたは本日期限）をチェックし、LINEでプッシュ通知する。
    """
    my_line_user_id = os.environ.get("MY_LINE_USER_ID")
    if not my_line_user_id:
        print("ERROR: MY_LINE_USER_ID is not set. Cannot send push notifications.")
        return

    # タイムゾーンをJSTで取得し、今日の日付を文字列にする
    jst = timezone(timedelta(hours=9))
    today_jst = datetime.now(jst)
    today_jst_str = today_jst.strftime('%Y-%m-%d')
    
    print(f"Current JST date: {today_jst_str}")  # デバッグ用ログ追加

    # .envから未完了ステータスのIDを取得
    open_status_ids = os.environ.get("REDMINE_OPEN_STATUS_IDS")
    if not open_status_ids:
        print("WARNING: REDMINE_OPEN_STATUS_IDS is not set. Push notification might not work as expected.")
        return

    # Redmine APIで "本日以前が期日の未完了チケット" を取得するクエリ
    path = f"/issues.json?status_id={open_status_ids}&due_date=<={today_jst_str}&sort=due_date:asc"
    print(f"Fetching overdue tickets with path: {path}")
    result = redmine_request(path=path, method="get")

    messages_to_send = []

    if result.get("error"):
        print(f"ERROR: Could not fetch overdue tickets from Redmine. Details: {result.get('error')}")
    else:
        issues = result.get("body", {}).get("issues", [])
        if issues:
            message = "【Redmine期限通知】\n以下のチケットが期限切れまたは本日期限です。\n\n"
            for issue in issues:
                due_date = issue.get('due_date', '期限未設定')
                message += f"- ID: {issue['id']}, 件名: {issue['subject']}, 期限: {due_date}\n"
            messages_to_send.append(message)
        else:
            print(f"No overdue or due today tickets found for date: {today_jst_str}")

    if messages_to_send:
        for msg_text in messages_to_send:
            try:
                with ApiClient(configuration) as api_client:
                    line_bot_api = MessagingApi(api_client)
                    # v3の正しいプッシュメッセージ送信方法
                    line_bot_api.push_message(
                        PushMessageRequest(
                            to=my_line_user_id,
                            messages=[V3TextMessage(text=msg_text)]
                        )
                    )
                print(f"Sent push notification to {my_line_user_id}")
            except Exception as e:
                print(f"Failed to send push message to {my_line_user_id}: {e}")

# --- FastAPIアプリケーションロジック ---

async def create_redmine_ticket_from_text(user_text: str, project_id: int = 1) -> str:
    try:
        # プロンプトを少し調整
        prompt = (f"以下のユーザーからの依頼内容を分析し、Redmineのチケットを作成してください。\n"
                  f"依頼内容に「雨」「緊急」「至急」「急ぎ」などの言葉が含まれる場合、優先度を 'Urgent' と判断してください。\n"
                  f"それ以外の場合は、優先度を 'Normal' としてください。\n\n"
                  f"チケットの件名（subject）と詳細（description）を生成してください。件名は依頼内容を要約したものにしてください。\n\n"
                  f"--- ユーザーの依頼内容 ---\n"
                  f"{user_text}\n\n"
                  f"--- 出力形式（この形式で出力してください） ---\n"
                  f"```json\n"
                  f"{{\n"
                  f'  "priority": "Urgent または Normal",\n'
                  f'  "subject": "チケットの件名",\n'
                  f'  "description": "チケットの詳細な内容"\n'
                  f"}}\n"
                  f"```")

        model = genai.GenerativeModel("gemini-2.5-flash")  # モデル名を統一
        
        # IOバウンドな処理を非同期に実行
        response = await asyncio.to_thread(model.generate_content, prompt)
        
        # Geminiからの応答をパース
        response_text = response.text.strip().replace("```json", "").replace("```", "")
        ticket_data = json.loads(response_text)
        
        priority_str = ticket_data.get("priority", "Normal").strip().lower()
        subject = ticket_data.get("subject", "件名なし").strip()
        description = ticket_data.get("description", user_text).strip()

        # 優先度をIDに変換
        priority_id_to_use = PRIORITY_IDS["急いで"] if "urgent" in priority_str else PRIORITY_IDS["通常"]

        # Redmine API呼び出し (これは同期的だが、短い処理なのでこのままでも可)
        result = create_issue(
            project_id=project_id,
            subject=subject,
            description=description,
            priority_id=priority_id_to_use
        )

        if result.get("error"):
            print(f"Redmine API Error: {result}") # エラーログを追加
            return f"チケット作成に失敗しました。\nエラー: {result.get('body', result.get('error'))}"
        
        ticket_info = result.get("body", {}).get("issue", {})
        ticket_id = ticket_info.get("id")

        if not ticket_id:
            print(f"Ticket ID not found in response: {result}") # エラーログを追加
            return "チケットは作成されましたが、IDの取得に失敗しました。"

        ticket_url = f"{REDMINE_URL}/issues/{ticket_id}"
        return f"チケットを作成しました！\n\nチケットID: {ticket_id}\nリンク: {ticket_url}"

    except json.JSONDecodeError as e:
        print(f"JSON Decode Error from Gemini response: {e}\nRaw Response: {response.text}")
        return "AIからの応答を解析できませんでした。もう一度試してください。"
    except Exception as e:
        print(f"Unexpected Error in create_redmine_ticket_from_text: {e}") # エラーログを追加
        return f"予期せぬエラーが発生しました。\n{str(e)}"

@app.post("/webhook")
async def webhook_handler_endpoint(request: Request): # 関数名を変更
    signature = request.headers.get('X-Line-Signature')
    if not signature:
        raise HTTPException(status_code=400, detail="X-Line-Signature header missing")
    
    body = await request.body()
    try:
        # ここでは同期的なハンドラを呼び出すだけ
        handler.handle(body.decode('utf-8'), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return 'OK'

# --- Geminiに教えるツールの定義 ---

def create_redmine_ticket(subject: str, description: str, priority_name: str = "通常"):
    """
    新しいRedmineチケットを作成します。
    Args:
        subject (str): チケットの件名。
        description (str): チケットの詳細な内容。
        priority_name (str): 優先度名 ('通常', '急いで'など)。
    """
    print(f"Executing: create_redmine_ticket(subject='{subject}', description='{description}', priority_name='{priority_name}')")
    priority_id = PRIORITY_IDS.get(priority_name, PRIORITY_IDS.get("通常"))
    
    # 既存の create_issue 関数を呼び出す
    result = create_issue(
        project_id=1,
        subject=subject,
        description=description,
        priority_id=priority_id
    )

    if result.get("error"):
        return json.dumps({
            "status": "error", 
            "message": f"チケット作成に失敗しました: {result.get('body', result.get('error'))}"
        })
    
    ticket_info = result.get("body", {}).get("issue", {})
    ticket_id = ticket_info.get("id")
    
    if not ticket_id:
        return json.dumps({
            "status": "error",
            "message": "チケットは作成されましたが、IDの取得に失敗しました。"
        })
    
    ticket_url = f"{REDMINE_PUBLIC_URL}/issues/{ticket_id}"  # 公開用URLで返す
    
    return json.dumps({
        "status": "success",
        "message": f"チケットを正常に作成しました！",
        "ticket_id": ticket_id,
        "ticket_url": ticket_url,
        "subject": subject,
        "priority": priority_name
    })

def search_redmine_issues(query: str = None, due_date: str = None, assigned_to_id: str = None):
    """
    キーワード、期日、担当者に基づいてRedmineの未完了チケットを検索します。
    Args:
        query (str, optional): 検索したいキーワード。件名に含まれるものを検索します。
        due_date (str, optional): 期日を指定します。'today' (今日), 'this_week' (今週)などが使えます。
        assigned_to_id (str, optional): 担当者IDを指定します。'me' (自分)が使えます。
    """
    print(f"Executing: search_redmine_issues(query='{query}', due_date='{due_date}', assigned_to_id='{assigned_to_id}')")
    
    params = []
    if query:
        params.append(f"subject=~{query}")
    
    if due_date:
        # タイムゾーンをJSTで取得
        jst = timezone(timedelta(hours=9))
        now_jst = datetime.now(jst)

        if due_date == 'today':
            today_str = now_jst.strftime('%Y-%m-%d')
            params.append(f"due_date={today_str}")
        elif due_date == 'this_week':
            start_of_week = now_jst - timedelta(days=now_jst.weekday())
            end_of_week = start_of_week + timedelta(days=6)
            # Redmineの日付範囲フィルタ '><' を使用
            params.append(f"due_date=><{start_of_week.strftime('%Y-%m-%d')}|{end_of_week.strftime('%Y-%m-%d')}")
    
    if assigned_to_id:
        if assigned_to_id == 'me':
            params.append("assigned_to_id=me")
        else:
            params.append(f"assigned_to_id={assigned_to_id}")

    if not params:
        return json.dumps({"status": "error", "message": "検索条件が指定されていません。"})

    # .envに REDMINE_OPEN_STATUS_IDS=1|2|3 のように設定されていることを期待
    open_status_ids = os.getenv("REDMINE_OPEN_STATUS_IDS")
    if open_status_ids:
        params.append(f"status_id={open_status_ids}")

    path = f"/issues.json?{'&'.join(params)}&sort=due_date:asc"
    print(f"Searching issues with path: {path}")
    result = redmine_request(path=path, method="get")

    if result.get("error"):
        return json.dumps({"status": "error", "message": result.get('body', result.get('error'))})

    issues = result.get("body", {}).get("issues", [])
    if not issues:
        search_terms_list = [f"キーワード「{query}」" if query else "", f"期日「{due_date}」" if due_date else "", f"担当者「{assigned_to_id}」" if assigned_to_id else ""]
        search_terms = "、".join(filter(None, search_terms_list))
        return json.dumps({"status": "not_found", "message": f"{search_terms}に一致する未完了チケットは見つかりませんでした。"})

    # 見つかったチケットの情報を要約して返す
    summarized_issues = [
        {"id": i["id"], "subject": i["subject"], "status": i["status"]["name"], "due_date": i.get("due_date", "未設定")}
        for i in issues
    ]
    return json.dumps({"status": "success", "issues": summarized_issues})

def get_ticket_summary(limit: int = 10, priority_order: str = "high_to_low", status_filter: str = "open"):
    """
    チケットの要約を優先度順で取得します。
    Args:
        limit (int): 取得するチケット数の上限（デフォルト: 10）
        priority_order (str): 優先度の並び順。'high_to_low'（高→低）または 'low_to_high'（低→高）
        status_filter (str): ステータスフィルタ。'open'（未完了のみ）、'all'（全て）
    """
    print(f"Executing: get_ticket_summary(limit={limit}, priority_order='{priority_order}', status_filter='{status_filter}')")
    
    # 現在の日付を取得
    jst = timezone(timedelta(hours=9))
    today_jst = datetime.now(jst)
    today_str = today_jst.strftime('%Y-%m-%d')
    
    print(f"Current date for analysis: {today_str}")  # デバッグ用
    
    params = []
    
    # ステータスフィルタを適用
    if status_filter == "open":
        open_status_ids = os.getenv("REDMINE_OPEN_STATUS_IDS")
        if open_status_ids:
            params.append(f"status_id={open_status_ids}")
    
    # 優先度順でソート
    params.append(f"sort=priority:desc,created_on:desc")
    
    # 取得件数の制限
    params.append(f"limit={limit}")
    
    path = f"/issues.json?{'&'.join(params)}"
    print(f"Fetching ticket summary with path: {path}")
    result = redmine_request(path=path, method="get")

    if result.get("error"):
        return json.dumps({"status": "error", "message": f"チケット取得に失敗しました: {result.get('body', result.get('error'))}"})

    issues = result.get("body", {}).get("issues", [])
    if not issues:
        return json.dumps({"status": "not_found", "message": "条件に一致するチケットは見つかりませんでした。"})

    # チケット情報をGeminiが分析しやすい形式で返す
    ticket_data = []
    overdue_count = 0
    due_today_count = 0
    
    for issue in issues:
        priority_name = issue.get("priority", {}).get("name", "未設定")
        status_name = issue.get("status", {}).get("name", "未設定")
        due_date = issue.get("due_date", "")
        
        # 期日の状況を分析
        date_status = "normal"
        if due_date:
            try:
                due_date_obj = datetime.strptime(due_date, '%Y-%m-%d').date()
                today_date = today_jst.date()
                
                if due_date_obj < today_date:
                    date_status = "overdue"
                    overdue_count += 1
                elif due_date_obj == today_date:
                    date_status = "due_today"
                    due_today_count += 1
            except ValueError:
                pass  # 日付形式が不正な場合はスキップ
        
        ticket_data.append({
            "id": issue["id"],
            "subject": issue["subject"],
            "priority": priority_name,
            "status": status_name,
            "due_date": due_date if due_date else "未設定",
            "date_status": date_status,
            "created_on": issue.get("created_on", "")
        })
    
    return json.dumps({
        "status": "success", 
        "current_date": today_str,
        "total_count": len(ticket_data),
        "overdue_count": overdue_count,
        "due_today_count": due_today_count,
        "tickets": ticket_data,
        "instruction": f"現在の日付は{today_str}です。これらのチケット情報を基に、秘書として状況を整理し、類似タスクをまとめ、期日の緊急度（期限切れ{overdue_count}件、本日期限{due_today_count}件）を考慮した実用的なアドバイスを含めて報告してください。期日が古い（2025年6月など）場合は期限切れとして扱ってください。"
    })

# --- 会話処理のメインロジック ---

# Geminiモデルとツールの設定 (グローバルに定義しておくと再利用しやすい)
gemini_tools = [
    Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="create_redmine_ticket",
                description="ユーザーの発言が命令形でなくても、ToDoや予定・依頼・思いつきなどタスク化できる内容ならRedmineチケットを作成する。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "subject": genai.protos.Schema(type=genai.protos.Type.STRING, description="チケットの件名"),
                        "description": genai.protos.Schema(type=genai.protos.Type.STRING, description="チケットの詳細な内容。ユーザーの依頼全体を含めること。"),
                        "priority_name": genai.protos.Schema(type=genai.protos.Type.STRING, description="優先度。'急いで' または '通常' を指定する。緊急性が高い場合は'急いで'を選ぶ。")
                    },
                    required=["subject", "description"]
                )
            ),
            genai.protos.FunctionDeclaration(
                name="search_redmine_issues",
                description="キーワード、期日、担当者に基づいて既存のRedmineチケットを検索する。複数の条件を組み合わせることも可能。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "query": genai.protos.Schema(type=genai.protos.Type.STRING, description="検索キーワード。件名に含まれる単語。"),
                        "due_date": genai.protos.Schema(type=genai.protos.Type.STRING, description="期日。'today'（今日）、'this_week'（今週）などを指定できる。"),
                        "assigned_to_id": genai.protos.Schema(type=genai.protos.Type.STRING, description="担当者。自分自身のチケットを検索する場合は 'me' を指定する。")
                    }
                )
            ),
            # ★★★ 新しいツール: チケット要約機能 ★★★
            genai.protos.FunctionDeclaration(
                name="get_ticket_summary",
                description="チケットの要約を優先度順で取得し、秘書として状況を分析・報告する。優先度の高いタスクから確認したい場合や、全体の状況を把握したい場合に使用。結果は類似タスクをまとめ、期日の緊急度を考慮して整理すること。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "limit": genai.protos.Schema(type=genai.protos.Type.INTEGER, description="取得するチケット数の上限（デフォルト: 10）"),
                        "priority_order": genai.protos.Schema(type=genai.protos.Type.STRING, description="優先度の並び順。'high_to_low'（高→低）または 'low_to_high'（低→高）。デフォルトは 'high_to_low'"),
                        "status_filter": genai.protos.Schema(type=genai.protos.Type.STRING, description="ステータスフィルタ。'open'（未完了のみ）または 'all'（全て）。デフォルトは 'open'")
                    }
                )
            ),
        ]
    )
]

# 会話履歴を保存するシンプルな辞書 (本番ではDBなどを使う)
conversation_history = {}

async def handle_conversation(user_id: str, user_text: str) -> str:
    """ユーザーとの対話を管理し、適切な応答やツール実行を行う（多段階エージェントループ対応＋function_call強制リトライ＋詳細ログ＋OK時一括チケット化）"""

    # 現在の日付を取得
    jst = timezone(timedelta(hours=9))
    current_date = datetime.now(jst).strftime('%Y年%m月%d日')
    
    # システムプロンプトの定義
    system_instruction = (
        f"あなたは、ユーザーの優秀な秘書兼アシスタントです。今日は{current_date}です。"
        "ユーザーが命令形でなくても、ToDo・やるべきこと・予定・希望・依頼・思いつきなど、"
        "タスク化できそうな発言があれば必ずRedmineチケット化function_callを発動してください。"
        "function_callを返さずにテキスト応答だけで済ませてはいけません。"
        "チケットの要約を求められた場合は、単なるリストではなく、秘書として状況を分析し、"
        "類似のタスクをまとめ、優先度を考慮した実用的なアドバイスを含めて報告してください。"
        "期日が今日より前の日付（例：2025年6月の日付）の場合は、期限切れとして適切に報告してください。"
        "例：'開発関連のタスクが3件、個人用務が2件、期日が今日のものが1件、期限切れが2件ございます。'"
        "「今日のタスク」「重要なもの」「チケット一覧」などの問い合わせには、get_ticket_summaryツールを使用し、"
        "その結果を基に、まるで有能な秘書が状況を整理して報告するような口調で回答してください。"
        "チケット作成時は必ずURLを含めて報告し、検索結果も分かりやすく整理して提示してください。"
        "ユーザーの意図を先読みし、効率的なタスク管理をサポートすることを心がけてください。"
        "機械的なリスト表示は避け、常に人間らしい温かみのある対応を心がけてください。"
    )

    # ユーザーごとの会話履歴を取得（なければ初期化）
    if user_id not in conversation_history:
        conversation_history[user_id] = [{"role": "model", "parts": [system_instruction]}]
    conversation_history[user_id].append({"role": "user", "parts": [user_text]})

    # --- OK時一括チケット化用の分割提案リスト記憶 ---
    if "_last_split_proposal" not in conversation_history:
        conversation_history["_last_split_proposal"] = {}

    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        tools=gemini_tools,
        generation_config=GenerationConfig(temperature=0.7)
    )
    chat = model.start_chat(history=conversation_history[user_id])

    max_steps = 3
    step = 0
    final_reply = ""
    last_tool_result = None
    function_call_retry_done = False # リトライ済みフラグ
    last_important_reply = None

    while step < max_steps:
        # ループの入力テキストを決定するロジックを簡素化
        if step == 0:
            # 最初のステップでは、ユーザーの入力をそのまま使う
            input_text_for_ai = user_text
        else:
            # 2回目以降のステップでは、自己反省を促す
            input_text_for_ai = (
                "前回のチケット化・分割・最適化の結果を踏まえ、"
                "他に分割すべきタスクや、追加でチケット化すべき内容、"
                "または依存関係・順序最適化の必要があれば提案してください。"
                "十分であれば「これで十分です」と返答してください。"
            )
            if last_tool_result:
                input_text_for_ai += f"\n\n【前回のチケット化・分割・最適化結果】\n{last_tool_result}"

        print(f"\n[AgentLoop] Step {step} - input_text to AI:\n{input_text_for_ai}\n")
        response = await asyncio.to_thread(chat.send_message, input_text_for_ai)

        # --- function_call強制リトライ（step 0の初回のみ） ---
        is_function_call_in_response = any(hasattr(part, 'function_call') and part.function_call for part in response.parts)
        if step == 0 and not function_call_retry_done and not is_function_call_in_response:
            print("[AgentLoop] No function_call detected on first attempt. Forcing retry with explicit instruction.")
            function_call_retry_done = True # リトライは一度しか行わない
            retry_prompt = (
                "上記の内容はRedmineチケット化すべきです。"
                "必ずfunction_callでcreate_redmine_ticketを呼び出してください。"
            )
            response = await asyncio.to_thread(chat.send_message, retry_prompt)
            print(f"[AgentLoop] Retry response received.")
            # レスポンスが更新されたので、再度function_callの有無をチェック
            is_function_call_in_response = any(hasattr(part, 'function_call') and part.function_call for part in response.parts)

        # --- 分割提案リストを記憶 ---
        if not is_function_call_in_response:
            text_content = "".join([part.text for part in response.parts if hasattr(part, 'text')])
            import re
            split_tasks = re.findall(r"\*\*(.+?)\*\*", text_content)
            if split_tasks:
                conversation_history["_last_split_proposal"][user_id] = split_tasks
                print(f"[AgentLoop] Detected split proposal: {split_tasks}")

        # --- OK時一括チケット化 ---
        if user_text.strip().lower() in ["ok", "ｏｋ", "はい", "はい。", "了解", "了解です", "お願いします", "お願い", "よろしいです", "よろしいです。", "それでいいよ", "それでok"]:
            split_tasks = conversation_history["_last_split_proposal"].get(user_id)
            if split_tasks:
                created = []
                for task in split_tasks:
                    res = create_redmine_ticket(subject=task, description=task, priority_name="通常")
                    try:
                        res_json = json.loads(res)
                        if res_json.get("status") == "success":
                            created.append(f"・{res_json['subject']}\n  {res_json['ticket_url']}")
                    except Exception as e:
                        created.append(f"・{task}\n  (作成失敗) {e}")
                if created:
                    reply = "以下のタスクをRedmineチケットとして一括登録しました。\n\n" + "\n".join(created)
                    print(f"[AgentLoop] Bulk ticket creation reply: {reply}")
                    return reply
                else:
                    return "チケット作成に失敗しました。"

        # --- 応答の処理 ---
        final_reply = ""
        tool_called = False
        for part in response.parts:
            if hasattr(part, 'function_call') and part.function_call:
                tool_called = True
                fc = part.function_call
                print(f"[AgentLoop] Gemini function_call: {fc.name} args={fc.args}")
                tool_name = fc.name
                tool_args = {key: value for key, value in fc.args.items()}

                if tool_name == "create_redmine_ticket":
                    tool_result = create_redmine_ticket(**tool_args)
                elif tool_name == "search_redmine_issues":
                    tool_result = search_redmine_issues(**tool_args)
                elif tool_name == "get_ticket_summary":
                    tool_result = get_ticket_summary(**tool_args)
                else:
                    tool_result = json.dumps({"status": "error", "message": f"Unknown tool: {tool_name}"})

                print(f"[AgentLoop] Tool executed: {tool_name} args={tool_args}\nResult: {tool_result}")
                last_tool_result = tool_result

                feedback_response = await asyncio.to_thread(
                    chat.send_message,
                    genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name=tool_name,
                            response={"result": tool_result}
                        )
                    )
                )

                # ツール実行後の応答を処理
                for feedback_part in feedback_response.parts:
                    if hasattr(feedback_part, 'text') and feedback_part.text:
                        final_reply += feedback_part.text
                last_important_reply = final_reply
                print(f"[AgentLoop] AI reply after tool: {final_reply}")
                break # 1回のループでツールコールは1つと仮定
            
            elif hasattr(part, 'text') and part.text:
                final_reply += part.text

        if not tool_called:
             print(f"[AgentLoop] AI text reply: {final_reply}")

        if "これで十分" in final_reply or "十分です" in final_reply or "追加のチケットはありません" in final_reply:
            print(f"[AgentLoop] AI judged as sufficient. Breaking loop at step {step}.")
            break
        step += 1

    conversation_history[user_id] = chat.history
    if final_reply.strip() in ["これで十分です。", "これで十分です", "十分です。", "十分です", "追加のチケットはありません。", "追加のチケットはありません"] and last_important_reply:
        print("[AgentLoop] Returning last important reply instead of generic '十分' message.")
        return last_important_reply
    print(f"[AgentLoop] Final AI reply to user:\n{final_reply}\n")
    return final_reply

# --- `handle_message` (LINEからのWebhook) の修正 ---
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id # ユーザーを識別するためにIDを取得
    user_message = event.message.text
    print(f"Received message from {user_id}: {user_message}")

    loop = asyncio.get_event_loop()
    
    async def task():
        try:
            # 新しい会話処理関数を呼び出す
            reply_message = await handle_conversation(user_id, user_message)
            
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[V3TextMessage(text=reply_message)]
                    )
                )
        except Exception as e:
            print(f"Error in async task for LINE message: {e}")
            try:
                with ApiClient(configuration) as api_client:
                    line_bot_api = MessagingApi(api_client)
                    line_bot_api.reply_message(
                        ReplyMessageRequest(
                            reply_token=event.reply_token,
                            messages=[V3TextMessage(text=f"処理中にエラーが発生しました: {e}")]
                        )
                    )
            except Exception as reply_e:
                print(f"Failed to even send error reply: {reply_e}")

    loop.create_task(task())

# ★★★ `if __name__ == "__main__"` ブロックの修正 ★★★
if __name__ == "__main__":
    print(f"🚀 Starting LINE Bot server on http://0.0.0.0:{WEBHOOK_PORT}")
    try:
        import httpx
        from linebot.v3.messaging import MessagingApi
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError:
        print("\n!!! httpx, line-bot-sdk or apscheduler is not installed. Please run: pip install httpx line-bot-sdk python-dotenv apscheduler !!!\n")
        sys.exit(1)
        
    # reloadオプションを使用する場合は、アプリケーションを文字列で指定
    uvicorn.run("webhook_app:app", host="0.0.0.0", port=int(WEBHOOK_PORT), log_level="info", reload=True)