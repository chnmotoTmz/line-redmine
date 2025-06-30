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
    today_jst_str = datetime.now(jst).strftime('%Y-%m-%d')

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
            print("No overdue or due today tickets found.")

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
    
    ticket_url = f"{REDMINE_URL}/issues/{ticket_id}"
    
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
    
    params = []
    
    # ステータスフィルタを適用
    if status_filter == "open":
        open_status_ids = os.getenv("REDMINE_OPEN_STATUS_IDS")
        if open_status_ids:
            params.append(f"status_id={open_status_ids}")
    
    # 優先度順でソート（priority.id を使用）
    sort_order = "desc" if priority_order == "high_to_low" else "asc"
    params.append(f"sort=priority:desc,created_on:desc")  # 優先度順、次に作成日順
    
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

    # チケット情報を要約して返す
    summarized_issues = []
    for issue in issues:
        ticket_url = f"{REDMINE_URL}/issues/{issue['id']}"
        priority_name = issue.get("priority", {}).get("name", "未設定")
        status_name = issue.get("status", {}).get("name", "未設定")
        due_date = issue.get("due_date", "未設定")
        
        summarized_issues.append({
            "id": issue["id"],
            "subject": issue["subject"],
            "priority": priority_name,
            "status": status_name,
            "due_date": due_date,
            "url": ticket_url,
            "created_on": issue.get("created_on", "")
        })
    
    return json.dumps({
        "status": "success", 
        "total_count": len(summarized_issues),
        "issues": summarized_issues,
        "filter_info": f"優先度順: {priority_order}, ステータス: {status_filter}, 件数: {limit}"
    })

# --- 会話処理のメインロジック ---

# Geminiモデルとツールの設定 (グローバルに定義しておくと再利用しやすい)
gemini_tools = [
    Tool(
        function_declarations=[
            genai.protos.FunctionDeclaration(
                name="create_redmine_ticket",
                description="ユーザーの依頼に基づいて新しいRedmineチケットを作成する。",
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
                description="チケットの要約を優先度順で取得する。優先度の高いタスクから確認したい場合や、全体の状況を把握したい場合に使用。",
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
    """ユーザーとの対話を管理し、適切な応答やツール実行を行う"""
    
    # システムプロンプトの定義
    system_instruction = (
        "あなたは、ユーザーの意図を積極的に汲み取り、先回りして行動する非常に優秀なRedmineアシスタントです。"
        "ユーザーの曖昧な依頼からでも、タスクの目的を推測し、チケット作成や検索を自律的に実行してください。"
        "ユーザーから提供された情報は、それが断片的であっても、まずはチケットとして記録することを優先します。情報の不足を理由に、何度も質問を繰り返さないでください。"
        "例えば、バスの予約情報が共有されたら、それを「移動の記録」や「リマインダー」として解釈し、適切な件名と説明で `create_redmine_ticket` を実行してください。目的をユーザーに聞き返す必要はありません。"
        "「〇〇を忘れないように」という依頼も、そのままチケットの件名や説明にして記録してください。"
        "「任せます」と言われたら、あなたの判断で最適なアクションを実行し、その結果を報告してください。"
        "「今日のタスク」「〇〇の件はどうなってる？」のような問い合わせには、`search_redmine_issues` ツールを積極的に使用してください。キーワードだけでなく、日付（'today', 'this_week'など）や担当者（'me'）も指定できることを理解し、ユーザーの言葉から適切な引数を判断してください。"
        "「優先度の高いタスク」「重要なチケット」「チケット一覧」「要約」などの問い合わせには、`get_ticket_summary` ツールを使用してください。優先度順でチケットを表示し、各チケットのURLも含めて報告してください。"
        "**重要**: チケット作成が成功した場合は、必ずチケットのURLを含めてユーザーに報告してください。「チケットを作成しました！」だけでなく、「チケットID: XXX」「URL: https://...」のように具体的な情報を提供してください。"
        "ツール実行後は、その結果（例：作成したチケットのURL、検索結果の要約）を、簡潔で分かりやすい言葉でユーザーに伝えてください。"
    )

    # ユーザーごとの会話履歴を取得（なければ初期化）
    if user_id not in conversation_history:
        # 会話の最初にシステムプロンプトを設定
        conversation_history[user_id] = [{"role": "model", "parts": [system_instruction]}]

    # 今回のユーザー発言を履歴に追加
    conversation_history[user_id].append({"role": "user", "parts": [user_text]})

    # Geminiモデルを初期化
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash", # Tool Callingには高機能なモデルが適している
        tools=gemini_tools,
        generation_config=GenerationConfig(temperature=0.7)
    )
    
    # 会話履歴を使ってチャットセッションを開始
    chat = model.start_chat(history=conversation_history[user_id])

    try:
        # ユーザーメッセージを送信
        response = await asyncio.to_thread(chat.send_message, user_text)
        
        # --- Geminiからの応答を解析 ---
        final_reply = ""
        
        # responseのpartsを確認してfunction_callがあるかチェック
        if response.parts and hasattr(response.parts[0], 'function_call') and response.parts[0].function_call:
            # 1. ツールを使うように指示された場合
            fc = response.parts[0].function_call
            tool_name = fc.name
            tool_args = {key: value for key, value in fc.args.items()}

            print(f"Tool called: {tool_name} with args: {tool_args}")

            tool_result = "" # 初期化
            if tool_name == "create_redmine_ticket":
                tool_result = create_redmine_ticket(**tool_args)
            elif tool_name == "search_redmine_issues":
                tool_result = search_redmine_issues(**tool_args)
            elif tool_name == "get_ticket_summary":
                tool_result = get_ticket_summary(**tool_args)
            else:
                tool_result = json.dumps({"status": "error", "message": f"Unknown tool: {tool_name}"})

            print(f"Tool result: {tool_result}")

            # ツール実行結果をGeminiにフィードバック
            feedback_response = await asyncio.to_thread(
                chat.send_message,
                genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=tool_name,
                        response={"result": tool_result}
                    )
                )
            )
            # Geminiが生成した最終的な返答を取得
            final_reply = feedback_response.text
        else:
            # 2. 通常のテキスト応答の場合
            final_reply = response.text

        # 今回のAIの応答を履歴に追加
        conversation_history[user_id] = chat.history
        
        return final_reply

    except Exception as e:
        print(f"Error during conversation: {e}")
        print(f"Error type: {type(e)}")
        import traceback
        traceback.print_exc()
        
        # エラーが発生したら履歴をリセットするなどの処理も検討
        if user_id in conversation_history:
            del conversation_history[user_id]
        return "申し訳ありません、処理中にエラーが発生しました。もう一度お試しください。"

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
        print("\n!!! httpx, linebot v3 or apscheduler is not installed. Please run: pip install httpx line-bot-sdk python-dotenv apscheduler !!!\n")
        sys.exit(1)
        
    # reloadオプションを使用する場合は、アプリケーションを文字列で指定
    uvicorn.run("webhook_app:app", host="0.0.0.0", port=int(WEBHOOK_PORT), log_level="info", reload=True)