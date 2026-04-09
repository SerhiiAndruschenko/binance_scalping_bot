from __future__ import annotations

import asyncio
import threading
from datetime import date, datetime, timezone
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
        self._app.add_handler(CommandHandler("s_start",  self._cmd_start))
        self._app.add_handler(CommandHandler("s_info",   self._cmd_info))
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

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            f"{config.BOT_PREFIX} 🤖 <b>Scalping Bot — команди</b>\n\n"
            "<b>📊 Інформація</b>\n"
            "/s_info   — баланс рахунку, PnL за сьогодні і місяць\n"
            "/s_status — стан бота, відкриті позиції\n"
            "/s_today  — статистика угод за сьогодні\n"
            "/s_month  — статистика за поточний місяць\n\n"
            "<b>⚙️ Управління</b>\n"
            "/s_pause  — зупинити нові угоди (поточні залишаються)\n"
            "/s_resume — відновити роботу після паузи\n"
            "/s_stop   — закрити всі позиції і зупинити бота\n\n"
            "<b>ℹ️ Поточні налаштування</b>\n"
            f"Пари: {', '.join(config.SYMBOLS)}\n"
            f"Таймфрейм: {config.TIMEFRAME} | Плече: x{config.LEVERAGE}\n"
            f"Ризик/угода: {config.RISK_PER_TRADE*100:.0f}% | "
            f"TP: +{config.TAKE_PROFIT_PCT*100:.1f}% | "
            f"SL: -{config.STOP_LOSS_PCT*100:.1f}%\n"
            f"Торговий баланс: {config.MAX_TRADING_BALANCE:.0f} USDT | "
            f"Денний ліміт: -{config.DAILY_LOSS_LIMIT*100:.0f}%"
        )
        await self._reply(update, text)

    async def _cmd_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Баланс рахунку + реалізований PnL за сьогодні і за поточний місяць."""
        available  = self.trader.binance.get_usdt_balance()
        wallet     = self.trader.binance.get_total_wallet_balance()
        unrealized = self.trader.binance.get_unrealized_pnl()

        # Часові межі для запиту до Binance
        now = datetime.now(tz=timezone.utc)

        # Сьогодні: з початку дня UTC
        today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        today_ms    = int(today_start.timestamp() * 1000)
        now_ms      = int(now.timestamp() * 1000)

        # Місяць: з першого числа поточного місяця UTC
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        month_ms    = int(month_start.timestamp() * 1000)

        today_pnl = self.trader.binance.get_income_history(today_ms, now_ms)
        month_pnl = self.trader.binance.get_income_history(month_ms, now_ms)

        def fmt(val: float) -> str:
            sign = "+" if val >= 0 else ""
            icon = "📈" if val >= 0 else "📉"
            return f"{icon} {sign}{val:.4f} USDT"

        mode = "🧪 TESTNET" if config.TESTNET else "🔴 LIVE"

        text = (
            f"{config.BOT_PREFIX} 💼 <b>Стан рахунку</b>\n"
            f"🌐 {mode}\n\n"
            f"<b>💰 Баланс</b>\n"
            f"Доступно:     <b>{available:,.2f} USDT</b>\n"
            f"Гаманець:     {wallet:,.2f} USDT\n"
            f"Нереаліз. PnL: {unrealized:+.4f} USDT\n\n"
            f"<b>📊 Реалізований PnL</b>\n"
            f"Сьогодні:  {fmt(today_pnl)}\n"
            f"Місяць:    {fmt(month_pnl)}\n\n"
            f"<i>Дані станом на {now.strftime('%d.%m.%Y %H:%M')} UTC</i>"
        )
        await self._reply(update, text)

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
