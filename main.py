# -*- coding: utf-8 -*-
"""
LINE Messenger Platform上で動作する、Google Gemini APIを活用した多機能チャットボット。
Web検索、画像認識、会話履歴管理などの機能を備える。
"""

import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime

import firebase_admin
import pytz
import requests
from bs4 import BeautifulSoup
from firebase_admin import credentials, db
from flask import Flask, abort, jsonify, request
from googleapiclient.discovery import build
import google.generativeai as genai
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (ImageMessage, MessageEvent, TextMessage,
                          TextSendMessage)

# --- 設定と定数 ---
class Config:
    LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    ADMIN_SECRET = os.environ.get("ADMIN_SECRET")
    FIREBASE_DATABASE_URL = os.environ.get("FIREBASE_DATABASE_URL")
    FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY")
    SEARCH_ENGINE_ID = os.environ.get("SEARCH_ENGINE_ID")

    MAX_HISTORY_LENGTH = 20
    JST = pytz.timezone('Asia/Tokyo')
    PRO_MODE_LIMIT = 5

    CMD_RESET = "/reset"
    CMD_PRO = "/pro"
    CMD_FLASH = "/flash"
    CMD_SEARCH = "/search"

app = Flask(__name__)

# Firebaseの初期化
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


def display_loading_animation(user_id):
    headers = {
        'Authorization': f'Bearer {Config.LINE_CHANNEL_ACCESS_TOKEN}',
        'Content-Type': 'application/json'
    }
    data = {'chatId': user_id, 'loadingSeconds': 20}
    try:
        requests.post('https://api.line.me/v2/bot/chat/loading/start', headers=headers, json=data, timeout=5)
    except requests.exceptions.RequestException as e:
        app.logger.warning(f"ローディング表示API呼び出しエラー: {e}")

# --- ✅ 変更後の Google 検索関数 ---
def google_search(query: str):
    """Google検索を実行し、結果を辞書のリストで返す"""
    app.logger.info(f"google_search関数（Google検索）を実行: {query}")
    if not Config.SEARCH_API_KEY or not Config.SEARCH_ENGINE_ID:
        return []
    try:
        service = build("customsearch", "v1", developerKey=Config.SEARCH_API_KEY)
        res = service.cse().list(q=query, cx=Config.SEARCH_ENGINE_ID, num=3).execute()
        if 'items' not in res or not res['items']:
            return []
        return [{'title': item.get('title'), 'link': item.get('link')} for item in res.get('items', [])]
    except Exception as e:
        app.logger.error(f"google_search関数（Google検索）でエラー: {e}")
        return []

def extract_text_from_url(url: str):
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        response.encoding = response.apparent_encoding
        soup = BeautifulSoup(response.text, 'html.parser')

        for element in soup(['script', 'style', 'header', 'footer', 'nav', 'aside']):
            element.decompose()

        text = soup.get_text(separator='\n', strip=True)
        return re.sub(r'\n\s*\n', '\n', text), None
    except requests.exceptions.RequestException as e:
        app.logger.error(f"URLへのアクセス失敗: {url}, エラー: {e}")
        return None, f"URLにアクセスできませんでした: {e}"
    except Exception as e:
        app.logger.error(f"テキスト抽出エラー: {url}, エラー: {e}")
        return None, f"ページの解析中にエラーが発生しました: {e}"

def get_db_reference(path_template: str, **kwargs):
    return db.reference(path_template.format(**kwargs))

def get_user_mode(user_id: str):
    ref = get_db_reference('/user_settings/{user_id}/mode', user_id=user_id)
    return ref.get() or 'flash'

def set_user_mode(user_id: str, mode: str):
    ref = get_db_reference('/user_settings/{user_id}/mode', user_id=user_id)
    ref.set(mode)

def get_conversation_history(user_id: str):
    ref = get_db_reference('/conversation_history/{user_id}', user_id=user_id)
    history = ref.get()
    return history[-Config.MAX_HISTORY_LENGTH:] if history else []

def save_conversation_history(user_id: str, history: list):
    ref = get_db_reference('/conversation_history/{user_id}', user_id=user_id)
    ref.set(history)

def reset_conversation_history(user_id: str):
    ref = get_db_reference('/conversation_history/{user_id}', user_id=user_id)
    ref.delete()

def check_pro_quota(user_id: str):
    today_jst_str = datetime.now(Config.JST).strftime('%Y-%m-%d')
    ref = get_db_reference('/pro_usage/{user_id}/{date}', user_id=user_id, date=today_jst_str)
    return (ref.get() or 0) < Config.PRO_MODE_LIMIT

def record_pro_usage(user_id: str):
    today_jst_str = datetime.now(Config.JST).strftime('%Y-%m-%d')
    ref = get_db_reference('/pro_usage/{user_id}/{date}', user_id=user_id, date=today_jst_str)
    ref.transaction(lambda current_count: (current_count or 0) + 1)

def is_user_authenticated(user_id: str):
    ref = get_db_reference('/authenticated_users/{user_id}', user_id=user_id)
    return ref.get() is not None

