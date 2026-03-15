"""
Настройки токенов по market_id
"""

import json
import os
from typing import Dict, Optional
from config import (
    SETTINGS_FILE, DEFAULT_POSITION_SIZE_USDT,
    DEFAULT_POSITION_SIZE_SHARES, DEFAULT_MIN_SPREAD,
    DEFAULT_ENABLED, DEFAULT_TARGET_LIQUIDITY,
    DEFAULT_MAX_AUTO_SPREAD, DEFAULT_LIQUIDITY_MODE,
    DEFAULT_VOLATILE_REPOSITION_LIMIT, DEFAULT_VOLATILE_WINDOW_SECONDS,
    DEFAULT_VOLATILE_COOLDOWN_SECONDS,
)


class TokenSettings:
    def __init__(
        self,
        market_id: str,
        position_size_usdt: Optional[float] = DEFAULT_POSITION_SIZE_USDT,
        position_size_shares: Optional[float] = DEFAULT_POSITION_SIZE_SHARES,
        min_spread: Optional[float] = DEFAULT_MIN_SPREAD,
        enabled: bool = DEFAULT_ENABLED,
        target_liquidity: float = DEFAULT_TARGET_LIQUIDITY,
        max_auto_spread: float = DEFAULT_MAX_AUTO_SPREAD,
        liquidity_mode: str = DEFAULT_LIQUIDITY_MODE,
        volatile_reposition_limit: Optional[int] = DEFAULT_VOLATILE_REPOSITION_LIMIT,
        volatile_window_seconds: Optional[float] = DEFAULT_VOLATILE_WINDOW_SECONDS,
        volatile_cooldown_seconds: Optional[float] = DEFAULT_VOLATILE_COOLDOWN_SECONDS,
        is_custom: bool = False
    ):
        self.market_id = market_id
        self.position_size_usdt = position_size_usdt
        self.position_size_shares = position_size_shares
        self.min_spread = min_spread
        self.enabled = enabled
        self.target_liquidity = target_liquidity
        self.max_auto_spread = max_auto_spread
        self.liquidity_mode = liquidity_mode if liquidity_mode in ("bid", "ask") else DEFAULT_LIQUIDITY_MODE
        self.volatile_reposition_limit = volatile_reposition_limit if volatile_reposition_limit is not None else DEFAULT_VOLATILE_REPOSITION_LIMIT
        self.volatile_window_seconds = volatile_window_seconds if volatile_window_seconds is not None else DEFAULT_VOLATILE_WINDOW_SECONDS
        self.volatile_cooldown_seconds = volatile_cooldown_seconds if volatile_cooldown_seconds is not None else DEFAULT_VOLATILE_COOLDOWN_SECONDS
        self.is_custom = is_custom

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dict__.items()}

    @classmethod
    def from_dict(cls, data: Dict) -> "TokenSettings":
        return cls(
            market_id=data.get("market_id", ""),
            position_size_usdt=data.get("position_size_usdt", DEFAULT_POSITION_SIZE_USDT),
            position_size_shares=data.get("position_size_shares", DEFAULT_POSITION_SIZE_SHARES),
            min_spread=data.get("min_spread", DEFAULT_MIN_SPREAD),
            enabled=data.get("enabled", DEFAULT_ENABLED),
            target_liquidity=data.get("target_liquidity", DEFAULT_TARGET_LIQUIDITY),
            max_auto_spread=data.get("max_auto_spread", DEFAULT_MAX_AUTO_SPREAD),
            liquidity_mode=data.get("liquidity_mode", DEFAULT_LIQUIDITY_MODE),
            volatile_reposition_limit=data.get("volatile_reposition_limit", DEFAULT_VOLATILE_REPOSITION_LIMIT),
            volatile_window_seconds=data.get("volatile_window_seconds", DEFAULT_VOLATILE_WINDOW_SECONDS),
            volatile_cooldown_seconds=data.get("volatile_cooldown_seconds", DEFAULT_VOLATILE_COOLDOWN_SECONDS),
            is_custom=data.get("is_custom", False),
        )


class SettingsManager:
    def __init__(self, settings_file: str = SETTINGS_FILE):
        self.settings_file = settings_file
        self.settings: Dict[str, TokenSettings] = {}
        self.load_settings()

    def load_settings(self):
        if not os.path.exists(self.settings_file):
            self.settings = {}
            return
        try:
            with open(self.settings_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.settings = {mid: TokenSettings.from_dict(d) for mid, d in data.items()}
        except Exception:
            self.settings = {}

    def save_settings(self):
        try:
            data = {mid: s.to_dict() for mid, s in self.settings.items()}
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Ошибка сохранения: {e}")

    def get_settings(self, market_id: str, use_defaults_if_not_custom: bool = False) -> TokenSettings:
        if market_id not in self.settings:
            self.settings[market_id] = TokenSettings(market_id=market_id)
        elif use_defaults_if_not_custom and not self.settings[market_id].is_custom:
            return TokenSettings(market_id=market_id)
        return self.settings[market_id]

    def update_settings(self, market_id: str, **kwargs):
        settings = self.get_settings(market_id)
        for k, v in kwargs.items():
            if v is not None and hasattr(settings, k):
                setattr(settings, k, v)
                settings.is_custom = True
        self.save_settings()
