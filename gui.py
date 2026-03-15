"""
GUI Predict Fun Liquidity v3 — модульная архитектура
"""

import html
import os
import re
import sys
import asyncio
import time
from typing import Dict, List, Optional, Tuple
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLabel, QLineEdit, QFrame, QMessageBox, QGroupBox,
    QPlainTextEdit, QScrollArea, QSizePolicy, QCheckBox,
    QComboBox, QDialog, QDialogButtonBox, QToolButton, QFileDialog,
)
from PySide6.QtCore import Qt, QThread, Signal, QUrl, QPropertyAnimation, QEasingCurve, QRect, QRectF, QTimer, QSize
from PySide6.QtGui import (
    QFont, QDesktopServices, QDoubleValidator, QPixmap, QPainter, QPainterPath,
    QConicalGradient, QColor,
)

from api import APIClient
from balance import BalanceUpdater
from auth import get_auth_jwt
from websocket import WebSocketClient
from loader import load_markets as loader_load_markets
from executor import Executor
from market import MarketModule
from inspector import Inspector, send_telegram_notification
from settings import SettingsManager, TokenSettings
from calculator import Calculator
from accounts import load_accounts_from_file, save_accounts_to_file
from logger import log_error_to_file, get_timestamp, console_log, init_session_log, write_session_log
from orderbook_history import save_orderbook
from config import (
    LAST_MARKETS_FILE, ACCOUNTS_FILE, APP_STATE_FILE, SHARE_FILE_EXT,
    format_proxy, get_telegram_status_interval_sec,
    DEFAULT_POSITION_SIZE_USDT, DEFAULT_MIN_SPREAD,
    DEFAULT_TARGET_LIQUIDITY, DEFAULT_MAX_AUTO_SPREAD, DEFAULT_LIQUIDITY_MODE,
    DEFAULT_VOLATILE_REPOSITION_LIMIT, DEFAULT_VOLATILE_WINDOW_SECONDS,
    DEFAULT_VOLATILE_COOLDOWN_SECONDS,
)

CARD_WIDTH = 440
CARD_SPACING = 4


def _bold_title_in_question(question: str, title: str) -> str:
    """Выделяет title жирным в question. Если title не найден — пробует без спецсимволов."""
    if not question:
        return ""
    escaped = html.escape(question)
    if not title:
        return escaped
    title_escaped = html.escape(title)
    if title_escaped in escaped:
        return escaped.replace(title_escaped, f"<b>{title_escaped}</b>", 1)
    norm_title = re.sub(r"[^\w\s$€£¥.,]", "", title).strip()
    if len(norm_title) >= 2:
        norm_escaped = html.escape(norm_title)
        if norm_escaped in escaped:
            return escaped.replace(norm_escaped, f"<b>{norm_escaped}</b>", 1)
    for part in norm_title.split():
        if len(part) >= 2:
            part_esc = html.escape(part)
            if part_esc in escaped:
                return escaped.replace(part_esc, f"<b>{part_esc}</b>", 1)
    return escaped


class NoScrollComboBox(QComboBox):
    """ComboBox, который не реагирует на прокрутку колёсиком мыши."""

    def wheelEvent(self, event):
        event.ignore()


class RotatingGradientBorderFrame(QWidget):
    """Контейнер с вращающимся градиентным бордером (conic-gradient как в CSS)."""

    def __init__(self, child: QWidget, border_width: int = 3, gradient_color: Optional[QColor] = None, parent=None):
        super().__init__(parent)
        self._child = child
        self._border_width = border_width
        self._gradient_color = gradient_color or QColor(64, 196, 255)
        self._angle = 0.0
        self._show_gradient = False
        self._timer = QTimer(self)
        self._timer.setInterval(25)
        self._timer.timeout.connect(self._on_tick)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(border_width, border_width, border_width, border_width)
        layout.addWidget(child)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

    def set_gradient_visible(self, visible: bool) -> None:
        self._show_gradient = visible
        if visible:
            self._angle = 0.0
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _on_tick(self) -> None:
        self._angle = (self._angle + 2.0) % 360.0
        self.update()

    def paintEvent(self, event):
        if not self._show_gradient:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        rect = self.rect()
        center = rect.center()
        grad = QConicalGradient(center, self._angle)
        bright = self._gradient_color
        transparent = QColor(0, 0, 0, 0)
        grad.setColorAt(0.0, bright)
        grad.setColorAt(0.04, transparent)
        grad.setColorAt(0.25, transparent)
        grad.setColorAt(0.5, transparent)
        grad.setColorAt(0.75, transparent)
        grad.setColorAt(0.96, transparent)
        grad.setColorAt(1.0, bright)
        painter.setBrush(grad)
        painter.setPen(Qt.PenStyle.NoPen)
        r = QRectF(rect)
        outer = QPainterPath()
        outer.addRoundedRect(r, 11, 11)
        inner = QPainterPath()
        inner.addRoundedRect(r.adjusted(self._border_width, self._border_width, -self._border_width, -self._border_width), 9, 9)
        ring = outer.subtracted(inner)
        painter.drawPath(ring)


class ProxyTestWorker(QThread):
    """Проверка прокси в фоне."""
    result = Signal(bool, str)

    def __init__(self, proxy_str: str, parent=None):
        super().__init__(parent)
        self.proxy_str = (proxy_str or "").strip()

    def run(self):
        if not self.proxy_str:
            self.result.emit(False, "Прокси не указан")
            return
        try:
            import requests
            proxies = format_proxy(self.proxy_str)
            if not proxies:
                self.result.emit(False, "Неверный формат прокси")
                return
            r = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=10)
            if r.status_code == 200:
                try:
                    ip = r.json().get("origin", "")
                    self.result.emit(True, f"Прокси работает. IP: {ip}")
                except Exception:
                    self.result.emit(True, "Прокси работает")
            else:
                self.result.emit(False, f"Ответ: {r.status_code}")
        except requests.exceptions.ProxyError as e:
            self.result.emit(False, f"Ошибка прокси: {str(e)[:80]}")
        except requests.exceptions.Timeout:
            self.result.emit(False, "Таймаут")
        except Exception as e:
            self.result.emit(False, str(e)[:80])


class TelegramTestWorker(QThread):
    """Отправка тестового уведомления в Telegram."""
    result = Signal(bool, str)

    def __init__(self, token: str, chat_id: str, parent=None):
        super().__init__(parent)
        self.token = (token or "").strip()
        self.chat_id = (chat_id or "").strip()

    def run(self):
        if not self.token or not self.chat_id:
            self.result.emit(False, "Укажите токен и Chat ID")
            return
        try:
            import requests
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            r = requests.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": "✓ Тестовое уведомление из Predict Fun Liquidity\n\nby <a href=\"https://t.me/rudy_web3\"><b>Rudy vs Web3</b></a>",
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if r.ok:
                self.result.emit(True, "Уведомление отправлено")
            else:
                data = r.json() if r.text else {}
                err = data.get("description", r.text[:80] or f"Код {r.status_code}")
                self.result.emit(False, err)
        except Exception as e:
            self.result.emit(False, str(e)[:80])


BNB_STYLE = """
QMainWindow, QWidget { background-color: #1a1a1d; }
QLabel { color: #e0e0e0; font-family: 'Segoe UI', 'SF Pro Display', sans-serif; }
QLabel#title { color: #ffffff; font-size: 22px; font-weight: bold; }
QLabel#subtitle { color: #8e8e93; font-size: 13px; }
QLineEdit { background-color: #2c2c30; color: #e0e0e0; border: 1px solid #3a3a3e; border-radius: 8px; padding: 8px 12px; font-size: 13px; selection-background-color: #F7931A; }
QLineEdit:focus { border-color: #F7931A; }
QLineEdit::placeholder { color: #6e6e73; }
QComboBox { background-color: #2c2c30; color: #e0e0e0; border: 1px solid #3a3a3e; border-radius: 6px; padding: 6px 10px; min-width: 80px; }
QPushButton { font-family: 'Segoe UI', sans-serif; font-size: 13px; font-weight: 600; border-radius: 8px; padding: 10px 20px; min-height: 20px; }
QPushButton#primary { background-color: #F7931A; color: #ffffff; border: none; }
QPushButton#primary:hover { background-color: #ffa940; }
QPushButton#primary:pressed { background-color: #e08510; }
QPushButton#primary:disabled { background-color: #4a4a4e; color: #8e8e93; }
QPushButton#secondary { background-color: #2c2c30; color: #e0e0e0; border: 1px solid #3a3a3e; }
QPushButton#secondary:hover { background-color: #3a3a3e; border-color: #F7931A; }
QPushButton#secondary:disabled { background-color: #4a4a4e; color: #8e8e93; border-color: #3a3a3e; }
QPushButton#outline { background-color: transparent; color: #F7931A; border: 2px solid #F7931A; }
QPushButton#outline:hover { background-color: rgba(247, 147, 26, 0.15); }
QPushButton#outline:disabled { background-color: #2c2c30; color: #8e8e93; border-color: #3a3a3e; }
QPushButton#danger { background-color: #dc3545; color: #ffffff; border: none; }
QPushButton#danger:hover { background-color: #e84555; }
QPushButton#danger:disabled { background-color: #4a4a4e; color: #8e8e93; }
QPushButton#link { background: transparent; color: #F7931A; border: none; }
QPushButton#link:hover { color: #ffa940; }
QGroupBox { color: #e0e0e0; font-size: 12px; font-weight: 600; border: 1px solid #3a3a3e; border-radius: 8px; margin-top: 12px; padding: 12px 12px 8px 12px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 12px; padding: 0 6px; background-color: #2c2c30; }
QCheckBox { color: #e0e0e0; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px; border: 2px solid #3a3a3e; background-color: #2c2c30; }
QCheckBox::indicator:checked { background-color: #F7931A; border-color: #F7931A; }
QPlainTextEdit { background-color: #141416; color: #a0a0a5; border: 1px solid #2c2c30; border-radius: 8px; padding: 10px; font-family: 'Consolas', 'Monaco', monospace; font-size: 12px; }
QScrollArea { border: none; background: transparent; }
QFrame#card { background-color: #2c2c30; border: 1px solid #3a3a3e; border-radius: 12px; padding: 16px; }
QFrame#card:hover { border-color: #4a4a4e; }
QFrame:disabled, QLineEdit:disabled, QComboBox:disabled, QPushButton:disabled { opacity: 0.5; color: #6e6e73; }
QFrame:disabled { background-color: #252528; }
QMessageBox { background-color: #1a1a1d; }
QMessageBox QLabel { color: #e0e0e0; font-size: 13px; }
QMessageBox QPushButton { background-color: #2c2c30; color: #e0e0e0; border: 1px solid #3a3a3e; border-radius: 8px; padding: 8px 16px; }
QMessageBox QPushButton:hover { background-color: #3a3a3e; border-color: #F7931A; color: #ffffff; }
QMessageBox QPushButton:focus { border-color: #F7931A; }
QFileDialog { background-color: #1a1a1d; }
QFileDialog QLabel { color: #e0e0e0; font-size: 13px; }
QFileDialog QLineEdit { background-color: #2c2c30; color: #e0e0e0; border: 1px solid #3a3a3e; border-radius: 6px; padding: 6px 10px; }
QFileDialog QListView, QFileDialog QTreeView { background-color: #2c2c30; color: #e0e0e0; border: 1px solid #3a3a3e; border-radius: 6px; }
QFileDialog QComboBox { background-color: #2c2c30; color: #e0e0e0; border: 1px solid #3a3a3e; }
QFileDialog QPushButton { background-color: #2c2c30; color: #e0e0e0; border: 1px solid #3a3a3e; border-radius: 8px; }
QFileDialog QPushButton:hover { background-color: #3a3a3e; border-color: #F7931A; }
QFileDialog QToolButton { background-color: #2c2c30; color: #e0e0e0; }
"""


