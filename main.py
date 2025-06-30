import os
import sys
import uuid
import json
import re  # ### 追加 ### URLの形式をチェックするためにインポート
import requests
from flask import Flask, request, abort, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageMessage  # ### 変更 ### ImageMessageを追加
import google.generativeai as genai
import firebase_admin
from firebase_admin import credentials, db
from googleapiclient.discovery import build
from datetime import datetime
import pytz
from bs4 import BeautifulSoup  # ### 追加 ### URL要約のためにインポート

# --- 設定と定数 ---
class Config:
    """アプリケーションの設定と定数を管理するクラス"""
    # ... (既存の設定は省略) ...
    LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
    ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "DEFAULT_SECRET_CHANGE_ME")
    FIREBASE_DATABASE_URL = os.environ.get("FIREBASE_DATABASE_URL", "")
    FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")
    SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY", "")
    SEARCH_ENGINE_ID = os.environ.get("SEARCH_ENGINE_ID", "")
    MAX_HISTORY_LENGTH = 20
    JST = pytz.timezone('Asia/Tokyo')
    PRO_MODE_LIMIT = 5
    
    # コマンド定義
    CMD_RESET = "/reset"
    CMD_PRO = "/pro"
    CMD_FLASH = "/flash"
    CMD_SEARCH = "/search"
    CMD_SUMMARIZE = "/summarize"  # ### 追加 ###

# --- 初期化 ---
# ... (既存の初期化処理は省略) ...
app = Flask(__name__)
try:
    if Config.FIREBASE_CREDENTIALS_JSON and Config.FIREBASE_DATABASE_URL:
        cred_json = json.loads(Config.FIREBASE_CREDENTIALS_JSON)
        cred = credentials.Certificate(cred_json)
        firebase_admin.initialize_app(cred, {'databaseURL': Config.FIREBASE_DATABASE_URL})
        app.logger.info("Firebaseの初期化に成功しました。")
    else:
        app.logger.error("エラー: Firebaseの環境変数が設定されていません。")
except Exception as e:
    app.logger.error(f"Firebase初期化エラー: {e}")
