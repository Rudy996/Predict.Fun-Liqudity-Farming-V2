"""
Async WebSocket клиент Predict Fun
"""

import asyncio
import json
from typing import Dict, Callable, Optional
from logger import log_error_to_file

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False


class WebSocketClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        on_orderbook_update: Optional[Callable[[str, dict], None]] = None,
        on_heartbeat: Optional[Callable[[], None]] = None,
        on_connection_change: Optional[Callable[[bool], None]] = None,
        log_func: Optional[Callable[[str], None]] = None,
    ):
        self.api_key = api_key
        self.on_orderbook_update = on_orderbook_update
        self.on_heartbeat = on_heartbeat
        self.on_connection_change = on_connection_change
        self.log_func = log_func or (lambda _: None)
        self.ws = None
        self.connected = False
        self.subscriptions: Dict[str, None] = {}
        self.request_id_counter = 0
        self._task = None
        self._running = False
        self._loop = None
        self._reconnect_attempt = 0
        ws_url = "wss://ws.predict.fun/ws"
        if api_key:
            ws_url += f"?apiKey={api_key}"
        self.ws_url = ws_url

    def _get_next_request_id(self) -> int:
        self.request_id_counter += 1
        return self.request_id_counter

    async def _run(self):
        if not HAS_WEBSOCKETS:
            log_error_to_file("websockets не установлен", context="pip install websockets")
            return
        self._running = True
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=10,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    self._reconnect_attempt = 0
                    self.log_func("[WebSocket] ✓ Подключено")
                    if self.on_connection_change:
                        try:
                            self.on_connection_change(True)
                        except Exception:
                            pass
                    await asyncio.sleep(0.5)
                    for mid in list(self.subscriptions.keys()):
                        await self._subscribe(mid)
                    ping_log_task = asyncio.create_task(self._ping_pong_log_loop())
                    try:
                        async for message in ws:
                            if not self._running:
                                break
                            try:
                                data = json.loads(message)
                                if data.get("type") == "M" and data.get("topic") == "heartbeat":
                                    ts = data.get("data")
                                    await self._send_heartbeat(ws, ts)
                                    if self.on_heartbeat:
                                        try:
                                            self.on_heartbeat()
                                        except Exception:
                                            pass
                                    continue
                                if data.get("type") == "R":
                                    continue
                                if data.get("type") == "M":
                                    topic = data.get("topic", "")
                                    if topic.startswith("predictOrderbook/"):
                                        mid = topic.split("/")[1]
                                        ob = data.get("data", {})
                                        if ob and (ob.get("bids") or ob.get("asks")):
                                            from logger import debug_module
                                            debug_module("WebSocket", f"получен стакан market_id={mid}", {"bids": len(ob.get("bids", [])), "asks": len(ob.get("asks", []))})
                                            if self.on_orderbook_update:
                                                try:
                                                    self.on_orderbook_update(mid, ob)
                                                except Exception as e:
                                                    log_error_to_file(
                                                        f"on_orderbook_update: {e}",
                                                        exception=e,
                                                        context=f"market_id={mid}",
                                                    )
                            except json.JSONDecodeError:
                                pass
                            except Exception as e:
                                log_error_to_file("WebSocket message", exception=e, context="websocket")
                    finally:
                        ping_log_task.cancel()
                        try:
                            await ping_log_task
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                self.log_func("[WebSocket] Остановлен")
                break
            except Exception as e:
                self._reconnect_attempt += 1
                attempt_suffix = f" (попытка {self._reconnect_attempt})" if self._reconnect_attempt > 1 else ""
                err_msg = str(e) if e else repr(e)
                if "ping" in err_msg.lower() or "pong" in err_msg.lower() or "timeout" in err_msg.lower():
                    self.log_func(f"[WebSocket] ✗ connect{attempt_suffix}: Pong не получен (таймаут)")
                elif "connect" in err_msg.lower() or "host" in err_msg.lower() or "connection" in err_msg.lower():
                    self.log_func(f"[WebSocket] ✗ connect{attempt_suffix}: {e}")
                else:
                    self.log_func(f"[WebSocket] ✗ connect{attempt_suffix}: {e}")
                if self._running:
                    self.log_func("[WebSocket] Повтор через 5 сек…")
                log_error_to_file("WebSocket error", exception=e if isinstance(e, Exception) else None, context="websocket")
            finally:
                self.ws = None
                self.connected = False
                if self.on_connection_change:
                    try:
                        self.on_connection_change(False)
                    except Exception:
                        pass
            if self._running:
                await asyncio.sleep(5)
        self.connected = False

    async def _subscribe(self, market_id: str):
        req_id = self._get_next_request_id()
        msg = {"method": "subscribe", "requestId": req_id, "params": [f"predictOrderbook/{market_id}"]}
        if self.ws:
            await self.ws.send(json.dumps(msg))

    async def _ping_pong_log_loop(self):
        """Раз в 10 сек логируем успешный ping/pong (соединение живо)."""
        while self._running and self.connected:
            try:
                await asyncio.sleep(10)
                if self._running and self.connected and self.ws:
                    self.log_func("[WebSocket] ping ✓ pong ✓")
            except asyncio.CancelledError:
                break
            except Exception:
                break

    async def _send_heartbeat(self, ws, timestamp):
        msg = {"method": "heartbeat", "data": timestamp}
        await ws.send(json.dumps(msg))

    def start(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        if self._task and not self._task.done():
            return
        self._task = loop.create_task(self._run())

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    def subscribe_orderbook(self, market_id: str):
        self.subscriptions[market_id] = None
        if self._loop and self.connected and self.ws:
            asyncio.run_coroutine_threadsafe(self._subscribe(market_id), self._loop)

    def unsubscribe_orderbook(self, market_id: str):
        self.subscriptions.pop(market_id, None)