class AsyncWorker(QThread):
    log_signal = Signal(str)
    markets_loaded = Signal(dict)
    market_load_progress = Signal(int, int)
    orderbook_update = Signal(str, dict)
    connect_done = Signal(bool, object, object)
    market_display_update = Signal(str, dict)
    place_done = Signal(str, bool)
    cancel_done = Signal(str, bool)
    ws_status_signal = Signal(bool)
    balance_updated_signal = Signal(str, float, float)
    ws_last_update_signal = Signal(float)
    inspector_orders_count_signal = Signal(int)
    category_fetched = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.loop = None
        self.ws_client: Optional[WebSocketClient] = None
        self.balance_updater: Optional[BalanceUpdater] = None

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def stop(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

    def do_connect(self, acc: dict, log_func):
        async def _connect():
            try:
                jwt = await get_auth_jwt(
                    acc["api_key"], acc["predict_account_address"],
                    acc["privy_wallet_private_key"], acc.get("proxy"), log_func
                )
                client = APIClient(acc["api_key"], jwt, acc.get("proxy"))
                account_info = await client.get_account_info()
                if account_info is None:
                    account_info = {}
                try:
                    balance = await asyncio.to_thread(
                        client.get_usdt_balance,
                        acc["predict_account_address"],
                        acc["privy_wallet_private_key"],
                    )
                    if balance is not None:
                        account_info["balance"] = balance
                except Exception:
                    pass
                self.connect_done.emit(True, (jwt, client), account_info)
            except Exception as e:
                self.connect_done.emit(False, str(e), None)
        if self.loop:
            asyncio.run_coroutine_threadsafe(_connect(), self.loop)

    def start_balance_updates(self, acc: dict, jwt_token: str, client: APIClient):
        def get_bal():
            return client.get_usdt_balance(
                acc["predict_account_address"],
                acc["privy_wallet_private_key"],
            )

        def on_updated(addr: str, balance: float, ts: float):
            self.balance_updated_signal.emit(addr, balance, ts)

        self.balance_updater = BalanceUpdater(
            get_balance_fn=get_bal,
            on_updated=on_updated,
            address=acc["predict_account_address"],
            interval_sec=60,
        )
        if self.loop:
            self.balance_updater.start(self.loop)

    def do_fetch_category(self, slug: str, api_client: "APIClient", log_func):
        async def _fetch():
            try:
                data = await api_client.get_category_by_slug(slug, log_func)
                self.category_fetched.emit(slug, data)
            except Exception as e:
                log_func(str(e))
                self.category_fetched.emit(slug, None)
        if self.loop:
            asyncio.run_coroutine_threadsafe(_fetch(), self.loop)


class MarketCard(QFrame):
    def __init__(self, market_id: str, market_info: dict, main_win: QMainWindow):
        super().__init__()
        self.setObjectName("card")
        self.setStyleSheet(
            "QFrame#card { background-color: #2c2c30; border: 1px solid #3a3a3e; border-radius: 12px; font-family: 'Segoe UI', sans-serif; }"
            " QFrame#card QGroupBox, QFrame#card QLabel, QFrame#card QLineEdit, QFrame#card QComboBox, QFrame#card QPushButton { font-family: 'Segoe UI', sans-serif; }"
            " QFrame#card QGroupBox { margin-top: 6px; padding: 6px 6px 4px 6px; }"
            " QFrame#card QGroupBox#settings_group QLineEdit, QFrame#card QGroupBox#settings_group QComboBox { background-color: #252528; border: 1px solid #3a3a3e; border-radius: 6px; padding: 6px 10px; min-height: 32px; font-size: 12px; }"
            " QPushButton#link { background: transparent; color: #F7931A; border: none; padding: 2px 4px; font-size: 11px; min-width: 0; }"
            " QPushButton#link:hover { color: #ffa940; }"
            " QPushButton#dangerLink { background: transparent; color: #e84555; border: none; padding: 2px 4px; font-size: 11px; min-width: 0; }"
            " QPushButton#dangerLink:hover { color: #ff6b6b; }"
        )
        self.market_id = market_id
        self.market_info = market_info
        self.main_win = main_win
        self.settings_manager = main_win.settings_manager
        self.settings = self.settings_manager.get_settings(market_id)
        self.last_orderbook = None
        self.orders_placed = False
        self.setFixedWidth(CARD_WIDTH)
        self.setMinimumHeight(480)
        self.last_order_info = None

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 10, 12, 10)
        question_text = market_info.get("question") or market_info.get("title") or market_id
        if len(question_text) > 100:
            question_text = question_text[:97] + "..."
        title_val = market_info.get("title")
        display_text = _bold_title_in_question(question_text, title_val or "")
        full_text = f"[{market_id}] {display_text}"
        self.title_label = QLabel(full_text)
        self.title_label.setWordWrap(True)
        self.title_label.setTextFormat(Qt.TextFormat.RichText)
        self.title_label.setStyleSheet("color: #ffffff; font-size: 17px;")
        slug = market_info.get("slug") or market_info.get("categorySlug") or market_id
        if slug and "/" in slug:
            slug = slug.split("/")[-1]
        self.market_url = f"https://predict.fun/market/{slug}" if slug else ""
        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        self.image_label = QLabel()
        self.image_label.setFixedSize(40, 40)
        self.image_label.setStyleSheet("background: #1a1a1d; border-radius: 8px;")
        self.image_label.setScaledContents(False)
        title_row.addWidget(self.image_label, 0)
        title_row.addSpacing(12)
        title_row.addWidget(self.title_label, 1)
        btns_col = QVBoxLayout()
        btns_col.setSpacing(0)
        btns_col.setContentsMargins(0, 0, 0, 0)
        btns_col.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        if self.market_url:
            link_btn = QPushButton("🔗 Открыть рынок")
            link_btn.setObjectName("link")
            link_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            link_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.market_url)))
            btns_col.addWidget(link_btn)
        remove_btn = QPushButton("✕ Удалить рынок")
        remove_btn.setObjectName("dangerLink")
        remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_btn.clicked.connect(self._on_remove_clicked)
        btns_col.addWidget(remove_btn)
        title_row.addLayout(btns_col, 0)
        layout.addLayout(title_row)

        img_data = market_info.get("_image_data")
        if img_data:
            self._set_image_from_bytes(img_data)
        else:
            img_url = market_info.get("imageUrl") or market_info.get("image_url")
            if img_url:
                QTimer.singleShot(100, lambda: self._load_image_from_url(img_url))

        orderbook_group = QGroupBox("Стакан")
        ob_layout = QVBoxLayout(orderbook_group)
        self.yes_label = QLabel("Yes: Mid -- | Bid/Ask -- / --")
        self.yes_label.setStyleSheet("color: #a0e0a0; font-size: 14px;")
        self.no_label = QLabel("No: Mid -- | Bid/Ask -- / --")
        self.no_label.setStyleSheet("color: #e0a0a0; font-size: 14px;")
        self.last_update_label = QLabel("")
        self.last_update_label.setStyleSheet("color: #6e6e73; font-size: 11px;")
        ob_layout.addWidget(self.yes_label)
        ob_layout.addWidget(self.no_label)
        ob_layout.addWidget(self.last_update_label)
        layout.addWidget(orderbook_group)

        preview_group = QGroupBox("Предварительные ордера")
        prev_layout = QVBoxLayout(preview_group)
        self.yes_order_label = QLabel("Yes: --")
        self.yes_order_label.setStyleSheet("color: #7dd87d; font-size: 14px; font-weight: 500;")
        self.yes_order_label.setWordWrap(True)
        self.no_order_label = QLabel("No: --")
        self.no_order_label.setStyleSheet("color: #e08080; font-size: 14px; font-weight: 500;")
        self.no_order_label.setWordWrap(True)
        self.orders_value_label = QLabel("Стоимость: --")
        self.orders_value_label.setStyleSheet("color: #F7931A; font-size: 14px; font-weight: 500;")
        self.placed_yes_no_label = QLabel("Выставлено: Yes: -- | No: --")
        self.placed_yes_no_label.setStyleSheet("color: #e0e0e0; font-size: 12px;")
        self.placed_yes_no_label.setWordWrap(True)
        prev_layout.addWidget(self.yes_order_label)
        prev_layout.addWidget(self.no_order_label)
        prev_layout.addWidget(self.orders_value_label)
        prev_layout.addWidget(self.placed_yes_no_label)
        layout.addWidget(preview_group)

        settings_group = QGroupBox("")
        settings_group.setObjectName("settings_group")
        s_layout = QVBoxLayout(settings_group)
        s_layout.setSpacing(10)
        settings_header = QHBoxLayout()
        settings_title = QLabel("Настройки")
        settings_title.setStyleSheet("color: #e0e0e0; font-size: 12px; font-weight: 600;")
        settings_header.addWidget(settings_title)
        faq_btn = QToolButton()
        faq_btn.setText("❓")
        faq_btn.setToolTip("FAQ")
        faq_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        faq_btn.setStyleSheet("QToolButton { background: transparent; border: none; font-size: 14px; } QToolButton:hover { opacity: 0.8; }")
        faq_btn.clicked.connect(lambda: _show_faq(self))
        settings_header.addWidget(faq_btn)
        settings_header.addStretch()
        s_layout.addLayout(settings_header)

        def _num_edit(value, min_v, max_v, decimals=2):
            e = QLineEdit()
            e.setValidator(QDoubleValidator(min_v, max_v, decimals))
            e.setText(str(value))
            e.setPlaceholderText(f"{min_v}-{max_v}")
            e.editingFinished.connect(self._recalc_preview)
            e.setMinimumHeight(32)
            return e

        def _field(label_text, widget):
            w = QWidget()
            l = QVBoxLayout(w)
            l.setContentsMargins(0, 0, 0, 2)
            lbl = QLabel(label_text)
            lbl.setStyleSheet("color: #8e8e93; font-size: 11px;")
            l.addWidget(lbl)
            l.addWidget(widget)
            return w

        self.pos_type_combo = NoScrollComboBox()
        self.pos_type_combo.addItems(["usdt", "shares"])
        self.pos_type_combo.setCurrentText("usdt" if self.settings.position_size_usdt else "shares")
        self.pos_type_combo.currentTextChanged.connect(self._on_pos_type_changed)
        self.pos_type_combo.currentTextChanged.connect(self._recalc_preview)
        self.pos_type_combo.setMinimumHeight(32)
        self.pos_edit = _num_edit(self.settings.position_size_usdt or self.settings.position_size_shares or 100, 0.1, 100000, 1)
        self.target_liq_edit = _num_edit(self.settings.target_liquidity or 1000, 1, 1000000, 0)
        self.min_spread_edit = _num_edit(self.settings.min_spread or 0.2, 0, 100, 2)
        self.max_spread_edit = _num_edit(self.settings.max_auto_spread or 6, 0.1, 100, 2)
        self.liquidity_mode_combo = NoScrollComboBox()
        self.liquidity_mode_combo.addItems(["По BID", "По ASK"])
        self.liquidity_mode_combo.setCurrentIndex(0 if (self.settings.liquidity_mode or "bid") == "bid" else 1)
        self.liquidity_mode_combo.currentIndexChanged.connect(self._recalc_preview)
        self.liquidity_mode_combo.setMinimumHeight(32)
        self.volatile_limit_edit = _num_edit(getattr(self.settings, "volatile_reposition_limit", DEFAULT_VOLATILE_REPOSITION_LIMIT) or 0, 0, 100, 0)
        self.volatile_window_edit = _num_edit(getattr(self.settings, "volatile_window_seconds", DEFAULT_VOLATILE_WINDOW_SECONDS) or 60, 1, 86400, 0)
        self.volatile_cooldown_edit = _num_edit(getattr(self.settings, "volatile_cooldown_seconds", DEFAULT_VOLATILE_COOLDOWN_SECONDS) or 3600, 0, 86400 * 7, 0)

        row1 = QHBoxLayout()
        row1.setSpacing(10)
        pos_w = QWidget()
        pos_lo = QVBoxLayout(pos_w)
        pos_lo.setContentsMargins(0, 0, 0, 2)
        pos_lbl = QLabel("Размер")
        pos_lbl.setStyleSheet("color: #8e8e93; font-size: 11px;")
        pos_lo.addWidget(pos_lbl)
        pos_inner = QHBoxLayout()
        pos_inner.addWidget(self.pos_type_combo)
        pos_inner.addWidget(self.pos_edit)
        pos_lo.addLayout(pos_inner)
        row1.addWidget(pos_w, 1)
        row1.addWidget(_field("Целевая ликвидность $", self.target_liq_edit), 1)
        s_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        row2.addWidget(_field("Мин. спред (¢)", self.min_spread_edit), 1)
        row2.addWidget(_field("Макс. спред (¢)", self.max_spread_edit), 1)
        s_layout.addLayout(row2)

        s_layout.addWidget(_field("Расчёт ликвидности", self.liquidity_mode_combo))

        vol_row = QHBoxLayout()
        vol_row.setSpacing(10)
        vol_row.addWidget(_field("Лимит переставлений (0=выкл)", self.volatile_limit_edit), 1)
        vol_row.addWidget(_field("Окно (сек)", self.volatile_window_edit), 1)
        vol_row.addWidget(_field("Пауза (сек)", self.volatile_cooldown_edit), 1)
        s_layout.addLayout(vol_row)

        self.target_liq_edit.editingFinished.connect(self._recalc_preview)
        self.max_spread_edit.editingFinished.connect(self._recalc_preview)
        layout.addWidget(settings_group)
        self.liquidity_btn = QPushButton("Выставить ликвидность")
        self.liquidity_btn.setObjectName("primary")
        self.liquidity_btn.setFixedHeight(40)
        self.liquidity_btn.setMinimumWidth(180)
        self.liquidity_btn.setStyleSheet(
            "QPushButton#primary { background-color: #F7931A; color: #ffffff; border: none; padding: 10px 20px; }"
            " QPushButton#primary:hover { background-color: #ffa940; }"
            " QPushButton#danger { background-color: #dc3545; color: #ffffff; border: none; padding: 10px 20px; }"
            " QPushButton#danger:hover { background-color: #e84555; }"
        )
        self.liquidity_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.liquidity_btn.clicked.connect(self._on_liquidity_click)
        layout.addWidget(self.liquidity_btn)
        layout.addStretch()

    def _on_pos_type_changed(self):
        t = self.pos_type_combo.currentText()
        val = self._parse(self.pos_edit, 100)
        if t == "usdt":
            self.settings.position_size_usdt = val
            self.settings.position_size_shares = None
        else:
            self.settings.position_size_shares = val
            self.settings.position_size_usdt = None
        self.settings.is_custom = True
        self.settings_manager.save_settings()

    def _parse(self, edit, default):
        try:
            v = float((edit.text() or "").strip().replace(",", "."))
            return v if v >= 0 else default
        except (ValueError, TypeError):
            return default

    def _apply_settings(self):
        val = self._parse(self.pos_edit, 100)
        if self.pos_type_combo.currentText() == "usdt":
            self.settings.position_size_usdt = val
            self.settings.position_size_shares = None
        else:
            self.settings.position_size_shares = val
            self.settings.position_size_usdt = None
        self.settings.min_spread = self._parse(self.min_spread_edit, 0.2)
        self.settings.target_liquidity = self._parse(self.target_liq_edit, 1000)
        self.settings.max_auto_spread = self._parse(self.max_spread_edit, 6)
        self.settings.liquidity_mode = "ask" if self.liquidity_mode_combo.currentIndex() == 1 else "bid"
        self.settings.volatile_reposition_limit = int(self._parse(self.volatile_limit_edit, 0))
        self.settings.volatile_window_seconds = self._parse(self.volatile_window_edit, 60)
        self.settings.volatile_cooldown_seconds = self._parse(self.volatile_cooldown_edit, 3600)
        self.settings_manager.update_settings(
            self.market_id,
            position_size_usdt=self.settings.position_size_usdt,
            position_size_shares=self.settings.position_size_shares,
            min_spread=self.settings.min_spread,
            target_liquidity=self.settings.target_liquidity,
            max_auto_spread=self.settings.max_auto_spread,
            liquidity_mode=self.settings.liquidity_mode,
            volatile_reposition_limit=self.settings.volatile_reposition_limit,
            volatile_window_seconds=self.settings.volatile_window_seconds,
            volatile_cooldown_seconds=self.settings.volatile_cooldown_seconds,
        )

    def _load_image_from_url(self, url: str):
        try:
            from loader import _fetch_image_bytes
            data = _fetch_image_bytes(url)
            if data:
                self._set_image_from_bytes(data)
        except Exception:
            pass

    def _set_image_from_bytes(self, data: bytes):
        pixmap = QPixmap()
        if not pixmap.loadFromData(data) or pixmap.isNull():
            return
        scaled = pixmap.scaled(
            40, 40,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - 40) // 2)
        y = max(0, (scaled.height() - 40) // 2)
        cropped = scaled.copy(x, y, 40, 40)
        result = QPixmap(40, 40)
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, 40, 40), 8, 8)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, cropped)
        painter.end()
        self.image_label.setPixmap(result)

    def _on_remove_clicked(self):
        dlg = ConfirmRemoveDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.main_win._remove_market(self.market_id)

    def _recalc_preview(self):
        self._apply_settings()
        if self.last_orderbook and self.main_win.market_modules.get(self.market_id):
            mod = self.main_win.market_modules[self.market_id]
            mod.process_orderbook(
                self.last_orderbook,
                lambda: self.main_win.executor.get_active_orders(self.market_id),
            )

    def _on_liquidity_click(self):
        if self.orders_placed:
            self.settings_manager.update_settings(self.market_id, enabled=False)
            self.settings = self.settings_manager.get_settings(self.market_id)
            self.orders_placed = False
            self.liquidity_btn.setText("Выставить ликвидность")
            self.liquidity_btn.setObjectName("primary")
            self.liquidity_btn.style().unpolish(self.liquidity_btn)
            self.liquidity_btn.style().polish(self.liquidity_btn)
            if self.main_win.worker and self.main_win.executor:
                asyncio.run_coroutine_threadsafe(
                    self.main_win.executor.enqueue_cancel(self.market_id, (self.market_info.get("title") or self.market_id)[:30]),
                    self.main_win.worker.loop
                )
            self._update_placed_display()
            return
        self._apply_settings()
        self.settings_manager.update_settings(self.market_id, enabled=True)
        self.settings = self.settings_manager.get_settings(self.market_id)
        self.orders_placed = True
        self.liquidity_btn.setText("Убрать ордера")
        self.liquidity_btn.setObjectName("danger")
        self.liquidity_btn.style().unpolish(self.liquidity_btn)
        self.liquidity_btn.style().polish(self.liquidity_btn)
        if self.last_orderbook and self.main_win.executor and self.main_win.worker:
            oi = Calculator.calculate_limit_orders(
                self.last_orderbook, self.settings,
                decimal_precision=self.market_info.get("decimalPrecision", 3),
                active_orders=self.main_win.executor.get_active_orders(self.market_id),
            )
            if oi and (oi.get("can_place_yes") or oi.get("can_place_no")):
                title = (self.market_info.get("title") or self.market_id)[:30]
                mod = self.main_win.market_modules.get(self.market_id)
                prev_t = mod._prev_orderbook_time if mod else None
                curr_t = mod._update_time if mod else None
                asyncio.run_coroutine_threadsafe(
                    self.main_win.executor.enqueue_place_orders(
                        self.market_id, oi, oi["mid_price_yes"],
                        self.market_info, title, self.last_orderbook, self.settings,
                        prev_orderbook_time=prev_t, curr_orderbook_time=curr_t,
                    ),
                    self.main_win.worker.loop
                )
        self.main_win.worker.place_done.emit(self.market_id, True)

    def _update_placed_display(self):
        if not self.main_win.executor:
            return
        active = self.main_win.executor.get_active_orders(self.market_id)
        yes_o = active.get("yes")
        no_o = active.get("no")
        yes_t = f"Yes: {yes_o['price']*100:.2f}¢ {yes_o['shares']:.1f}" if yes_o else "Yes: --"
        no_t = f"No: {no_o['price']*100:.2f}¢ {no_o['shares']:.1f}" if no_o else "No: --"
        self.placed_yes_no_label.setText(f"Выставлено: {yes_t} | {no_t}")

    def update_display(self, data: dict):
        mid = data.get("mid_price")
        bid = data.get("best_bid")
        ask = data.get("best_ask")
        order_info = data.get("order_info")
        update_time = data.get("update_time")
        self.last_order_info = order_info
        if update_time:
            import datetime
            self.last_update_label.setText(f"Обновлено: {datetime.datetime.fromtimestamp(update_time).strftime('%H:%M:%S')}")
        if mid is not None and bid is not None and ask is not None:
            self.yes_label.setText(f"Yes: Mid {mid*100:.2f}¢ | Bid/Ask {bid*100:.2f}¢ / {ask*100:.2f}¢")
            no_mid, no_bid, no_ask = 1 - mid, 1 - ask, 1 - bid
            self.no_label.setText(f"No: Mid {no_mid*100:.2f}¢ | Bid/Ask {no_bid*100:.2f}¢ / {no_ask*100:.2f}¢")
        if order_info:
            buy_yes = order_info.get("buy_yes", {})
            buy_no = order_info.get("buy_no", {})
            tv = order_info.get("total_value_usd", 0)
            liq_yes = order_info.get("liquidity_yes", 0)
            liq_no = order_info.get("liquidity_no", 0)
            can_yes = order_info.get("can_place_yes", False)
            can_no = order_info.get("can_place_no", False)

            def fmt_liq(v):
                if v >= 1000:
                    return f"${v:,.2f}"
                return f"${v:.2f}" if v >= 1 else f"${v:.4f}"

            if buy_yes:
                icon = "✓" if can_yes else "✗"
                self.yes_order_label.setText(
                    f"Yes: {buy_yes.get('price',0)*100:.2f}¢ ({buy_yes.get('shares',0):.1f}) | Ликв: {fmt_liq(liq_yes)} {icon}"
                )
            else:
                self.yes_order_label.setText("Yes: --")
            if buy_no:
                icon = "✓" if can_no else "✗"
                self.no_order_label.setText(
                    f"No: {buy_no.get('price',0)*100:.2f}¢ ({buy_no.get('shares',0):.1f}) | Ликв: {fmt_liq(liq_no)} {icon}"
                )
            else:
                self.no_order_label.setText("No: --")
            self.orders_value_label.setText(f"Стоимость: ${tv:.2f}")
        self._update_placed_display()
        if hasattr(self.main_win, "_update_orders_count"):
            self.main_win._update_orders_count()


