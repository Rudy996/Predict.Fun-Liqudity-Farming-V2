"""
Inspector: каждые 5 сек сверяет ордера с API, отменяет orphans.
Работает в отдельном потоке с собственным event loop — не влияет на основной софт.
Получает данные через snapshot (ожидается словарь), без вызовов в основной поток.
"""

import asyncio
import threading
from typing import Callable, Dict, List, Set
import aiohttp
from config import API_BASE_URL, format_proxy_for_aiohttp, get_telegram_config
from logger import log_error_to_file

INSPECTOR_INTERVAL_SEC = 5
INSPECTOR_CYCLE_TIMEOUT_SEC = 25
PAGE_SIZE = 100
CANCEL_BATCH_SIZE = 50
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_FOOTER = "\n\nby <a href=\"https://t.me/rudy_web3\"><b>Rudy vs Web3</b></a>"


def _get_snapshot_safe(get_snapshot: Callable[[], dict]) -> dict:
    """Читает snapshot без блокировки — быстрый доступ к данным."""
    try:
        return get_snapshot() or {}
    except Exception:
        return {}


async def send_telegram_notification(message: str) -> None:
    token, chat_id = get_telegram_config()
    if not token or not chat_id:
        return
    text = (message or "") + TELEGRAM_FOOTER
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(
                TELEGRAM_API.format(token=token),
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception:
        pass


async def fetch_all_open_orders(
    headers: dict,
    proxy_url: str | None,
    api_key: str | None = None,
    log_func=print,
) -> List[Dict]:
    all_orders = []
    after = None
    while True:
        params = {"status": "OPEN", "first": str(PAGE_SIZE)}
        if api_key:
            params["apiKey"] = api_key.strip()
        if after:
            params["after"] = after
        try:
            connector = aiohttp.TCPConnector(force_close=True)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(
                    f"{API_BASE_URL}/v1/orders",
                    headers=headers,
                    params=params,
                    proxy=proxy_url,
                    timeout=aiohttp.ClientTimeout(connect=5, total=12),
                ) as resp:
                    if not resp.ok:
                        text = await resp.text()
                        log_func(f"[Inspector] ✗ GET orders: {resp.status} - {text[:200]}")
                        break
                    data = await resp.json()
                    orders = data.get("data", [])
                    all_orders.extend(orders)
                    cursor = data.get("cursor")
                    if not cursor or len(orders) < PAGE_SIZE:
                        break
                    after = cursor
        except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as e:
            log_func(f"[Inspector] ✗ Ошибка получения ордеров: {e}")
            try:
                import concurrent.futures
                exc = e
                loop = asyncio.get_event_loop()
                loop.run_in_executor(None, lambda ex=exc: log_error_to_file("Inspector fetch orders", exception=ex))
            except Exception:
                pass
            break
    return all_orders


async def cancel_orders_direct(
    order_ids: List[str],
    headers: dict,
    proxy_url: str | None,
) -> bool:
    """Отменяет ордера напрямую через API, без Executor."""
    if not headers or not order_ids:
        return True
    for i in range(0, len(order_ids), CANCEL_BATCH_SIZE):
        batch = order_ids[i:i + CANCEL_BATCH_SIZE]
        try:
            connector = aiohttp.TCPConnector(force_close=True)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    f"{API_BASE_URL}/v1/orders/remove",
                    headers=headers,
                    json={"data": {"ids": [str(x) for x in batch]}},
                    proxy=proxy_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 401:
                        return False
                    if not resp.ok:
                        return False
                    data = await resp.json()
                    if not data.get("success", True):
                        return False
        except Exception:
            return False
        if i + CANCEL_BATCH_SIZE < len(order_ids):
            await asyncio.sleep(0.5)
    return True


async def run_inspector_cycle(
    get_snapshot: Callable[[], dict],
    log_func=print,
    on_orders_count: Callable[[int], None] | None = None,
) -> None:
    snapshot = _get_snapshot_safe(get_snapshot)
    headers = snapshot.get("headers") or {}
    if not headers:
        return
    proxy_url = format_proxy_for_aiohttp(snapshot.get("proxy"))
    api_key = snapshot.get("api_key")
    expected: Set[str] = snapshot.get("expected") or set()
    managed: Set[str] = snapshot.get("managed") or set()
    if not isinstance(expected, set):
        expected = set(expected) if expected else set()
    if not isinstance(managed, set):
        managed = set(managed) if managed else set()

    api_orders = await fetch_all_open_orders(headers, proxy_url, api_key, log_func)
    if on_orders_count is not None:
        try:
            on_orders_count(len(api_orders))
        except Exception:
            pass
    if not api_orders or not managed:
        return

    # Snapshot заново — за время fetch мы могли выставить ордера, expected должен быть актуальным
    snapshot = _get_snapshot_safe(get_snapshot)
    expected = snapshot.get("expected") or set()
    managed = snapshot.get("managed") or set()
    if not isinstance(expected, set):
        expected = set(expected) if expected else set()
    if not isinstance(managed, set):
        managed = set(managed) if managed else set()

    orphans = []
    for o in api_orders:
        mid = str(o.get("marketId", ""))
        if mid not in managed:
            continue
        oid = o.get("id") or o.get("orderId")
        if oid and str(oid) not in expected:
            orphans.append(str(oid))

    if orphans:
        from logger import debug_module
        debug_module("Inspector", "найдены orphans", {"count": len(orphans), "ids": orphans[:5]})
        ids_str = ", ".join(orphans[:10]) + ("..." if len(orphans) > 10 else "")
        log_func(f"[Inspector] Обнаружено {len(orphans)} ордер(ов) не в софте — отменяем: {ids_str}")
        ok = await cancel_orders_direct(orphans, headers, proxy_url)
        if ok:
            log_func(f"[Inspector] ✓ Отменено {len(orphans)} ордер(ов)")


def _run_inspector_thread(
    get_snapshot: Callable[[], dict],
    log_func: Callable[[str], None],
    on_orders_count: Callable[[int], None] | None,
    stop_event: threading.Event,
) -> None:
    """Запускает Inspector в отдельном потоке с собственным event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _loop():
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    run_inspector_cycle(get_snapshot, log_func, on_orders_count),
                    timeout=INSPECTOR_CYCLE_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                log_func("[Inspector] ✗ Таймаут цикла, пропуск")
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_func(f"[Inspector] ✗ Ошибка цикла: {e}")
                try:
                    loop.run_in_executor(None, lambda ex=e: log_error_to_file("Inspector loop", exception=ex))
                except Exception:
                    pass
            for _ in range(INSPECTOR_INTERVAL_SEC):
                if stop_event.is_set():
                    return
                await asyncio.sleep(1)

    try:
        loop.run_until_complete(_loop())
    finally:
        loop.close()


class Inspector:
    """
    Inspector в отдельном потоке. Не блокирует основной софт.
    Данные получает через get_snapshot() — словарь с keys:
      expected, managed, headers, proxy, api_key
    """

    def __init__(
        self,
        get_snapshot: Callable[[], dict],
        on_orders_count: Callable[[int], None] | None = None,
        log_func: Callable[[str], None] = print,
    ):
        self.get_snapshot = get_snapshot
        self.on_orders_count = on_orders_count
        self.log_func = log_func
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop_event = threading.Event()

    def start(self, _loop=None) -> None:
        """Запускает Inspector в отдельном потоке. _loop игнорируется."""
        if self._running:
            return
        self._running = True
        self._stop_event.clear()

        def _thread_target():
            try:
                _run_inspector_thread(self.get_snapshot, self.log_func, self.on_orders_count, self._stop_event)
            except Exception as e:
                try:
                    self.log_func(f"[Inspector] ✗ Поток завершён: {e}")
                except Exception:
                    pass
            finally:
                self._running = False

        self._thread = threading.Thread(target=_thread_target, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
