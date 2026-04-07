"""
Binance Scalping Bot — точка входу.

Цикл: кожні SCAN_INTERVAL (15 секунд) сканує SYMBOLS,
розраховує індикатори і виконує угоди.
"""

from __future__ import annotations

import signal
import sys
import time

from dotenv import load_dotenv
load_dotenv()  # завантажує .env перед усіма імпортами config

import config
from binance_client import BinanceClient
from logger import logger
from notifications import Notifier
from risk_manager import RiskManager
from strategy import ScalpStrategy
from trader import Trader

# Глобальний прапор для graceful shutdown (читається з telegram_bot.py)
SHUTDOWN_REQUESTED: bool = False


def _setup_signal_handlers() -> None:
    """SIGINT / SIGTERM — graceful shutdown."""

    def _handler(signum: int, frame: object) -> None:
        global SHUTDOWN_REQUESTED
        logger.info("Отримано сигнал %d — ініціюємо shutdown...", signum)
        SHUTDOWN_REQUESTED = True

    signal.signal(signal.SIGINT,  _handler)
    signal.signal(signal.SIGTERM, _handler)


def _print_startup_info(binance: BinanceClient) -> float:
    """Виводить стартову інформацію і повертає баланс."""
    balance = binance.get_usdt_balance()
    mode = "TESTNET" if config.TESTNET else "LIVE"

    logger.info("=" * 60)
    logger.info("  %s Scalping Bot стартує", config.BOT_PREFIX)
    logger.info("=" * 60)
    logger.info("  Режим:         %s", mode)
    logger.info("  Баланс:        %.2f USDT", balance)
    logger.info("  Макс баланс:   %.2f USDT", config.MAX_TRADING_BALANCE)
    logger.info("  Пари:          %s", ", ".join(config.SYMBOLS))
    logger.info("  Таймфрейм:     %s", config.TIMEFRAME)
    logger.info("  Інтервал:      %d сек", config.SCAN_INTERVAL)
    logger.info("  Плече:         x%d", config.LEVERAGE)
    logger.info("  TP / SL:       %.1f%% / %.1f%%",
                config.TAKE_PROFIT_PCT * 100, config.STOP_LOSS_PCT * 100)
    logger.info("  Ризик/угода:   %.1f%%", config.RISK_PER_TRADE * 100)
    logger.info("  Денний ліміт:  -%.1f%%", config.DAILY_LOSS_LIMIT * 100)
    logger.info("  Мах угод:      %d", config.MAX_OPEN_TRADES_GLOBAL)
    logger.info("  State file:    %s", config.STATE_FILE)
    logger.info("=" * 60)

    return balance


def _scan_symbol(
    symbol: str,
    strategy: ScalpStrategy,
    trader: Trader,
    risk: RiskManager,
) -> None:
    """Один цикл аналізу і торгівлі для одного символу."""

    result = strategy.analyze(symbol)

    # ── Verbose лог ──────────────────────────────────────────────────────────
    ema_arrow = "⬆️ UP" if result.ema_trend == "UP" else (
        "⬇️ DOWN" if result.ema_trend == "DOWN" else "➡️ FLAT"
    )
    imb_side = "BID" if result.imbalance >= 0.5 else "ASK"
    signal_label = f"→ {result.signal} ✅" if result.signal != "NONE" else "→ NONE ⏳"

    logger.info(
        "[%s] Ціна=%.2f | VWAP=%.2f | EMA9/21 %s | RSI=%.1f | "
        "Imbalance=%.0f%% %s | %s",
        symbol,
        result.price,
        result.vwap,
        ema_arrow,
        result.rsi,
        result.imbalance * 100,
        imb_side,
        signal_label,
    )

    # ── Торгівля ─────────────────────────────────────────────────────────────
    if result.signal != "NONE" and not risk.is_stopped:
        trader.open_position(symbol, result)


def main() -> None:
    global SHUTDOWN_REQUESTED

    _setup_signal_handlers()

    # ── Ініціалізація компонентів ─────────────────────────────────────────────
    logger.info("Ініціалізація компонентів...")

    binance  = BinanceClient()
    risk     = RiskManager(binance)
    notifier = Notifier()
    strategy = ScalpStrategy(binance)
    trader   = Trader(binance, risk, notifier)

    # ── Telegram command bot (опціонально) ────────────────────────────────────
    tg_bot = None
    if config.TELEGRAM_BOT_TOKEN:
        from telegram_bot import TelegramCommandBot
        tg_bot = TelegramCommandBot(risk, trader)
        tg_bot.start()

    # ── Стартова інформація ───────────────────────────────────────────────────
    balance = _print_startup_info(binance)

    # ── Reconcile при старті ──────────────────────────────────────────────────
    trader.reconcile_open_trades()

    # ── Сповіщення про старт ──────────────────────────────────────────────────
    notifier.notify_startup(balance, config.TESTNET, config.SYMBOLS)

    logger.info("Починаємо основний цикл (інтервал=%d сек)...", config.SCAN_INTERVAL)

    # ── Основний цикл ─────────────────────────────────────────────────────────
    while not SHUTDOWN_REQUESTED:
        cycle_start = time.monotonic()

        # 1) Моніторинг SL/TP
        try:
            trader.check_sl_tp_all()
        except Exception as exc:
            logger.error("Помилка check_sl_tp_all: %s", exc, exc_info=True)

        # 2) Перевірка денного ліміту
        try:
            if risk.check_daily_loss_limit():
                if not risk.is_stopped:
                    summary = risk.get_daily_summary()
                    notifier.notify_daily_loss_limit(
                        summary["pnl"],
                        config.DAILY_LOSS_LIMIT,
                    )
                    logger.warning(
                        "Торгівля зупинена: денний ліміт збитку"
                    )
        except Exception as exc:
            logger.error("Помилка перевірки ліміту: %s", exc, exc_info=True)

        # 3) Сканування символів
        if not risk.is_stopped:
            for symbol in config.SYMBOLS:
                if SHUTDOWN_REQUESTED:
                    break
                try:
                    _scan_symbol(symbol, strategy, trader, risk)
                except Exception as exc:
                    logger.error(
                        "[%s] Помилка сканування: %s", symbol, exc, exc_info=True
                    )

        # 4) Очікування до наступного циклу
        elapsed = time.monotonic() - cycle_start
        sleep_time = max(0.0, config.SCAN_INTERVAL - elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)

    # ── Graceful Shutdown ─────────────────────────────────────────────────────
    logger.info("Shutdown: закриваємо всі позиції...")
    try:
        trader.close_all_positions(reason="shutdown")
    except Exception as exc:
        logger.error("Помилка при закритті позицій: %s", exc, exc_info=True)

    notifier.notify_shutdown()

    if tg_bot:
        try:
            tg_bot.stop()
        except Exception:
            pass

    logger.info("Scalping Bot завершив роботу.")
    sys.exit(0)


if __name__ == "__main__":
    main()
