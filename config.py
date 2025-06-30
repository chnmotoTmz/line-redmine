import os
import dotenv

# .envファイルから環境変数を読み込む
dotenv.load_dotenv()

# Gemini APIキー
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")

# Webhookサーバー設定
WEBHOOK_PORT = os.environ.get("WEBHOOK_PORT", "8001")

# LINE Bot設定
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")