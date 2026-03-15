"""
Конфигурация Predict Fun Liquidity
"""

import os

API_BASE_URL = "https://api.predict.fun"
_script_dir = os.path.dirname(os.path.abspath(__file__))
ACCOUNTS_FILE = os.path.join(_script_dir, "accounts.txt")
SETTINGS_FILE = os.path.join(_script_dir, "token_settings.json")
LAST_MARKETS_FILE = os.path.join(_script_dir, "last_market_ids.txt")
MARKET_HISTORY_DIR = os.path.join(_script_dir, "market")
APP_STATE_FILE = os.path.join(_script_dir, "app_state.json")
SHARE_FILE_EXT = ".pfshare"

DEFAULT_POSITION_SIZE_USDT = 100.0
DEFAULT_POSITION_SIZE_SHARES = None
DEFAULT_MIN_SPREAD = 0.2
DEFAULT_ENABLED = True
DEFAULT_TARGET_LIQUIDITY = 1000.0
DEFAULT_MAX_AUTO_SPREAD = 6.0
DEFAULT_LIQUIDITY_MODE = "bid"

# Защита от волатильного стакана: 0 = выключена
DEFAULT_VOLATILE_REPOSITION_LIMIT = 0
DEFAULT_VOLATILE_WINDOW_SECONDS = 60
DEFAULT_VOLATILE_COOLDOWN_SECONDS = 3600

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


def get_telegram_status_interval_sec() -> int:
    """Интервал статус-репорта в Telegram (секунды). По умолчанию 3600 (1 час)."""
    try:
        import json
        with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        mins = d.get("telegram_status_interval_minutes")
        if mins is not None:
            v = int(mins)
            return max(60, min(86400, v * 60))
        return 3600
    except Exception:
        return 3600


def get_telegram_config() -> tuple:
    """Читает telegram token и chat_id из app_state.json. Если уведомления выключены — возвращает пустые строки."""
    try:
        import json
        with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not d.get("telegram_enabled", True):
            return ("", "")
        t = d.get("telegram_token") or TELEGRAM_TOKEN
        c = d.get("telegram_chat_id") or TELEGRAM_CHAT_ID
        return (t, c)
    except Exception:
        return (TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)

def get_log_settings() -> dict:
    """Читает настройки логирования из app_state.json. По умолчанию всё включено."""
    try:
        import json
        with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return {
            "log_software": d.get("log_software", True),
            "log_orderbook": d.get("log_orderbook", True),
            "log_orders": d.get("log_orders", True),
        }
    except Exception:
        return {"log_software": True, "log_orderbook": True, "log_orders": True}


# Отладочный вывод модулей (стакан, API и т.п.) — отключён
DEBUG_MODULES = False

# Детальный вывод расчёта ликвидности по ASK — отключён
DEBUG_LIQUIDITY_CALC = False


def format_proxy(proxy_string) -> dict | None:
    if not proxy_string:
        return None
    if isinstance(proxy_string, dict):
        return proxy_string
    if isinstance(proxy_string, str):
        if not proxy_string.startswith("http://"):
            proxy_string = f"http://{proxy_string}"
        return {"http": proxy_string, "https": proxy_string}
    return None


def format_proxy_for_aiohttp(proxy_string) -> str | None:
    if not proxy_string:
        return None
    if isinstance(proxy_string, str):
        if not proxy_string.startswith("http://") and not proxy_string.startswith("https://"):
            return f"http://{proxy_string}"
        return proxy_string
    return None
