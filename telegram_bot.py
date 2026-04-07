from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Optional

from telegram import Update
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

import config
from logger import logger

if TYPE_CHECKING:
    from risk_manager import RiskManager
    from trader import Trader


class TelegramCommandBot:
    """
    Telegram-бот для управління scalping-ботом через команди.

    Команди:
      /status  — поточний статус (баланс, відкриті позиції)
      /today   — денна статистика
      /month   — місячна статистика (заглушка)
      /pause   — призупинити торгівлю
      /resume  — відновити торгівлю
      /stop    — зупинити бота
    """

    def __init__(self, risk_manager: "RiskManager", trader: "Trader") -> None:
        self.risk = risk_manager
        self.trader = trader
        self._app: Optional[Application] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._enabled = bool(config.TELEGRAM_BOT_TOKEN)

        if not self._enabled:
            logger.warning("Telegram Bot Token не задано — команди недоступні")

    # ── Запуск / Зупинка ──────────────────────────────────────────────────────

    def start(self) -> None:
        """Запускає polling у фоновому потоці."""
        if not self._enabled:
            return

        self._thread = threading.Thread(target=self._run_polling, daemon=True)
        self._thread.start()
        logger.info("Telegram command bot запущено (polling)")

    def stop(self) -> None:
        """Зупиняє polling."""
        if self._app and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._app.updater.stop(), self._loop
            )
            logger.info("Telegram command bot зупинено")

    # ── Внутрішній запуск ─────────────────────────────────────────────────────

    def _run_polling(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        self._app = (
            Application.builder()
            .token(config.TELEGRAM_BOT_TOKEN)
            .build()
        )

        # Команди з префіксом /s_ щоб не конфліктувати з основним ботом
        self._app.add_handler(CommandHandler("s_status", self._cmd_status))
        self._app.add_handler(CommandHandler("s_today",  self._cmd_today))
        self._app.add_handler(CommandHandler("s_month",  self._cmd_month))
        self._app.add_handler(CommandHandler("s_pause",  self._cmd_pause))
        self._app.add_handler(CommandHandler("s_resume", self._cmd_resume))
        self._app.add_handler(CommandHandler("s_stop",   self._cmd_stop))

        # Приглушуємо Conflict помилки (виникають якщо той самий токен
        # використовується іншим ботом — рекомендується окремий токен)
        self._app.add_error_handler(self._error_handler)

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )

        # Тримаємо цикл живим
        while True:
            await asyncio.sleep(1)

    async def _error_handler(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Обробляє помилки polling — Conflict логується як WARNING, решта як ERROR."""
        if isinstance(context.error, Conflict):
            logger.warning(
                "Telegram Conflict: той самий токен використовується ще одним процесом. "
                "Команди недоступні. Рекомендується окремий TELEGRAM_BOT_TOKEN для скальпінг-бота."
            )
        else:
            logger.error("Telegram помилка: %s", context.error)

    # ── Хелпер відправки ──────────────────────────────────────────────────────

    async def _reply(self, update: Update, text: str) -> None:
        """Відповідає у правильний топік (якщо задано TELEGRAM_THREAD_ID)."""
        thread_id: Optional[int] = None
        if config.TELEGRAM_THREAD_ID:
            try:
                thread_id = int(config.TELEGRAM_THREAD_ID)
            except ValueError:
                pass

        if update.message:
            await update.message.reply_text(
                text=text,
                parse_mode="HTML",
                message_thread_id=thread_id,
            )
        elif update.effective_chat:
            await self._app.bot.send_message(
                chat_id=update.effective_chat.id,
                message_thread_id=thread_id,
                text=text,
                parse_mode="HTML",
            )

    # ── Команди ───────────────────────────────────────────────────────────────

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        balance = self.trader.binance.get_usdt_balance()
        open_pos = self.trader.binance.get_open_positions()
        mode = "🧪 TESTNET" if config.TESTNET else "🔴 LIVE"
        stopped = "⛔️ ЗУПИНЕНО" if self.risk.is_stopped else "✅ АКТИВНИЙ"

        lines = [
            f"{config.BOT_PREFIX} 📊 <b>Статус бота</b>",
            f"🌐 Режим: {mode}",
            f"🔋 Стан: {stopped}",
            f"💰 Баланс: {balance:,.2f} USDT",
            f"📋 Відкритих позицій: {len(open_pos)}",
        ]

        for p in open_pos:
            sym = p["symbol"]
            amt = float(p["positionAmt"])
            side_label = "LONG" if amt > 0 else "SHORT"
            pnl = float(p.get("unrealizedProfit", 0))
            lines.append(
                f"  • {sym} {side_label} | unrealPnL: {pnl:+.4f} USDT"
            )

        await self._reply(update, "\n".join(lines))

    async def _cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        summary = self.risk.get_daily_summary()
        pnl  = summary["pnl"]
        sign = "+" if pnl >= 0 else ""

        winrate = 0.0
        if summary["trades"] > 0:
            winrate = summary["win"] / summary["trades"] * 100

        text = (
            f"{config.BOT_PREFIX} 📅 <b>Статистика за сьогодні</b>\n"
            f"📆 Дата: {summary['date']}\n"
            f"💰 P&L: {sign}{pnl:.4f} USDT\n"
            f"📊 Угод: {summary['trades']} "
            f"(✅ {summary['win']} / ❌ {summary['loss']})\n"
            f"🎯 Winrate: {winrate:.1f}%"
        )
        await self._reply(update, text)

    async def _cmd_month(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Місячна статистика потребує окремого збереження - заглушка
        text = (
            f"{config.BOT_PREFIX} 📈 <b>Місячна статистика</b>\n"
            f"ℹ️ Детальна місячна статистика буде доступна у наступній версії.\n"
            f"Поки що доступна денна статистика через /s_today"
        )
        await self._reply(update, text)

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.risk.set_stopped(True)
        text = (
            f"{config.BOT_PREFIX} ⏸ <b>Торгівлю призупинено</b>\n"
            f"Нові позиції не відкриватимуться.\n"
            f"Відновити: /s_resume"
        )
        await self._reply(update, text)
        logger.info("Торгівлю призупинено через Telegram команду /s_pause")

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self.risk.set_stopped(False)
        text = (
            f"{config.BOT_PREFIX} ▶️ <b>Торгівлю відновлено</b>\n"
            f"Бот продовжує сканування."
        )
        await self._reply(update, text)
        logger.info("Торгівлю відновлено через Telegram команду /s_resume")

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            f"{config.BOT_PREFIX} 🛑 <b>Зупинка бота...</b>\n"
            f"Закриваю всі позиції та завершую роботу."
        )
        await self._reply(update, text)
        logger.info("Команда /s_stop отримана — ініціюємо graceful shutdown")
        self.risk.set_stopped(True)
        # Сигналізуємо main.py через флаг
        import main as main_module
        main_module.SHUTDOWN_REQUESTED = True