def authenticate_user(user_id: str, code: str):
    codes_ref = get_db_reference('/valid_codes')
    valid_codes = codes_ref.get()
    if valid_codes and code in valid_codes:
        get_db_reference('/authenticated_users/{user_id}', user_id=user_id).set(True)
        codes_ref.child(code).delete()
        return True
    return False

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.warning("Invalid signature. Please check your channel secret.")
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event: MessageEvent):
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
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="エラーが発生しました。「/reset」でリセットできます。"))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event: MessageEvent):
    user_id = event.source.user_id
    try:
        if not is_user_authenticated(user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="先に認証コードを入力してください。"))
            return

        display_loading_animation(user_id)
        message_content = line_bot_api.get_message_content(event.message.id)
        image_data = message_content.content
        image_part = {"mime_type": "image/jpeg", "data": image_data}
        prompt_part = "この画像について、見たままを詳しく、そして分かりやすく説明してください。"
        response = models['flash'].generate_content([prompt_part, image_part])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🖼️ 画像を解析しました。\n\n{response.text}"))
    except Exception as e:
        app.logger.error(f"画像処理中にエラー: {e}", exc_info=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="画像の処理中にエラーが発生しました。"))

def handle_authentication(event: MessageEvent, user_id: str, code: str):
    if authenticate_user(user_id, code):
        welcome = "認証が完了しました。ご質問をどうぞ。"
        command_list = (
            f"【コマンド一覧】\n"
            f"{Config.CMD_SEARCH} [キーワード]\n"
            f"{Config.CMD_PRO} - 高精度モード\n"
            f"{Config.CMD_FLASH} - 高速モード\n"
            f"{Config.CMD_RESET} - 履歴リセット"
        )
        line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=welcome), TextSendMessage(text=command_list)])
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="認証コードを入力してください。"))

def handle_conversation(event: MessageEvent, user_id: str, user_message: str):
    display_loading_animation(user_id)
    user_mode = get_user_mode(user_id)
    active_model, mode_icon = (models['flash'], "⚡️")
    if user_mode == 'pro':
        if check_pro_quota(user_id):
            active_model, mode_icon = (models['pro'], "🤖")
            record_pro_usage(user_id)
        else:
            msg = f"Proモードの上限({Config.PRO_MODE_LIMIT}回/日)に達しました。Flashモードで応答します。"
            line_bot_api.push_message(user_id, TextSendMessage(text=msg))
    
    history = get_conversation_history(user_id)
    history.append({'role': 'user', 'parts': [{'text': user_message}]})
    response = active_model.generate_content(history)
    reply_text = response.text
    history.append({'role': 'model', 'parts': [{'text': reply_text}]})
    save_conversation_history(user_id, history)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{mode_icon} {reply_text}"))

def handle_command(event: MessageEvent, user_id: str, user_message: str):
    parts = user_message.split(' ', 1)
    command = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    command_functions = {
        Config.CMD_RESET: cmd_reset,
        Config.CMD_PRO: cmd_pro,
        Config.CMD_FLASH: cmd_flash,
        Config.CMD_SEARCH: cmd_search,
    }
    func = command_functions.get(command)
    if func:
        func(event, user_id, args)
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"不明なコマンドです: {command}"))

def cmd_reset(event: MessageEvent, user_id: str, args: str):
    reset_conversation_history(user_id)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="履歴をリセットしました。"))

def cmd_pro(event: MessageEvent, user_id: str, args: str):
    set_user_mode(user_id, 'pro')
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🤖 Proモードに切り替えました。({Config.PRO_MODE_LIMIT}回/日)"))

def cmd_flash(event: MessageEvent, user_id: str, args: str):
    set_user_mode(user_id, 'flash')
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚡️ Flashモードに切り替えました。"))

def cmd_search(event: MessageEvent, user_id: str, query: str):
    if not query:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="検索キーワードを入力してください。\n例: /search 今日のニュース"))
        return

    display_loading_animation(user_id)

    # ✅ 修正された関数名で検索
    search_results = google_search(query)

    if not search_results:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="関連する情報が見つかりませんでした。"))
        return

    scraped_contents, referenced_urls = [], []
    for result in search_results[:2]:
        url = result.get('link')
        if not url:
            continue

        text, error_message = extract_text_from_url(url)
        if text and not error_message:
            scraped_contents.append(f"--- 参照サイト: {url} ---\n\n{text[:7000]}")
            referenced_urls.append(url)

    if not scraped_contents:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ページの取得に失敗しました。"))
        return

    combined_text = "\n\n".join(scraped_contents)
    prompt = (
        f"以下の情報と質問に基づき回答してください。\n\n"
        f"■ 質問: {query}\n\n"
        f"■ ページ情報:\n{combined_text}\n\n"
        f"■ 指示:\n"
        f"- 回答を明確に\n- 背景を補足\n- 箇条書きOK\n- 日本語・丁寧に"
    )

    try:
        response = models['flash'].generate_content(prompt)
        reply_text = f"🌐 Webで調査しました。\n\n{response.text}"
        if referenced_urls:
            reply_text += "\n\n【参考URL】\n" + "\n".join(f"・{url}" for url in referenced_urls)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))
    except Exception as e:
        app.logger.error(f"Search/Summarize Error: {e}", exc_info=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="回答生成中にエラーが発生しました。"))

@app.route("/admin/add_code", methods=['GET'])
def add_code():
    secret = request.args.get('secret')
    if secret != Config.ADMIN_SECRET:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    new_code = str(uuid.uuid4())[:8]
    get_db_reference('/valid_codes/{code}', code=new_code).set(True)
    return jsonify({"status": "success", "added_code": new_code})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
