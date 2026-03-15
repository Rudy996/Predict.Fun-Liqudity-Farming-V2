"""
Модуль Рынок: Решатель + Мониторинг. На каждый market_id.
"""

from typing import Dict, Optional, Callable
from calculator import Calculator
from settings import TokenSettings


class MarketModule:
    """Решатель и Мониторинг для одного рынка."""

    def __init__(
        self,
        market_id: str,
        market_info: dict,
        get_settings: Callable[[str], TokenSettings],
        on_state_changed: Optional[Callable[[str, dict], None]] = None,
    ):
        self.market_id = market_id
        self.market_info = market_info
        self.get_settings = get_settings
        self.on_state_changed = on_state_changed
        self._last_orderbook = None
        self._last_order_info = None
        self._update_time = None
        self._prev_orderbook_time = None  # время прошлого стакана (до текущего)

    def process_orderbook(
        self,
        orderbook: dict,
        get_active_orders: Callable[[], Dict],
        emit_state: bool = True,
    ) -> Optional[dict]:
        """
        Обрабатывает новый стакан: Решатель считает, Мониторинг проверяет.
        Возвращает order_info (предварительные ордера). При emit_state=True вызывает on_state_changed.
        """
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])
        if not bids or not asks:
            return None
        settings = self.get_settings(self.market_id)
        decimal_precision = self.market_info.get("decimalPrecision", 3)
        active_orders = get_active_orders()
        order_info = Calculator.calculate_limit_orders(
            orderbook, settings,
            decimal_precision=decimal_precision,
            active_orders=active_orders,
        )
        if not order_info:
            return None
        from logger import debug_module
        debug_module("Market (Решатель)", f"обработан стакан market_id={self.market_id}", {"mid_price": order_info.get("mid_price_yes"), "can_yes": order_info.get("can_place_yes"), "can_no": order_info.get("can_place_no")})
        import time
        self._last_orderbook = orderbook
        self._last_order_info = order_info
        self._prev_orderbook_time = self._update_time
        self._update_time = time.time()
        if emit_state and self.on_state_changed:
            state = {
                "order_info": order_info,
                "orderbook": orderbook,
                "settings": settings,
                "update_time": self._update_time,
                "prev_orderbook_time": self._prev_orderbook_time,
                "mid_price": order_info.get("mid_price_yes"),
                "best_bid": order_info.get("best_bid_yes"),
                "best_ask": order_info.get("best_ask_yes"),
            }
            try:
                self.on_state_changed(self.market_id, state)
            except Exception:
                pass
        return order_info

    def should_cancel_and_replace(
        self, order_info: dict, active_orders: Dict, settings: TokenSettings
    ) -> tuple[bool, bool]:
        """
        Мониторинг: нужно ли отменять YES и/или NO?
        Возвращает (cancel_yes, cancel_no).
        """
        can_yes = order_info.get("can_place_yes", True)
        can_no = order_info.get("can_place_no", True)
        has_yes = active_orders.get("yes") is not None
        has_no = active_orders.get("no") is not None
        buy_yes = order_info.get("buy_yes", {})
        buy_no = order_info.get("buy_no", {})
        cur_yes = active_orders.get("yes")
        cur_no = active_orders.get("no")
        cancel_yes = False
        cancel_no = False
        if has_yes and not can_yes:
            cancel_yes = True
        elif has_yes and can_yes and cur_yes and buy_yes:
            if abs((cur_yes.get("price") or 0) - buy_yes.get("price", 0)) > 0.001:
                cancel_yes = True
        if has_no and not can_no:
            cancel_no = True
        elif has_no and can_no and cur_no and buy_no:
            if abs((cur_no.get("price") or 0) - buy_no.get("price", 0)) > 0.001:
                cancel_no = True
        return (cancel_yes, cancel_no)

    def get_last_state(self) -> Optional[dict]:
        return {
            "order_info": self._last_order_info,
            "orderbook": self._last_orderbook,
            "update_time": self._update_time,
            "prev_orderbook_time": self._prev_orderbook_time,
        }
