import os
import sys
import uuid
import json
import requests # ローディング表示用にインポート
from flask import Flask, request, abort, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import firebase_admin
from firebase_admin import credentials, db
from googleapiclient.discovery import build

# --- 設定項目 ---
# 環境変数から設定を読み込む
channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "")
gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
admin_secret = os.environ.get("ADMIN_SECRET", "DEFAULT_SECRET_CHANGE_ME")
firebase_database_url = os.environ.get("FIREBASE_DATABASE_URL", "")
firebase_credentials_json = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
search_api_key = os.environ.get("SEARCH_API_KEY", "")
search_engine_id = os.environ.get("SEARCH_ENGINE_ID", "")

# --- 定数 ---
MAX_HISTORY_LENGTH = 30 

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

# --- ローディング表示関数 (NEW!) ---
def display_loading_animation(user_id):
    """ユーザーの画面にローディングアニメーションを表示する"""
    headers = {
        'Authorization': f'Bearer {channel_access_token}',
        'Content-Type': 'application/json'
    }
    data = {
        'chatId': user_id,
        'loadingSeconds': 20 # 最大60秒まで設定可能
    }
    try:
        response = requests.post('https://api.line.me/v2/bot/chat/loading/start', headers=headers, json=data)
        response.raise_for_status()
        print(f"ローディング表示成功: {user_id}")
    except requests.exceptions.RequestException as e:
        print(f"ローディング表示エラー: {e}")

# --- Web検索関数 (変更なし) ---
def google_search(query: str) -> dict:
    """最新の情報、特定の事実、時事問題、天気、株価など、リアルタイムの情報が必要な場合にウェブを検索します。"""
    print(f"Executing Google Search for: {query}")
    if not search_api_key or not search_engine_id:
        return {"error": "検索機能が設定されていません。"}
    try:
        service = build("customsearch", "v1", developerKey=search_api_key)
        res = service.cse().list(q=query, cx=search_engine_id, num=3).execute()
        if 'items' not in res:
            return {"result": "検索結果が見つかりませんでした。"}
        search_results = []
        for item in res['items']:
            title = item.get('title', '')
            link = item.get('link', '')
            snippet = item.get('snippet', '').replace('\n', '')
            search_results.append(f"タイトル: {title}\n概要: {snippet}\nURL: {link}")
        return {"search_results": "\n\n---\n\n".join(search_results)}
    except Exception as e:
        print(f"Google Search Error: {e}")
        return {"error": f"検索中にエラーが発生しました: {e}"}

# --- Geminiモデルの初期化 (変更なし) ---
genai.configure(api_key=gemini_api_key)
model = genai.GenerativeModel('gemini-1.5-flash', tools=[google_search])

# --- 初期化 ---
app = Flask(__name__)
line_bot_api = LineBotApi(channel_access_token)
handler = WebhookHandler(channel_secret)

# --- 会話履歴関連の関数 (変更なし) ---
def get_conversation_history(user_id):
    ref = db.reference(f'/conversation_history/{user_id}')
    history = ref.get()
    if history is None: return []
    return [genai.types.Content(**msg) for msg in history][-MAX_HISTORY_LENGTH:]

def save_conversation_history(user_id, history):
    ref = db.reference(f'/conversation_history/{user_id}')
    serializable_history = [genai.types.Content.to_dict(msg) for msg in history]
    ref.set(serializable_history)

def reset_conversation_history(user_id):
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
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# --- メッセージ処理を最終形態に！ ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    if not is_user_authenticated(user_id):
        if authenticate_user(user_id, user_message):
            welcome_message = "認証が完了しました。ご質問をどうぞ。"
            caution_message = "【ご利用上の注意】\n\n・AIは時に誤った情報を生成することがあります。\n・1分間に15回を超える連続投稿はお控えください。\n\nこれらの点にご留意の上、AIとの対話をお楽しみください。"
            line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=welcome_message), TextSendMessage(text=caution_message)])
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="認証コードを入力してください。"))
        return

    if user_message.strip().lower() == '/reset':
        reset_conversation_history(user_id)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="会話の履歴をリセットしました。"))
        return

    # --- ここからがメイン処理 ---
    try:
        # 1. ローディング表示を開始 (NEW!)
        display_loading_animation(user_id)

        # 2. 履歴を取得してチャットセッションを開始
        history = get_conversation_history(user_id)
        chat = model.start_chat(history=history)

        # 3. ユーザーのメッセージを送信
        response = chat.send_message(user_message)
        
        # 4. 最終的な回答を取得
        reply_text = response.text

        # 5. 検索が実行されたかチェック (NEW!)
        searched_web = False
        # chat.historyにはユーザーの質問とAIの応答の全履歴が入っている
        # 最後のAIの応答(から2番目)にtool_callsがあれば検索したと判断
        if len(chat.history) > 1 and chat.history[-2].parts[0].function_call:
            searched_web = True

        # 6. 検索した場合、前置きを追加 (NEW!)
        if searched_web:
            reply_text = "🌐 Webで検索しました。\n\n" + reply_text

        # 7. 最新の会話履歴を保存
        save_conversation_history(user_id, chat.history)

        # 8. ユーザーに応答
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))

    except Exception as e:
        app.logger.error(f"Main process error: {e}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="申し訳ありません、エラーが発生しました。「/reset」で会話をリセットしてみてください。"))

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