DIALOG_STYLE = (
    "QDialog { background-color: #1a1a1d; } "
    "QLabel { color: #e0e0e0; font-size: 13px; } "
    "QPushButton#primary { background-color: #F7931A; color: #ffffff; border: none; border-radius: 8px; padding: 8px 16px; } "
    "QPushButton#secondary { background-color: #2c2c30; color: #e0e0e0; border: 1px solid #3a3a3e; border-radius: 8px; padding: 8px 16px; } "
    "QPushButton:hover { border-color: #F7931A; } "
)


def _styled_info(parent, title: str, text: str):
    """Информация с тёмным дизайном."""
    d = QDialog(parent)
    d.setWindowTitle(title)
    d.setMinimumWidth(320)
    d.setStyleSheet(DIALOG_STYLE)
    layout = QVBoxLayout(d)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.addWidget(QLabel(text))
    ok_btn = QPushButton("OK")
    ok_btn.setObjectName("primary")
    ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    ok_btn.clicked.connect(d.accept)
    layout.addWidget(ok_btn)
    d.exec()


def _styled_warning(parent, title: str, text: str):
    """Предупреждение с тёмным дизайном."""
    d = QDialog(parent)
    d.setWindowTitle(title)
    d.setMinimumWidth(320)
    d.setStyleSheet(DIALOG_STYLE)
    layout = QVBoxLayout(d)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.addWidget(QLabel(text))
    ok_btn = QPushButton("OK")
    ok_btn.setObjectName("primary")
    ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    ok_btn.clicked.connect(d.accept)
    layout.addWidget(ok_btn)
    d.exec()


FAQ_TEXT = """
<h2>Что такое Predict Fun Liquidity?</h2>
<p>Инструмент для <b>провайдеров ликвидности</b> на Predict Fun. Помогает держать лимитные ордера в спреде — между лучшим bid и ask. Софт получает стакан по WebSocket, считает цену и размер ордеров, выставляет их и переставляет при изменении рынка.</p>

<h2>Как это работает</h2>
<p>1) Стакан приходит по WebSocket. 2) По целевой ликвидности и настройкам считается цена. 3) Ордера размещаются на Yes и No. 4) При изменении стакана — отмена старых и выставление новых по новой цене. 5) Защита от волатильности может ограничить частоту переставлений.</p>

<h2>Настройки рынка</h2>

<h3>Размер (usdt / shares)</h3>
<p><b>usdt</b> — сколько долларов на каждую сторону (Yes и No). <b>shares</b> — сколько контрактов.</p>

<h3>Целевая ликвидность ($)</h3>
<p>Софт ищет цену, на которой «перед нами» в стакане накопилось не меньше этого объёма. Чем больше значение — тем в более безопасном месте находится наш ордер.</p>

<h3>Мин. спред (¢)</h3>
<p>Когда ордер оказывается на <b>крайней цене</b> (0.1¢), софт проверяет: расстояние от нашей цены до mid price должно быть не меньше min_spread. Если меньше — ордер не выставляется.</p>
<p>Пример: наш ордер 0.1, mid price 0.3, min_spread задан 0.4. Расстояние 0.2 &lt; 0.4 — не проходим, ордер не выставляем.</p>
<p>Зачем: когда исход почти предрешён, люди выкупают на большие суммы. Мин. спред гарантирует — перед нами всегда есть хоть какая-то «подушка» из чужих ордеров.</p>

<h2>Модуль «Автоспред»</h2>

<h3>Макс. спред (¢)</h3>
<p>Софт не выставляет лимитные ордера за пределами максимального спреда, потому что это не имеет смысла — за пределами спреда не зарабатываются поинты.</p>

<h2>Расчёт ликвидности: По BID / По ASK</h2>
<p><b>По ASK</b>: Сумма, которую нужно выкупить другому человеку, чтобы дойти до нашего ордера. Целевая ликвидность 1000$ = ищем уровень, где перед нами нужно выкупить на 1000$, чтобы нас достичь.</p>
<p><b>По BID</b>: Суммируем объём ордеров (цена × шарды) перед нами в стакане. Целевая ликвидность 1000$ = ищем уровень, где перед нами уже висит ≥1000$ ордеров.</p>

<h2>Защита от волатильности</h2>
<p>При резких движениях стакан меняется часто — софт мог бы постоянно переставлять ордера. Защита ограничивает это.</p>
<p><b>Лимит</b> — сколько переставлений разрешено за окно. 0 = выкл.</p>
<p><b>Окно (сек)</b> — за какой период считается лимит. Лимит 3, окно 60 = не больше 3 переставлений в минуту.</p>
<p><b>Пауза (сек)</b> — когда лимит достигнут, пауза перед следующим переставлением. 3600 = 1 час.</p>
<p>Пример: лимит 3, окно 60, пауза 3600. Уперлись в лимит (3 переставления за минуту) — на час переставления отключены. Через час снова выставляем ордера.</p>
"""


def _show_faq(parent):
    """Показать окно FAQ."""
    d = QDialog(parent)
    d.setWindowTitle("FAQ — Всё о софте")
    d.setMinimumSize(520, 560)
    d.setStyleSheet(DIALOG_STYLE)
    layout = QVBoxLayout(d)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
    lbl = QLabel(FAQ_TEXT)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setStyleSheet("color: #e0e0e0; font-size: 13px; padding: 8px;")
    lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    scroll.setWidget(lbl)
    layout.addWidget(scroll)
    ok_btn = QPushButton("Закрыть")
    ok_btn.setObjectName("primary")
    ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    ok_btn.clicked.connect(d.accept)
    layout.addWidget(ok_btn)
    d.exec()


def _styled_question(parent, title: str, text: str, *buttons: tuple) -> Optional[str]:
    """Вопрос с тёмным дизайном. buttons: (label, value). Возвращает value или None."""
    d = QDialog(parent)
    d.setWindowTitle(title)
    d.setMinimumWidth(340)
    d.setStyleSheet(DIALOG_STYLE)
    layout = QVBoxLayout(d)
    layout.setContentsMargins(20, 20, 20, 20)
    layout.addWidget(QLabel(text))
    btns = QDialogButtonBox()
    result = [None]
    for i, (label, val) in enumerate(buttons):
        btn = btns.addButton(label, QDialogButtonBox.ButtonRole.AcceptRole)
        btn.setObjectName("primary" if i == 0 else "secondary")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        def make_handler(v):
            def h():
                result[0] = v
                d.accept()
            return h
        btn.clicked.connect(make_handler(val))
    layout.addWidget(btns)
    d.exec()
    return result[0]


