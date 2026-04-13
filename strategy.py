"""
strategy.py — логіка торгових сигналів скальпінгового бота.

7 умов для LONG / SHORT входу:
  1. EMA9 vs EMA21      — мікротренд на 1m
  2. trend_1h           — макротренд на 1h (EMA21 vs EMA50)  ← трендовий фільтр
  3. Ціна vs VWAP       — позиція відносно рівня VWAP
  4. RSI [35, 65]       — нейтральна зона, не перекуп/перепродано
  5. Order Book ≥ 0.70  — жорсткий фільтр стакану
  6. |MACD − signal| ≥ MACD_MIN_DIFF  — підтвердження імпульсу
  7. ATR ratio ≥ ATR_MIN_MULTIPLIER   — фільтр флету

Модуль data_collector вбудовано сюди щоб не залежати від окремого файлу.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

import pandas as pd
import pandas_ta as ta

import config
from binance_client import BinanceClient
from logger import logger


# ═══════════════════════════════════════════════════════════════════════════════
# Вбудований data_collector
# ═══════════════════════════════════════════════════════════════════════════════

TrendDirection = str  # "UP" | "DOWN" | "NEUTRAL"


class MarketData(TypedDict):
    trend_1h:    TrendDirection
    atr_current: float
    atr_avg:     float
    atr_ratio:   float


def get_trend_direction(symbol: str, binance: BinanceClient) -> TrendDirection:
    """
    Макротренд на TREND_TIMEFRAME (1h):
      EMA21 > EMA50 → "UP" | EMA21 < EMA50 → "DOWN" | інше → "NEUTRAL"
    """
    try:
        df = binance.get_klines(
            symbol, config.TREND_TIMEFRAME, config.TREND_CANDLES
        )
        if df.empty or len(df) < config.TREND_EMA_SLOW + 5:
            logger.warning(
                "[%s] Недостатньо 1h свічок для тренд-EMA (%d)",
                symbol, len(df),
            )
            return "NEUTRAL"

        ema_fast = ta.ema(df["close"], length=config.TREND_EMA_FAST)
        ema_slow = ta.ema(df["close"], length=config.TREND_EMA_SLOW)

        if ema_fast is None or ema_slow is None:
            return "NEUTRAL"

        last_fast = float(ema_fast.iloc[-1])
        last_slow = float(ema_slow.iloc[-1])

        if pd.isna(last_fast) or pd.isna(last_slow):
            return "NEUTRAL"

        if last_fast > last_slow:
            return "UP"
        elif last_fast < last_slow:
            return "DOWN"
        return "NEUTRAL"

    except Exception as exc:
        logger.error("[%s] Помилка get_trend_direction: %s", symbol, exc)
        return "NEUTRAL"


def _get_atr_metrics(
    symbol: str,
    binance: BinanceClient,
    df: pd.DataFrame | None = None,
) -> tuple[float, float, float]:
    """Повертає (atr_current, atr_avg, atr_ratio)."""
    try:
        if df is None or df.empty:
            df = binance.get_klines(symbol, config.TIMEFRAME, config.CANDLES_LIMIT)
        if df.empty or len(df) < config.ATR_PERIOD + config.ATR_AVG_PERIOD + 5:
            return 0.0, 0.0, 1.0

        atr_series = ta.atr(
            df["high"], df["low"], df["close"], length=config.ATR_PERIOD
        )
        if atr_series is None or atr_series.dropna().empty:
            return 0.0, 0.0, 1.0

        atr_vals    = atr_series.dropna()
        atr_current = float(atr_vals.iloc[-1])
        atr_avg     = float(atr_vals.iloc[-config.ATR_AVG_PERIOD:].mean())
        atr_ratio   = atr_current / atr_avg if atr_avg else 1.0
        return atr_current, atr_avg, atr_ratio

    except Exception as exc:
        logger.error("[%s] Помилка _get_atr_metrics: %s", symbol, exc)
        return 0.0, 0.0, 1.0


def collect_market_data(symbol: str, binance: BinanceClient) -> MarketData:
    """Агрегує trend_1h і ATR-метрики для strategy.analyze()."""
    trend_1h = get_trend_direction(symbol, binance)
    atr_current, atr_avg, atr_ratio = _get_atr_metrics(symbol, binance)
    return MarketData(
        trend_1h=trend_1h,
        atr_current=atr_current,
        atr_avg=atr_avg,
        atr_ratio=atr_ratio,
    )


Signal = Literal["LONG", "SHORT", "NONE"]


@dataclass
class StrategyResult:
    signal:      Signal
    price:       float
    vwap:        float
    ema_fast:    float
    ema_slow:    float
    rsi:         float
    imbalance:   float      # bid / total (0.0 – 1.0)
    macd_diff:   float
    ema_trend:   str        # "UP" / "DOWN" / "FLAT"
    trend_1h:    str        # "UP" / "DOWN" / "NEUTRAL"
    atr_ratio:   float      # поточний ATR / середній ATR
    atr_current: float
    reason:      str        # текстовий опис для логів


class ScalpStrategy:
    """
    Скальпінгова стратегія з трендовим фільтром (1h) і фільтром флету (ATR).

    LONG:  EMA9>EMA21  AND  trend_1h==UP  AND  price≤VWAP
           AND  RSI∈[35,65]  AND  imbalance≥0.70
           AND  |macd−signal|≥MIN_DIFF  AND  atr_ratio≥0.8

    SHORT: EMA9<EMA21  AND  trend_1h==DOWN  AND  price≥VWAP
           AND  RSI∈[35,65]  AND  imbalance≤0.30
           AND  |macd−signal|≥MIN_DIFF  AND  atr_ratio≥0.8
    """

    def __init__(self, binance: BinanceClient) -> None:
        self.binance = binance

    # ── Публічний інтерфейс ───────────────────────────────────────────────────

    def analyze(self, symbol: str) -> StrategyResult:
        """
        Повний аналіз символу:
          1. Збирає додаткові ринкові дані (trend_1h, ATR)
          2. Завантажує 1m свічки і рахує індикатори
          3. Перевіряє 7 умов входу
          4. Повертає StrategyResult
        """
        # ── Крок 1: макродані (trend_1h, ATR) ────────────────────────────────
        market: MarketData = collect_market_data(symbol, self.binance)
        trend_1h  = market["trend_1h"]
        atr_ratio = market["atr_ratio"]
        atr_cur   = market["atr_current"]

        # Якщо тренд невизначений — не торгуємо взагалі
        if trend_1h == "NEUTRAL":
            logger.info("[%s] Тренд невизначений — пропускаємо", symbol)
            return self._empty_result("Тренд NEUTRAL — не торгуємо", trend_1h, atr_ratio, atr_cur)

        # ── Крок 2: 1m свічки і індикатори ───────────────────────────────────
        df = self.binance.get_klines(symbol, config.TIMEFRAME, config.CANDLES_LIMIT)
        if df.empty or len(df) < config.EMA_SLOW + 5:
            return self._empty_result("Недостатньо свічок", trend_1h, atr_ratio, atr_cur)

        df = self._calculate_indicators(df)
        last = df.iloc[-1]

        price     = float(last["close"])
        ema_fast  = float(last["ema_fast"])
        ema_slow  = float(last["ema_slow"])
        rsi       = float(last["rsi"])
        vwap      = float(last["vwap"])
        macd_diff = float(last["macd_diff"])

        # Order Book imbalance
        imbalance = self._get_order_book_imbalance(symbol)

        # EMA мікротренд
        if ema_fast > ema_slow:
            ema_trend = "UP"
        elif ema_fast < ema_slow:
            ema_trend = "DOWN"
        else:
            ema_trend = "FLAT"

        # ── Крок 3: перевірка умов ────────────────────────────────────────────

        # Спільні фільтри (умови 4, 6, 7)
        cond_rsi  = config.RSI_LONG_MIN <= rsi <= config.RSI_LONG_MAX
        cond_macd = abs(macd_diff) >= config.MACD_MIN_DIFF
        cond_atr  = atr_ratio >= config.ATR_MIN_MULTIPLIER

        # ── LONG ──────────────────────────────────────────────────────────────
        long_ema    = ema_fast > ema_slow              # умова 1
        long_trend  = trend_1h == "UP"                 # умова 2
        long_vwap   = price <= vwap                    # умова 3
        long_book   = imbalance >= config.ORDER_BOOK_IMBALANCE  # умова 5

        if long_ema and long_trend and long_vwap and cond_rsi and long_book and cond_macd and cond_atr:
            reason = (
                f"EMA9({ema_fast:.2f})>EMA21({ema_slow:.2f}) | "
                f"Trend1h=UP | Ціна({price:.2f})≤VWAP({vwap:.2f}) | "
                f"RSI={rsi:.1f} | Imbalance={imbalance*100:.0f}%BID | "
                f"MACD_diff={macd_diff:.4f} | ATR_ratio={atr_ratio:.2f}"
            )
            return StrategyResult(
                signal="LONG", price=price, vwap=vwap,
                ema_fast=ema_fast, ema_slow=ema_slow,
                rsi=rsi, imbalance=imbalance,
                macd_diff=macd_diff, ema_trend=ema_trend,
                trend_1h=trend_1h, atr_ratio=atr_ratio, atr_current=atr_cur,
                reason=reason,
            )

        # ── SHORT ─────────────────────────────────────────────────────────────
        short_ema   = ema_fast < ema_slow                          # умова 1
        short_trend = trend_1h == "DOWN"                           # умова 2
        short_vwap  = price >= vwap                                # умова 3
        short_book  = imbalance <= (1.0 - config.ORDER_BOOK_IMBALANCE)  # умова 5

        if short_ema and short_trend and short_vwap and cond_rsi and short_book and cond_macd and cond_atr:
            reason = (
                f"EMA9({ema_fast:.2f})<EMA21({ema_slow:.2f}) | "
                f"Trend1h=DOWN | Ціна({price:.2f})≥VWAP({vwap:.2f}) | "
                f"RSI={rsi:.1f} | Imbalance={imbalance*100:.0f}%BID | "
                f"MACD_diff={macd_diff:.4f} | ATR_ratio={atr_ratio:.2f}"
            )
            return StrategyResult(
                signal="SHORT", price=price, vwap=vwap,
                ema_fast=ema_fast, ema_slow=ema_slow,
                rsi=rsi, imbalance=imbalance,
                macd_diff=macd_diff, ema_trend=ema_trend,
                trend_1h=trend_1h, atr_ratio=atr_ratio, atr_current=atr_cur,
                reason=reason,
            )

        # ── Причина відсутності сигналу ───────────────────────────────────────
        reason = self._build_no_signal_reason(
            ema_trend, trend_1h,
            price, vwap, rsi, imbalance, macd_diff, atr_ratio,
        )
        return StrategyResult(
            signal="NONE", price=price, vwap=vwap,
            ema_fast=ema_fast, ema_slow=ema_slow,
            rsi=rsi, imbalance=imbalance,
            macd_diff=macd_diff, ema_trend=ema_trend,
            trend_1h=trend_1h, atr_ratio=atr_ratio, atr_current=atr_cur,
            reason=reason,
        )

    # ── Внутрішні методи ──────────────────────────────────────────────────────

    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Розраховує EMA, RSI, VWAP, MACD на 1m свічках."""

        # EMA мікротренд
        df["ema_fast"] = ta.ema(df["close"], length=config.EMA_FAST)
        df["ema_slow"] = ta.ema(df["close"], length=config.EMA_SLOW)

        # RSI
        df["rsi"] = ta.rsi(df["close"], length=config.RSI_PERIOD)

        # VWAP (ковзний за VWAP_PERIOD свічок)
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
            cols        = macd_result.columns.tolist()
            macd_col    = next(c for c in cols if c.startswith("MACD_"))
            signal_col  = next(c for c in cols if c.startswith("MACDs_"))
            df["macd"]        = macd_result[macd_col]
            df["macd_signal"] = macd_result[signal_col]
            df["macd_diff"]   = df["macd"] - df["macd_signal"]
        else:
            df["macd_diff"] = 0.0

        return df

    def _get_order_book_imbalance(self, symbol: str) -> float:
        """bid_volume / (bid_volume + ask_volume)"""
        ob      = self.binance.get_order_book(symbol, limit=config.ORDER_BOOK_DEPTH)
        bids    = ob.get("bids", [])
        asks    = ob.get("asks", [])
        if not bids or not asks:
            return 0.5
        bid_vol = sum(float(b[1]) for b in bids)
        ask_vol = sum(float(a[1]) for a in asks)
        total   = bid_vol + ask_vol
        return bid_vol / total if total else 0.5

    def _build_no_signal_reason(
        self,
        ema_trend: str, trend_1h: str,
        price: float, vwap: float,
        rsi: float, imbalance: float,
        macd_diff: float, atr_ratio: float,
    ) -> str:
        """Збирає список причин чому сигнал не виник (для логів)."""
        missing: list[str] = []

        # Трендовий фільтр — найважливіший
        if ema_trend == "UP" and trend_1h != "UP":
            missing.append(f"Trend1h={trend_1h} (потрібен UP)")
        elif ema_trend == "DOWN" and trend_1h != "DOWN":
            missing.append(f"Trend1h={trend_1h} (потрібен DOWN)")

        # ATR фільтр
        if atr_ratio < config.ATR_MIN_MULTIPLIER:
            missing.append(f"ATR_ratio={atr_ratio:.2f}<{config.ATR_MIN_MULTIPLIER} (флет)")

        # MACD фільтр
        if abs(macd_diff) < config.MACD_MIN_DIFF:
            missing.append(f"|MACD_diff|={abs(macd_diff):.4f}<{config.MACD_MIN_DIFF}")

        # VWAP
        if ema_trend == "UP" and price > vwap:
            missing.append(f"Ціна({price:.2f})>VWAP({vwap:.2f})")
        elif ema_trend == "DOWN" and price < vwap:
            missing.append(f"Ціна({price:.2f})<VWAP({vwap:.2f})")

        # RSI
        if not (config.RSI_LONG_MIN <= rsi <= config.RSI_LONG_MAX):
            missing.append(f"RSI={rsi:.1f} поза [35,65]")

        # Order book
        if ema_trend == "UP" and imbalance < config.ORDER_BOOK_IMBALANCE:
            missing.append(f"Imbalance={imbalance*100:.0f}%<{config.ORDER_BOOK_IMBALANCE*100:.0f}%BID")
        elif ema_trend == "DOWN" and imbalance > (1 - config.ORDER_BOOK_IMBALANCE):
            missing.append(f"Imbalance={imbalance*100:.0f}%>{(1-config.ORDER_BOOK_IMBALANCE)*100:.0f}%BID")

        if ema_trend == "FLAT":
            missing.append("EMA тренд FLAT")

        return "Немає сигналу: " + " | ".join(missing) if missing else "Умови не виконані"

    @staticmethod
    def _empty_result(
        reason: str,
        trend_1h: str = "NEUTRAL",
        atr_ratio: float = 1.0,
        atr_current: float = 0.0,
    ) -> StrategyResult:
        return StrategyResult(
            signal="NONE",
            price=0.0, vwap=0.0,
            ema_fast=0.0, ema_slow=0.0,
            rsi=0.0, imbalance=0.5,
            macd_diff=0.0, ema_trend="FLAT",
            trend_1h=trend_1h,
            atr_ratio=atr_ratio,
            atr_current=atr_current,
            reason=reason,
        )
