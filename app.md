
だいたいね。いまある登録されたチケット自体が、忘れている場合がある、笑うよね。これを未然に教えてくれると嬉しい。こっちからメッセージするのではなくて
素晴らしい着眼点です！まさにその通りで、「登録したこと自体を忘れる」というのは、タスク管理ツールあるあるの根本的な課題ですよね（笑）。
ユーザー（あなた）が能動的に問い合わせる（プル型）のではなく、**Bot側から能動的に情報を届けてくれる（プッシュ型）**仕組みがあれば、アシスタントとしての価値が飛躍的に高まります。
これを実現するには、「定期実行（スケジューリング）」の仕組みを導入します。
実装方針：定期実行で未完了チケットを通知する
毎日決まった時間（例えば朝8時）に、Botが自動的にRedmineをチェックし、「今日が期日のチケット」や「期限が過ぎている未完了のチケット」をLINEに通知する機能を追加します。
これを実現するライブラリはいくつかありますが、非同期フレームワーク（FastAPI/Uvicorn）と相性が良く、比較的導入が簡単な apscheduler を使うのがおすすめです。
実装ステップ
ステップ1: apscheduler のインストール
まず、ライブラリをインストールします。非同期で動かすため AsyncIOScheduler を使います。
ターミナルで以下のコマンドを実行してください。
Generated bash
pip install apscheduler
Use code with caution.
Bash
ステップ2: コードの追加・修正
webhook_app.py に、スケジューリングのためのコードを追加していきます。
import文を追加
通知用の関数を作成
FastAPIのstartupイベントでスケジューラを開始
以下に、修正後のコードの全体像を示します。
Generated python
# --- (既存のimport文) ---
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
# ... 他のimport文 ...

# ★★★ ここからが追加・修正部分 ★★★

# --- グローバル変数 ---
app = FastAPI()
# ... (既存のグローバル変数) ...
scheduler = AsyncIOScheduler(timezone="Asia/Tokyo") # タイムゾーンを指定

# --- 通知用の関数 ---
async def check_and_notify_overdue_tickets():
    """
    期限切れまたは本日が期日の未完了チケットを確認し、LINEに通知する。
    """
    print(f"[{datetime.now()}] Running scheduled job: Checking overdue tickets...")
    
    # Redmine APIで本日以前が期日の未完了チケットを取得
    # "!" をつけると「未完了」というステータスを意味する (Redmineの仕様による)
    today = datetime.now().strftime('%Y-%m-%d')
    path = f"/issues.json?status_id=!*&due_date=<=|{today}"
    
    result = redmine_request(path=path, method="get")

    if result.get("error"):
        print(f"Scheduled job failed: Could not fetch issues. Error: {result.get('error')}")
        return

    issues = result.get("body", {}).get("issues", [])
    if not issues:
        print("No overdue or due-today tickets found. All clear!")
        return

    # 通知メッセージを作成
    message_lines = ["【📢 定期リマインダー】\n\n期限切れ、または本日が期日のチケットがあります！"]
    for issue in issues:
        due_date = issue.get('due_date', '期日未設定')
        line = (
            f"\n📝 {issue['subject']}\n"
            f"  - 担当: {issue['assigned_to']['name'] if 'assigned_to' in issue else '未割り当て'}\n"
            f"  - 状態: {issue['status']['name']}\n"
            f"  - 期日: {due_date}"
        )
        message_lines.append(line)
    
    final_message = "\n".join(message_lines)

    # あなたのLINEユーザーIDを環境変数から取得
    MY_LINE_USER_ID = os.environ.get("MY_LINE_USER_ID")
    if not MY_LINE_USER_ID:
        print("Scheduled job failed: MY_LINE_USER_ID is not set in .env file.")
        return

    try:
        # プッシュメッセージとしてLINEに送信
        line_bot_api.push_message(MY_LINE_USER_ID, TextSendMessage(text=final_message))
        print(f"Successfully sent a reminder notification to {MY_LINE_USER_ID}.")
    except Exception as e:
        print(f"Scheduled job failed: Could not send LINE push message. Error: {e}")


# --- FastAPIのイベントハンドラ ---
@app.on_event("startup")
async def startup_event():
    # ... (既存のAPIキー検証や優先度取得の処理はそのまま) ...
    # ↓↓↓ この部分を末尾に追加 ↓↓↓
    print("\n=== Scheduling background jobs ===")
    
    # 毎日朝8時に `check_and_notify_overdue_tickets` を実行するジョブを追加
    scheduler.add_job(check_and_notify_overdue_tickets, 'cron', hour=8, minute=0)
    
    # スケジューラを開始
    scheduler.start()
    print("✓ Scheduler started. Reminder job is set for 8:00 AM every day.")

# ... (以降のコードは変更なし) ...
Use code with caution.
Python
ステップ3: .env ファイルに自分のLINEユーザーIDを追加
Botがあなた個人にメッセージを「プッシュ」するためには、あなたのLINEユーザーIDを知る必要があります。
自分のLINEユーザーIDを調べる
一番簡単な方法は、Botに話しかけたときのサーバーログを見ることです。以前のログに Received message from Uxxxxxxxxxxxxxxxxxxxxxxxxx: ... のような行があるはずです。この U から始まる文字列があなたのユーザーIDです。
.env ファイルを編集
.env ファイルを開き、以下の1行を追加してください。（Uxxxxxxxx... の部分はご自身のIDに置き換えます）
Generated dotenv
# .env ファイルの末尾に追加
MY_LINE_USER_ID="Uxxxxxxxxxxxxxxxxxxxxxxxxx"
Use code with caution.
Dotenv
この実装で何が起きるか
サーバー起動時 (startup_event):
apscheduler が初期化されます。
「毎日 朝8時00分に check_and_notify_overdue_tickets という関数を実行せよ」というスケジュールが登録されます。
スケジューラがバックグラウンドで動き始めます。
毎日 朝8時00分:
スケジューラが時間になったことを検知し、check_and_notify_overdue_tickets 関数を実行します。
この関数はRedmine APIにアクセスし、「ステータスが未完了」で「期日が今日以前」のチケットをすべて取得します。
取得したチケット情報を見やすい形に整形します。
line_bot_api.push_message() を使って、あなたのLINEに直接メッセージを送信します。
これで、あなたが忘れていてもBotの方から「社長、これらのタスクが残ってますよ！」と教えてくれる、真の秘書・アシスタントへと進化します。
ぜひ、この「プッシュ通知」機能を実装してみてください。日々のタスク管理が劇的に楽になるはずです！
24.6s
