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
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

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

# --- グローバル変数の準備 ---
app = FastAPI()
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
PRIORITY_IDS = {}

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

# --- FastAPIアプリケーションロジック ---

@app.on_event("startup")
async def startup_event():
    global PRIORITY_IDS
    print("=== Verifying API connections on startup ===")
    
    # 1. Google APIキーの有効性を最終チェック
    try:
        # モデルを gemini-1.5-flash に変更
        genai.configure(api_key=GOOGLE_API_KEY)
        genai.GenerativeModel("gemini-1.5-flash").generate_content("Hello")
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

        model = genai.GenerativeModel("gemini-1.5-flash")
        
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
        return json.dumps({"status": "error", "message": result.get('body', result.get('error'))})
    
    ticket_info = result.get("body", {}).get("issue", {})
    ticket_id = ticket_info.get("id")
    ticket_url = f"{REDMINE_URL}/issues/{ticket_id}" if ticket_id else ""
    
    return json.dumps({
        "status": "success",
        "ticket_id": ticket_id,
        "url": ticket_url
    })

def search_redmine_issues(query: str):
    """
    キーワードに基づいてRedmineのチケットを検索します。
    Args:
        query (str): 検索したいキーワード (例: 'バス', 'エアコン')。
    """
    print(f"Executing: search_redmine_issues(query='{query}')")
    # RedmineのAPIでは件名(subject)でのフィルタリングが直接使える
    path = f"/issues.json?subject=~{query}" 
    result = redmine_request(path=path, method="get")

    if result.get("error"):
        return json.dumps({"status": "error", "message": result.get('body', result.get('error'))})

    issues = result.get("body", {}).get("issues", [])
    if not issues:
        return json.dumps({"status": "not_found", "message": f"キーワード「{query}」に一致するチケットは見つかりませんでした。"})

    # 見つかったチケットの情報を要約して返す
    summarized_issues = [
        {"id": i["id"], "subject": i["subject"], "status": i["status"]["name"]}
        for i in issues
    ]
    return json.dumps({"status": "success", "issues": summarized_issues})

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
            # ★★★ 新しいツールの定義を追加 ★★★
            genai.protos.FunctionDeclaration(
                name="search_redmine_issues",
                description="キーワードを使って既存のRedmineチケットを検索する。",
                parameters=genai.protos.Schema(
                    type=genai.protos.Type.OBJECT,
                    properties={
                        "query": genai.protos.Schema(type=genai.protos.Type.STRING, description="検索キーワード。ユーザーの質問から最も重要と思われる単語を抽出する。")
                    },
                    required=["query"]
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
        "あなたはRedmineを操作する優秀なアシスタントです。"
        "ユーザーとの自然な対話を通じて、タスクを処理してください。"
        "ユーザーからの情報が不十分な場合は、チケットを作成する前に質問して詳細を確認してください。"
        "例えば、「PCの調子が悪い」と言われたら、「具体的にどのような状況ですか？」と聞き返してください。"
        "ユーザーがチケット作成を明確に依頼した場合、または必要な情報が揃ったと判断した場合にのみ、create_redmine_ticketツールを使用してください。"
    )

    # ユーザーごとの会話履歴を取得（なければ初期化）
    if user_id not in conversation_history:
        # 会話の最初にシステムプロンプトを設定
        conversation_history[user_id] = [{"role": "model", "parts": [system_instruction]}]

    # 今回のユーザー発言を履歴に追加
    conversation_history[user_id].append({"role": "user", "parts": [user_text]})

    # Geminiモデルを初期化
    # Geminiモデルを初期化
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash", # Tool Callingには高機能なモデルが適している
        tools=gemini_tools,
        generation_config=GenerationConfig(temperature=0.7)
    )
    
    # 会話履歴を使ってチャットセッションを開始
    chat = model.start_chat(history=conversation_history[user_id])

    try:
        # ユーザーメッセージを送信
        response = await asyncio.to_thread(chat.send_message, user_text)
        
        # --- Geminiからの応答を解析 ---
        response_part = response.parts[0]

        # 1. ツールを使うように指示された場合
        if response_part.function_call:
            fc = response_part.function_call
            tool_name = fc.name
            tool_args = {key: value for key, value in fc.args.items()}

            tool_result = "" # 初期化
            if tool_name == "create_redmine_ticket":
                tool_result = create_redmine_ticket(**tool_args)
            # ★★★ 新しいツールの処理を追加 ★★★
            elif tool_name == "search_redmine_issues":
                tool_result = search_redmine_issues(**tool_args)
            else:
                tool_result = json.dumps({"status": "error", "message": f"Unknown tool: {tool_name}"})

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
        
        # 2. 通常のテキスト応答の場合
        else:
            final_reply = response.text

        # 今回のAIの応答を履歴に追加
        conversation_history[user_id] = chat.history
        
        return final_reply

    except Exception as e:
        print(f"Error during conversation: {e}")
        # エラーが発生したら履歴をリセットするなどの処理も検討
        if user_id in conversation_history:
            del conversation_history[user_id]
        return "申し訳ありません、処理中にエラーが発生しました。もう一度お試しください。"

# --- `handle_message` (LINEからのWebhook) の修正 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id # ユーザーを識別するためにIDを取得
    user_message = event.message.text
    print(f"Received message from {user_id}: {user_message}")

    loop = asyncio.get_event_loop()
    
    async def task():
        try:
            # 新しい会話処理関数を呼び出す
            reply_message = await handle_conversation(user_id, user_message)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_message))
        except Exception as e:
            print(f"Error in async task for LINE message: {e}")
            try:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"処理中にエラーが発生しました: {e}"))
            except Exception as reply_e:
                print(f"Failed to even send error reply: {reply_e}")

    loop.create_task(task())

# ★★★ `if __name__ == "__main__"` ブロックの修正 ★★★
if __name__ == "__main__":
    print(f"🚀 Starting LINE Bot server on http://0.0.0.0:{WEBHOOK_PORT}")
    try:
        import httpx
        from linebot import LineBotApi
    except ImportError:
        print("\n!!! httpx or line-bot-sdk is not installed. Please run: pip install httpx line-bot-sdk python-dotenv !!!\n")
        sys.exit(1)
        
    # uvicorn.runの第一引数を文字列ではなく、appオブジェクトに修正
    uvicorn.run(app, host="0.0.0.0", port=int(WEBHOOK_PORT), log_level="info")