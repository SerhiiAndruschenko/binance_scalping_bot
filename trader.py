from __future__ import annotations

from datetime import datetime
from typing import Optional

from binance.exceptions import BinanceAPIException

import config
from binance_client import BinanceClient
from logger import logger
from notifications import Notifier
from risk_manager import RiskManager, TradeRecord
from strategy import StrategyResult


class Trader:
    """
    Виконує відкриття і закриття позицій.

    Методи:
      open_position()        — відкриває нову позицію
      close_position()       — закриває позицію по символу
      close_all_positions()  — закриває всі відкриті позиції
      reconcile_open_trades()— синхронізує стан з біржею при старті
      check_sl_tp_all()      — soft-моніторинг SL/TP кожні 15 секунд
    """

    TAKER_FEE = config.TAKER_FEE

    def __init__(
        self,
        binance: BinanceClient,
        risk: RiskManager,
        notifier: Notifier,
    ) -> None:
        self.binance  = binance
        self.risk     = risk
        self.notifier = notifier

    # ── Відкриття позиції ─────────────────────────────────────────────────────

    def open_position(self, symbol: str, result: StrategyResult) -> bool:
        """
        Відкриває позицію якщо всі перевірки пройдено.
        Повертає True при успіху.
        """
        side = result.signal  # "LONG" | "SHORT"

        # 1) Захист від дублювання
        if self.risk.has_open_position(symbol):
            logger.debug("[%s] Позиція вже відкрита — пропускаємо", symbol)
            return False

        # 2) Глобальний ліміт одночасних угод
        if self.risk.count_open_trades() >= config.MAX_OPEN_TRADES_GLOBAL:
            logger.debug(
                "[%s] Досягнуто ліміт одночасних угод (%d)",
                symbol, config.MAX_OPEN_TRADES_GLOBAL,
            )
            return False

        # 3) Перевірка зупинки
        if self.risk.is_stopped:
            logger.debug("[%s] Торгівля зупинена — пропускаємо", symbol)
            return False

        # 4) Перевірка денного ліміту збитку
        if self.risk.check_daily_loss_limit():
            return False

        # 5) Розрахунок параметрів
        params = self.risk.calculate_trade_params(symbol, side, result.price)
        if params is None:
            return False

        # 6) Встановлення плеча і типу маржі
        self.binance.set_leverage(symbol, config.LEVERAGE)
        self.binance.set_margin_type(symbol, "ISOLATED")

        # 7) Розміщення ордеру
        order_side = "BUY" if side == "LONG" else "SELL"
        order = self.binance.place_market_order(
            symbol=symbol,
            side=order_side,
            quantity=params["quantity"],
        )
        if order is None:
            return False

        # 8) Реєстрація в ризик-менеджері
        record: TradeRecord = {
            "symbol":      symbol,
            "side":        side,
            "entry_price": result.price,
            "quantity":    params["quantity"],
            "leverage":    config.LEVERAGE,
            "tp_price":    params["tp_price"],
            "sl_price":    params["sl_price"],
            "open_time":   datetime.utcnow().isoformat(),
        }
        self.risk.register_trade(symbol, record)

        # 9) Сповіщення
        self.notifier.notify_open(
            symbol=symbol,
            side=side,
            price=result.price,
            vwap=result.vwap,
            imbalance=result.imbalance,
            tp_price=params["tp_price"],
            sl_price=params["sl_price"],
        )

        logger.info(
            "[%s] %s відкрито | ціна=%.4f qty=%.4f TP=%.4f SL=%.4f",
            symbol, side, result.price,
            params["quantity"], params["tp_price"], params["sl_price"],
        )
        return True

    # ── Закриття позиції ──────────────────────────────────────────────────────

    def close_position(
        self,
        symbol: str,
        exit_price: Optional[float] = None,
        reason: str = "manual",
    ) -> bool:
        """
        Закриває позицію для символу.
        Якщо exit_price не вказано — використовує поточну ціну.
        Повертає True при успіху.
        """
        record = self.risk.open_trades.get(symbol)
        if record is None:
            logger.warning("[%s] Немає запису угоди для закриття", symbol)
            return False

        side = record["side"]

        # Отримуємо поточну ціну якщо не передано
        if exit_price is None:
            exit_price = self.binance.get_current_price(symbol)
        if exit_price == 0:
            logger.error("[%s] Не вдалося отримати ціну для закриття", symbol)
            return False

        # Ринковий ордер на закриття
        close_side = "SELL" if side == "LONG" else "BUY"
        try:
            order = self.binance.place_market_order(
                symbol=symbol,
                side=close_side,
                quantity=record["quantity"],
                reduce_only=True,
            )
        except BinanceAPIException as exc:
            if exc.code == -2022:
                # Позиція вже закрита на біржі (наприклад спрацював біржовий SL/TP)
                # Прибираємо із стану і виходимо без сповіщення
                logger.warning(
                    "[%s] ReduceOnly відхилено — позиція вже закрита на біржі. "
                    "Видаляємо зі стану.",
                    symbol,
                )
                self.risk.remove_trade(symbol)
                return False
            raise

        if order is None:
            return False

        # Тривалість
        try:
            open_dt = datetime.fromisoformat(record["open_time"])
            duration_min = int((datetime.utcnow() - open_dt).total_seconds() / 60)
        except (ValueError, KeyError):
            duration_min = 0

        # P&L і закриття в ризик-менеджері
        pnl = self.risk.close_trade(symbol, exit_price, side)

        # Сповіщення
        self.notifier.notify_close(
            symbol=symbol,
            side=side,
            entry_price=record["entry_price"],
            exit_price=exit_price,
            pnl=pnl,
            duration_min=duration_min,
        )

        logger.info(
            "[%s] %s закрито | причина=%s вхід=%.4f вихід=%.4f PnL=%.4f USDT тривалість=%dхв",
            symbol, side, reason,
            record["entry_price"], exit_price, pnl, duration_min,
        )
        return True

    # ── Закрити всі позиції ───────────────────────────────────────────────────

    def close_all_positions(self, reason: str = "shutdown") -> None:
        """Закриває всі відкриті позиції (graceful shutdown або ліміт)."""
        open_positions = self.binance.get_open_positions()

        # Фільтруємо нульові позиції (мають бути вже відфільтровані, але для надійності)
        open_positions = [p for p in open_positions if float(p.get("positionAmt", 0)) != 0]

        if not open_positions:
            logger.info("Немає відкритих позицій для закриття")
            return

        for pos in open_positions:
            sym = pos["symbol"]
            pos_amt = float(pos["positionAmt"])
            side = "LONG" if pos_amt > 0 else "SHORT"
            exit_price = self.binance.get_current_price(sym)

            close_side = "SELL" if side == "LONG" else "BUY"
            qty = abs(pos_amt)

            order = self.binance.place_market_order(
                symbol=sym,
                side=close_side,
                quantity=qty,
                reduce_only=True,
            )
            if order:
                logger.info(
                    "[%s] Позицію закрито | причина=%s", sym, reason
                )
                # Якщо є запис — закриваємо через ризик-менеджер
                if self.risk.has_open_position(sym):
                    self.risk.close_trade(sym, exit_price, side)
            else:
                logger.error("[%s] Помилка закриття позиції", sym)

    # ── Моніторинг SL/TP ──────────────────────────────────────────────────────

    def check_sl_tp_all(self) -> None:
        """
        Soft-моніторинг: перевіряє SL/TP для кожної відкритої угоди.
        Викликається кожні 15 секунд у main loop.
        P&L розраховується з урахуванням плеча.
        """
        if not self.risk.open_trades:
            return

        for symbol, record in list(self.risk.open_trades.items()):
            price = self.binance.get_current_price(symbol)
            if price == 0:
                continue

            side     = record["side"]
            tp_price = record["tp_price"]
            sl_price = record["sl_price"]

            hit: Optional[str] = None

            if side == "LONG":
                if price >= tp_price:
                    hit = "TP"
                elif price <= sl_price:
                    hit = "SL"
            else:  # SHORT
                if price <= tp_price:
                    hit = "TP"
                elif price >= sl_price:
                    hit = "SL"

            if hit:
                logger.info(
                    "[%s] %s спрацював при ціні=%.4f (target=%.4f)",
                    symbol, hit, price,
                    tp_price if hit == "TP" else sl_price,
                )
                self.close_position(symbol, exit_price=price, reason=hit)

    # ── Reconcile при старті ──────────────────────────────────────────────────

    def reconcile_open_trades(self) -> None:
        """
        Синхронізує open_trades з реальними позиціями на біржі при старті.
        Використовує updateTime з біржі для визначення часу входу.
        """
        logger.info("Reconcile: синхронізація позицій з біржею...")

        # Реальні позиції на біржі (positionAmt != 0)
        real_positions = {
            p["symbol"]: p
            for p in self.binance.get_open_positions()
        }

        # 1) Видаляємо угоди яких немає на біржі
        for sym in list(self.risk.open_trades.keys()):
            if sym not in real_positions:
                logger.warning(
                    "[%s] Угода в стані але немає на біржі — видаляємо",
                    sym,
                )
                self.risk.remove_trade(sym)

        # 2) Додаємо позиції які є на біржі але немає в стані
        for sym, pos in real_positions.items():
            if not self.risk.has_open_position(sym):
                pos_amt   = float(pos["positionAmt"])
                side      = "LONG" if pos_amt > 0 else "SHORT"
                entry_px  = float(pos.get("entryPrice", 0))
                qty       = abs(pos_amt)

                # Визначаємо TP/SL від поточної ціни входу
                if side == "LONG":
                    tp = round(entry_px * (1 + config.TAKE_PROFIT_PCT), 2)
                    sl = round(entry_px * (1 - config.STOP_LOSS_PCT), 2)
                else:
                    tp = round(entry_px * (1 - config.TAKE_PROFIT_PCT), 2)
                    sl = round(entry_px * (1 + config.STOP_LOSS_PCT), 2)

                # updateTime з біржі
                update_ts = int(pos.get("updateTime", 0))
                if update_ts:
                    open_time = datetime.utcfromtimestamp(update_ts / 1000).isoformat()
                else:
                    open_time = datetime.utcnow().isoformat()

                record: TradeRecord = {
                    "symbol":      sym,
                    "side":        side,
                    "entry_price": entry_px,
                    "quantity":    qty,
                    "leverage":    config.LEVERAGE,
                    "tp_price":    tp,
                    "sl_price":    sl,
                    "open_time":   open_time,
                }
                self.risk.open_trades[sym] = record
                logger.info(
                    "[%s] Reconcile: додано з біржі | %s %.4f @ %.4f",
                    sym, side, qty, entry_px,
                )

        self.risk._save_state()
        logger.info(
            "Reconcile завершено: %d активних угод",
            self.risk.count_open_trades(),
        )
