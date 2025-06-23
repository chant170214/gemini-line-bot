import os
import sys
import uuid
import json
import requests
from flask import Flask, request, abort, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, db
from googleapiclient.discovery import build
from datetime import datetime
import pytz

# --- 設定項目 ---
channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "")
gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
admin_secret = os.environ.get("ADMIN_SECRET", "DEFAULT_SECRET_CHANGE_ME")
firebase_database_url = os.environ.get("FIREBASE_DATABASE_URL", "")
firebase_credentials_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
search_api_key = os.environ.get("SEARCH_API_KEY", "")
search_engine_id = os.environ.get("SEARCH_ENGINE_ID", "")

# --- 定数 ---
MAX_HISTORY_LENGTH = 20
JST = pytz.timezone('Asia/Tokyo')
PRO_MODE_LIMIT = 5 # ★★★ Proモードの1日の上限回数をここで設定 ★★★

# --- Firebaseの初期化 ---
try:
    if firebase_credentials_json and firebase_database_url:
        cred_json = json.loads(firebase_credentials_json)
        cred = credentials.Certificate(cred_json)
        firebase_admin.initialize_app(cred, {'databaseURL': firebase_database_url})
        print("Firebaseの初期化に成功しました。")
    else:
        print("エラー: Firebaseの環境変数が設定されていません。")
except Exception as e:
    print(f"Firebase初期化エラー: {e}")

# --- ローディング表示関数 ---
def display_loading_animation(user_id):
    headers = {'Authorization': f'Bearer {channel_access_token}', 'Content-Type': 'application/json'}
    data = {'chatId': user_id, 'loadingSeconds': 20}
    try:
        requests.post('https://api.line.me/v2/bot/chat/loading/start', headers=headers, json=data)
    except requests.exceptions.RequestException as e:
        print(f"ローディング表示エラー: {e}")

# --- Web検索関数 ---
def google_search(query: str):
    print(f"Executing Google Search for: {query}")
    if not search_api_key or not search_engine_id:
        return "検索機能が設定されていません。"
    try:
        service = build("customsearch", "v1", developerKey=search_api_key)
        res = service.cse().list(q=query, cx=search_engine_id, num=3).execute()
        if 'items' not in res:
            return "検索結果が見つかりませんでした。"
        search_results = [f"タイトル: {item.get('title', '')}\n概要: {item.get('snippet', '').replace('\n', '')}\nURL: {item.get('link', '')}" for item in res['items']]
        return "\n\n---\n\n".join(search_results)
    except Exception as e:
        print(f"Google Search Error: {e}")
        return f"検索中にエラーが発生しました: {e}"

# --- Geminiモデルの初期化 ---
genai.configure(api_key=gemini_api_key)
model_flash = genai.GenerativeModel('gemini-1.5-flash-latest')
model_pro = genai.GenerativeModel('gemini-1.5-pro-latest')

# --- 初期化 ---
app = Flask(__name__)
line_bot_api = LineBotApi(channel_access_token)
handler = WebhookHandler(channel_secret)

# --- Proモード利用回数制限の関数 (回数設定可能版) ---
def check_pro_quota(user_id):
    """Proモードの利用回数をチェック。上限に達していなければTrueを返す"""
    today_jst_str = datetime.now(JST).strftime('%Y-%m-%d')
    ref = db.reference(f'/pro_usage/{user_id}/{today_jst_str}')
    usage_count = ref.get() or 0
    return usage_count < PRO_MODE_LIMIT

def record_pro_usage(user_id):
    """Proモードの利用を記録する"""
    today_jst_str = datetime.now(JST).strftime('%Y-%m-%d')
    ref = db.reference(f'/pro_usage/{user_id}/{today_jst_str}')
    def increment(current_count):
        return (current_count or 0) + 1
    ref.transaction(increment)

# --- データベース関連の関数 ---
def get_user_mode(user_id):
    ref = db.reference(f'/user_settings/{user_id}/mode')
    return ref.get() or 'flash'

def set_user_mode(user_id, mode):
    ref = db.reference(f'/user_settings/{user_id}/mode')
    ref.set(mode)

