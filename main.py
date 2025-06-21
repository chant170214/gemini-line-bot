import os
import sys
import uuid
from flask import Flask, request, abort

# ↓↓↓ ここを修正しました ↓↓↓
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
# ↑↑↑ ここを修正しました ↑↑↑

import google.generativeai as genai

# --- 設定項目 ---
# 環境変数から設定を読み込む
channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "")
gemini_api_key = os.environ.get("GEMINI_API_KEY", "")

# --- ファイルパス ---
# 認証済みユーザーを保存するファイル
AUTH_FILE = 'authenticated_users.txt'
# 有効なワンタイムコードを保存するファイル
CODE_FILE = 'valid_codes.txt'

# --- 初期化 ---
app = Flask(__name__)

# LINE Bot APIとWebhookHandlerの初期化
if not channel_access_token or not channel_secret:
    print("エラー: LINEのチャネルアクセストークンまたはチャネルシークレットが設定されていません。")
    sys.exit(1)
line_bot_api = LineBotApi(channel_access_token)
handler = WebhookHandler(channel_secret)

# Gemini APIの初期化
if not gemini_api_key:
    print("エラー: Gemini APIキーが設定されていません。")
    sys.exit(1)
genai.configure(api_key=gemini_api_key)
model = genai.GenerativeModel('gemini-1.5-flash')

# --- 認証関連の関数 ---

def is_user_authenticated(user_id):
    """ユーザーが認証済みか確認する"""
    if not os.path.exists(AUTH_FILE):
        return False
    with open(AUTH_FILE, 'r') as f:
        authenticated_users = f.read().splitlines()
    return user_id in authenticated_users

def authenticate_user(user_id, code):
    """提供されたコードでユーザーを認証する"""
    if not os.path.exists(CODE_FILE):
        return False
    
    with open(CODE_FILE, 'r') as f:
        valid_codes = [c.strip() for c in f.read().splitlines()]

    if code in valid_codes:
        # 認証成功
        # 認証済みリストに追加
        with open(AUTH_FILE, 'a') as f:
            f.write(user_id + '\n')
        
        # 使用済みコードをリストから削除
        valid_codes.remove(code)
        with open(CODE_FILE, 'w') as f:
            for c in valid_codes:
                f.write(c + '\n')
        return True
    return False

# --- Webhookの処理 ---

@app.route("/callback", methods=['POST'])
def callback():
    """LINEからのWebhookを受け取るエンドポイント"""
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    """メッセージイベントを処理する"""
    user_id = event.source.user_id
    user_message = event.message.text

    # 認証済みかチェック
    if is_user_authenticated(user_id):
        # 認証済みの場合：Geminiで応答を生成
        try:
            response = model.generate_content(user_message)
            reply_text = response.text
        except Exception as e:
            app.logger.error(f"Gemini API Error: {e}")
            reply_text = "申し訳ありません、AIとの通信中にエラーが発生しました。"
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text)
        )
    else:
        # 未認証の場合：コード認証を試みる
        if authenticate_user(user_id, user_message):
            # 認証成功
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="認証が完了しました。ご質問をどうぞ。")
            )
        else:
            # 認証失敗
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="認証コードを入力してください。")
            )

if __name__ == "__main__":
    # サーバー起動時にファイルが存在しない場合は作成する
    for f in [AUTH_FILE, CODE_FILE]:
        if not os.path.exists(f):
            open(f, 'w').close()
            
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
