"""
Модуль логирования
"""

import re
import datetime
import builtins
import os
import traceback
import time

_original_print = builtins.print
_script_dir = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(_script_dir, "logs")
ERROR_LOG_FILE = os.path.join(LOGS_DIR, "errors.log")
_session_log_path: str | None = None
_DEDUP_SEC = 2
_last_msg, _last_time = "", 0.0


def init_session_log() -> str:
    """Создаёт новый лог-файл сессии, возвращает путь."""
    global _session_log_path
    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    _session_log_path = os.path.join(LOGS_DIR, f"session_{ts}.log")
    return _session_log_path


def mask_sensitive(text: str) -> str:
    """Маскирует apiKey, token, пароли в URL и строках."""
    if not text:
        return text
    text = re.sub(r'apiKey=[^&\s]+', 'apiKey=***', text, flags=re.I)
    text = re.sub(r'api_key["\']?\s*[:=]\s*["\']?[^"\'\s]+', 'api_key=***', text, flags=re.I)
    text = re.sub(r'token["\']?\s*[:=]\s*["\']?[^"\'\s]+', 'token=***', text, flags=re.I)
    text = re.sub(r'Bearer\s+[^\s]+', 'Bearer ***', text, flags=re.I)
    return text


def write_session_log(msg: str):
    """Пишет строку в лог-файл текущей сессии."""
    try:
        from config import get_log_settings
        if not get_log_settings().get("log_software", True):
            return
    except Exception:
        pass
    if not _session_log_path:
        return
    try:
        ts = get_timestamp()
        safe_msg = mask_sensitive(msg)
        with open(_session_log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {safe_msg}\n")
    except Exception:
        pass


def _is_error(msg: str) -> bool:
    s = msg.lower()
    return ("недостаточно средств" in s or "ошибка" in s or "failed" in s or "error" in s
            or "✗ place_order" in msg or "✗ cancel_order" in msg or "✗ jwt" in s or "✗ некорректный" in s
            or "✗ не удалось" in s or "✗ не найден" in s or "rate limit" in s)


def get_timestamp() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def console_log(msg: str, dedup: bool = True):
    global _last_msg, _last_time
    if dedup and not _is_error(msg):
        now = time.time()
        if msg == _last_msg and (now - _last_time) < _DEDUP_SEC:
            return
        _last_msg, _last_time = msg, now
    _original_print(f"[{get_timestamp()}] {msg}")


def log_print(*args, **kwargs):
    timestamp = get_timestamp()
    if args and isinstance(args[0], str):
        new_args = (f"[{timestamp}] {args[0]}",) + args[1:]
    else:
        new_args = (f"[{timestamp}]",) + args
    _original_print(*new_args, **kwargs)


def debug_module(module_name: str, message: str, data=None):
    """Вывод отладки модуля (когда DEBUG_MODULES=True)."""
    try:
        from config import DEBUG_MODULES
        if not DEBUG_MODULES:
            return
    except ImportError:
        return
    ts = get_timestamp()
    msg = f"[{ts}] [{module_name}] {message}"
    if data is not None:
        try:
            s = str(data)[:300] + ("..." if len(str(data)) > 300 else "")
            msg += f" | данные: {s}"
        except Exception:
            pass
    _original_print(msg)


def log_error_to_file(error_message: str, exception: Exception = None, context: str = ""):
    try:
        from config import get_log_settings
        if not get_log_settings().get("log_software", True):
            return
    except Exception:
        pass
    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_entry = f"\n{'='*80}\n[{timestamp}]\n"
        if context:
            log_entry += f"Контекст: {mask_sensitive(context)}\n"
        log_entry += f"Ошибка: {mask_sensitive(error_message)}\n"
        if exception:
            exc_str = str(exception)
            tb_str = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
            log_entry += f"Тип: {type(exception).__name__}\n{mask_sensitive(exc_str)}\n"
            log_entry += mask_sensitive(tb_str)
        log_entry += f"{'='*80}\n"
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(ERROR_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception as e:
        _original_print(f"[ERROR] Не удалось записать в файл: {e}")
