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

# --- 新しい定数 ---
MAX_HISTORY_LENGTH = 10 # 記憶する会話の最大数 (5往復分)

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

# --- 会話履歴関連の関数 (NEW!) ---
def get_conversation_history(user_id):
    """Firebaseから会話履歴を取得し、長さを制限して返す"""
    ref = db.reference(f'/conversation_history/{user_id}')
    history = ref.get()
    if history is None:
        return []
    # 履歴が長すぎる場合は、最新のMAX_HISTORY_LENGTH件だけを返す
    return history[-MAX_HISTORY_LENGTH:]

def save_conversation_history(user_id, history):
    """会話履歴をFirebaseに保存する"""
    ref = db.reference(f'/conversation_history/{user_id}')
    ref.set(history)

def reset_conversation_history(user_id):
    """会話履歴をリセット（削除）する"""
    ref = db.reference(f'/conversation_history/{user_id}')
    ref.delete()

# --- 認証関連の関数 (変更なし) ---
def is_user_authenticated(user_id):
    ref = db.reference(f'/authenticated_users/{user_id}')
    return ref.get() is not None

def authenticate_user(user_id, code):
    codes_ref = db.reference('/valid_codes')
    valid_codes = codes_ref.get()
    if valid_codes and code in valid_codes:
        db.reference(f'/authenticated_users/{user_id}').set(True)
        del valid_codes[code]
        codes_ref.set(valid_codes)
        return True
    return False

# --- Webhookの処理 (変更なし) ---
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

# --- メッセージ処理を大幅に改造！ ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    if is_user_authenticated(user_id):
        # --- 会話リセットコマンド (NEW!) ---
        if user_message.strip().lower() == '/reset':
            reset_conversation_history(user_id)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="会話の履歴をリセットしました。新しい会話を始めましょう。")
            )
            return # 処理をここで終了

        # --- ここからが会話記憶のメイン処理 ---
        try:
            # 1. 過去の会話履歴を取得
            history = get_conversation_history(user_id)
            
            # 2. 今回のメッセージを履歴に追加
            # Gemini APIは role と parts の形式を要求する
            history.append({"role": "user", "parts": [{"text": user_message}]})
            
            # 3. 履歴全体をAIに渡して応答を生成
            response = model.generate_content(history)
            reply_text = response.text

            # 4. AIの応答を履歴に追加
            history.append({"role": "model", "parts": [{"text": reply_text}]})

            # 5. 最新の履歴を保存
            save_conversation_history(user_id, history)

            # 6. ユーザーに応答を送信
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_text)
            )

        except Exception as e:
            app.logger.error(f"Gemini API or History Error: {e}")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="申し訳ありません、AIとの通信中にエラーが発生しました。しばらくしてから「/reset」と送信して会話をリセットしてみてください。")
            )
    else:
        # 認証処理 (注意書き機能付き)
        if authenticate_user(user_id, user_message):
            welcome_message = "認証が完了しました。ご質問をどうぞ。"
            caution_message = (
                "【ご利用上の注意】\n\n"
                "・AIは時に誤った情報を生成することがあります。重要な情報は必ずご自身でご確認ください。\n"
                "・1分間に15回を超えるような、極端に速い連続投稿はお控えください。\n\n"
                "これらの点にご留意の上、AIとの対話をお楽しみください。"
            )
            line_bot_api.reply_message(
                event.reply_token,
                [
                    TextSendMessage(text=welcome_message),
                    TextSendMessage(text=caution_message)
                ]
            )
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="認証コードを入力してください。"))

# --- 管理者用機能 (変更なし) ---
@app.route("/admin/add_code", methods=['GET'])
def add_code():
    secret = request.args.get('secret')
    if secret != admin_secret:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    
    new_code = request.args.get('code', str(uuid.uuid4())[:8])
    
    codes_ref = db.reference(f'/valid_codes/{new_code}')
    codes_ref.set(True)
    
    return jsonify({"status": "success", "added_code": new_code})

# --- サーバー起動 (変更なし) ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
