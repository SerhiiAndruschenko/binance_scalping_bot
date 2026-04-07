from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd
import pandas_ta as ta

import config
from binance_client import BinanceClient
from logger import logger


Signal = Literal["LONG", "SHORT", "NONE"]


@dataclass
class StrategyResult:
    signal: Signal
    price: float
    vwap: float
    ema_fast: float
    ema_slow: float
    rsi: float
    imbalance: float          # bid_volume / total (0.0 – 1.0)
    macd_diff: float
    ema_trend: str            # "UP" / "DOWN" / "FLAT"
    reason: str               # текстовий опис для логів


class ScalpStrategy:
    """
    Скальпінгова стратегія на 1m свічках.

    LONG:  EMA9 > EMA21  AND  price <= VWAP  AND  RSI in [35,65]
           AND  imbalance > ORDER_BOOK_IMBALANCE (bid сильніший)

    SHORT: EMA9 < EMA21  AND  price >= VWAP  AND  RSI in [35,65]
           AND  imbalance < (1 - ORDER_BOOK_IMBALANCE) (ask сильніший)

    VWAP розраховується за останні VWAP_PERIOD свічок:
        typical_price = (H + L + C) / 3
        vwap = sum(tp * vol) / sum(vol)
    """

    def __init__(self, binance: BinanceClient) -> None:
        self.binance = binance

    # ── Публічний інтерфейс ───────────────────────────────────────────────────

    def analyze(self, symbol: str) -> StrategyResult:
        """
        Аналізує символ і повертає StrategyResult з сигналом і всіма
        проміжними значеннями для логування.
        """
        df = self.binance.get_klines(symbol, config.TIMEFRAME, config.CANDLES_LIMIT)
        if df.empty or len(df) < config.EMA_SLOW + 5:
            return self._empty_result("Недостатньо свічок")

        df = self._calculate_indicators(df)

        # Останні значення
        last = df.iloc[-1]
        price    = float(last["close"])
        ema_fast = float(last["ema_fast"])
        ema_slow = float(last["ema_slow"])
        rsi      = float(last["rsi"])
        vwap     = float(last["vwap"])
        macd_diff = float(last["macd_diff"])

        # Order Book
        imbalance = self._get_order_book_imbalance(symbol)

        # Визначення тренду EMA
        if ema_fast > ema_slow:
            ema_trend = "UP"
        elif ema_fast < ema_slow:
            ema_trend = "DOWN"
        else:
            ema_trend = "FLAT"

        # ── LONG перевірка ────────────────────────────────────────────────────
        long_ema   = ema_fast > ema_slow
        long_vwap  = price <= vwap
        long_rsi   = config.RSI_LONG_MIN <= rsi <= config.RSI_LONG_MAX
        long_book  = imbalance > config.ORDER_BOOK_IMBALANCE

        if long_ema and long_vwap and long_rsi and long_book:
            reason = (
                f"EMA9({ema_fast:.2f})>EMA21({ema_slow:.2f}) | "
                f"Ціна({price:.2f})<=VWAP({vwap:.2f}) | "
                f"RSI={rsi:.1f} | Imbalance={imbalance*100:.1f}%BID"
            )
            return StrategyResult(
                signal="LONG", price=price, vwap=vwap,
                ema_fast=ema_fast, ema_slow=ema_slow,
                rsi=rsi, imbalance=imbalance,
                macd_diff=macd_diff, ema_trend=ema_trend,
                reason=reason,
            )

        # ── SHORT перевірка ───────────────────────────────────────────────────
        short_ema  = ema_fast < ema_slow
        short_vwap = price >= vwap
        short_rsi  = config.RSI_SHORT_MIN <= rsi <= config.RSI_SHORT_MAX
        short_book = imbalance < (1.0 - config.ORDER_BOOK_IMBALANCE)

        if short_ema and short_vwap and short_rsi and short_book:
            reason = (
                f"EMA9({ema_fast:.2f})<EMA21({ema_slow:.2f}) | "
                f"Ціна({price:.2f})>=VWAP({vwap:.2f}) | "
                f"RSI={rsi:.1f} | Imbalance={imbalance*100:.1f}%BID"
            )
            return StrategyResult(
                signal="SHORT", price=price, vwap=vwap,
                ema_fast=ema_fast, ema_slow=ema_slow,
                rsi=rsi, imbalance=imbalance,
                macd_diff=macd_diff, ema_trend=ema_trend,
                reason=reason,
            )

        # ── Немає сигналу ─────────────────────────────────────────────────────
        missing: list[str] = []
        if ema_trend == "UP":
            if not long_vwap:
                missing.append(f"Ціна({price:.2f})>VWAP({vwap:.2f})")
            if not long_rsi:
                missing.append(f"RSI={rsi:.1f} поза [35,65]")
            if not long_book:
                missing.append(f"Imbalance={imbalance*100:.1f}%<60%BID")
        elif ema_trend == "DOWN":
            if not short_vwap:
                missing.append(f"Ціна({price:.2f})<VWAP({vwap:.2f})")
            if not short_rsi:
                missing.append(f"RSI={rsi:.1f} поза [35,65]")
            if not short_book:
                missing.append(f"Imbalance={imbalance*100:.1f}%>40%BID")
        else:
            missing.append("EMA тренд невизначений (FLAT)")

        reason = "Немає сигналу: " + ", ".join(missing) if missing else "Умови не виконані"

        return StrategyResult(
            signal="NONE", price=price, vwap=vwap,
            ema_fast=ema_fast, ema_slow=ema_slow,
            rsi=rsi, imbalance=imbalance,
            macd_diff=macd_diff, ema_trend=ema_trend,
            reason=reason,
        )

    # ── Внутрішні методи ──────────────────────────────────────────────────────

    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Розраховує всі індикатори і додає їх до DataFrame."""

        # EMA
        df["ema_fast"] = ta.ema(df["close"], length=config.EMA_FAST)
        df["ema_slow"] = ta.ema(df["close"], length=config.EMA_SLOW)

        # RSI
        df["rsi"] = ta.rsi(df["close"], length=config.RSI_PERIOD)

        # VWAP (ковзний, за останні VWAP_PERIOD свічок)
        typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
        df["vwap"] = (
            (typical_price * df["volume"]).rolling(config.VWAP_PERIOD).sum()
            / df["volume"].rolling(config.VWAP_PERIOD).sum()
        )

        # MACD
        macd_result = ta.macd(
            df["close"],
            fast=config.MACD_FAST,
            slow=config.MACD_SLOW,
            signal=config.MACD_SIGNAL,
        )
        if macd_result is not None and not macd_result.empty:
            # pandas_ta повертає DataFrame: MACD_f_s_sig, MACDh_..., MACDs_...
            cols = macd_result.columns.tolist()
            macd_col    = [c for c in cols if c.startswith("MACD_")][0]
            signal_col  = [c for c in cols if c.startswith("MACDs_")][0]
            df["macd"]        = macd_result[macd_col]
            df["macd_signal"] = macd_result[signal_col]
            df["macd_diff"]   = df["macd"] - df["macd_signal"]
        else:
            df["macd_diff"] = 0.0

        return df

    def _get_order_book_imbalance(self, symbol: str) -> float:
        """
        Розраховує imbalance стакану:
        imbalance = bid_volume / (bid_volume + ask_volume)
        """
        ob = self.binance.get_order_book(symbol, limit=config.ORDER_BOOK_DEPTH)
        bids = ob.get("bids", [])
        asks = ob.get("asks", [])

        if not bids or not asks:
            return 0.5  # нейтральне значення при помилці

        bid_vol = sum(float(b[1]) for b in bids)
        ask_vol = sum(float(a[1]) for a in asks)
        total   = bid_vol + ask_vol

        if total == 0:
            return 0.5

        return bid_vol / total

    @staticmethod
    def _empty_result(reason: str) -> StrategyResult:
        return StrategyResult(
            signal="NONE",
            price=0.0, vwap=0.0,
            ema_fast=0.0, ema_slow=0.0,
            rsi=0.0, imbalance=0.5,
            macd_diff=0.0, ema_trend="FLAT",
            reason=reason,
        )