class ConfirmRemoveDialog(QDialog):
    """Диалог подтверждения удаления рынка."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Удалить рынок")
        self.setMinimumWidth(320)
        self.setStyleSheet(DIALOG_STYLE)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Вы уверены, что хотите удалить этот рынок?"))
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setObjectName("primary")
        cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setObjectName("secondary")
        layout.addWidget(btns)


def _parse_add_market_input(text: str) -> Tuple[str, str]:
    """
    Парсит ввод: URL или Market ID.
    Возвращает ("slug", slug) или ("ids", "id1,id2,...").
    """
    t = (text or "").strip()
    if not t:
        return ("", "")
    m = re.search(r"predict\.fun/market/([a-zA-Z0-9_-]+)", t, re.IGNORECASE)
    if m:
        return ("slug", m.group(1))
    ids = [x.strip() for x in t.replace("\n", ",").split(",") if x.strip()]
    if ids:
        return ("ids", ",".join(ids))
    return ("", "")


class CategorySelectionDialog(QDialog):
    """Диалог выбора рынков из категории — заголовок, картинка, чекбоксы по исходам."""

    def __init__(self, category_data: dict, parent: "MainWindow"):
        super().__init__(parent)
        self.main_win = parent
        self.category_data = category_data
        self.setWindowTitle("Выберите рынки")
        self.setMinimumWidth(520)
        self.setMinimumHeight(400)
        self.setStyleSheet(
            "QDialog { background-color: #1a1a1d; }"
            "QLabel { color: #e0e0e0; }"
            "QCheckBox { color: #e0e0e0; font-size: 16px; }"
            "QCheckBox::indicator { width: 20px; height: 20px; border-radius: 4px; border: 2px solid #3a3a3e; background-color: #2c2c30; }"
            "QCheckBox::indicator:checked { background-color: #F7931A; border-color: #F7931A; }"
            "QScrollArea { border: none; background: transparent; }"
            "QFrame#market_row { background: transparent; border: none; border-bottom: 1px solid #2c2c30; padding: 10px 0; }"
            "QFrame#market_row:hover { background: rgba(255,255,255,0.03); }"
        )
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        header_row = QHBoxLayout()
        header_row.setSpacing(16)
        header_row.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        img_url = category_data.get("imageUrl") or category_data.get("image_url") or ""
        if img_url:
            self.img_label = QLabel()
            self.img_label.setFixedSize(120, 120)
            self.img_label.setStyleSheet("background: #1a1a1d; border-radius: 8px;")
            self.img_label.setScaledContents(False)
            self.img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header_row.addWidget(self.img_label, 0)
            QTimer.singleShot(0, lambda: self._load_image(img_url))
        title = category_data.get("title") or "Категория"
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("color: #ffffff; font-size: 18px; font-weight: 600;")
        self.title_label.setWordWrap(True)
        header_row.addWidget(self.title_label, 1)
        layout.addLayout(header_row)

        markets = category_data.get("markets") or []
        self.market_checkboxes: Dict[str, QCheckBox] = {}
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setContentsMargins(0, 8, 0, 0)
        scroll_layout.setSpacing(4)

        STATUS_REGISTERED = "REGISTERED"
        for m in markets:
            mid = str(m.get("id", ""))
            status = (m.get("status") or "").strip().upper()
            if status != STATUS_REGISTERED:
                continue
            title_m = m.get("title") or m.get("question") or mid
            res = m.get("resolution") or {}
            outcome_name = res.get("name") or ""
            if outcome_name:
                display = f"{title_m} — {outcome_name}"
            else:
                display = title_m
            if len(display) > 90:
                display = display[:87] + "..."
            row = QFrame()
            row.setObjectName("market_row")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 8, 0, 8)
            cb = QCheckBox(display)
            cb.setChecked(True)
            cb.setProperty("market_id", mid)
            self.market_checkboxes[mid] = cb
            row_layout.addWidget(cb)
            scroll_layout.addWidget(row)

        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        select_all_btn = QPushButton("Выбрать все")
        select_all_btn.setObjectName("secondary")
        select_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        select_all_btn.clicked.connect(self._select_all)
        deselect_btn = QPushButton("Убрать все")
        deselect_btn.setObjectName("secondary")
        deselect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        deselect_btn.clicked.connect(self._deselect_all)
        btn_row.addWidget(select_all_btn)
        btn_row.addWidget(deselect_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setObjectName("primary")
        ok_btn.setText("Добавить выбранные")
        cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setObjectName("secondary")
        layout.addWidget(btns)

    def _load_image(self, url: str):
        try:
            from loader import _fetch_image_bytes
            data = _fetch_image_bytes(url)
            if data:
                pix = QPixmap()
                pix.loadFromData(data)
                if not pix.isNull():
                    scaled = pix.scaled(120, 120, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                    self.img_label.setPixmap(scaled)
        except Exception:
            pass

    def _select_all(self):
        for cb in self.market_checkboxes.values():
            cb.setChecked(True)

    def _deselect_all(self):
        for cb in self.market_checkboxes.values():
            cb.setChecked(False)

    def _on_ok(self):
        selected = [mid for mid, cb in self.market_checkboxes.items() if cb.isChecked()]
        if not selected:
            QMessageBox.warning(self, "Ошибка", "Выберите хотя бы один рынок")
            return
        self.main_win._add_markets(selected)
        self.accept()


class AddMarketDialog(QDialog):
    """Диалог добавления рынков — ссылка predict.fun или Market ID."""

    def __init__(self, parent: "MainWindow"):
        super().__init__(parent)
        self.main_win = parent
        self.setWindowTitle("Добавить рынок")
        self.setMinimumWidth(480)
        self.setStyleSheet(
            "QDialog { background-color: #1a1a1d; }"
            "QLabel { color: #e0e0e0; }"
            "QLineEdit { background-color: #2c2c30; color: #e0e0e0; border: 1px solid #3a3a3e; border-radius: 8px; padding: 10px 12px; }"
        )
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)
        hint = QLabel("Ссылка или Market ID (через запятую):")
        hint.setStyleSheet("color: #8e8e93; font-size: 12px;")
        layout.addWidget(hint)
        self.ids_edit = QLineEdit()
        self.ids_edit.setPlaceholderText("https://predict.fun/market/will-opinion-launch-a-token-by  или  123, 456")
        self.ids_edit.setMinimumHeight(40)
        layout.addWidget(self.ids_edit)
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #8e8e93; font-size: 11px;")
        layout.addWidget(self.status_label)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setObjectName("primary")
        ok_btn.setText("Загрузить")
        cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setObjectName("secondary")
        layout.addWidget(btns)

    def _on_ok(self):
        text = (self.ids_edit.text() or "").strip()
        mode, value = _parse_add_market_input(text)
        if not value:
            QMessageBox.warning(self, "Ошибка", "Введите ссылку или Market ID")
            return
        if mode == "ids":
            ids = [x.strip() for x in value.split(",") if x.strip()]
            self.main_win._add_markets(ids)
            self.accept()
            return
        self.status_label.setText("Загрузка категории...")
        self.ids_edit.setEnabled(False)
        ok_btn = self.findChild(QPushButton)  # first button
        for w in self.findChildren(QPushButton):
            if w.text() == "Загрузить":
                w.setEnabled(False)
                break
        self.main_win._fetch_category_and_show(slug=value, add_dialog=self)

    def reset_for_retry(self):
        self.status_label.setText("")
        self.ids_edit.setEnabled(True)
        for w in self.findChildren(QPushButton):
            if w.text() == "Загрузить":
                w.setEnabled(True)
                break


def _sensitive_field(parent, label_text: str, placeholder: str, password: bool = True) -> tuple:
    """Создаёт поле с лейблом и кнопкой-глазиком для reveal/hide. Возвращает (edit, container)."""
    row = QWidget(parent)
    row_lo = QVBoxLayout(row)
    row_lo.setContentsMargins(0, 0, 0, 4)
    lbl = QLabel(label_text)
    lbl.setStyleSheet("color: #8e8e93; font-size: 11px;")
    row_lo.addWidget(lbl)
    edit = QLineEdit()
    edit.setPlaceholderText(placeholder)
    if password:
        edit.setEchoMode(QLineEdit.EchoMode.Password)
    h = QHBoxLayout()
    h.addWidget(edit)
    if password:
        eye_btn = QToolButton()
        eye_btn.setFixedSize(32, 32)
        eye_btn.setText("👁")
        eye_btn.setToolTip("Показать")
        eye_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        eye_btn.setStyleSheet("QToolButton { background: transparent; border: none; font-size: 16px; } QToolButton:hover { background: #3a3a3e; border-radius: 4px; }")
        def _toggle():
            if edit.echoMode() == QLineEdit.EchoMode.Password:
                edit.setEchoMode(QLineEdit.EchoMode.Normal)
                eye_btn.setText("🔒")
                eye_btn.setToolTip("Скрыть")
            else:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
                eye_btn.setText("👁")
                eye_btn.setToolTip("Показать")
        eye_btn.clicked.connect(_toggle)
        h.addWidget(eye_btn)
    row_lo.addLayout(h)
    return edit, row


class SettingsDialog(QDialog):
    """Диалог настроек — API, прокси, Telegram и т.п."""

    def __init__(self, parent: "MainWindow"):
        super().__init__(parent)
        self.main_win = parent
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)

        acc_group = QGroupBox("🔑 API ключ, адрес, приватный ключ, прокси")
        acc_layout = QVBoxLayout(acc_group)
        self.api_key_edit, api_row = _sensitive_field(self, "API Key", "API key", password=True)
        self.address_edit = QLineEdit()
        self.address_edit.setPlaceholderText("0x... Predict account address")
        self.privy_key_edit, privy_row = _sensitive_field(self, "Privy wallet private key", "Privy wallet private key", password=True)
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("http://user:pass@host:port или host:port")
        acc_layout.addWidget(api_row)
        addr_lbl = QLabel("Predict account address")
        addr_lbl.setStyleSheet("color: #8e8e93; font-size: 11px;")
        acc_layout.addWidget(addr_lbl)
        acc_layout.addWidget(self.address_edit)
        acc_layout.addWidget(privy_row)
        proxy_lbl = QLabel("Прокси")
        proxy_lbl.setStyleSheet("color: #8e8e93; font-size: 11px;")
        acc_layout.addWidget(proxy_lbl)
        proxy_row = QHBoxLayout()
        proxy_row.addWidget(self.proxy_edit)
        self.proxy_test_btn = QToolButton()
        self.proxy_test_btn.setFixedSize(32, 32)
        self.proxy_test_btn.setText("🔍")
        self.proxy_test_btn.setToolTip("Проверить прокси")
        self.proxy_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.proxy_test_btn.setStyleSheet("QToolButton { background: transparent; border: none; font-size: 16px; } QToolButton:hover { background: #3a3a3e; border-radius: 4px; } QToolButton:disabled { opacity: 0.5; }")
        self.proxy_test_btn.clicked.connect(self._test_proxy)
        proxy_row.addWidget(self.proxy_test_btn)
        acc_layout.addLayout(proxy_row)
        self._proxy_worker = None
        self._telegram_worker = None
        layout.addWidget(acc_group)

        tg_group = QGroupBox("📱 Telegram")
        tg_layout = QVBoxLayout(tg_group)
        self.telegram_enabled_cb = QCheckBox("Включить уведомления в Telegram")
        self.telegram_enabled_cb.setStyleSheet("color: #e0e0e0; font-size: 13px;")
        self.telegram_enabled_cb.toggled.connect(self._on_telegram_enabled_toggled)
        tg_layout.addWidget(self.telegram_enabled_cb)
        self.tg_fields_widget = QWidget()
        tg_fields_layout = QVBoxLayout(self.tg_fields_widget)
        tg_fields_layout.setContentsMargins(0, 4, 0, 0)
        self.telegram_token_edit, tg_token_row = _sensitive_field(self.tg_fields_widget, "Telegram Bot Token", "Bot token", password=True)
        self.telegram_chat_id_edit = QLineEdit()
        self.telegram_chat_id_edit.setPlaceholderText("Chat ID")
        chat_lbl = QLabel("Telegram Chat ID")
        chat_lbl.setStyleSheet("color: #8e8e93; font-size: 11px;")
        interval_lbl = QLabel("Интервал статус-репорта (минут)")
        interval_lbl.setStyleSheet("color: #8e8e93; font-size: 11px;")
        self.telegram_interval_edit = QLineEdit()
        self.telegram_interval_edit.setPlaceholderText("60 = каждый час")
        self.telegram_interval_edit.setValidator(QDoubleValidator(1, 1440, 0))
        self.telegram_test_btn = QPushButton("Тест уведомление")
        self.telegram_test_btn.setObjectName("secondary")
        self.telegram_test_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.telegram_test_btn.clicked.connect(self._test_telegram)
        tg_fields_layout.addWidget(tg_token_row)
        tg_fields_layout.addWidget(chat_lbl)
        tg_fields_layout.addWidget(self.telegram_chat_id_edit)
        tg_fields_layout.addWidget(interval_lbl)
        tg_fields_layout.addWidget(self.telegram_interval_edit)
        tg_fields_layout.addWidget(self.telegram_test_btn)
        tg_layout.addWidget(self.tg_fields_widget)
        layout.addWidget(tg_group)

        log_group = QGroupBox("📋 Логирование")
        log_layout = QVBoxLayout(log_group)
        log_hint = QLabel("Сохранять в файл:")
        log_hint.setStyleSheet("color: #8e8e93; font-size: 11px;")
        log_layout.addWidget(log_hint)
        self.log_software_cb = QCheckBox("Логи софта (session_*.log)")
        self.log_software_cb.setStyleSheet("color: #e0e0e0;")
        self.log_orderbook_cb = QCheckBox("Логи стакана (market/*.txt)")
        self.log_orderbook_cb.setStyleSheet("color: #e0e0e0;")
        self.log_orders_cb = QCheckBox("Логи наших ордеров (market/*_orders.txt)")
        self.log_orders_cb.setStyleSheet("color: #e0e0e0;")
        log_layout.addWidget(self.log_software_cb)
        log_layout.addWidget(self.log_orderbook_cb)
        log_layout.addWidget(self.log_orders_cb)
        layout.addWidget(log_group)

        ie_group = QGroupBox("📤 Импорт и Экспорт")
        ie_layout = QVBoxLayout(ie_group)
        ie_hint = QLabel("Обмен рынками и настройками с другими пользователями")
        ie_hint.setStyleSheet("color: #8e8e93; font-size: 11px;")
        ie_hint.setWordWrap(True)
        ie_layout.addWidget(ie_hint)
        ie_btns = QHBoxLayout()
        self.export_btn = QPushButton("Экспорт")
        self.export_btn.setObjectName("secondary")
        self.export_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_btn.clicked.connect(self._do_export)
        self.import_btn = QPushButton("Импорт")
        self.import_btn.setObjectName("secondary")
        self.import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_btn.clicked.connect(self._do_import)
        ie_btns.addWidget(self.export_btn)
        ie_btns.addWidget(self.import_btn)
        ie_layout.addLayout(ie_btns)
        layout.addWidget(ie_group)

        del_group = QGroupBox("🗑 Удалить все рынки")
        del_layout = QVBoxLayout(del_group)
        del_hint = QLabel("При повторном добавлении рынка — старые настройки применятся, если не удалить с настройками.")
        del_hint.setStyleSheet("color: #8e8e93; font-size: 11px;")
        del_hint.setWordWrap(True)
        del_layout.addWidget(del_hint)
        del_btns = QHBoxLayout()
        self.del_markets_btn = QPushButton("Удалить все рынки")
        self.del_markets_btn.setObjectName("secondary")
        self.del_markets_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.del_markets_btn.clicked.connect(self._do_delete_all_markets)
        self.del_markets_with_settings_btn = QPushButton("Удалить все рынки с настройками")
        self.del_markets_with_settings_btn.setObjectName("secondary")
        self.del_markets_with_settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.del_markets_with_settings_btn.clicked.connect(lambda: self._do_delete_all_markets(with_settings=True))
        del_btns.addWidget(self.del_markets_btn)
        del_btns.addWidget(self.del_markets_with_settings_btn)
        del_layout.addLayout(del_btns)
        layout.addWidget(del_group)

        faq_btn = QPushButton("FAQ")
        faq_btn.setObjectName("secondary")
        faq_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        faq_btn.setToolTip("Описание софта и всех настроек")
        faq_btn.clicked.connect(lambda: _show_faq(self))
        layout.addWidget(faq_btn)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setObjectName("primary")
        cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setObjectName("secondary")
        layout.addWidget(btns)
        self._load()

    def _test_proxy(self):
        if self._proxy_worker and self._proxy_worker.isRunning():
            return
        self.proxy_test_btn.setEnabled(False)
        self.proxy_test_btn.setText("…")
        self._proxy_worker = ProxyTestWorker(self.proxy_edit.text(), self)
        self._proxy_worker.finished.connect(self._on_proxy_test_done)
        self._proxy_worker.result.connect(self._on_proxy_test_result)
        self._proxy_worker.start()

    def _on_proxy_test_done(self):
        self.proxy_test_btn.setEnabled(True)
        self.proxy_test_btn.setText("🔍")

    def _on_proxy_test_result(self, ok: bool, msg: str):
        if ok:
            QMessageBox.information(self, "Прокси", f"✓ {msg}")
        else:
            QMessageBox.warning(self, "Прокси", f"✗ {msg}")

    def _on_telegram_enabled_toggled(self, checked: bool):
        self.tg_fields_widget.setEnabled(checked)

    def _test_telegram(self):
        if self._telegram_worker and self._telegram_worker.isRunning():
            return
        self.telegram_test_btn.setEnabled(False)
        self.telegram_test_btn.setText("Отправка…")
        self._telegram_worker = TelegramTestWorker(
            self.telegram_token_edit.text(),
            self.telegram_chat_id_edit.text(),
            self,
        )
        self._telegram_worker.finished.connect(self._on_telegram_test_done)
        self._telegram_worker.result.connect(self._on_telegram_test_result)
        self._telegram_worker.start()

    def _on_telegram_test_done(self):
        if self.telegram_enabled_cb.isChecked():
            self.telegram_test_btn.setEnabled(True)
        self.telegram_test_btn.setText("Тест уведомление")

    def _on_telegram_test_result(self, ok: bool, msg: str):
        if ok:
            QMessageBox.information(self, "Telegram", f"✓ {msg}")
        else:
            QMessageBox.warning(self, "Telegram", f"✗ {msg}")

    def _do_export(self):
        mw = self.main_win
        markets = list(mw.cards.keys()) if mw.cards else list(mw.settings_manager.settings.keys())
        if not markets:
            try:
                with open(LAST_MARKETS_FILE, "r", encoding="utf-8") as f:
                    markets = [x.strip() for x in f.read().replace("\n", ",").split(",") if x.strip()]
            except Exception:
                pass
        if not markets:
            _styled_warning(self, "Экспорт", "Нет рынков для экспорта.")
            return
        n = len(markets)
        res = _styled_question(self, "Экспорт",
            f"Экспорт {n} рынков.\n\n"
            "Что сохранить в файл?",
            ("Только список ID (без настроек)", "yes"), ("ID + настройки (токен, лимиты и т.д.)", "no"), ("Отмена", "cancel"))
        if res == "cancel" or res is None:
            return
        include_settings = res == "no"
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт", "markets" + SHARE_FILE_EXT,
            f"Predict Fun Share (*{SHARE_FILE_EXT});;JSON (*.json);;Все файлы (*)",
        )
        if not path:
            return
        if not path.endswith(SHARE_FILE_EXT) and not path.endswith(".json"):
            path += SHARE_FILE_EXT
        data = {"version": 1, "markets": markets}
        if include_settings:
            data["settings"] = {
                mid: mw.settings_manager.settings[mid].to_dict()
                for mid in markets
                if mid in mw.settings_manager.settings
            }
        try:
            import json
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            n = len(markets)
            settings_count = len(data.get("settings") or {})
            if include_settings and settings_count > 0:
                msg = f"Экспортировано {n} рынков и настройки к {settings_count} из них.\n\n{path}"
            else:
                msg = f"Экспортировано {n} рынков.\n\n{path}"
            _styled_info(self, "Экспорт", msg)
        except Exception as e:
            _styled_warning(self, "Ошибка", f"Не удалось сохранить: {e}")

    def _do_delete_all_markets(self, with_settings: bool = False):
        mw = self.main_win
        n = len(mw.cards)
        if n == 0:
            _styled_warning(self, "Удаление", "Нет рынков для удаления.")
            return
        action = "с настройками" if with_settings else ""
        res = _styled_question(self, "Удалить все рынки",
            f"Удалить все {n} рынков{action}?\n\n"
            + ("Настройки будут удалены. При повторном добавлении — дефолтные." if with_settings else "Настройки сохранятся. При повторном добавлении — старые применятся."),
            ("Удалить", "yes"), ("Отмена", "cancel"))
        if res != "yes":
            return
        for mid in list(mw.cards.keys()):
            mw._remove_market(mid)
        if with_settings:
            mw.settings_manager.settings.clear()
            mw.settings_manager.save_settings()
        _styled_info(self, "Удаление", f"Удалено {n} рынков" + (" и настройки" if with_settings else "") + ".")
        self.accept()

    def _do_import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Импорт", "",
            f"Predict Fun Share (*{SHARE_FILE_EXT} *.json);;Все файлы (*)",
        )
        if not path:
            return
        try:
            import json
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            _styled_warning(self, "Ошибка", f"Не удалось прочитать файл: {e}")
            return
        markets = data.get("markets") or []
        settings_data = data.get("settings") or {}
        if not markets:
            _styled_warning(self, "Импорт", "В файле нет рынков.")
            return
        mw = self.main_win
        existing = set(mw.cards.keys()) | set(mw.settings_manager.settings.keys())
        n_import = len(markets)
        replace_all = _styled_question(self, "Импорт",
            f"В файле {n_import} рынков.\n\n"
            "Удалить все текущие и заменить импортом?\n"
            "(Да — очистить и добавить только из файла)\n"
            "(Нет — добавить новые, для уже есть — спросим)",
            ("Удалить всё и заменить", "yes"), ("Добавить к существующим", "no"), ("Отмена", "cancel"))
        if replace_all == "cancel" or replace_all is None:
            return
        if replace_all == "yes":
            for mid in list(mw.cards.keys()):
                mw._remove_market(mid)
            mw.settings_manager.settings.clear()
            for mid in markets:
                s = settings_data.get(mid)
                if s:
                    mw.settings_manager.settings[mid] = TokenSettings.from_dict({**s, "market_id": mid})
                else:
                    mw.settings_manager.settings[mid] = TokenSettings(market_id=mid)
            mw.settings_manager.save_settings()
            try:
                with open(LAST_MARKETS_FILE, "w", encoding="utf-8") as f:
                    f.write(",".join(markets))
            except Exception:
                pass
            if mw.api_client:
                mw._add_markets(markets)
            else:
                _styled_info(self, "Импорт", f"Импортировано {len(markets)} рынков. Подключитесь для загрузки.")
            self.accept()
            return
        dupes = [m for m in markets if m in existing]
        replace_dupes = False
        if dupes:
            replace_dupes = _styled_question(self, "Импорт",
                f"Есть {len(dupes)} рынков, которые уже у вас добавлены.\n\n"
                "Чьи настройки использовать для них?",
                ("Из файла (импорт)", "yes"), ("Мои текущие (оставить)", "no")) == "yes"
        new_ids = [m for m in markets if m not in existing]
        for mid in dupes:
            if replace_dupes and mid in settings_data:
                mw.settings_manager.settings[mid] = TokenSettings.from_dict({**settings_data[mid], "market_id": mid})
            elif mid not in mw.settings_manager.settings and mid in settings_data:
                mw.settings_manager.settings[mid] = TokenSettings.from_dict({**settings_data[mid], "market_id": mid})
        for mid in new_ids:
            if mid in settings_data:
                mw.settings_manager.settings[mid] = TokenSettings.from_dict({**settings_data[mid], "market_id": mid})
            else:
                mw.settings_manager.settings[mid] = TokenSettings(market_id=mid)
        mw.settings_manager.save_settings()
        all_ids = list(set(mw.cards.keys()) | set(new_ids))
        try:
            with open(LAST_MARKETS_FILE, "w", encoding="utf-8") as f:
                f.write(",".join(all_ids))
        except Exception:
            pass
        if new_ids and mw.api_client:
            mw._add_markets(new_ids)
        msg = f"Импорт: {len(new_ids)} новых, {len(dupes)} существующих"
        if replace_dupes:
            msg += " (настройки заменены)"
        _styled_info(self, "Импорт", msg)
        if new_ids and mw.api_client:
            self.accept()
        elif not new_ids:
            self.accept()

    def _load(self):
        import json
        accounts = load_accounts_from_file()
        if accounts:
            acc = accounts[0]
            self.api_key_edit.setText(acc.get("api_key") or "")
            self.address_edit.setText(acc.get("predict_account_address") or "")
            self.privy_key_edit.setText(acc.get("privy_wallet_private_key") or "")
            self.proxy_edit.setText(acc.get("proxy") or "")
        try:
            with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.telegram_enabled_cb.setChecked(d.get("telegram_enabled", True))
            self.telegram_token_edit.setText((d.get("telegram_token") or "").strip())
            self.telegram_chat_id_edit.setText((d.get("telegram_chat_id") or "").strip())
            interval = d.get("telegram_status_interval_minutes", 60)
            self.telegram_interval_edit.setText(str(int(interval) if interval else 60))
            self.log_software_cb.setChecked(d.get("log_software", True))
            self.log_orderbook_cb.setChecked(d.get("log_orderbook", True))
            self.log_orders_cb.setChecked(d.get("log_orders", True))
        except Exception:
            self.telegram_enabled_cb.setChecked(True)
            self.telegram_token_edit.setText("")
            self.telegram_chat_id_edit.setText("")
            self.telegram_interval_edit.setText("60")
            self.log_software_cb.setChecked(True)
            self.log_orderbook_cb.setChecked(True)
            self.log_orders_cb.setChecked(True)
        self._on_telegram_enabled_toggled(self.telegram_enabled_cb.isChecked())

    def _save(self):
        import json
        api_key = (self.api_key_edit.text() or "").strip()
        address = (self.address_edit.text() or "").strip()
        privy_key = (self.privy_key_edit.text() or "").strip()
        proxy = (self.proxy_edit.text() or "").strip()
        if not address or not address.startswith("0x"):
            QMessageBox.warning(self, "Ошибка", "Укажите корректный Predict account address (0x...).")
            return
        accounts = load_accounts_from_file()
        if not accounts:
            accounts = [{}]
        accounts[0] = {
            "api_key": api_key,
            "predict_account_address": address,
            "privy_wallet_private_key": privy_key,
            "proxy": proxy or None,
        }
        save_accounts_to_file(accounts)
        data = {}
        try:
            with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
        data["telegram_enabled"] = self.telegram_enabled_cb.isChecked()
        data["telegram_token"] = (self.telegram_token_edit.text() or "").strip()
        data["telegram_chat_id"] = (self.telegram_chat_id_edit.text() or "").strip()
        try:
            mins = int(float((self.telegram_interval_edit.text() or "60").strip().replace(",", ".")))
            data["telegram_status_interval_minutes"] = max(1, min(1440, mins))
        except (ValueError, TypeError):
            data["telegram_status_interval_minutes"] = 60
        data["log_software"] = self.log_software_cb.isChecked()
        data["log_orderbook"] = self.log_orderbook_cb.isChecked()
        data["log_orders"] = self.log_orders_cb.isChecked()
        try:
            with open(APP_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", f"Не удалось сохранить app_state: {e}")
            return
        QMessageBox.information(self, "Настройки", "Настройки сохранены. Переподключитесь, чтобы применить изменения аккаунта.")
        if self.main_win._status_report_timer.isActive():
            interval_sec = get_telegram_status_interval_sec()
            self.main_win._status_report_timer.setInterval(interval_sec * 1000)
            self.main_win._status_report_timer.start()
        self.accept()


class GlobalSettingsDialog(QDialog):
    """Диалог общих настроек — выборочное применение ко всем рынкам."""

    def __init__(self, parent: "MainWindow"):
        super().__init__(parent)
        self.main_win = parent
        self.setWindowTitle("Общие настройки")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)

        hint = QLabel("Отметьте параметры, которые нужно применить ко всем рынкам:")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888; margin-bottom: 4px;")
        layout.addWidget(hint)

        select_btns = QHBoxLayout()
        select_all_btn = QPushButton("Выбрать все")
        select_all_btn.setObjectName("secondary")
        select_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        select_all_btn.setStyleSheet("QPushButton { font-size: 12px; padding: 12px 16px; border: 1px solid #3a3a3e; border-radius: 6px; }"
            " QPushButton:hover { border-color: #F7931A; }")
        deselect_all_btn = QPushButton("Убрать все")
        deselect_all_btn.setObjectName("secondary")
        deselect_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        deselect_all_btn.setStyleSheet("QPushButton { font-size: 12px; padding: 12px 16px; border: 1px solid #3a3a3e; border-radius: 6px; }"
            " QPushButton:hover { border-color: #F7931A; }")
        select_btns.addWidget(select_all_btn)
        select_btns.addWidget(deselect_all_btn)
        select_btns.addStretch()
        layout.addLayout(select_btns)

        def _num_edit(value, min_v, max_v, decimals=2):
            e = QLineEdit()
            e.setValidator(QDoubleValidator(min_v, max_v, decimals))
            e.setText(str(value))
            e.setPlaceholderText(f"{min_v}-{max_v}")
            return e

        def _setting_row(cb_label, input_widgets):
            cb = QCheckBox(cb_label)
            cb.setChecked(False)
            row = QHBoxLayout()
            row.addWidget(cb)
            for w in input_widgets:
                row.addWidget(w)
            def _toggle(on):
                for w in input_widgets:
                    w.setEnabled(on)
            cb.toggled.connect(_toggle)
            _toggle(False)
            return cb, row

        self.pos_type_combo = QComboBox()
        self.pos_type_combo.addItems(["usdt", "shares"])
        self.pos_type_combo.setCurrentText("usdt")
        self.pos_edit = _num_edit(DEFAULT_POSITION_SIZE_USDT or 100, 0.1, 100000, 1)
        self.cb_position, row = _setting_row("Размер позиции:", [self.pos_type_combo, self.pos_edit])
        layout.addLayout(row)

        self.liquidity_mode_combo = QComboBox()
        self.liquidity_mode_combo.addItems(["По BID", "По ASK"])
        self.liquidity_mode_combo.setCurrentIndex(0 if (DEFAULT_LIQUIDITY_MODE or "bid") == "bid" else 1)
        self.cb_liq_mode, row = _setting_row("Режим ликвидности:", [self.liquidity_mode_combo])
        layout.addLayout(row)

        self.min_spread_edit = _num_edit(DEFAULT_MIN_SPREAD or 0.2, 0, 100, 2)
        self.cb_min_spread, row = _setting_row("Мин. спред (¢):", [self.min_spread_edit])
        layout.addLayout(row)

        auto_group = QGroupBox("Автоспред")
        auto_layout = QVBoxLayout(auto_group)
        self.target_liq_edit = _num_edit(DEFAULT_TARGET_LIQUIDITY or 1000, 1, 1000000, 0)
        self.cb_target_liq, row_tl = _setting_row("Целевая ликвидность ($):", [self.target_liq_edit])
        auto_layout.addLayout(row_tl)
        self.max_spread_edit = _num_edit(DEFAULT_MAX_AUTO_SPREAD or 6, 0.1, 100, 2)
        self.cb_max_spread, row_ms = _setting_row("Макс. спред (¢):", [self.max_spread_edit])
        auto_layout.addLayout(row_ms)
        layout.addWidget(auto_group)

        vol_group = QGroupBox("Защита от волатильности")
        vol_layout = QVBoxLayout(vol_group)
        self.volatile_limit_edit = _num_edit(DEFAULT_VOLATILE_REPOSITION_LIMIT or 0, 0, 100, 0)
        self.cb_volatile_limit, row_vl = _setting_row("Лимит переставлений (0 = выкл):", [self.volatile_limit_edit])
        vol_layout.addLayout(row_vl)
        self.volatile_window_edit = _num_edit(DEFAULT_VOLATILE_WINDOW_SECONDS or 60, 1, 86400, 0)
        self.cb_volatile_window, row_vw = _setting_row("Окно (сек):", [self.volatile_window_edit])
        vol_layout.addLayout(row_vw)
        self.volatile_cooldown_edit = _num_edit(DEFAULT_VOLATILE_COOLDOWN_SECONDS or 3600, 0, 86400 * 7, 0)
        self.cb_volatile_cooldown, row_vc = _setting_row("Пауза (сек):", [self.volatile_cooldown_edit])
        vol_layout.addLayout(row_vc)
        layout.addWidget(vol_group)

        self._all_checkboxes = [
            self.cb_position, self.cb_liq_mode, self.cb_min_spread,
            self.cb_target_liq, self.cb_max_spread,
            self.cb_volatile_limit, self.cb_volatile_window, self.cb_volatile_cooldown,
        ]
        select_all_btn.clicked.connect(self._select_all_checkboxes)
        deselect_all_btn.clicked.connect(self._deselect_all_checkboxes)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._apply)
        btns.rejected.connect(self.reject)
        ok_btn = btns.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setObjectName("primary")
        cancel_btn = btns.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_btn.setObjectName("secondary")
        layout.addWidget(btns)

    def _select_all_checkboxes(self):
        for cb in self._all_checkboxes:
            cb.setChecked(True)

    def _deselect_all_checkboxes(self):
        for cb in self._all_checkboxes:
            cb.setChecked(False)

    def _parse(self, edit, default):
        try:
            v = float((edit.text() or "").strip().replace(",", "."))
            return v if v >= 0 else default
        except (ValueError, TypeError):
            return default

    def _apply(self):
        any_checked = any([
            self.cb_position.isChecked(), self.cb_liq_mode.isChecked(),
            self.cb_min_spread.isChecked(), self.cb_target_liq.isChecked(),
            self.cb_max_spread.isChecked(), self.cb_volatile_limit.isChecked(),
            self.cb_volatile_window.isChecked(), self.cb_volatile_cooldown.isChecked(),
        ])
        if not any_checked:
            QMessageBox.warning(self, "Общие настройки", "Не выбрано ни одного параметра для применения.")
            return

        val = self._parse(self.pos_edit, 100)
        use_usdt = self.pos_type_combo.currentText() == "usdt"
        min_spread = self._parse(self.min_spread_edit, 0.2)
        target_liq = self._parse(self.target_liq_edit, 1000)
        max_spread = self._parse(self.max_spread_edit, 6)
        liq_mode = "ask" if self.liquidity_mode_combo.currentIndex() == 1 else "bid"
        vol_limit = int(self._parse(self.volatile_limit_edit, 0))
        vol_window = self._parse(self.volatile_window_edit, 60)
        vol_cooldown = self._parse(self.volatile_cooldown_edit, 3600)

        mgr = self.main_win.settings_manager
        for mid in list(self.main_win.cards.keys()):
            kwargs = {}
            if self.cb_position.isChecked():
                kwargs["position_size_usdt"] = val if use_usdt else None
                kwargs["position_size_shares"] = val if not use_usdt else None
            if self.cb_liq_mode.isChecked():
                kwargs["liquidity_mode"] = liq_mode
            if self.cb_min_spread.isChecked():
                kwargs["min_spread"] = min_spread
            if self.cb_target_liq.isChecked():
                kwargs["target_liquidity"] = target_liq
            if self.cb_max_spread.isChecked():
                kwargs["max_auto_spread"] = max_spread
            if self.cb_volatile_limit.isChecked():
                kwargs["volatile_reposition_limit"] = vol_limit
            if self.cb_volatile_window.isChecked():
                kwargs["volatile_window_seconds"] = vol_window
            if self.cb_volatile_cooldown.isChecked():
                kwargs["volatile_cooldown_seconds"] = vol_cooldown
            if kwargs:
                mgr.update_settings(mid, **kwargs)

        for mid, card in self.main_win.cards.items():
            card.settings = mgr.get_settings(mid)
            if self.cb_position.isChecked():
                card.pos_type_combo.setCurrentText("usdt" if use_usdt else "shares")
                card.pos_edit.setText(str(val))
            if self.cb_min_spread.isChecked():
                card.min_spread_edit.setText(str(min_spread))
            if self.cb_target_liq.isChecked():
                card.target_liq_edit.setText(str(target_liq))
            if self.cb_max_spread.isChecked():
                card.max_spread_edit.setText(str(max_spread))
            if self.cb_volatile_limit.isChecked():
                card.volatile_limit_edit.setText(str(vol_limit))
            if self.cb_volatile_window.isChecked():
                card.volatile_window_edit.setText(str(vol_window))
            if self.cb_volatile_cooldown.isChecked():
                card.volatile_cooldown_edit.setText(str(vol_cooldown))
            if self.cb_liq_mode.isChecked():
                card.liquidity_mode_combo.setCurrentIndex(1 if liq_mode == "ask" else 0)
            card._recalc_preview()

        self.accept()


class ScrollWithLogOverlay(QWidget):
    LOG_HEIGHT = 120

    def __init__(self, parent=None):
        super().__init__(parent)
        self._log_expanded = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; }")
        layout.addWidget(self.scroll)
        self.log_drawer = QFrame(self)
        self.log_drawer.setStyleSheet("QFrame { background-color: rgba(28, 28, 31, 0.96); border-top: 1px solid #3a3a3e; }")
        log_layout = QVBoxLayout(self.log_drawer)
        log_layout.setContentsMargins(8, 6, 8, 8)
        self.log_close_btn = QPushButton("▼ Свернуть лог")
        self.log_close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_close_btn.setStyleSheet("QPushButton { color: #ffffff; background: transparent; border: none; font-size: 12px; } QPushButton:hover { color: #F7931A; }")
        self.log_close_btn.setMaximumHeight(24)
        self.log_close_btn.clicked.connect(self._collapse_log)
        log_layout.addWidget(self.log_close_btn)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setStyleSheet("background: #1a1a1d; color: #e0e0e0; border: none;")
        log_layout.addWidget(self.log_edit)
        self.log_drawer.setGeometry(0, self.height(), self.width(), self.LOG_HEIGHT + 36)
        self.toggle_btn = QPushButton("▲ Показать лог", self)
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.setStyleSheet("QPushButton { color: #ffffff; background: transparent; border: none; font-size: 12px; } QPushButton:hover { color: #F7931A; }")
        self.toggle_btn.clicked.connect(self._expand_log)
        self.toggle_btn.raise_()
        self._anim = QPropertyAnimation(self.log_drawer, b"geometry")
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.finished.connect(self._on_anim_finished)

    def _on_anim_finished(self):
        if not self._log_expanded:
            self.toggle_btn.setVisible(True)
            self.toggle_btn.raise_()

    def _expand_log(self):
        if self._log_expanded:
            return
        self._log_expanded = True
        self.toggle_btn.setVisible(False)
        w, h = self.width(), self.height()
        self._anim.setStartValue(self.log_drawer.geometry())
        self._anim.setEndValue(QRect(0, h - self.LOG_HEIGHT - 36, w, self.LOG_HEIGHT + 36))
        self._anim.start()

    def _collapse_log(self):
        if not self._log_expanded:
            return
        self._log_expanded = False
        w, h = self.width(), self.height()
        self._anim.setStartValue(self.log_drawer.geometry())
        self._anim.setEndValue(QRect(0, h, w, self.LOG_HEIGHT + 36))
        self._anim.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        w, h = self.width(), self.height()
        self.toggle_btn.setGeometry(w - 130, h - 28, 120, 24)
        if self._log_expanded:
            self.log_drawer.setGeometry(0, h - self.LOG_HEIGHT - 36, w, self.LOG_HEIGHT + 36)
        else:
            self.log_drawer.setGeometry(0, h, w, self.LOG_HEIGHT + 36)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Predict Fun — Ликвидность")
        self.setGeometry(80, 60, 1480, 900)
        self.setMinimumSize(1100, 700)
        self.setStyleSheet(BNB_STYLE)
        self.settings_manager = SettingsManager()
        self.worker = AsyncWorker()
        self.worker.start()
        self.jwt_token = None
        self.api_client: Optional[APIClient] = None
        self.executor: Optional[Executor] = None
        self.current_account: Optional[dict] = None
        self.account_info: Dict[str, dict] = {}
        self.markets: Dict[str, dict] = {}
        self.cards: Dict[str, MarketCard] = {}
        self.market_modules: Dict[str, MarketModule] = {}
        self.inspector: Optional[Inspector] = None
        self.inspector_orders_count: Optional[int] = None
        self.inspector_orders_updated_at: Optional[float] = None
        self._inspector_start_timer = QTimer(self)
        self._inspector_start_timer.setSingleShot(True)
        self._inspector_start_timer.timeout.connect(self._start_inspector)
        self._status_report_timer = QTimer(self)
        self._status_report_timer.timeout.connect(self._send_status_report)
        self._start_time = time.time()
        self.ws_client: Optional[WebSocketClient] = None
        self._volatile_state: Dict[str, dict] = {}
        self._ws_connected = False
        self._balance_update_time: Optional[float] = None
        self._ws_update_time: Optional[float] = None

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(16)
        layout.setContentsMargins(24, 24, 24, 24)

        header = QHBoxLayout()
        header.setSpacing(16)
        self.connect_btn = QPushButton("Подключить")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.connect_btn.setFixedHeight(40)
        self.connect_btn.clicked.connect(self._connect)
        self.rudy_btn_wrapper = RotatingGradientBorderFrame(self.connect_btn, border_width=3)
        self.rudy_btn_wrapper.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        header.addWidget(self.rudy_btn_wrapper)
        self.account_ws_frame = QFrame()
        self.account_ws_frame.setStyleSheet("background-color: #2c2c30; border-radius: 6px; padding: 6px 10px;")
        self.account_ws_frame.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        self.account_ws_frame.setEnabled(False)
        aw_layout = QVBoxLayout(self.account_ws_frame)
        aw_layout.setContentsMargins(0, 0, 0, 0)
        aw_layout.setSpacing(0)
        self.account_ws_label = QLabel("")
        self.account_ws_label.setStyleSheet("color: #e0e0e0; font-size: 12px; line-height: 1.0;")
        self.account_ws_label.setTextFormat(Qt.TextFormat.RichText)
        aw_layout.addWidget(self.account_ws_label)
        header.addWidget(self.account_ws_frame)
        self.orders_frame = QFrame()
        self.orders_frame.setStyleSheet("background-color: #2c2c30; border-radius: 4px; padding: 2px 8px;")
        self.orders_frame.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.orders_frame.setMinimumWidth(140)
        self.orders_frame.setMaximumHeight(64)
        self.orders_frame.setEnabled(False)
        orders_layout = QVBoxLayout(self.orders_frame)
        orders_layout.setContentsMargins(0, 0, 0, 0)
        self.orders_count_label = QLabel("Можно выставить: 0\nВыставлено: 0\nAPI: —")
        self.orders_count_label.setStyleSheet("color: #8e8e93; font-size: 11px; line-height: 1.0;")
        orders_layout.addWidget(self.orders_count_label)
        header.addWidget(self.orders_frame)

        self.add_market_btn = QPushButton("+ Добавить рынок")
        self.add_market_btn.setObjectName("outline")
        self.add_market_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_market_btn.clicked.connect(self._open_add_market_dialog)
        self.add_market_btn.setEnabled(False)
        header.addWidget(self.add_market_btn)

        self.inspector_checkbox = QCheckBox("Inspector")
        self.inspector_checkbox.setStyleSheet("color: #e0e0e0; font-size: 12px;")
        self.inspector_checkbox.setChecked(self._load_inspector_state())
        self.inspector_checkbox.stateChanged.connect(self._on_inspector_toggle)
        header.addWidget(self.inspector_checkbox)

        header.addStretch()
        self.place_all_btn = QPushButton("Выставить")
        self.place_all_btn.setObjectName("primary")
        self.place_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.place_all_btn.clicked.connect(self._place_all)
        self.place_all_btn.setEnabled(False)
        header.addWidget(self.place_all_btn)
        self.cancel_all_btn = QPushButton("Убрать")
        self.cancel_all_btn.setObjectName("danger")
        self.cancel_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_all_btn.clicked.connect(self._cancel_all)
        self.cancel_all_btn.setEnabled(False)
        header.addWidget(self.cancel_all_btn)
        self.global_settings_btn = QPushButton("📋 Общие")
        self.global_settings_btn.setObjectName("secondary")
        self.global_settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.global_settings_btn.clicked.connect(self._open_global_settings)
        self.global_settings_btn.setEnabled(False)
        header.addWidget(self.global_settings_btn)
        self.settings_btn = QPushButton("⚙️ Настройки")
        self.settings_btn.setObjectName("secondary")
        self.settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_btn.clicked.connect(self._open_settings)
        self.settings_btn_wrapper = RotatingGradientBorderFrame(
            self.settings_btn, border_width=3, gradient_color=QColor(247, 147, 26)
        )
        self.settings_btn_wrapper.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Maximum)
        header.addWidget(self.settings_btn_wrapper)
        layout.addLayout(header)
        self._update_connect_button_state()

        self.status_label = QLabel("Подключитесь — рынки загрузятся автоматически")
        self.status_label.setStyleSheet("color: #8e8e93; font-size: 12px;")
        layout.addWidget(self.status_label)
        self.search_frame = QFrame()
        self.search_frame.setEnabled(False)
        search_layout = QHBoxLayout(self.search_frame)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.addWidget(QLabel("Поиск:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("По названию, вопросу, slug, ID...")
        self.search_edit.textChanged.connect(self._filter_cards)
        search_layout.addWidget(self.search_edit)
        self._sort_labels = ["Сортировка: Новые", "Сортировка: По ID", "Сортировка: По названию"]
        self._sort_mode = self._load_sort_mode()
        self.sort_btn = QPushButton(self._sort_labels[self._sort_mode])
        self.sort_btn.setObjectName("secondary")
        self.sort_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.sort_btn.setFixedWidth(230)
        self.sort_btn.clicked.connect(self._on_sort_clicked)
        search_layout.addWidget(self.sort_btn)
        layout.addWidget(self.search_frame)

        self.scroll_with_log = ScrollWithLogOverlay()
        self.cards_container = QWidget()
        self.cards_container.setStyleSheet("background: transparent;")
        self.cards_layout = QGridLayout(self.cards_container)
        self.cards_layout.setSpacing(4)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_with_log.scroll.setWidget(self.cards_container)
        layout.addWidget(self.scroll_with_log, 1)
        self.log_edit = self.scroll_with_log.log_edit

        self.worker.log_signal.connect(self._log)
        self.worker.connect_done.connect(self._on_connect_done)
        self.worker.markets_loaded.connect(self._on_markets_loaded)
        self.worker.market_load_progress.connect(self._on_load_progress)
        self.worker.market_display_update.connect(self._on_market_display)
        self.worker.place_done.connect(self._on_place_done)
        self.worker.cancel_done.connect(self._on_cancel_done)
        self.worker.balance_updated_signal.connect(self._on_balance_updated)
        self.worker.ws_status_signal.connect(self._on_ws_status)
        self.worker.ws_last_update_signal.connect(self._on_ws_last_update)
        self.worker.inspector_orders_count_signal.connect(self._on_inspector_orders_count)
        self.worker.category_fetched.connect(self._on_category_fetched)
        self._pending_add_dialog = None

    def _fetch_category_and_show(self, slug: str, add_dialog: "AddMarketDialog"):
        self._pending_add_dialog = add_dialog
        self.worker.do_fetch_category(slug, self.api_client, lambda m: self.worker.log_signal.emit(m))

    def _on_category_fetched(self, slug: str, category_data: Optional[dict]):
        dlg = self._pending_add_dialog
        self._pending_add_dialog = None
        if category_data is None:
            if dlg:
                dlg.reset_for_retry()
            QMessageBox.warning(self, "Ошибка", f"Не удалось загрузить категорию: {slug}")
            return
        markets = category_data.get("markets") or []
        STATUS_REGISTERED = "REGISTERED"
        valid = [str(m["id"]) for m in markets if (m.get("status") or "").strip().upper() == STATUS_REGISTERED]
        if not valid:
            if dlg:
                dlg.reset_for_retry()
            QMessageBox.warning(self, "Ошибка", "В категории нет рынков со статусом REGISTERED")
            return
        if dlg:
            dlg.accept()
        sel_dlg = CategorySelectionDialog(category_data, self)
        sel_dlg.exec()

    def _has_valid_account_data(self) -> bool:
        """Есть ли заполненные данные аккаунта: api_key, адрес, приватный ключ. Прокси опционален."""
        accounts = load_accounts_from_file()
        if not accounts:
            return False
        acc = accounts[0]
        api_key = (acc.get("api_key") or "").strip()
        address = (acc.get("predict_account_address") or "").strip()
        privy_key = (acc.get("privy_wallet_private_key") or "").strip()
        return bool(api_key and address.startswith("0x") and privy_key)

    def _update_connect_button_state(self) -> None:
        """Активна кнопка Подключить только при заполненных данных аккаунта."""
        if self.jwt_token is not None:
            self._stop_settings_pulse()
            return
        enabled = self._has_valid_account_data()
        self.connect_btn.setEnabled(enabled)
        if enabled:
            self._stop_settings_pulse()
        else:
            self._start_settings_pulse()

    def _start_settings_pulse(self) -> None:
        self.settings_btn_wrapper.set_gradient_visible(True)

    def _stop_settings_pulse(self) -> None:
        self.settings_btn_wrapper.set_gradient_visible(False)

    def _switch_to_rudy_button(self) -> None:
        """Заменяет кнопку Подключено на Rudy vs Web3 с Telegram-стилистикой и вращающимся градиентным бордером."""
        try:
            self.connect_btn.clicked.disconnect(self._connect)
        except TypeError:
            pass
        self.connect_btn.setText("Rudy vs Web3")
        self.connect_btn.setObjectName("telegram")
        self.connect_btn.setEnabled(True)
        self.connect_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://t.me/rudy_web3")))
        self.connect_btn.setStyleSheet(
            "QPushButton#telegram {"
            "background-color: #0088cc; color: #ffffff; border: none; border-radius: 8px; "
            "font-weight: 600; padding: 10px 20px; min-height: 20px;"
            "}"
            "QPushButton#telegram:hover { background-color: #0099dd; }"
            "QPushButton#telegram:pressed { background-color: #0077aa; }"
        )
        self.rudy_btn_wrapper.set_gradient_visible(True)

    def _log(self, msg: str):
        self.log_edit.appendPlainText(f"[{get_timestamp()}] {msg}")
        console_log(msg)
        write_session_log(msg)

    def is_volatile_cooldown(self, market_id: str) -> bool:
        """Рынок в паузе по защите от волатильности?"""
        st = self._volatile_state.get(market_id)
        if not st:
            return False
        until = st.get("cooldown_until")
        if until is None:
            return False
        if time.time() >= until:
            st["cooldown_until"] = None
            return False
        return True

    def _volatile_before_place(self, market_id: str) -> bool:
        """
        Вызвать перед place. Возвращает False если в паузе (place не делать).
        Иначе обновляет окно при необходимости и возвращает True.
        """
        settings = self.settings_manager.get_settings(market_id)
        limit = getattr(settings, "volatile_reposition_limit", 0) or 0
        window_sec = getattr(settings, "volatile_window_seconds", 60) or 0
        if limit <= 0 or window_sec <= 0:
            return True
        now = time.time()
        st = self._volatile_state.setdefault(market_id, {"window_start": None, "reposition_count": 0, "cooldown_until": None})
        if st.get("cooldown_until") is not None and now < st["cooldown_until"]:
            return False
        if st["cooldown_until"] is not None and now >= st["cooldown_until"]:
            st["cooldown_until"] = None
        window_start = st.get("window_start")
        if window_start is None or (now - window_start) > window_sec:
            st["window_start"] = now
            st["reposition_count"] = 0
        return True

    def _volatile_on_cancel_done(self, market_id: str) -> None:
        """Вызвать после успешной отмены: +1 переставление, при достижении лимита — пауза."""
        settings = self.settings_manager.get_settings(market_id)
        limit = getattr(settings, "volatile_reposition_limit", 0) or 0
        window_sec = getattr(settings, "volatile_window_seconds", 60) or 0
        cooldown_sec = getattr(settings, "volatile_cooldown_seconds", 3600) or 0
        if limit <= 0 or window_sec <= 0:
            return
        now = time.time()
        st = self._volatile_state.setdefault(market_id, {"window_start": None, "reposition_count": 0, "cooldown_until": None})
        if st.get("cooldown_until") is not None:
            return
        window_start = st.get("window_start")
        if window_start is None or (now - window_start) > window_sec:
            return
        st["reposition_count"] = st.get("reposition_count", 0) + 1
        title = ""
        card = self.cards.get(market_id)
        if card:
            title = (card.market_info.get("title") or market_id)[:30]
        if st["reposition_count"] >= limit:
            st["cooldown_until"] = now + cooldown_sec
            import datetime
            until_ts = datetime.datetime.fromtimestamp(st["cooldown_until"]).strftime("%H:%M:%S")
            self._log(f"[{market_id} | {title}] Защита от волатильности: {st['reposition_count']} переставлений за окно — пауза до {until_ts}")
        else:
            remaining = max(0, int(window_start + window_sec - now))
            self._log(f"[{market_id} | {title}] Переставление {st['reposition_count']}/{limit}, в цикле осталось {remaining} сек")

    def _open_settings(self):
        dlg = SettingsDialog(self)
        dlg.exec()
        self._update_connect_button_state()

    def _open_global_settings(self):
        if not self.cards:
            QMessageBox.information(self, "Общие настройки", "Сначала загрузите рынки.")
            return
        dlg = GlobalSettingsDialog(self)
        first_card = next(iter(self.cards.values()))
        s = first_card.settings
        dlg.pos_type_combo.setCurrentText("usdt" if s.position_size_usdt else "shares")
        dlg.pos_edit.setText(str(s.position_size_usdt or s.position_size_shares or 100))
        dlg.min_spread_edit.setText(str(s.min_spread or 0.2))
        dlg.target_liq_edit.setText(str(s.target_liquidity or 1000))
        dlg.max_spread_edit.setText(str(s.max_auto_spread or 6))
        dlg.liquidity_mode_combo.setCurrentIndex(1 if (s.liquidity_mode or "bid") == "ask" else 0)
        dlg.volatile_limit_edit.setText(str(int(getattr(s, "volatile_reposition_limit", DEFAULT_VOLATILE_REPOSITION_LIMIT) or 0)))
        dlg.volatile_window_edit.setText(str(getattr(s, "volatile_window_seconds", DEFAULT_VOLATILE_WINDOW_SECONDS) or 60))
        dlg.volatile_cooldown_edit.setText(str(getattr(s, "volatile_cooldown_seconds", DEFAULT_VOLATILE_COOLDOWN_SECONDS) or 3600))
        dlg.exec()

    def _on_sort_clicked(self):
        self._sort_mode = (self._sort_mode + 1) % 3
        self.sort_btn.setText(self._sort_labels[self._sort_mode])
        self._save_sort_mode(self._sort_mode)
        self._reflow_cards()

    def _get_sorted_cards(self):
        cards = list(self.cards.values())
        if self._sort_mode == 0:
            return list(reversed(cards))
        if self._sort_mode == 1:
            def _id_key(c):
                mid = c.market_id
                return (0, int(mid)) if mid.isdigit() else (1, mid.lower())
            return sorted(cards, key=_id_key)
        if self._sort_mode == 2:
            return sorted(cards, key=lambda c: (c.market_info.get("title") or c.market_info.get("question") or c.market_id).lower())
        return cards

    def _filter_cards(self):
        q = (self.search_edit.text() or "").strip().lower()
        for mid, card in self.cards.items():
            info = self.markets.get(mid, card.market_info) or {}
            title = (info.get("title") or "").lower()
            question = (info.get("question") or "").lower()
            slug = (info.get("slug") or "").lower()
            matches = not q or q in str(mid).lower() or q in title or q in question or q in slug
            card.setVisible(matches)
        self._reflow_cards()

    def _reflow_cards(self):
        if not self.cards:
            return
        vp = self.scroll_with_log.scroll.viewport()
        w = max(100, vp.width() - 30)
        cols = max(1, w // (CARD_WIDTH + CARD_SPACING))
        ordered = self._get_sorted_cards()
        for card in ordered:
            self.cards_layout.removeWidget(card)
        for c in range(cols):
            self.cards_layout.setColumnStretch(c, 0)
        for i, card in enumerate(ordered):
            self.cards_layout.addWidget(card, i // cols, i % cols)

    def _connect(self):
        accounts = load_accounts_from_file()
        if not accounts:
            QMessageBox.warning(self, "Ошибка", f"Файл {ACCOUNTS_FILE} не найден. Формат: api_key,address,privy_key,proxy")
            return
        self.connect_btn.setText("Подключаемся...")
        self.connect_btn.setEnabled(False)
        self._log("Подключение...")
        self.current_account = accounts[0]
        self.worker.do_connect(accounts[0], lambda m: self.worker.log_signal.emit(m))

    def _on_connect_done(self, ok: bool, jwt_or_err, account_info):
        if not ok:
            self.connect_btn.setText("Подключить")
            self._update_connect_button_state()
            self._log(f"✗ Ошибка: {jwt_or_err}")
            QMessageBox.critical(self, "Ошибка", str(jwt_or_err))
            return
        self._switch_to_rudy_button()
        jwt, client = jwt_or_err
        self.jwt_token = jwt
        self.api_client = client
        self._log("✓ Подключено")

        async def _set_referral():
            ok = await client.set_referral("26F1B")
            if ok:
                self.worker.log_signal.emit("Братишка, ты успешно стал моим рефералом, спасибо!")
            else:
                self.worker.log_signal.emit("Жалко, что ты не стал моим рефералом :(")
        asyncio.run_coroutine_threadsafe(_set_referral(), self.worker.loop)
        acc = self.current_account
        log_fn = lambda m: self.worker.log_signal.emit(m)
        self.executor = Executor(
            acc["api_key"], jwt, acc["predict_account_address"],
            acc["privy_wallet_private_key"], acc.get("proxy"),
            log_func=log_fn, api_client=client,
        )
        self.executor.on_cancel_done = lambda mid: self.worker.cancel_done.emit(mid, True)
        if acc:
            addr = acc["predict_account_address"]
            self.account_info[addr] = account_info or {}
            self._update_account_display()
            self.worker.start_balance_updates(acc, jwt, client)
        self.add_market_btn.setEnabled(True)
        self.account_ws_frame.setEnabled(True)
        self.orders_frame.setEnabled(True)
        self.status_label.setText("Подключено")
        if os.path.exists(LAST_MARKETS_FILE):
            try:
                with open(LAST_MARKETS_FILE, "r", encoding="utf-8") as f:
                    ids = [x.strip() for x in f.read().replace("\n", ",").split(",") if x.strip()]
                if ids:
                    self._log("Автозагрузка сохранённых рынков...")
                    self._add_markets(ids)
            except Exception:
                pass

        def on_ob(mid, ob):
            save_orderbook(mid, ob)
            async def process_and_act():
                def sync_process():
                    mod = self.market_modules.get(mid)
                    if not mod or not self.executor:
                        return None
                    get_active = lambda: self.executor.get_active_orders(mid)
                    return mod.process_orderbook(ob, get_active, emit_state=False)

                try:
                    order_info = await asyncio.to_thread(sync_process)
                except Exception as e:
                    log_error_to_file("process_orderbook", exception=e, context=mid)
                    return
                if not order_info:
                    return

                card = self.cards.get(mid)
                settings = self.settings_manager.get_settings(mid)
                mod = self.market_modules.get(mid)
                state = {
                    "order_info": order_info,
                    "orderbook": ob,
                    "settings": settings,
                    "update_time": mod._update_time if mod else time.time(),
                    "prev_orderbook_time": mod._prev_orderbook_time if mod else None,
                    "mid_price": order_info.get("mid_price_yes"),
                    "best_bid": order_info.get("best_bid_yes"),
                    "best_ask": order_info.get("best_ask_yes"),
                    "enqueue_handled": True,
                }
                self.worker.market_display_update.emit(mid, state)
                self.worker.ws_last_update_signal.emit(time.time())

                if card and card.orders_placed and self.executor:
                    mod = self.market_modules.get(mid)
                    oi = order_info
                    active = self.executor.get_active_orders(mid)
                    settings = self.settings_manager.get_settings(mid)
                    cancel_yes, cancel_no = mod.should_cancel_and_replace(oi, active, settings) if mod else (False, False)
                    need_cancel = cancel_yes or cancel_no
                    yes_blocked = self.executor.is_outcome_blocked(mid, "yes")
                    no_blocked = self.executor.is_outcome_blocked(mid, "no")
                    need_place = (
                        (oi.get("can_place_yes") and not active.get("yes") and not yes_blocked)
                        or (oi.get("can_place_no") and not active.get("no") and not no_blocked)
                    )
                    if need_cancel:
                        title = (card.market_info.get("title") or mid)[:30]
                        prev_t = mod._prev_orderbook_time if mod else None
                        curr_t = mod._update_time if mod else None
                        full_msg, reason_str = self._format_cancel_reason(
                            mid, title, oi, active, cancel_yes, cancel_no,
                            prev_orderbook_time=prev_t, curr_orderbook_time=curr_t,
                        )
                        self.worker.log_signal.emit(full_msg)
                        await self.executor.enqueue_cancel(mid, title, cancel_reason=reason_str)
                    elif need_place and self._volatile_before_place(mid):
                        title = (card.market_info.get("title") or mid)[:30]
                        prev_t = mod._prev_orderbook_time if mod else None
                        curr_t = mod._update_time if mod else None
                        await self.executor.enqueue_place_orders(
                            mid, oi, oi.get("mid_price_yes", 0),
                            card.market_info, title, ob, settings,
                            prev_orderbook_time=prev_t, curr_orderbook_time=curr_t,
                        )

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(process_and_act())
            except RuntimeError:
                asyncio.run_coroutine_threadsafe(process_and_act(), self.worker.loop)
        def on_hb():
            self.worker.ws_last_update_signal.emit(time.time())
        def on_conn(c):
            self.worker.ws_status_signal.emit(c)
        self.ws_client = WebSocketClient(
            api_key=acc["api_key"],
            on_orderbook_update=on_ob,
            on_heartbeat=on_hb,
            on_connection_change=on_conn,
            log_func=lambda m: self.worker.log_signal.emit(m),
        )
        self.ws_client.start(self.worker.loop)

    def _on_load_progress(self, cur: int, total: int):
        self.status_label.setText(f"Загрузка {cur}/{total}...")

    def _open_add_market_dialog(self):
        if not self.api_client:
            return
        dlg = AddMarketDialog(self)
        dlg.exec()

    def _add_markets(self, ids: List[str]):
        if not ids or not self.api_client:
            return
        self._log(f"Загрузка {len(ids)} рынков...")
        self.add_market_btn.setEnabled(False)

        async def _load():
            markets = await loader_load_markets(
                ids, self.api_client,
                log_func=lambda m: self.worker.log_signal.emit(m),
                on_progress=lambda c, t: self.worker.market_load_progress.emit(c, t),
            )
            self.worker.markets_loaded.emit(markets)

        asyncio.run_coroutine_threadsafe(_load(), self.worker.loop)

    def _remove_market(self, market_id: str):
        card = self.cards.get(market_id)
        if not card:
            return
        if card.orders_placed and self.executor:
            title = (card.market_info.get("title") or market_id)[:30]
            asyncio.run_coroutine_threadsafe(
                self.executor.enqueue_cancel(market_id, title, cancel_reason="удаление"),
                self.worker.loop
            )
        if self.ws_client:
            self.ws_client.unsubscribe_orderbook(market_id)
        if self.executor and market_id in self.executor.active_orders:
            del self.executor.active_orders[market_id]
        self._volatile_state.pop(market_id, None)
        self.cards_layout.removeWidget(card)
        card.deleteLater()
        del self.cards[market_id]
        del self.market_modules[market_id]
        self.markets.pop(market_id, None)
        try:
            with open(LAST_MARKETS_FILE, "w", encoding="utf-8") as f:
                f.write(",".join(self.cards.keys()))
        except Exception:
            pass
        self.status_label.setText(f"Рынков: {len(self.cards)}")
        self._log(f"Удалён рынок {market_id}")
        if not self.cards:
            self.place_all_btn.setEnabled(False)
            self.cancel_all_btn.setEnabled(False)
            self.global_settings_btn.setEnabled(False)
        self._filter_cards()
        QTimer.singleShot(50, self._reflow_cards)
        self._update_orders_count()

    def _on_markets_loaded(self, markets: dict):
        had_cards = len(self.cards) > 0
        self.markets.update(markets)
        for mid, info in markets.items():
            if mid not in self.cards:
                card = MarketCard(mid, info, self)
                mod = MarketModule(
                    mid, info,
                    get_settings=lambda m=mid: self.settings_manager.get_settings(m),
                    on_state_changed=lambda mk, st: self.worker.market_display_update.emit(mk, st),
                )
                self.market_modules[mid] = mod
                self.cards[mid] = card
                row, col = divmod(len(self.cards) - 1, max(1, 3))
                self.cards_layout.addWidget(card, row, col)
                if self.ws_client:
                    self.ws_client.subscribe_orderbook(mid)
        self.place_all_btn.setEnabled(True)
        self.cancel_all_btn.setEnabled(True)
        self.global_settings_btn.setEnabled(True)
        self.add_market_btn.setEnabled(True)
        self.search_frame.setEnabled(True)
        try:
            with open(LAST_MARKETS_FILE, "w", encoding="utf-8") as f:
                f.write(",".join(self.cards.keys()))
        except Exception:
            pass
        self.status_label.setText(f"Рынков: {len(self.cards)}")
        if markets:
            self._log(f"✓ Добавлено {len(markets)} рынков, всего {len(self.cards)}")
        else:
            self._log("Нет подходящих рынков (только REGISTERED)")
        self._filter_cards()
        QTimer.singleShot(100, self._reflow_cards)
        if not had_cards and len(self.cards) > 0:
            if self.inspector_checkbox.isChecked():
                self._inspector_start_timer.stop()
                self._inspector_start_timer.start(60 * 1000)
                self._log("Inspector запустится через 1 мин")
            else:
                self._log("Inspector отключён — пропускаем")
            interval_sec = get_telegram_status_interval_sec()
            self._status_report_timer.setInterval(interval_sec * 1000)
            self._status_report_timer.start()
            mins = interval_sec // 60
            self._log(f"Telegram статус — каждые {mins} мин")
        self._update_orders_count()

    def _on_market_display(self, market_id: str, data: dict):
        card = self.cards.get(market_id)
        if card:
            if "orderbook" in data:
                card.last_orderbook = data["orderbook"]
            card.update_display(data)
            if not data.get("enqueue_handled") and card.orders_placed and self.executor and data.get("order_info"):
                oi = data["order_info"]
                active = self.executor.get_active_orders(market_id)
                settings = data.get("settings") or self.settings_manager.get_settings(market_id)
                mod = self.market_modules.get(market_id)
                cancel_yes, cancel_no = (mod.should_cancel_and_replace(oi, active, settings) if mod else (False, False))
                need_cancel = cancel_yes or cancel_no
                yes_blocked = self.executor.is_outcome_blocked(market_id, "yes")
                no_blocked = self.executor.is_outcome_blocked(market_id, "no")
                need_place = ((oi.get("can_place_yes") and not active.get("yes") and not yes_blocked)
                             or (oi.get("can_place_no") and not active.get("no") and not no_blocked))
                if need_cancel:
                    title = (card.market_info.get("title") or market_id)[:30]
                    full_msg, reason_str = self._format_cancel_reason(
                        market_id, title, oi, active, cancel_yes, cancel_no,
                        prev_orderbook_time=data.get("prev_orderbook_time"),
                        curr_orderbook_time=data.get("update_time"),
                    )
                    self.worker.log_signal.emit(full_msg)
                    asyncio.run_coroutine_threadsafe(
                        self.executor.enqueue_cancel(market_id, title, cancel_reason=reason_str),
                        self.worker.loop
                    )
                elif need_place:
                    orderbook = data.get("orderbook") or card.last_orderbook
                    settings = data.get("settings") or card.settings
                    title = (card.market_info.get("title") or market_id)[:30]
                    asyncio.run_coroutine_threadsafe(
                        self.executor.enqueue_place_orders(
                            market_id, oi, oi.get("mid_price_yes", 0),
                            card.market_info, title, orderbook, settings,
                            prev_orderbook_time=data.get("prev_orderbook_time"),
                            curr_orderbook_time=data.get("update_time"),
                        ),
                        self.worker.loop
                    )

    def _on_balance_updated(self, address: str, balance: float, timestamp: float):
        if address in self.account_info:
            old_balance = self.account_info[address].get("balance")
            self.account_info[address]["balance"] = balance
            self._update_account_display()
            if old_balance is not None and abs(balance - old_balance) > 0.001:
                diff = balance - old_balance
                sign = "+" if diff > 0 else ""
                msg = (
                    f"💰 <b>Баланс обновлён</b>\n"
                    f"{sign}{diff:,.2f} USDT → ${balance:,.2f}"
                )
                if self.worker and self.worker.loop:
                    asyncio.run_coroutine_threadsafe(send_telegram_notification(msg), self.worker.loop)
        self._balance_update_time = timestamp
        self._update_account_ws_display()

    def _update_account_display(self):
        self._update_account_ws_display()

    def _update_account_ws_display(self):
        import datetime
        line1_parts = []
        if self.account_info:
            addr = next(iter(self.account_info.keys()), None)
            if addr:
                info = self.account_info[addr]
                nick = info.get("nickname") or info.get("username") or info.get("name")
                line1_parts.append(f"👤 {nick}" if nick else f"👤 {addr[:6]}…{addr[-4:]}")
                bal = info.get("balance")
                if bal is not None:
                    line1_parts.append(f"💰 ${bal:,.2f}" if bal >= 1000 else f"💰 ${bal:.2f}" if bal >= 1 else f"💰 ${bal:.4f}")
        if self._balance_update_time:
            line1_parts.append(f"({datetime.datetime.fromtimestamp(self._balance_update_time).strftime('%H:%M:%S')})")
        line1 = "  ".join(line1_parts)

        ws_text = "WS: ✓" if self._ws_connected else "WS: —"
        if self._ws_update_time:
            ws_text += f"  ({datetime.datetime.fromtimestamp(self._ws_update_time).strftime('%H:%M:%S')})"
        ws_color = "#4ade80" if self._ws_connected else "#8e8e93"
        full = f"{line1}<br><span style='color:{ws_color}'>{ws_text}</span>" if line1 else f"<span style='color:{ws_color}'>{ws_text}</span>"
        self.account_ws_label.setText(full)

    def _on_ws_status(self, connected: bool):
        self._ws_connected = connected
        self._update_account_ws_display()

    def _on_ws_last_update(self, timestamp: float):
        self._ws_update_time = timestamp
        self._update_account_ws_display()

    def _on_inspector_orders_count(self, count: int):
        self.inspector_orders_count = count
        self.inspector_orders_updated_at = time.time()
        self._update_orders_count()

    def _update_orders_count(self):
        prelim = 0
        placed = 0
        for mid, card in self.cards.items():
            if hasattr(card, "last_order_info") and card.last_order_info and not self.is_volatile_cooldown(mid):
                oi = card.last_order_info
                if oi.get("can_place_yes") and not (self.executor and self.executor.is_outcome_blocked(mid, "yes")):
                    prelim += 1
                if oi.get("can_place_no") and not (self.executor and self.executor.is_outcome_blocked(mid, "no")):
                    prelim += 1
            if self.executor:
                active = self.executor.get_active_orders(mid)
                if active.get("yes"):
                    placed += 1
                if active.get("no"):
                    placed += 1
        if self.inspector_orders_count is not None:
            import datetime
            ts = self.inspector_orders_updated_at or 0
            time_str = datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
            api_str = f"{self.inspector_orders_count} ({time_str})"
        else:
            api_str = "—"
        self.orders_count_label.setText(f"Можно выставить: {prelim}\nВыставлено: {placed}\nAPI: {api_str}")

    def _on_place_done(self, market_id: str, ok: bool):
        card = self.cards.get(market_id)
        if card:
            card._update_placed_display()
        self._update_orders_count()

    def _format_cancel_reason(
        self, mid: str, title: str, oi: dict, active: dict, cancel_yes: bool, cancel_no: bool,
        prev_orderbook_time: float | None = None, curr_orderbook_time: float | None = None,
    ) -> tuple[str, str]:
        """Формирует (полное сообщение, причина для строки Отменено)."""
        import datetime
        parts = []
        can_yes = oi.get("can_place_yes", True)
        can_no = oi.get("can_place_no", True)
        buy_yes = oi.get("buy_yes", {})
        buy_no = oi.get("buy_no", {})
        cur_yes = active.get("yes")
        cur_no = active.get("no")
        if cancel_yes and cur_yes:
            old_p = (cur_yes.get("price") or 0) * 100
            if not can_yes:
                parts.append(f"Yes: {old_p:.1f}¢ — причина: нельзя выставить (ликв./спред)")
            else:
                new_p = (buy_yes.get("price") or 0) * 100
                parts.append(f"Yes: {old_p:.1f}¢ → {new_p:.1f}¢ — причина: цена изменилась")
        if cancel_no and cur_no:
            old_p = (cur_no.get("price") or 0) * 100
            if not can_no:
                parts.append(f"No: {old_p:.1f}¢ — причина: нельзя выставить (ликв./спред)")
            else:
                new_p = (buy_no.get("price") or 0) * 100
                parts.append(f"No: {old_p:.1f}¢ → {new_p:.1f}¢ — причина: цена изменилась")
        if not parts:
            return f"[{title or mid}] Отмена ордеров", ""
        reason_str = "; ".join(parts)
        msg = f"[{title or mid}] Отмена — {reason_str}"
        if prev_orderbook_time is not None or curr_orderbook_time is not None:
            def _ts(t: float | None) -> str:
                return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "—"
            msg += f" | прошлый стакан: {_ts(prev_orderbook_time)}, текущий стакан: {_ts(curr_orderbook_time)}"
        return msg, reason_str

    def _on_cancel_done(self, market_id: str, ok: bool):
        """После отмены: сразу place по last_order_info из Решателя, без ожидания нового стакана."""
        if ok:
            self._volatile_on_cancel_done(market_id)
        card = self.cards.get(market_id)
        if not ok or not card or not card.orders_placed or not self.executor or not self.worker:
            if card:
                card._update_placed_display()
            self._update_orders_count()
            return
        oi = card.last_order_info
        if not oi:
            card._update_placed_display()
            self._update_orders_count()
            return
        can_yes = oi.get("can_place_yes", False)
        can_no = oi.get("can_place_no", False)
        yes_blocked = self.executor.is_outcome_blocked(market_id, "yes")
        no_blocked = self.executor.is_outcome_blocked(market_id, "no")
        active = self.executor.get_active_orders(market_id)
        has_yes = active.get("yes") is not None
        has_no = active.get("no") is not None
        need_place = (
            (can_yes and not has_yes and not yes_blocked)
            or (can_no and not has_no and not no_blocked)
        )
        if need_place and self._volatile_before_place(market_id):
            title = (card.market_info.get("title") or market_id)[:30]
            ob = card.last_orderbook
            mod = self.market_modules.get(market_id)
            prev_t = mod._prev_orderbook_time if mod else None
            curr_t = mod._update_time if mod else None
            asyncio.run_coroutine_threadsafe(
                self.executor.enqueue_place_orders(
                    market_id, oi, oi.get("mid_price_yes", 0),
                    card.market_info, title, ob, card.settings,
                    prev_orderbook_time=prev_t, curr_orderbook_time=curr_t,
                ),
                self.worker.loop
            )
        card._update_placed_display()
        self._update_orders_count()

    def _place_all(self):
        for mid, card in self.cards.items():
            if not card.orders_placed and card.last_orderbook and self.executor:
                card._apply_settings()
                self.settings_manager.update_settings(mid, enabled=True)
                card.settings = self.settings_manager.get_settings(mid)
                card.orders_placed = True
                card.liquidity_btn.setText("Убрать ордера")
                card.liquidity_btn.setObjectName("danger")
                card.liquidity_btn.style().unpolish(card.liquidity_btn)
                card.liquidity_btn.style().polish(card.liquidity_btn)
                oi = Calculator.calculate_limit_orders(
                    card.last_orderbook, card.settings,
                    decimal_precision=card.market_info.get("decimalPrecision", 3),
                    active_orders=self.executor.get_active_orders(mid),
                )
                if oi and (oi.get("can_place_yes") or oi.get("can_place_no")) and self._volatile_before_place(mid):
                    title = (card.market_info.get("title") or mid)[:30]
                    mod = self.market_modules.get(mid)
                    prev_t = mod._prev_orderbook_time if mod else None
                    curr_t = mod._update_time if mod else None
                    asyncio.run_coroutine_threadsafe(
                        self.executor.enqueue_place_orders(
                            mid, oi, oi["mid_price_yes"],
                            card.market_info, title, card.last_orderbook, card.settings,
                            prev_orderbook_time=prev_t, curr_orderbook_time=curr_t,
                        ),
                        self.worker.loop
                    )
                card._update_placed_display()

    def _cancel_all(self):
        for mid, card in self.cards.items():
            if card.orders_placed and self.executor:
                self.settings_manager.update_settings(mid, enabled=False)
                card.settings = self.settings_manager.get_settings(mid)
                card.orders_placed = False
                card.liquidity_btn.setText("Выставить ликвидность")
                card.liquidity_btn.setObjectName("primary")
                card.liquidity_btn.style().unpolish(card.liquidity_btn)
                card.liquidity_btn.style().polish(card.liquidity_btn)
                title = (card.market_info.get("title") or mid)[:30]
                asyncio.run_coroutine_threadsafe(
                    self.executor.enqueue_cancel(mid, title),
                    self.worker.loop
                )
                card._update_placed_display()

    def _send_status_report(self):
        import datetime
        uptime_sec = int(time.time() - self._start_time)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, _ = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m"

        balance_str = "—"
        if self.account_info:
            addr = next(iter(self.account_info.keys()), None)
            if addr:
                bal = self.account_info[addr].get("balance")
                if bal is not None:
                    balance_str = f"${bal:,.2f}"

        prelim = 0
        placed = 0
        for mid, card in self.cards.items():
            if hasattr(card, "last_order_info") and card.last_order_info and not self.is_volatile_cooldown(mid):
                oi = card.last_order_info
                if oi.get("can_place_yes") and not (self.executor and self.executor.is_outcome_blocked(mid, "yes")):
                    prelim += 1
                if oi.get("can_place_no") and not (self.executor and self.executor.is_outcome_blocked(mid, "no")):
                    prelim += 1
            if self.executor:
                active = self.executor.get_active_orders(mid)
                if active.get("yes"):
                    placed += 1
                if active.get("no"):
                    placed += 1

        api_str = str(self.inspector_orders_count) if self.inspector_orders_count is not None else "—"
        ws_status = "Live" if self.ws_client and getattr(self.ws_client, "_connected", False) else "Offline"
        now_str = datetime.datetime.now().strftime("%H:%M:%S")

        msg = (
            f"📊 <b>Статус</b> ({now_str})\n\n"
            f"💰 Баланс: {balance_str}\n\n"
            f"📈 Рынков: {len(self.markets)}\n"
            f"📍 Можно выставить: {prelim}\n"
            f"✅ Выставлено: {placed}\n"
            f"📋 API ордеров: {api_str}\n\n"
            f"⏱ Аптайм: {uptime_str}"
        )

        if self.worker and self.worker.loop:
            asyncio.run_coroutine_threadsafe(send_telegram_notification(msg), self.worker.loop)
        self._log(f"Telegram статус отправлен")

    def _load_inspector_state(self) -> bool:
        import json as _json
        try:
            with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return data.get("inspector_enabled", True)
        except Exception:
            return True

    def _load_sort_mode(self) -> int:
        import json as _json
        try:
            with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
                data = _json.load(f)
            m = data.get("sort_mode", 0)
            return max(0, min(2, int(m) if isinstance(m, (int, float)) else 0))
        except Exception:
            return 0

    def _save_sort_mode(self, mode: int):
        import json as _json
        data = {}
        try:
            with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            pass
        data["sort_mode"] = mode
        try:
            with open(APP_STATE_FILE, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _save_inspector_state(self, enabled: bool):
        import json as _json
        data = {}
        try:
            with open(APP_STATE_FILE, "r", encoding="utf-8") as f:
                data = _json.load(f)
        except Exception:
            pass
        data["inspector_enabled"] = enabled
        try:
            with open(APP_STATE_FILE, "w", encoding="utf-8") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _on_inspector_toggle(self, state):
        enabled = self.inspector_checkbox.isChecked()
        self._save_inspector_state(enabled)
        if enabled:
            if not self.inspector and self.executor:
                self._log("Inspector включён — запуск через 1 мин")
                self._inspector_start_timer.stop()
                self._inspector_start_timer.start(60 * 1000)
        else:
            self._inspector_start_timer.stop()
            if self.inspector:
                self.inspector.stop()
                self.inspector = None
                self.inspector_orders_count = None
                self.inspector_orders_updated_at = None
                self._update_orders_count()
            self._log("Inspector отключён")

    def _start_inspector(self):
        if not self.inspector_checkbox.isChecked():
            self._log("Inspector отключён — пропускаем запуск")
            return
        if self.inspector:
            self.inspector.stop()
        self.inspector_orders_count = None
        self.inspector_orders_updated_at = None
        self._update_orders_count()
        log_via = lambda m: self.worker.log_signal.emit(m)

        def get_snapshot():
            if not self.executor:
                return {}
            return {
                "expected": self.executor.get_all_active_order_ids(),
                "managed": set(self.markets.keys()),
                "headers": self.executor.headers,
                "proxy": (self.current_account or {}).get("proxy"),
                "api_key": (self.current_account or {}).get("api_key"),
            }

        self.inspector = Inspector(
            get_snapshot=get_snapshot,
            on_orders_count=lambda n: self.worker.inspector_orders_count_signal.emit(n),
            log_func=log_via,
        )
        self.inspector.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(80, self._reflow_cards)

    def closeEvent(self, event):
        self._stop_settings_pulse()
        self._inspector_start_timer.stop()
        self._status_report_timer.stop()
        if self.inspector:
            self.inspector.stop()
        if self.ws_client:
            self.ws_client.stop()
        if self.worker.balance_updater:
            self.worker.balance_updater.stop()
        self.worker.stop()
        event.accept()


def main():
    from PySide6.QtWidgets import QApplication
    session_path = init_session_log()
    app = QApplication(sys.argv)
    app.setStyleSheet(BNB_STYLE)
    app.setFont(QFont("Segoe UI", 10))
    win = MainWindow()
    win.show()
    win.worker.log_signal.emit(f"Лог сессии: {session_path}")
    sys.exit(app.exec())
