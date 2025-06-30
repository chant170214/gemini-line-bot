# -*- coding: utf-8 -*-
"""
アプリケーションの設定と定数を管理します。
"""

import os
import pytz

class Config:
    """アプリケーションの設定と定数を管理するクラス"""
    # 環境変数から取得
    LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
    ADMIN_SECRET = os.environ.get("ADMIN_SECRET")
    FIREBASE_DATABASE_URL = os.environ.get("FIREBASE_DATABASE_URL")
    FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY")
    SEARCH_ENGINE_ID = os.environ.get("SEARCH_ENGINE_ID")

    # 固定値
    MAX_HISTORY_LENGTH = 20
    JST = pytz.timezone('Asia/Tokyo')
    PRO_MODE_LIMIT = 5

    # コマンド定義
    CMD_RESET = "/reset"
    CMD_PRO = "/pro"
    CMD_FLASH = "/flash"
    CMD_SEARCH = "/search"
