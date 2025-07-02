# Line-Redmine Integration

Google Gemini AIとRedmineを統合し、自然言語処理を通じて自動的にチケットを作成するPythonアプリケーションです。

## 機能

- **AI駆動チケット作成**: Google Gemini AIを使用して自然言語プロンプトを処理し、文脈に応じたRedmineチケットを作成
- **複数の統合方法**:
  - Redmine REST API直接統合
  - Claude Desktop経由のMCP（Model Context Protocol）統合
- **スマートチケット処理**:
  - 入力データに基づいた文脈的優先度設定
  - 自動説明文生成
  - カスタマイズ可能なチケットフィールド
- **環境設定**: `.env`ファイルを使用したセキュアな設定管理
- **包括的ログ記録**: `application.log`での詳細なアクティビティログ

## 必要要件

- Python 3.8+
- Redmine 5.0.12+ (REST API有効化必須)
- Google Gemini APIキー
- 必要なPythonパッケージ（`requirements.txt`を参照）
- オプション: 高度な統合のためのClaude Desktop with MCP

## インストール

1. リポジトリをクローン:
   ```bash
   git clone https://github.com/your-repo/line-redmine.git
   cd line-redmine
   ```

2. 依存関係をインストール:
   ```bash
   pip install -r requirements.txt
   ```

3. `.env`ファイルで環境変数を設定:
   ```env
   GOOGLE_API_KEY="your_gemini_api_key"
   REDMINE_URL="http://your-redmine-url"
   REDMINE_API_KEY="your_redmine_api_key"
   REDMINE_PROJECT_ID="your_project_id"
   MCP_URL="http://localhost:8000/mcp-redmine"  # MCP統合用（オプション）
   ```

## 使用方法

### 基本的な使用法
```bash
python main.py "チケットの説明"
```

### 使用例
```bash
# 基本的なチケットを作成
python main.py "サーバーメンテナンス用の高優先度タスクを作成"

# 天気ベースのチケットを作成
python main.py "ボストンの天気がメンテナンスを必要とする場合チケットを作成"
```

## 統合方法

### 直接REST API統合
- RedmineのREST APIとの直接通信
- 高速レスポンス時間（通常500ms未満）
- シンプルなチケット作成ワークフローに最適

### MCP統合（オプション）
- Claude Desktop経由の拡張AI処理
- 複雑なワークフローと複数ツール統合のサポート
- Claude DesktopとMCPの追加セットアップが必要

## 設定

### 必須環境変数
- `GOOGLE_API_KEY`: Google Gemini API認証
- `REDMINE_URL`: RedmineインスタンスのURL
- `REDMINE_API_KEY`: Redmine API認証
- `REDMINE_PROJECT_ID`: チケット作成対象プロジェクト

### オプション環境変数
- `MCP_URL`: MCPサーバーエンドポイント（Claude Desktop統合用）
- `LOG_LEVEL`: ログ詳細レベル（デフォルト: INFO）

## エラーハンドリング

以下の項目に対する包括的なエラーハンドリングを含みます：
- API認証失敗
- ネットワーク接続問題
- 無効な入力データ
- MCPサーバー接続問題

## ログ記録

`application.log`で詳細なログを維持し、以下を含みます：
- APIリクエストとレスポンス
- チケット作成詳細
- エラーメッセージと警告
- パフォーマンス指標

## 開発

### 新機能の追加
1. リポジトリをフォーク
2. 機能ブランチを作成
3. プルリクエストを送信

### テストの実行
```bash
python -m pytest tests/
```

## パフォーマンス

- REST APIレスポンス時間: 500ms未満
- MCP統合レスポンス時間: 1秒未満
- 同時チケット作成をサポート

## セキュリティ

- APIキーは環境変数で安全に保存
- すべてのAPI通信でHTTPS必須
- 入力検証とサニタイゼーション
- セキュアなエラーログ記録

## サポート

問題や機能要求については：
1. 既存のGitHubイシューを確認
2. 必要に応じて新しいイシューを開く
3. ログと環境詳細を含める

## ライセンス

このプロジェクトはMITライセンスの下でライセンスされています。詳細はLICENSEファイルを参照してください。
