"""
Загрузка рынков по Market ID с фильтром по статусу REGISTERED
"""

import asyncio
import urllib.request
from typing import Dict, List, Optional
from api import APIClient


STATUS_REGISTERED = "REGISTERED"


def _fetch_image_bytes(url: str) -> Optional[bytes]:
    """Скачивает изображение по URL. Возвращает bytes или None."""
    if not url or not url.strip():
        return None
    full_url = url.strip()
    if not full_url.startswith("http"):
        full_url = "https://api.predict.fun" + (full_url if full_url.startswith("/") else "/" + full_url)
    urls_to_try = [full_url]
    if "." not in full_url.split("/")[-1]:
        urls_to_try.extend([full_url + ".png", full_url + ".webp", full_url + ".jpg"])
    for u in urls_to_try:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "PredictFun-Liquidity/1.0"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = resp.read()
            if data and len(data) > 100:
                return data
        except Exception:
            continue
    return None


async def load_markets(
    market_ids: List[str],
    api_client: APIClient,
    log_func=print,
    on_progress=None,
) -> Dict[str, dict]:
    """
    Загружает рынки по ID параллельно. Возвращает только с статусом REGISTERED.
    """
    valid_ids = [m.strip() for m in market_ids if m.strip()]
    total = len(valid_ids)
    sem = asyncio.Semaphore(10)

    async def fetch_one(mid: str):
        async with sem:
            try:
                info = await api_client.get_market_info(mid, log_func)
                if info:
                    img_url = info.get("imageUrl") or info.get("image_url")
                    if img_url:
                        try:
                            img_data = await asyncio.to_thread(_fetch_image_bytes, img_url)
                            if img_data:
                                info = {**info, "_image_data": img_data}
                        except Exception:
                            pass
                return mid, info
            except Exception:
                return mid, None

    tasks = [asyncio.create_task(fetch_one(mid)) for mid in valid_ids]
    markets = {}
    for i, fut in enumerate(asyncio.as_completed(tasks), 1):
        mid, info = await fut
        from logger import debug_module
        debug_module("Loader", f"загружен market_id={mid}", {"status": info.get("status") if info else None, "title": (info.get("title") or "")[:40] if info else None})
        if info:
            status = (info.get("status") or "").strip().upper()
            if status == STATUS_REGISTERED:
                markets[mid] = info
                title = info.get("title") or info.get("question") or mid
                log_func(f"✓ Рынок {mid}: {str(title)[:50]}...")
            else:
                log_func(f"✗ Рынок {mid} пропущен: статус не Registered ({status or '—'})")
        if on_progress:
            try:
                on_progress(i, total)
            except Exception:
                pass
    return markets
