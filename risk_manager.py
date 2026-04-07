from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import TypedDict

import config
from binance_client import BinanceClient
from logger import logger


class TradeRecord(TypedDict):
    symbol: str
    side: str          # "LONG" | "SHORT"
    entry_price: float
    quantity: float
    leverage: int
    tp_price: float
    sl_price: float
    open_time: str     # ISO-формат


class TradeParams(TypedDict):
    quantity: float
    tp_price: float
    sl_price: float
    notional: float


class RiskManager:
    """
    Управління ризиками для скальпінгового бота.

    Функції:
      - calculate_trade_params()  — розмір позиції від 1% балансу
      - check_daily_loss_limit()  — зупинка при -3%
      - has_open_position()       — захист від дублювання
      - _save_state() / _load_state() — персистентність через scalp_state.json
    """

    STATE_FILE = Path(config.STATE_FILE)

    def __init__(self, binance: BinanceClient) -> None:
        self.binance = binance

        # Відкриті позиції в памʼяті: symbol → TradeRecord
        self.open_trades: dict[str, TradeRecord] = {}

        # Щоденна статистика
        self.daily_pnl: float      = 0.0
        self.daily_date: str       = str(date.today())
        self.daily_trades: int     = 0
        self.daily_win: int        = 0
        self.daily_loss: int       = 0

        # Стартовий баланс для розрахунку денного ліміту
        self._start_of_day_balance: float = 0.0

        # Прапор зупинки (зберігається в state)
        self._is_stopped: bool = False

        self._load_state()

    # ── Публічний інтерфейс ───────────────────────────────────────────────────

    def calculate_trade_params(
        self, symbol: str, side: str, price: float
    ) -> TradeParams | None:
        """
        Розраховує параметри угоди:
          - quantity на основі 1% балансу і плеча
          - tp_price і sl_price
        """
        balance = min(
            self.binance.get_usdt_balance(),
            config.MAX_TRADING_BALANCE,
        )
        if balance <= 0:
            logger.warning("[%s] Баланс нульовий або недоступний", symbol)
            return None

        risk_usdt = balance * config.RISK_PER_TRADE
        notional  = risk_usdt * config.LEVERAGE

        # Binance Futures вимагає мінімум MIN_NOTIONAL (100 USDT)
        # Якщо розрахований notional менший — підтягуємо до мінімуму
        if notional < config.MIN_NOTIONAL:
            logger.info(
                "[%s] Notional %.2f USDT < мінімум %.2f USDT → використовуємо мінімум",
                symbol, notional, config.MIN_NOTIONAL,
            )
            notional = config.MIN_NOTIONAL

        # Не перевищуємо доступний баланс з урахуванням плеча
        max_notional = balance * config.LEVERAGE
        if notional > max_notional:
            logger.warning(
                "[%s] Notional %.2f USDT перевищує макс %.2f USDT — пропускаємо",
                symbol, notional, max_notional,
            )
            return None

        qty_raw   = notional / price
        precision = self.binance.get_quantity_precision(symbol)
        step      = 1 / 10**precision  # мінімальний крок кількості
        quantity  = math.floor(qty_raw * 10**precision) / 10**precision

        # Після floor-округлення реальний notional може впасти нижче мінімуму.
        # Приклад: 100 / 68611 = 0.001458 → floor → 0.001 → 0.001*68611 = 68.6 USDT
        # Додаємо один крок щоб реальний notional >= MIN_NOTIONAL
        actual_notional = quantity * price
        if actual_notional < config.MIN_NOTIONAL:
            quantity = round(quantity + step, precision)
            logger.info(
                "[%s] Після округлення notional=%.2f USDT — додаємо крок → qty=%.{p}f (notional=%.2f USDT)".format(p=precision),
                symbol, actual_notional, quantity, quantity * price,
            )

        if quantity <= 0:
            logger.warning(
                "[%s] Кількість = 0 після округлення (balance=%.2f, price=%.2f)",
                symbol, balance, price,
            )
            return None

        if side == "LONG":
            tp_price = round(price * (1 + config.TAKE_PROFIT_PCT), 2)
            sl_price = round(price * (1 - config.STOP_LOSS_PCT), 2)
        else:
            tp_price = round(price * (1 - config.TAKE_PROFIT_PCT), 2)
            sl_price = round(price * (1 + config.STOP_LOSS_PCT), 2)

        return TradeParams(
            quantity=quantity,
            tp_price=tp_price,
            sl_price=sl_price,
            notional=notional,
        )

    def check_daily_loss_limit(self) -> bool:
        """
        Перевіряє чи досягнуто щоденний ліміт збитку.
        Повертає True якщо торгівлю треба зупинити.
        """
        if self._is_stopped:
            return True

        self._refresh_daily_date()

        if self._start_of_day_balance <= 0:
            self._start_of_day_balance = self.binance.get_total_wallet_balance()
            self._save_state()
            return False

        current_balance = self.binance.get_total_wallet_balance()
        pnl_pct = (current_balance - self._start_of_day_balance) / self._start_of_day_balance

        if pnl_pct <= -config.DAILY_LOSS_LIMIT:
            logger.warning(
                "Денний ліміт збитку досягнуто: %.2f%% (ліміт: %.2f%%)",
                pnl_pct * 100, config.DAILY_LOSS_LIMIT * 100,
            )
            self._is_stopped = True
            self._save_state()
            return True

        return False

    def has_open_position(self, symbol: str) -> bool:
        """Перевіряє чи є відкрита позиція для символу."""
        return symbol in self.open_trades

    def count_open_trades(self) -> int:
        """Повертає кількість відкритих угод."""
        return len(self.open_trades)

    def register_trade(self, symbol: str, record: TradeRecord) -> None:
        """Реєструє нову відкриту угоду."""
        self.open_trades[symbol] = record
        self.daily_trades += 1
        self._save_state()

    def close_trade(self, symbol: str, exit_price: float, side: str) -> float:
        """
        Закриває угоду і повертає P&L в USDT (після комісії).
        P&L розраховується з урахуванням плеча.
        """
        record = self.open_trades.pop(symbol, None)
        if record is None:
            return 0.0

        entry = record["entry_price"]
        qty   = record["quantity"]
        lev   = record["leverage"]

        if side == "LONG":
            raw_pnl = (exit_price - entry) * qty * lev
        else:
            raw_pnl = (entry - exit_price) * qty * lev

        # Комісія (два боки: відкриття + закриття)
        commission = entry * qty * config.TAKER_FEE + exit_price * qty * config.TAKER_FEE
        net_pnl = raw_pnl - commission

        self.daily_pnl += net_pnl
        if net_pnl >= 0:
            self.daily_win += 1
        else:
            self.daily_loss += 1

        self._save_state()
        return net_pnl

    def remove_trade(self, symbol: str) -> None:
        """Видаляє угоду без розрахунку P&L (для reconcile)."""
        self.open_trades.pop(symbol, None)
        self._save_state()

    def set_stopped(self, stopped: bool) -> None:
        self._is_stopped = stopped
        self._save_state()

    @property
    def is_stopped(self) -> bool:
        return self._is_stopped

    def get_daily_summary(self) -> dict:
        return {
            "date":        self.daily_date,
            "pnl":         round(self.daily_pnl, 4),
            "trades":      self.daily_trades,
            "win":         self.daily_win,
            "loss":        self.daily_loss,
            "open_trades": list(self.open_trades.keys()),
        }

    # ── Персистентність ───────────────────────────────────────────────────────

    def _save_state(self) -> None:
        state = {
            "open_trades":            self.open_trades,
            "daily_pnl":              self.daily_pnl,
            "daily_date":             self.daily_date,
            "daily_trades":           self.daily_trades,
            "daily_win":              self.daily_win,
            "daily_loss":             self.daily_loss,
            "start_of_day_balance":   self._start_of_day_balance,
            "is_stopped":             self._is_stopped,
        }
        try:
            self.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(self.STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.error("Помилка збереження стану: %s", exc)

    def _load_state(self) -> None:
        if not self.STATE_FILE.exists():
            logger.info("scalp_state.json не знайдено — починаємо з нуля")
            self._start_of_day_balance = self.binance.get_total_wallet_balance()
            self._save_state()
            return

        try:
            with open(self.STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)

            self.open_trades              = state.get("open_trades", {})
            self.daily_pnl               = state.get("daily_pnl", 0.0)
            self.daily_date              = state.get("daily_date", str(date.today()))
            self.daily_trades            = state.get("daily_trades", 0)
            self.daily_win               = state.get("daily_win", 0)
            self.daily_loss              = state.get("daily_loss", 0)
            self._start_of_day_balance   = state.get("start_of_day_balance", 0.0)
            self._is_stopped             = state.get("is_stopped", False)

            logger.info(
                "Стан завантажено: відкритих угод=%d | денний PnL=%.4f USDT | stopped=%s",
                len(self.open_trades), self.daily_pnl, self._is_stopped,
            )
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            logger.error("Помилка читання стану: %s — скидаємо", exc)
            self._start_of_day_balance = self.binance.get_total_wallet_balance()
            self._save_state()

    def _refresh_daily_date(self) -> None:
        """Скидає денну статистику якщо новий день."""
        today = str(date.today())
        if self.daily_date != today:
            logger.info("Новий день (%s) — скидаємо денну статистику", today)
            self.daily_pnl    = 0.0
            self.daily_date   = today
            self.daily_trades = 0
            self.daily_win    = 0
            self.daily_loss   = 0
            self._start_of_day_balance = self.binance.get_total_wallet_balance()
            self._is_stopped  = False
            self._save_state()