line_bot_api = LineBotApi(Config.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(Config.LINE_CHANNEL_SECRET)
genai.configure(api_key=Config.GEMINI_API_KEY)
models = {
    'flash': genai.GenerativeModel('gemini-1.5-flash-latest'),
    'pro': genai.GenerativeModel('gemini-1.5-pro-latest')
}

# --- ユーティリティ関数 ---
# ... (display_loading_animation, Google Search は省略) ...
def display_loading_animation(user_id):
    headers = {'Authorization': f'Bearer {Config.LINE_CHANNEL_ACCESS_TOKEN}', 'Content-Type': 'application/json'}
    data = {'chatId': user_id, 'loadingSeconds': 20}
    try:
        requests.post('https://api.line.me/v2/bot/chat/loading/start', headers=headers, json=data, timeout=5)
    except requests.exceptions.RequestException as e:
        app.logger.warning(f"ローディング表示API呼び出しエラー: {e}")
def Google Search(query: str):
    app.logger.info(f"Google検索を実行: {query}")
    if not Config.SEARCH_API_KEY or not Config.SEARCH_ENGINE_ID: return "検索機能が設定されていません。"
    try:
        service = build("customsearch", "v1", developerKey=Config.SEARCH_API_KEY)
        res = service.cse().list(q=query, cx=Config.SEARCH_ENGINE_ID, num=3).execute()
        if 'items' not in res or not res['items']: return "検索結果が見つかりませんでした。"
        results = [f"タイトル: {item.get('title', '')}\n概要: {item.get('snippet', '').replace('\n', '')}\nURL: {item.get('link', '')}" for item in res['items']]
        return "\n\n---\n\n".join(results)
    except Exception as e:
        app.logger.error(f"Google Search Error: {e}")
        return "検索中にエラーが発生しました。詳細はログを確認してください。"

# ### 追加 ### URLからテキストを抽出するヘルパー関数
def extract_text_from_url(url):
    """URLから本文を抽出する"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()  # HTTPエラーがあれば例外を発生
        
        # 文字化け対策
        response.encoding = response.apparent_encoding
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 不要なタグを除去
        for script_or_style in soup(['script', 'style', 'header', 'footer', 'nav', 'aside']):
            script_or_style.decompose()
            
        text = soup.get_text(separator='\n', strip=True)
        
        # 連続する空行を一つにまとめる
        return re.sub(r'\n\s*\n', '\n', text)
    except requests.exceptions.RequestException as e:
        app.logger.error(f"URLへのアクセス失敗: {url}, エラー: {e}")
        return None, f"URLにアクセスできませんでした: {e}"
    except Exception as e:
        app.logger.error(f"テキスト抽出エラー: {url}, エラー: {e}")
        return None, f"ページの解析中にエラーが発生しました: {e}"


# --- データベース関連関数 ---
# ... (既存のDB関連関数は省略) ...
def get_db_reference(path_template, **kwargs): return db.reference(path_template.format(**kwargs))
def get_user_mode(user_id):
    ref = get_db_reference('/user_settings/{user_id}/mode', user_id=user_id)
    return ref.get() or 'flash'
def set_user_mode(user_id, mode):
    ref = get_db_reference('/user_settings/{user_id}/mode', user_id=user_id)
    ref.set(mode)
def get_conversation_history(user_id):
    ref = get_db_reference('/conversation_history/{user_id}', user_id=user_id)
    history = ref.get()
    return history[-Config.MAX_HISTORY_LENGTH:] if history else []
def save_conversation_history(user_id, history):
    ref = get_db_reference('/conversation_history/{user_id}', user_id=user_id)
    ref.set(history)
def reset_conversation_history(user_id):
    ref = get_db_reference('/conversation_history/{user_id}', user_id=user_id)
    ref.delete()
def check_pro_quota(user_id):
    today_jst_str = datetime.now(Config.JST).strftime('%Y-%m-%d')
    ref = get_db_reference('/pro_usage/{user_id}/{date}', user_id=user_id, date=today_jst_str)
    return (ref.get() or 0) < Config.PRO_MODE_LIMIT
def record_pro_usage(user_id):
    today_jst_str = datetime.now(Config.JST).strftime('%Y-%m-%d')
    ref = get_db_reference('/pro_usage/{user_id}/{date}', user_id=user_id, date=today_jst_str)
    ref.transaction(lambda current_count: (current_count or 0) + 1)
def is_user_authenticated(user_id):
    ref = get_db_reference('/authenticated_users/{user_id}', user_id=user_id)
    return ref.get() is not None
def authenticate_user(user_id, code):
    codes_ref = get_db_reference('/valid_codes')
    valid_codes = codes_ref.get()
    if valid_codes and code in valid_codes:
        get_db_reference('/authenticated_users/{user_id}', user_id=user_id).set(True)
        codes_ref.child(code).delete()
        return True
    return False

# --- メッセージハンドラ ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

### 変更 ### TextMessageのハンドラを明示的に指定
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """受信したテキストメッセージを処理する"""
    user_id = event.source.user_id
    user_message = event.message.text.strip()
    try:
        if not is_user_authenticated(user_id):
            handle_authentication(event, user_id, user_message)
        elif user_message.startswith('/'):
            handle_command(event, user_id, user_message)
        else:
            handle_conversation(event, user_id, user_message)
    except Exception as e:
        app.logger.error(f"テキストメッセージ処理中に予期せぬエラー: {e}", exc_info=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="申し訳ありません、エラーが発生しました。「/reset」で会話をリセットしてみてください。"))

### 追加 ### ImageMessageのハンドラを追加
@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    """受信した画像メッセージを処理する"""
    user_id = event.source.user_id
    try:
        if not is_user_authenticated(user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="先に認証コードを入力して、認証を完了してください。"))
            return

        display_loading_animation(user_id)
        
        # LINEサーバーから画像データを取得
        message_content = line_bot_api.get_message_content(event.message.id)
        image_data = message_content.content

        # Geminiに渡すための画像パートを作成
        image_part = {"mime_type": "image/jpeg", "data": image_data}
        prompt_part = "この画像について、見たままを詳しく、そして分かりやすく説明してください。"
        
        # Gemini 1.5 Flashモデルで画像を解析
        response = models['flash'].generate_content([prompt_part, image_part])
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🖼️ 画像を解析しました。\n\n{response.text}"))

    except Exception as e:
        app.logger.error(f"画像処理中に予期せぬエラー: {e}", exc_info=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="申し訳ありません、画像の処理中にエラーが発生しました。"))

# ... (handle_authentication, handle_conversationは変更なしのため省略) ...
def handle_authentication(event, user_id, code):
    if authenticate_user(user_id, code):
        welcome_message = "認証が完了しました。ご質問をどうぞ。"
        caution_message = (f"【ご利用上の注意】\n\n・AIは時に誤った情報を生成することがあります。\n"
                         f"・1分間に15回を超える連続投稿はお控えください。\n\n"
                         f"【コマンド一覧】\n{Config.CMD_PRO} - 高精度モード\n{Config.CMD_FLASH} - 高速モード\n"
                         f"{Config.CMD_RESET} - 履歴リセット\n{Config.CMD_SEARCH} [キーワード]\n{Config.CMD_SUMMARIZE} [URL]")
        line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=welcome_message), TextSendMessage(text=caution_message)])
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="認証コードを入力してください。"))
def handle_conversation(event, user_id, user_message):
    display_loading_animation(user_id)
    user_mode = get_user_mode(user_id)
    active_model, mode_icon = (models['flash'], "⚡️")
    if user_mode == 'pro':
        if check_pro_quota(user_id):
            active_model, mode_icon = (models['pro'], "🤖")
            record_pro_usage(user_id)
        else:
            limit_message = f"本日の高精度モード(Pro)のご利用回数上限({Config.PRO_MODE_LIMIT}回)に達しました。高速モード(Flash)で応答します。"
            line_bot_api.push_message(user_id, TextSendMessage(text=limit_message))
    history = get_conversation_history(user_id)
    history.append({'role': 'user', 'parts': [{'text': user_message}]})
    response = active_model.generate_content(history)
    reply_text = response.text
    history.append({'role': 'model', 'parts': [{'text': reply_text}]})
    save_conversation_history(user_id, history)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{mode_icon} {reply_text}"))


def handle_command(event, user_id, user_message):
    """コマンドに応じた処理を実行する"""
    parts = user_message.split(' ', 1)
    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    ### 変更 ### コマンド辞書に/summarizeを追加
    command_functions = {
        Config.CMD_RESET: cmd_reset,
        Config.CMD_PRO: cmd_pro,
        Config.CMD_FLASH: cmd_flash,
        Config.CMD_SEARCH: cmd_search,
        Config.CMD_SUMMARIZE: cmd_summarize,
    }

    func = command_functions.get(command)
    if func:
        func(event, user_id, args)
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"不明なコマンドです: {command}"))

# --- コマンド処理関数 ---
# ... (cmd_reset, cmd_pro, cmd_flash, cmd_search は省略) ...
def cmd_reset(event, user_id, args):
    reset_conversation_history(user_id)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="会話の履歴をリセットしました。"))
def cmd_pro(event, user_id, args):
    set_user_mode(user_id, 'pro')
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🤖 高精度モード (Pro) に切り替えました。\n(上限: {Config.PRO_MODE_LIMIT}回/日)"))
def cmd_flash(event, user_id, args):
    set_user_mode(user_id, 'flash')
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚡️ 高速モード (Flash) に切り替えました。"))
def cmd_search(event, user_id, query):
    if not query:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="検索キーワードを入力してください。\n例: /search 今日のニュース"))
        return
    display_loading_animation(user_id)
    search_results = Google Search(query)
    prompt = f"以下のWeb検索結果を元に、ユーザーの質問「{query}」に簡潔に答えてください。\n\n---\n{search_results}"
    try:
        response = models['flash'].generate_content(prompt)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🌐 Webで検索しました。\n\n" + response.text))
    except Exception as e:
        app.logger.error(f"Search Summary Error: {e}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="検索結果の要約中にエラーが発生しました。"))

# ### 追加 ### URL要約コマンドの処理関数
def cmd_summarize(event, user_id, url):
    """指定されたURLのページを要約する"""
    if not url:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="要約したいURLを入力してください。\n例: /summarize https://www.example.com"))
        return

    # 簡単なURL形式チェック
    if not re.match(r'^https?://', url):
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="有効なURLを入力してください。(http:// または https:// で始まる必要があります)"))
        return

    display_loading_animation(user_id)
    
    # URLからテキストを抽出
    text, error_message = extract_text_from_url(url)
    
    if error_message:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=error_message))
        return

    if not text or text.isspace():
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ページから本文を抽出できませんでした。JavaScriptを多用するサイトでは難しい場合があります。"))
        return
        
    # トークン数を考慮してテキストを短縮
    max_length = 15000
    if len(text) > max_length:
        text = text[:max_length]

    try:
        prompt = f"以下の記事を、重要なポイントを3〜5点にまとめて箇条書きで分かりやすく要約してください。\n\n---\n{text}"
        response = models['flash'].generate_content(prompt)
        reply_text = f"🔗 URLを要約しました。\n\n{response.text}"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        app.logger.error(f"URL要約エラー: {e}")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="要約中にエラーが発生しました。"))


# --- 管理者用機能 ---
# ... (既存の管理者機能は省略) ...
@app.route("/admin/add_code", methods=['GET'])
def add_code():
    secret = request.args.get('secret')
    if secret != Config.ADMIN_SECRET: return jsonify({"status": "error", "message": "Unauthorized"}), 401
    new_code = request.args.get('code', str(uuid.uuid4())[:8])
    get_db_reference('/valid_codes/{code}', code=new_code).set(True)
    return jsonify({"status": "success", "added_code": new_code})


# --- サーバー起動 ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
