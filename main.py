# -*- coding: utf-8 -*-
"""
LINE Messenger Platform上で動作する、Google Gemini APIを活用した多機能チャットボット。
非同期処理と会話文脈を考慮した検索機能を搭載。
"""
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime

import firebase_admin
import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
from firebase_admin import credentials, db
from flask import Flask, abort, jsonify, request
from flask_rq2 import RQ
from googleapiclient.discovery import build
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (ImageMessage, MessageEvent, TextMessage,
                            TextSendMessage)

# --- 外部ファイルから設定を読み込む ---
from config import Config

# --- アプリケーションと拡張機能の初期化 ---
app = Flask(__name__)
app.config.from_object(Config)
rq = RQ(app) # 非同期タスクキューを初期化

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

# APIクライアントとモデルの初期化
line_bot_api = LineBotApi(Config.LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(Config.LINE_CHANNEL_SECRET)
genai.configure(api_key=Config.GEMINI_API_KEY)
models = {
    'flash': genai.GenerativeModel('gemini-1.5-flash-latest'),
    'pro': genai.GenerativeModel('gemini-1.5-pro-latest')
}

# --- 非同期タスクの定義 ---
@rq.job
def run_search_task(user_id: str, query: str):
    """
    /searchコマンドの本体。検索、情報収集、要約、結果送信を非同期で実行。
    完了後、LINEのPush Messageでユーザーに結果を通知する。
    """
    # このタスクはFlaskのリクエスト外で動くため、APIクライアントを都度初期化する
    task_line_bot_api = LineBotApi(Config.LINE_CHANNEL_ACCESS_TOKEN)
    genai.configure(api_key=Config.GEMINI_API_KEY)
    task_model = genai.GenerativeModel('gemini-1.5-flash-latest')

    try:
        # 1. Google検索
        service = build("customsearch", "v1", developerKey=Config.SEARCH_API_KEY)
        res = service.cse().list(q=query, cx=Config.SEARCH_ENGINE_ID, num=3).execute()
        search_results = [{'title': item.get('title'), 'link': item.get('link')} for item in res.get('items', [])]
        
        if not search_results:
            task_line_bot_api.push_message(user_id, TextSendMessage(text=f"「{query}」に関する情報が見つかりませんでした。"))
            return

        # 2. Webサイトからの情報抽出
        scraped_contents, referenced_urls = [], []
        for result in search_results[:2]:
            url = result.get('link')
            if not url: continue
            
            try:
                headers = {'User-Agent': 'Mozilla/5.0'}
                response = requests.get(url, headers=headers, timeout=10)
                response.raise_for_status()
                response.encoding = response.apparent_encoding
                soup = BeautifulSoup(response.text, 'html.parser')
                for element in soup(['script', 'style', 'header', 'footer', 'nav', 'aside']):
                    element.decompose()
                text = soup.get_text(separator='\n', strip=True)
                scraped_contents.append(f"--- 参照サイト: {url} ---\n\n{text[:7000]}")
                referenced_urls.append(url)
            except Exception as e:
                app.logger.warning(f"サイトの読み込みに失敗: {url}, 理由: {e}")

        if not scraped_contents:
            task_line_bot_api.push_message(user_id, TextSendMessage(text=f"「{query}」について、Webサイトから情報を読み取れませんでした。"))
            return

        # 3. Geminiによる要約
        combined_text = "\n\n".join(scraped_contents)
        prompt = (
            f"あなたは優秀な調査アシスタントです。以下のWebサイトの情報とユーザーの質問を元に、回答を生成してください。\n\n"
            f"■ ユーザーの質問:\n{query}\n\n"
            f"■ 参照したWebサイトの情報:\n{combined_text}\n\n"
            f"■ 回答のルール:\n"
            f"- ユーザーの質問に対する直接的な答えを、まず最初に明確に記述してください。\n"
            f"- その後、背景や詳細、重要なポイントを箇条書きなども活用して分かりやすく説明してください。\n"
            f"- 日本語で、自然で丁寧な文章で回答してください。"
        )
        
        response = task_model.generate_content(prompt)
        reply_text = f"🌐 Web調査が完了しました。\n\n【質問】\n{query}\n\n【回答】\n{response.text}"
        if referenced_urls:
            reply_text += "\n\n【参考にしたサイト】\n" + "\n".join(f"・{url}" for url in referenced_urls)

        # 4. ユーザーへの結果送信
        task_line_bot_api.push_message(user_id, TextSendMessage(text=reply_text))

    except Exception as e:
        app.logger.error(f"非同期検索タスクでエラーが発生: {e}", exc_info=True)
        task_line_bot_api.push_message(user_id, TextSendMessage(text=f"調査中にエラーが発生しました。\n質問: {query}\nしばらくしてからもう一度お試しください。"))


# --- データベース関連関数 (変更なし) ---
def get_db_reference(path_template: str, **kwargs):
    return db.reference(path_template.format(**kwargs))
# (以下、get_user_mode, set_user_modeなどのDB関数は元のコードと同じなので省略)
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

# --- ユーティリティ関数 ---
def display_loading_animation(user_id):
    headers = {'Authorization': f'Bearer {Config.LINE_CHANNEL_ACCESS_TOKEN}', 'Content-Type': 'application/json'}
    data = {'chatId': user_id, 'loadingSeconds': 20}
    try:
        requests.post('https://api.line.me/v2/bot/chat/loading/start', headers=headers, json=data, timeout=5)
    except requests.exceptions.RequestException as e:
        app.logger.warning(f"ローディング表示API呼び出しエラー: {e}")

def refine_search_query(user_id: str, current_query: str):
    """【新規】会話の文脈を考慮して、曖昧な検索クエリを具体的なものに変換する"""
    # クエリが十分具体的であれば、そのまま返す
    if len(current_query) > 15 or ' ' in current_query or 'とは' in current_query:
        return current_query

    history = get_conversation_history(user_id)
    if not history:
        return current_query

    # 最後のユーザー発言（現在のクエリ自身を除く）を文脈として取得
    last_user_message = ""
    for msg in reversed(history):
        if msg.get('role') == 'user':
            text = msg.get('parts', [{}])[0].get('text', '')
            if text != current_query:
                last_user_message = text
                break
    
    if not last_user_message:
        return current_query
    
    # Geminiを使って、文脈を考慮した検索クエリを生成させる
    try:
        prompt = f"""以下の会話の文脈を踏まえて、ユーザーの新しい発言を、Web検索に適した具体的な検索キーワードに変換してください。

# 文脈（直前の会話）:
"{last_user_message}"

# ユーザーの新しい発言:
"{current_query}"

# 生成する検索キーワード（単一の具体的なフレーズのみを出力）:"""
        
        response = models['flash'].generate_content(prompt)
        refined_query = response.text.strip().replace("\n", " ")
        app.logger.info(f"検索クエリを補完: '{current_query}' + 文脈 -> '{refined_query}'")
        return refined_query
    except Exception as e:
        app.logger.error(f"クエリ補完中のAIエラー: {e}")
        return current_query # エラー時は元のクエリをそのまま使う

# --- メインハンドラとロジック (変更なしの部分はコメントで省略) ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
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
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="申し訳ありません、エラーが発生しました。「/reset」で会話をリセットしてみてください。"))

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event: MessageEvent): # (変更なし)
    # (元のコードと同じ)
    user_id = event.source.user_id
    try:
        if not is_user_authenticated(user_id):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="先に認証コードを入力して、認証を完了してください。"))
            return

        display_loading_animation(user_id)
        message_content = line_bot_api.get_message_content(event.message.id)
        image_data = message_content.content
        image_part = {"mime_type": "image/jpeg", "data": image_data}
        prompt_part = "この画像について、見たままを詳しく、そして分かりやすく説明してください。"

        response = models['flash'].generate_content([prompt_part, image_part])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🖼️ 画像を解析しました。\n\n{response.text}"))
    except Exception as e:
        app.logger.error(f"画像処理中に予期せぬエラー: {e}", exc_info=True)
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="申し訳ありません、画像の処理中にエラーが発生しました。"))

