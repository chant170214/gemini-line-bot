import os
import sys
import uuid
import json
from flask import Flask, request, abort, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, db

# --- 設定項目 ---
# 環境変数から設定を読み込む
channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "")
gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
admin_secret = os.environ.get("ADMIN_SECRET", "DEFAULT_SECRET_CHANGE_ME")
firebase_database_url = os.environ.get("FIREBASE_DATABASE_URL", "")
firebase_credentials_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")

# --- Firebaseの初期化 ---
try:
    if firebase_credentials_json and firebase_database_url:
        cred_json = json.loads(firebase_credentials_json)
        cred = credentials.Certificate(cred_json)
        firebase_admin.initialize_app(cred, {'databaseURL': firebase_database_url})
        print("Firebaseの初期化に成功しました。")
    else:
        print("エラー: Firebaseの環境変数が設定されていません。")
        sys.exit(1)
except Exception as e:
    print(f"Firebase初期化エラー: {e}")
    sys.exit(1)

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

# --- 認証関連の関数 (Firebase版) ---
def is_user_authenticated(user_id):
    ref = db.reference(f'/authenticated_users/{user_id}')
    return ref.get() is not None

def authenticate_user(user_id, code):
    codes_ref = db.reference('/valid_codes')
    valid_codes = codes_ref.get()
    if valid_codes and code in valid_codes:
        # 認証成功
        db.reference(f'/authenticated_users/{user_id}').set(True)
        # 使用済みコードを削除
        del valid_codes[code]
        codes_ref.set(valid_codes)
        return True
    return False

# --- Webhookの処理 ---
@app.route("/callback", methods=['POST'])
def callback():
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
    user_id = event.source.user_id
    user_message = event.message.text
    if is_user_authenticated(user_id):
        try:
            response = model.generate_content(user_message)
            reply_text = response.text
        except Exception as e:
            app.logger.error(f"Gemini API Error: {e}")
            reply_text = "申し訳ありません、AIとの通信中にエラーが発生しました。"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    else:
        if authenticate_user(user_id, user_message):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="認証が完了しました。ご質問をどうぞ。"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="認証コードを入力してください。"))

# --- 管理者用機能 (Firebase版) ---
@app.route("/admin/add_code", methods=['GET'])
def add_code():
    secret = request.args.get('secret')
    if secret != admin_secret:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    
    new_code = request.args.get('code', str(uuid.uuid4())[:8])
    
    # 新しいコードをFirebaseに保存
    codes_ref = db.reference(f'/valid_codes/{new_code}')
    codes_ref.set(True) # 値は何でも良いのでTrueにしておく
    
    return jsonify({"status": "success", "added_code": new_code})

# --- サーバー起動 ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
