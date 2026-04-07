from __future__ import annotations

from datetime import datetime
from typing import Optional

import requests

import config
from logger import logger


class Notifier:
    """
    Відправляє Telegram-повідомлення з префіксом [SCALP].

    Використовує прямий HTTP запит через requests (без asyncio),
    щоб уникнути конфліктів з event loop telegram_bot.py.

    Підтримує message_thread_id для відправки в конкретний топік:
      - якщо TELEGRAM_THREAD_ID задано — використовує його
      - якщо ні — пише в основний чат (message_thread_id=None)
    """

    _TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self) -> None:
        self._enabled = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)
        if not self._enabled:
            logger.warning("Telegram не налаштовано — сповіщення вимкнено")

    # ── Публічний інтерфейс ───────────────────────────────────────────────────

    def notify_open(
        self,
        symbol: str,
        side: str,
        price: float,
        vwap: float,
        imbalance: float,
        tp_price: float,
        sl_price: float,
    ) -> None:
        """Повідомлення про відкриття позиції."""
        side_icon = "🟢" if side == "LONG" else "🔴"
        side_label = "LONG" if side == "LONG" else "SHORT"
        bid_pct = imbalance * 100
        now = datetime.now().strftime("%d.%m.%Y %H:%M")

        tp_pct = config.TAKE_PROFIT_PCT * 100
        sl_pct = config.STOP_LOSS_PCT  * 100

        text = (
            f"{config.BOT_PREFIX} {side_icon} {side_label} відкрито | <b>{symbol}</b>\n"
            f"💰 Ціна входу: {price:,.2f} USDT\n"
            f"📊 VWAP: {vwap:,.2f} | Imbalance: {bid_pct:.0f}% BID\n"
            f"🎯 TP: {tp_price:,.2f} USDT (+{tp_pct:.1f}%)\n"
            f"🛑 SL: {sl_price:,.2f} USDT (-{sl_pct:.1f}%)\n"
            f"⏰ {now}"
        )
        self._send(text)

    def notify_close(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        duration_min: int,
    ) -> None:
        """Повідомлення про закриття позиції."""
        pnl_icon  = "✅" if pnl >= 0 else "❌"
        pnl_pct   = ((exit_price - entry_price) / entry_price) * 100
        if side == "SHORT":
            pnl_pct = -pnl_pct
        sign = "+" if pnl >= 0 else ""

        text = (
            f"{config.BOT_PREFIX} 🔴 Позиція закрита | <b>{symbol}</b>\n"
            f"{pnl_icon} Результат: {sign}{pnl:.4f} USDT ({sign}{pnl_pct:.2f}%)\n"
            f"📈 Вхід: {entry_price:,.2f} → Вихід: {exit_price:,.2f}\n"
            f"⏱ Тривалість: {duration_min}хв"
        )
        self._send(text)

    def notify_sl_tp_hit(
        self,
        symbol: str,
        side: str,
        hit: str,          # "TP" або "SL"
        entry_price: float,
        exit_price: float,
        pnl: float,
        duration_min: int,
    ) -> None:
        """Повідомлення про спрацювання TP/SL."""
        icon = "🎯" if hit == "TP" else "🛑"
        self.notify_close(symbol, side, entry_price, exit_price, pnl, duration_min)

    def notify_daily_loss_limit(self, daily_pnl: float, limit_pct: float) -> None:
        """Повідомлення про досягнення денного ліміту збитку."""
        text = (
            f"{config.BOT_PREFIX} ⛔️ Денний ліміт збитку досягнуто!\n"
            f"💸 Денний P&L: {daily_pnl:+.4f} USDT\n"
            f"📉 Ліміт: -{limit_pct*100:.1f}%\n"
            f"🔒 Торгівлю зупинено до наступного дня."
        )
        self._send(text)

    def notify_startup(
        self,
        balance: float,
        testnet: bool,
        symbols: list[str],
    ) -> None:
        """Повідомлення про запуск бота."""
        mode = "🧪 TESTNET" if testnet else "🔴 LIVE"
        syms = ", ".join(symbols)
        text = (
            f"{config.BOT_PREFIX} 🚀 Scalping Bot запущено\n"
            f"🌐 Режим: {mode}\n"
            f"💰 Баланс: {balance:,.2f} USDT\n"
            f"📋 Пари: {syms}\n"
            f"⏱ Інтервал: {config.SCAN_INTERVAL}с | TP {config.TAKE_PROFIT_PCT*100:.1f}% / SL {config.STOP_LOSS_PCT*100:.1f}%\n"
            f"📊 Плече: x{config.LEVERAGE} | Ліміт: {config.MAX_TRADING_BALANCE:.0f} USDT"
        )
        self._send(text)

    def notify_shutdown(self) -> None:
        """Повідомлення про зупинку бота."""
        text = f"{config.BOT_PREFIX} 🛑 Scalping Bot зупинено (graceful shutdown)"
        self._send(text)

    def notify_paused(self) -> None:
        text = f"{config.BOT_PREFIX} ⏸ Торгівлю призупинено (команда /pause)"
        self._send(text)

    def notify_resumed(self) -> None:
        text = f"{config.BOT_PREFIX} ▶️ Торгівлю відновлено (команда /resume)"
        self._send(text)

    def send_text(self, text: str) -> None:
        """Відправляє довільний текст."""
        self._send(text)

    # ── Внутрішній метод відправки ────────────────────────────────────────────

    def _send(self, text: str) -> None:
        """
        Синхронна відправка через Telegram HTTP API напряму (requests).
        Не використовує asyncio — нема конфліктів з event loop.
        """
        if not self._enabled:
            return

        thread_id: Optional[int] = None
        if config.TELEGRAM_THREAD_ID:
            try:
                thread_id = int(config.TELEGRAM_THREAD_ID)
            except ValueError:
                pass

        url = self._TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN)
        payload: dict = {
            "chat_id":    config.TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "HTML",
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id

        try:
            resp = requests.post(url, json=payload, timeout=10)
            if not resp.ok:
                logger.error(
                    "Telegram API помилка: %d %s",
                    resp.status_code, resp.text[:200],
                )
        except requests.RequestException as exc:
            logger.error("Помилка відправки Telegram: %s", exc)