def handle_authentication(event: MessageEvent, user_id: str, code: str): # (変更なし)
    # (元のコードと同じ)
    if authenticate_user(user_id, code):
        welcome_message = "認証が完了しました。ご質問をどうぞ。"
        command_list = (
            f"【コマンド一覧】\n"
            f"{Config.CMD_SEARCH} [キーワード]\n"
            f"{Config.CMD_PRO} - 高精度モード\n"
            f"{Config.CMD_FLASH} - 高速モード\n"
            f"{Config.CMD_RESET} - 履歴リセット"
        )
        line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=welcome_message), TextSendMessage(text=command_list)])
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="認証コードを入力してください。"))
    
def handle_conversation(event: MessageEvent, user_id: str, user_message: str): # (変更なし)
    # (元のコードと同じ、ただし会話履歴の保存を追加)
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
    # ユーザーの発言を履歴に保存
    history.append({'role': 'user', 'parts': [{'text': user_message}]})
    
    response = active_model.generate_content(history)
    reply_text = response.text

    # モデルの応答も履歴に保存
    history.append({'role': 'model', 'parts': [{'text': reply_text}]})
    save_conversation_history(user_id, history)

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"{mode_icon} {reply_text}"))

def handle_command(event: MessageEvent, user_id: str, user_message: str):
    """【改修】コマンド処理。/searchはcmd_searchを呼び出す"""
    parts = user_message.split(' ', 1)
    command = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    # ユーザーのコマンドも会話履歴に保存
    history = get_conversation_history(user_id)
    history.append({'role': 'user', 'parts': [{'text': user_message}]})
    save_conversation_history(user_id, history)

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

# --- 各コマンド ---
def cmd_reset(event: MessageEvent, user_id: str, args: str): # (変更なし)
    reset_conversation_history(user_id)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="会話の履歴をリセットしました。"))

def cmd_pro(event: MessageEvent, user_id: str, args: str): # (変更なし)
    set_user_mode(user_id, 'pro')
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🤖 高精度モード (Pro) に切り替えました。\n(上限: {Config.PRO_MODE_LIMIT}回/日)"))

def cmd_flash(event: MessageEvent, user_id: str, args: str): # (変更なし)
    set_user_mode(user_id, 'flash')
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚡️ 高速モード (Flash) に切り替えました。"))

def cmd_search(event: MessageEvent, user_id: str, query: str):
    """【全面改修】Web検索を非同期で実行する"""
    if not query:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="検索キーワードを入力してください。\n例: /search 今日のニュース"))
        return

    # 会話の文脈を考慮して検索クエリを洗練させる
    refined_query = refine_search_query(user_id, query)

    # ユーザーには即座に応答を返し、重い処理はバックグラウンドに任せる
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"「{refined_query}」についてWebでの調査を開始します。\n完了したら通知しますね！")
    )
    
    # 非同期タスクをキューに追加
    run_search_task.queue(user_id, refined_query)

# --- 管理用認証コード生成 ---
@app.route("/admin/add_code", methods=['GET']) # (変更なし)
def add_code():
    secret = request.args.get('secret')
    if secret != Config.ADMIN_SECRET:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    new_code = str(uuid.uuid4())[:8]
    get_db_reference('/valid_codes/{code}', code=new_code).set(True)
    return jsonify({"status": "success", "added_code": new_code})

# --- 起動 ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

