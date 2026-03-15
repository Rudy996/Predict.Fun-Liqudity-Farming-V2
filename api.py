"""
Async API клиент Predict Fun
"""

import asyncio
import aiohttp
from typing import Dict, Optional
from config import API_BASE_URL, format_proxy_for_aiohttp
from auth import get_auth_headers


class APIClient:
    def __init__(self, api_key: str, jwt_token: str, proxy: Optional[str] = None):
        self.api_key = api_key
        self.jwt_token = jwt_token
        self.proxy_url = format_proxy_for_aiohttp(proxy) if proxy else None
        self.headers = get_auth_headers(jwt_token, api_key)

    def update_token(self, jwt_token: str):
        self.jwt_token = jwt_token
        self.headers = get_auth_headers(jwt_token, self.api_key)

    async def get_category_by_slug(self, slug: str, log_func=None) -> Optional[Dict]:
        """GET /v1/categories/{slug} — категория и её рынки."""
        if log_func is None:
            log_func = print
        url = f"{API_BASE_URL}/v1/categories/{slug}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=self.headers,
                    proxy=self.proxy_url,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if not resp.ok:
                        log_func(f"Ошибка категории {slug}: {resp.status}")
                        return None
                    data = await resp.json()
                    if data.get("success") and "data" in data:
                        return data["data"]
                    return None
        except Exception as e:
            log_func(f"Ошибка получения категории {slug}: {e}")
            return None

    async def get_market_info(self, market_id: str, log_func=None) -> Optional[Dict]:
        if log_func is None:
            log_func = print
        url = f"{API_BASE_URL}/v1/markets/{market_id}"
        for attempt in range(1, 4):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        url,
                        headers=self.headers,
                        proxy=self.proxy_url,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if not resp.ok:
                            if attempt < 3:
                                await asyncio.sleep(1)
                                continue
                            return None
                        data = await resp.json()
                        if data.get("success") and "data" in data:
                            result = data["data"]
                            try:
                                from logger import debug_module
                                debug_module("API", f"get_market_info market_id={market_id}", {"status": result.get("status"), "title": (result.get("title") or "")[:30]})
                            except ImportError:
                                pass
                            return result
                        return None
            except Exception as e:
                log_func(f"Ошибка получения рынка {market_id}: {e}")
                if attempt < 3:
                    await asyncio.sleep(1)
        return None

    async def set_referral(self, code: str) -> bool:
        """POST /v1/account/referral — установка реферального кода."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{API_BASE_URL}/v1/account/referral",
                    headers={**self.headers, "Content-Type": "application/json"},
                    json={"data": {"referralCode": code}},
                    proxy=self.proxy_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json() if resp.ok else {}
                    return bool(data.get("success"))
        except Exception:
            return False

    async def get_account_info(self) -> Optional[Dict]:
        for ep in ["/v1/user", "/v1/account", "/v1/me"]:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{API_BASE_URL}{ep}",
                        headers=self.headers,
                        proxy=self.proxy_url,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        if resp.ok:
                            data = await resp.json()
                            if data.get("success") and "data" in data:
                                return data["data"]
            except Exception:
                continue
        return None

    def get_usdt_balance(
        self,
        predict_account_address: str,
        privy_wallet_private_key: str,
    ) -> Optional[float]:
        from predict_sdk import OrderBuilder, ChainId, OrderBuilderOptions
        privy_key = privy_wallet_private_key
        if privy_key.startswith("0x"):
            privy_key = privy_key[2:]
        builder = OrderBuilder.make(
            ChainId.BNB_MAINNET,
            privy_key,
            OrderBuilderOptions(predict_account=predict_account_address),
        )
        try:
            balance_wei = builder.balance_of()
            return float(balance_wei) / (10**18)
        except Exception:
            return None
