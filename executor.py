"""
Исполнитель: place, cancel. Один глобальный экземпляр на аккаунт.
"""

import asyncio
import json
import re
import time
from typing import Dict, Optional, Callable, List, Set, Tuple
import aiohttp
from predict_sdk import OrderBuilder, ChainId, OrderBuilderOptions, Side, BuildOrderInput, LimitHelperInput

from config import API_BASE_URL, format_proxy_for_aiohttp, MARKET_HISTORY_DIR
from auth import get_auth_headers, get_auth_jwt
from logger import log_error_to_file
from calculator import Calculator

PRECISION_ERROR_REDUCE_USD = 0.05
MIN_ORDER_VALUE_USD = 1.0
PRECISION_RETRY_LIMIT = 3
PRECISION_BLOCK_HOURS = 24
AMOUNT_PRECISION = 10**13


def _round_amount_to_precision(val) -> int:
    v = int(val)
    return (v // AMOUNT_PRECISION) * AMOUNT_PRECISION


def _is_precision_error(text: str) -> bool:
    return "InvalidPrecisionError" in text or ("Price precision" in text and "Max allowed is" in text and "decimal points" in text)


def _parse_allowed_decimal_points(text: str) -> Optional[int]:
    """Из 'Max allowed is 3 decimal points' извлекает 3. Иначе None."""
    m = re.search(r"Max allowed is (\d+) decimal points", text)
    return int(m.group(1)) if m else None


class Executor:
    def __init__(
        self,
        api_key: str,
        jwt_token: str,
        predict_account_address: str,
        privy_wallet_private_key: str,
        proxy: Optional[str] = None,
        log_func: Callable = print,
        api_client=None,
        on_precision_min_reached: Optional[Callable[[str, str], None]] = None,
    ):
        self.api_key = api_key
        self.jwt_token = jwt_token
        self.predict_account_address = predict_account_address
        self.privy_wallet_private_key = privy_wallet_private_key
        self.proxy = proxy
        self.proxy_url = format_proxy_for_aiohttp(proxy) if proxy else None
        self.log_func = log_func
        self.api_client = api_client
        self.on_precision_min_reached = on_precision_min_reached
        self._update_headers()
        privy_key = privy_wallet_private_key or ""
        if privy_key.startswith("0x"):
            privy_key = privy_key[2:]
        self.builder = OrderBuilder.make(
            ChainId.BNB_MAINNET,
            privy_key,
            OrderBuilderOptions(predict_account=predict_account_address),
        )
        self.active_orders: Dict[str, Dict] = {}
        self._lock = asyncio.Lock()
        self._op_queue: List[tuple] = []
        self._op_processing = False
        self._op_lock = asyncio.Lock()
        self._cancel_pending: Dict[str, bool] = {}
        self.last_mid_price: Dict[str, float] = {}
        self.placing: Dict[str, bool] = {}
        self._blocked_outcomes: Dict[Tuple[str, str], float] = {}
        self.on_cancel_done: Optional[Callable[[str], None]] = None  # после успешной отмены — для place с last order_info

    def _update_headers(self):
        self.headers = get_auth_headers(self.jwt_token, self.api_key)

    async def _refresh_jwt(self) -> bool:
        try:
            self.log_func("Обновление JWT...")
            new_jwt = await get_auth_jwt(
                self.api_key, self.predict_account_address,
                self.privy_wallet_private_key, self.proxy,
                log_func=self.log_func,
            )
            if new_jwt:
                self.jwt_token = new_jwt
                self._update_headers()
                if self.api_client:
                    self.api_client.update_token(new_jwt)
                return True
        except Exception as e:
            self.log_func(f"✗ JWT: {e}")
            log_error_to_file("JWT refresh", exception=e)
        return False

    def _ensure_market(self, market_id: str):
        if market_id not in self.active_orders:
            self.active_orders[market_id] = {"yes": None, "no": None}

    def _get_log_prefix(self, market_id: str, market_title: str = "") -> str:
        return f"[{market_id} | {market_title or market_id}]"

    def _log_order_event(self, market_id: str, event: dict) -> None:
        try:
            from config import get_log_settings
            if not get_log_settings().get("log_orders", True):
                return
        except Exception:
            pass
        import datetime, os
        event["ts"] = datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
        event["market_id"] = str(market_id)
        try:
            os.makedirs(MARKET_HISTORY_DIR, exist_ok=True)
            path = os.path.join(MARKET_HISTORY_DIR, f"{market_id}_orders.txt")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def _block_outcome(self, market_id: str, outcome: str) -> None:
        key = (market_id, outcome)
        self._blocked_outcomes[key] = time.time() + PRECISION_BLOCK_HOURS * 3600
        self.log_func(f"[{market_id}] {outcome.upper()}: precision fail после {PRECISION_RETRY_LIMIT} попыток — блок на {PRECISION_BLOCK_HOURS} ч")

    def is_outcome_blocked(self, market_id: str, outcome: str) -> bool:
        key = (market_id, outcome)
        until = self._blocked_outcomes.get(key)
        if until is None:
            return False
        if time.time() >= until:
            del self._blocked_outcomes[key]
            return False
        return True

    def _reason_cant_place(self, outcome: str, order_info: dict) -> str:
        """Причина, почему нельзя выставить ордер."""
        liq_ok = order_info.get("can_place_yes_liquidity" if outcome == "yes" else "can_place_no_liquidity", True)
        spr_ok = order_info.get("can_place_yes_spread" if outcome == "yes" else "can_place_no_spread", True)
        liq = order_info.get("liquidity_yes" if outcome == "yes" else "liquidity_no", 0)
        min_liq = order_info.get("min_liquidity", 300)
        if not liq_ok:
            return f"ликвидность ${liq:,.0f} < мин ${min_liq:,.0f}"
        if not spr_ok:
            return "спред меньше минимального при цене на минимуме"
        return "условия не выполнены"

    def _get_market_params(self, market_info: Optional[Dict]) -> Dict:
        if not market_info:
            return {"fee_rate_bps": 200, "is_neg_risk": False, "is_yield_bearing": True}
        return {
            "fee_rate_bps": market_info.get("feeRateBps", 200),
            "is_neg_risk": market_info.get("isNegRisk", False),
            "is_yield_bearing": market_info.get("isYieldBearing", True),
        }

    def _get_token_id(self, market_info: Dict, outcome: str) -> Optional[str]:
        outcomes = market_info.get("outcomes", [])
        if len(outcomes) < 2:
            return None
        ol = outcome.lower()
        for out in outcomes:
            name = (out.get("name") or "").lower()
            if (ol == "yes" and name in ("yes", "y")) or (ol == "no" and name in ("no", "n")) or name == ol:
                tid = out.get("onChainId") or out.get("on_chain_id") or out.get("tokenId") or out.get("token_id") or out.get("id")
                if tid:
                    return str(tid)
                return None
        idx = 0 if ol == "yes" else 1
        if idx < len(outcomes):
            out = outcomes[idx]
            tid = out.get("onChainId") or out.get("on_chain_id") or out.get("tokenId") or out.get("token_id") or out.get("id")
            return str(tid) if tid else None
        return None

    async def place_order(
        self, market_id: str, outcome: str, price: float, shares: float,
        market_info: Dict, market_title: str = ""
    ) -> Optional[Dict]:
        self._ensure_market(market_id)
        prefix = self._get_log_prefix(market_id, market_title)
        if price <= 0 or shares <= 0:
            return None
        price = round(price, 3)
        try:
            token_id = self._get_token_id(market_info, outcome)
            if not token_id:
                self.log_func(f"{prefix} ✗ Не найден tokenId для {outcome}")
                return None
            params = self._get_market_params(market_info)
            WEI = 10**18
            current_shares = shares
            precision_retry_count = 0
            while True:
                price_wei = _round_amount_to_precision(int(price * WEI))
                quantity_wei = _round_amount_to_precision(int(current_shares * WEI))
                amounts = self.builder.get_limit_order_amounts(
                    LimitHelperInput(side=Side.BUY, price_per_share_wei=price_wei, quantity_wei=quantity_wei)
                )
                order = self.builder.build_order(
                    "LIMIT",
                    BuildOrderInput(
                        side=Side.BUY,
                        token_id=str(token_id),
                        maker_amount=str(amounts.maker_amount),
                        taker_amount=str(amounts.taker_amount),
                        fee_rate_bps=params["fee_rate_bps"],
                    ),
                )
                typed_data = self.builder.build_typed_data(
                    order, is_neg_risk=params["is_neg_risk"], is_yield_bearing=params["is_yield_bearing"],
                )
                signed = self.builder.sign_typed_data_order(typed_data)
                order_hash = self.builder.build_typed_data_hash(typed_data)
                try:
                    d = signed.to_dict()
                except AttributeError:
                    try:
                        d = signed.dict()
                    except AttributeError:
                        d = {
                            "salt": str(order.salt), "maker": order.maker, "signer": order.signer,
                            "taker": order.taker, "token_id": order.token_id,
                            "maker_amount": str(order.maker_amount), "taker_amount": str(order.taker_amount),
                            "expiration": str(order.expiration), "nonce": str(order.nonce),
                            "fee_rate_bps": order.fee_rate_bps, "side": 0, "signature_type": 0,
                        }
                        d["signature"] = getattr(signed, "signature", getattr(signed, "sig", ""))
                key_map = {"maker_amount": "makerAmount", "taker_amount": "takerAmount", "token_id": "tokenId",
                           "fee_rate_bps": "feeRateBps", "signature_type": "signatureType"}
                final_order = {}
                for k, v in d.items():
                    ck = key_map.get(k, k)
                    if ck == "signature":
                        v = ("0x" + str(v)) if v and not str(v).startswith("0x") else str(v)
                    elif ck in ("makerAmount", "takerAmount"):
                        v = str(v)
                    final_order[ck] = v
                final_order["hash"] = order_hash
                body = {
                    "data": {
                        "pricePerShare": str(amounts.price_per_share),
                        "strategy": "LIMIT",
                        "slippageBps": "0",
                        "order": final_order,
                    }
                }
                from logger import debug_module
                debug_module("Executor", f"place {outcome} market_id={market_id}", {"price": price, "shares": current_shares})
                self.log_func(f"{prefix} {outcome.upper()}: отправка {price*100:.2f}¢ {current_shares:.1f}sh")
                for attempt in range(1, 4):
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.post(
                                f"{API_BASE_URL}/v1/orders",
                                headers=self.headers,
                                json=body,
                                proxy=self.proxy_url,
                                timeout=aiohttp.ClientTimeout(total=20),
                            ) as resp:
                                text = await resp.text()
                                if not resp.ok:
                                    if resp.status == 401:
                                        if await self._refresh_jwt():
                                            continue
                                        return None
                                    if resp.status == 400 and _is_precision_error(text):
                                        precision_retry_count += 1
                                        if precision_retry_count >= PRECISION_RETRY_LIMIT:
                                            self._block_outcome(market_id, outcome)
                                            if self.on_precision_min_reached:
                                                try:
                                                    self.on_precision_min_reached(market_id, outcome)
                                                except Exception:
                                                    pass
                                            return None
                                        allowed_dec = _parse_allowed_decimal_points(text)
                                        if allowed_dec is not None:
                                            price = round(price, allowed_dec)
                                            min_p = max(0.001, 10 ** (-allowed_dec))
                                            price = max(min_p, min(0.999, price))
                                            self.log_func(f"{prefix} Price precision, повтор с округлением до {allowed_dec} знаков: {price*100:.2f}¢")
                                        else:
                                            order_value_usd = price * current_shares
                                            new_value_usd = order_value_usd - PRECISION_ERROR_REDUCE_USD
                                            if new_value_usd < MIN_ORDER_VALUE_USD:
                                                return None
                                            current_shares = new_value_usd / price
                                            self.log_func(f"{prefix} InvalidPrecisionError, повтор с ${new_value_usd:.2f}")
                                        break
                                    if attempt < 3 and resp.status in (502, 503, 504):
                                        self.log_func(f"{prefix} ✗ place {outcome.upper()} (HTTP {resp.status}), повтор через 1 сек...")
                                        await asyncio.sleep(1)
                                        continue
                                    self.log_func(f"{prefix} ✗ place {outcome.upper()}: {resp.status} - {text[:150]}")
                                    self._log_order_event(market_id, {
                                        "action": "PLACE_FAIL",
                                        "outcome": outcome,
                                        "price": price,
                                        "shares": current_shares,
                                        "status": resp.status,
                                        "api_response": text[:500],
                                    })
                                    return None
                                try:
                                    data = json.loads(text)
                                except json.JSONDecodeError:
                                    return None
                                if data.get("success"):
                                    od = data.get("data", {})
                                    oid = od.get("id") or od.get("orderId")
                                    self.log_func(f"{prefix} {outcome.upper()} ✓ id={oid}")
                                    self._log_order_event(market_id, {
                                        "action": "PLACED",
                                        "order_id": str(oid) if oid else None,
                                        "outcome": outcome,
                                        "price": price,
                                        "shares": current_shares,
                                        "hash": order_hash,
                                        "api_response": data,
                                    })
                                    async with self._lock:
                                        self._ensure_market(market_id)
                                        self.active_orders[market_id][outcome] = {
                                            "order_id": str(oid) if oid else None,
                                            "hash": order_hash, "price": price,
                                            "shares": current_shares, "outcome": outcome,
                                        }
                                    return self.active_orders[market_id][outcome]
                                return None
                    except Exception as e:
                        attempt_suffix = f" (попытка {attempt} из 3)" if attempt > 1 else ""
                        self.log_func(f"{prefix} ✗ place {outcome.upper()}{attempt_suffix}: {e}")
                        if attempt < 3:
                            delay = attempt
                            self.log_func(f"{prefix} Повтор через {delay} сек...")
                            await asyncio.sleep(delay)
                            continue
                        self._log_order_event(market_id, {
                            "action": "PLACE_FAIL", "outcome": outcome, "price": price,
                            "shares": current_shares, "status": "exception", "api_response": str(e),
                        })
                        return None
                if precision_retry_count > 0:
                    continue
                return None
        except Exception as e:
            self.log_func(f"{prefix} ✗ place_order: {e}")
            log_error_to_file("place_order", exception=e, context=market_id)
            self._log_order_event(market_id, {
                "action": "PLACE_FAIL", "outcome": outcome, "price": price,
                "status": "exception", "api_response": str(e),
            })
            return None

    async def cancel_order_ids(self, order_ids: List[str], log_prefix: str = "", cancel_reason: str = "", market_id: str = "") -> bool:
        if not order_ids:
            return True
        for attempt in range(1, 4):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{API_BASE_URL}/v1/orders/remove",
                        headers=self.headers,
                        json={"data": {"ids": [str(x) for x in order_ids]}},
                        proxy=self.proxy_url,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 401:
                            text = await resp.text()
                            if await self._refresh_jwt():
                                continue
                            if market_id:
                                self._log_order_event(market_id, {
                                    "action": "CANCEL_FAIL", "order_ids": [str(x) for x in order_ids],
                                    "status": 401, "api_response": text[:500], "reason": cancel_reason,
                                })
                            return False
                        if not resp.ok:
                            if attempt < 3 and resp.status in (502, 503, 504):
                                if log_prefix:
                                    self.log_func(f"{log_prefix} ✗ cancel (HTTP {resp.status}), повтор через {attempt} сек...")
                                await asyncio.sleep(attempt)
                                continue
                            text = await resp.text()
                            if log_prefix:
                                self.log_func(f"{log_prefix} ✗ cancel: HTTP {resp.status} - {text}")
                            if market_id:
                                self._log_order_event(market_id, {
                                    "action": "CANCEL_FAIL", "order_ids": [str(x) for x in order_ids],
                                    "status": resp.status, "api_response": text[:500], "reason": cancel_reason,
                                })
                            return False
                        data = await resp.json()
                        if data.get("success"):
                            from logger import debug_module
                            debug_module("Executor", "cancel_order_ids", {"ids": order_ids[:5], "count": len(order_ids)})
                            if log_prefix:
                                suf = f" — {cancel_reason}" if cancel_reason else ""
                                self.log_func(f"{log_prefix} ✓ Отменено {len(order_ids)} ордер(ов){suf}")
                                self.log_func(f"{log_prefix} API response: {json.dumps(data, ensure_ascii=False)}")
                            if market_id:
                                self._log_order_event(market_id, {
                                    "action": "CANCEL_OK", "order_ids": [str(x) for x in order_ids],
                                    "reason": cancel_reason, "api_response": data,
                                })
                            async with self._lock:
                                for mid, orders in self.active_orders.items():
                                    for side in ("yes", "no"):
                                        o = orders.get(side)
                                        if o and str(o.get("order_id")) in [str(x) for x in order_ids]:
                                            self.active_orders[mid][side] = None
                            return True
                        err_msg = str(data.get("message") or data.get("error") or data)
                        if log_prefix:
                            self.log_func(f"{log_prefix} ✗ cancel: API отказал - {json.dumps(data, ensure_ascii=False)}")
                        if market_id:
                            self._log_order_event(market_id, {
                                "action": "CANCEL_FAIL", "order_ids": [str(x) for x in order_ids],
                                "status": "api_refused", "api_response": data, "reason": cancel_reason,
                            })
                        return False
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError) as e:
                attempt_suffix = f" (попытка {attempt} из 3)" if attempt > 1 else ""
                if log_prefix:
                    self.log_func(f"{log_prefix} ✗ cancel{attempt_suffix}: {e}")
                else:
                    ids_preview = ",".join(str(x) for x in order_ids[:3])
                    if len(order_ids) > 3:
                        ids_preview += f"... ({len(order_ids)} ордеров)"
                    self.log_func(f"✗ cancel ордеров [{ids_preview}]{attempt_suffix}: {e}")
                if attempt < 3:
                    delay = attempt
                    if log_prefix:
                        self.log_func(f"{log_prefix} Повтор через {delay} сек...")
                    await asyncio.sleep(delay)
                    continue
                log_error_to_file("cancel_order_ids", exception=e, context=f"ids={order_ids[:5]}")
                if market_id:
                    self._log_order_event(market_id, {
                        "action": "CANCEL_FAIL", "order_ids": [str(x) for x in order_ids],
                        "status": "exception", "api_response": str(e), "reason": cancel_reason,
                    })
                return False
        return False

    async def cancel_all(self, market_id: str, market_title: str = "", cancel_reason: str = "") -> bool:
        self._ensure_market(market_id)
        prefix = self._get_log_prefix(market_id, market_title)
        ids = []
        async with self._lock:
            for side in ("yes", "no"):
                o = self.active_orders[market_id].get(side)
                if o and o.get("order_id"):
                    ids.append(o["order_id"])
        if not ids:
            return True
        self._log_order_event(market_id, {
            "action": "CANCEL_SENT", "order_ids": [str(x) for x in ids], "reason": cancel_reason,
        })
        ok = await self.cancel_order_ids(ids, log_prefix=prefix, cancel_reason=cancel_reason, market_id=market_id)
        return ok

    def get_active_orders(self, market_id: str) -> Dict:
        self._ensure_market(market_id)
        yes = self.active_orders[market_id].get("yes")
        no = self.active_orders[market_id].get("no")
        return {
            "yes": yes.copy() if yes else None,
            "no": no.copy() if no else None,
        }

    def get_all_active_order_ids(self) -> Set[str]:
        ids = set()
        for orders in self.active_orders.values():
            for side in ("yes", "no"):
                o = orders.get(side)
                if o and o.get("order_id"):
                    ids.add(str(o["order_id"]))
        return ids

    def get_managed_market_ids(self) -> Set[str]:
        return set(self.active_orders.keys())

    def _orderbook_times_suffix(self, prev_time, curr_time) -> str:
        """Добавляет к логу суффикс с временами стаканов."""
        if prev_time is None and curr_time is None:
            return ""
        import datetime
        def _ts(t):
            return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "—"
        return f" | прошлый стакан: {_ts(prev_time)}, текущий стакан: {_ts(curr_time)}"

    async def place_orders_from_preliminary(
        self, market_id: str, order_info: Dict, mid_price_yes: float,
        market_info: Dict, market_title: str,
        orderbook: Optional[Dict] = None, settings=None,
        prev_orderbook_time=None, curr_orderbook_time=None,
    ) -> bool:
        """
        Только выставляет ордера по order_info. Никогда не отменяет.
        Cancel идёт отдельно; после отмены place вызывается из on_cancel_done с last_order_info.
        """
        self._ensure_market(market_id)
        prefix = self._get_log_prefix(market_id, market_title)
        async with self._lock:
            if self.placing.get(market_id):
                return True
            self.placing[market_id] = True
        try:
            self.last_mid_price[market_id] = mid_price_yes
            buy_yes = order_info.get("buy_yes", {})
            buy_no = order_info.get("buy_no", {})
            if not buy_yes or not buy_no:
                return False
            can_yes = order_info.get("can_place_yes", True)
            can_no = order_info.get("can_place_no", True)
            liquidity_yes = order_info.get("liquidity_yes", 0)
            liquidity_no = order_info.get("liquidity_no", 0)
            min_liquidity = order_info.get("min_liquidity", 300.0)
            dec_prec = market_info.get("decimalPrecision", 3) if market_info else 3
            has_yes = self.active_orders[market_id].get("yes") is not None
            has_no = self.active_orders[market_id].get("no") is not None
            yes_blocked = self.is_outcome_blocked(market_id, "yes")
            no_blocked = self.is_outcome_blocked(market_id, "no")
            results = []
            if can_yes and not has_yes and not yes_blocked:
                before_yes = ""
                if orderbook:
                    best_ask_yes = order_info.get("best_ask_yes")
                    if best_ask_yes is not None:
                        before_yes = Calculator.get_orders_before_us_str(
                            orderbook, "yes", buy_yes.get("price", 0), best_ask_yes, dec_prec
                        )
                suf = f" | перед нами: {before_yes}" if before_yes else (" | перед нами: нет других ордеров" if orderbook and order_info.get("best_ask_yes") is not None else "")
                suf += self._orderbook_times_suffix(prev_orderbook_time, curr_orderbook_time)
                self.log_func(f"{prefix} Yes: выставляем {buy_yes.get('price', 0)*100:.1f}¢ — ликв ${liquidity_yes:,.0f} ✓ (≥ мин ${min_liquidity:,.0f}){suf}")
                r = await self.place_order(market_id, "yes", buy_yes["price"], buy_yes["shares"], market_info, market_title)
                if r is None:
                    self.log_func(f"{prefix} Yes: не удалось выставить (API/сеть)")
                results.append(r is not None)
            if can_no and not has_no and not no_blocked:
                before_no = ""
                if orderbook:
                    best_bid_yes = order_info.get("best_bid_yes")
                    if best_bid_yes is not None:
                        best_no_ask = 1.0 - best_bid_yes
                        before_no = Calculator.get_orders_before_us_str(
                            orderbook, "no", buy_no.get("price", 0), best_no_ask, dec_prec
                        )
                suf = f" | перед нами: {before_no}" if before_no else (" | перед нами: нет других ордеров" if orderbook and order_info.get("best_bid_yes") is not None else "")
                suf += self._orderbook_times_suffix(prev_orderbook_time, curr_orderbook_time)
                self.log_func(f"{prefix} No: выставляем {buy_no.get('price', 0)*100:.1f}¢ — ликв ${liquidity_no:,.0f} ✓ (≥ мин ${min_liquidity:,.0f}){suf}")
                r = await self.place_order(market_id, "no", buy_no["price"], buy_no["shares"], market_info, market_title)
                if r is None:
                    self.log_func(f"{prefix} No: не удалось выставить (API/сеть)")
                results.append(r is not None)
            return all(results) if results else True
        finally:
            self.placing[market_id] = False

    async def enqueue_place_orders(
        self, market_id: str, order_info: Dict, mid_price_yes: float,
        market_info: Dict, market_title: str,
        orderbook=None, settings=None,
        prev_orderbook_time=None, curr_orderbook_time=None,
    ) -> bool:
        """Очередь: place с дедупликацией."""
        self._ensure_market(market_id)
        buy_yes = order_info.get("buy_yes", {})
        buy_no = order_info.get("buy_no", {})
        cur = self.get_active_orders(market_id)
        if (cur.get("yes") and abs((cur["yes"].get("price") or 0) - buy_yes.get("price", 0)) < 0.001
                and abs((cur["yes"].get("shares") or 0) - buy_yes.get("shares", 0)) < 0.01):
            if (cur.get("no") and abs((cur["no"].get("price") or 0) - buy_no.get("price", 0)) < 0.001
                    and abs((cur["no"].get("shares") or 0) - buy_no.get("shares", 0)) < 0.01):
                return True
        return await self.place_orders_from_preliminary(
            market_id, order_info, mid_price_yes, market_info, market_title,
            orderbook, settings,
            prev_orderbook_time=prev_orderbook_time, curr_orderbook_time=curr_orderbook_time,
        )

    async def enqueue_cancel(self, market_id: str, market_title: str = "", cancel_reason: str = "") -> bool:
        """Очередь: отмена всех ордеров по рынку. Вызывает on_cancel_done после успеха."""
        if self._cancel_pending.get(market_id):
            return True
        self._cancel_pending[market_id] = True
        try:
            ok = await self.cancel_all(market_id, market_title, cancel_reason=cancel_reason)
            if ok and self.on_cancel_done:
                try:
                    self.on_cancel_done(market_id)
                except Exception:
                    pass
            return ok
        finally:
            self._cancel_pending[market_id] = False