def get_conversation_history(user_id):
    ref = db.reference(f'/conversation_history/{user_id}')
    history = ref.get()
    return history[-MAX_HISTORY_LENGTH:] if history else []

def save_conversation_history(user_id, history):
    ref = db.reference(f'/conversation_history/{user_id}')
    ref.set(history)

def reset_conversation_history(user_id):
    ref = db.reference(f'/conversation_history/{user_id}')
    ref.delete()

# --- 認証関連の関数 ---
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

# --- Webhookの処理 ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# --- メッセージ処理 ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.strip()

    if not is_user_authenticated(user_id):
        if authenticate_user(user_id, user_message):
            welcome_message = "認証が完了しました。ご質問をどうぞ。"
            caution_message = f"【ご利用上の注意】\n\n・AIは時に誤った情報を生成することがあります。\n・1分間に15回を超える連続投稿はお控えください。\n\n【コマンド一覧】\n/pro - 高精度モードに切替(1日{PRO_MODE_LIMIT}回)\n/flash - 高速モードに切替\n/search [キーワード] - Web検索\n/reset - 会話履歴をリセット"
            line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=welcome_message), TextSendMessage(text=caution_message)])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="認証コードを入力してください。"))
        return

    # --- コマンド処理 ---
    if user_message.lower() == '/reset':
        reset_conversation_history(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="会話の履歴をリセットしました。"))
        return
    
    if user_message.lower() == '/pro':
        set_user_mode(user_id, 'pro')
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🤖 高精度モード (Pro) に切り替えました。\n(本日まだ未使用の場合、次の会話から適用されます。上限: {PRO_MODE_LIMIT}回/日)"))
        return

    if user_message.lower() == '/flash':
        set_user_mode(user_id, 'flash')
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚡️ 高速モード (Flash) に切り替えました。"))
        return

    if user_message.lower().startswith('/search '):
        query = user_message[8:]
        if not query:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="検索キーワードを入力してください。\n例: /search 今日のニュース"))
            return
        
        display_loading_animation(user_id)
        search_results = google_search(query)
        prompt = f"以下のWeb検索結果を元に、ユーザーの質問「{query}」に簡潔に答えてください。\n\n---\n{search_results}"
        
        try:
            response = model_flash.generate_content(prompt)
            reply_text = "🌐 Webで検索しました。\n\n" + response.text
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
        except Exception as e:
            app.logger.error(f"Search Summary Error: {e}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="検索結果の要約中にエラーが発生しました。"))
        return

    # --- 通常の会話処理 ---
    try:
        display_loading_animation(user_id)
        
        user_mode = get_user_mode(user_id)
        active_model = model_flash
        mode_icon = "⚡️"
        
        if user_mode == 'pro':
            if check_pro_quota(user_id):
                active_model = model_pro
                mode_icon = "🤖"
                record_pro_usage(user_id)
            else:
                quota_exceeded_message = f"本日の高精度モード(Pro)のご利用回数上限({PRO_MODE_LIMIT}回)に達しました。高速モード(Flash)で応答します。"
                line_bot_api.push_message(user_id, TextSendMessage(text=quota_exceeded_message))

        history = get_conversation_history(user_id)
        history.append({'role': 'user', 'parts': [{'text': user_message}]})
        
        response = active_model.generate_content(history)
        reply_text = response.text
        
        history.append({'role': 'model', 'parts': [{'text': reply_text}]})
        save_conversation_history(user_id, history)
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{mode_icon} {reply_text}"))

    except Exception as e:
        app.logger.error(f"Main process error: {e}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="申し訳ありません、エラーが発生しました。「/reset」で会話をリセットしてみてください。"))

# --- 管理者用機能 ---
@app.route("/admin/add_code", methods=['GET'])
def add_code():
    secret = request.args.get('secret')
    if secret != admin_secret:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    new_code = request.args.get('code', str(uuid.uuid4())[:8])
    codes_ref = db.reference(f'/valid_codes/{new_code}')
    codes_ref.set(True)
    return jsonify({"status": "success", "added_code": new_code})

# --- サーバー起動 ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
