"""
data_collector.py — збір та підготовка ринкових даних.

Відповідає за:
  - get_trend_direction()  — макротренд на 1h (EMA21 vs EMA50)
  - collect_market_data()  — агрегована структура для strategy.py
"""

from __future__ import annotations

from typing import TypedDict

import pandas as pd
import pandas_ta as ta

import config
from binance_client import BinanceClient
from logger import logger


# ── Типи ──────────────────────────────────────────────────────────────────────

TrendDirection = str  # "UP" | "DOWN" | "NEUTRAL"


class MarketData(TypedDict):
    trend_1h:    TrendDirection
    atr_current: float
    atr_avg:     float
    atr_ratio:   float          # atr_current / atr_avg


# ── Публічні функції ──────────────────────────────────────────────────────────

def get_trend_direction(symbol: str, binance: BinanceClient) -> TrendDirection:
    """
    Визначає макротренд на TREND_TIMEFRAME (1h):
      - EMA21 > EMA50  →  "UP"
      - EMA21 < EMA50  →  "DOWN"
      - Помилка / рівні  →  "NEUTRAL"
    """
    try:
        df = binance.get_klines(
            symbol,
            config.TREND_TIMEFRAME,
            config.TREND_CANDLES,
        )
        if df.empty or len(df) < config.TREND_EMA_SLOW + 5:
            logger.warning(
                "[%s] Недостатньо свічок для тренд-EMA (%d/%d)",
                symbol, len(df), config.TREND_EMA_SLOW + 5,
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
        else:
            return "NEUTRAL"

    except Exception as exc:
        logger.error("[%s] Помилка get_trend_direction: %s", symbol, exc)
        return "NEUTRAL"


def collect_market_data(symbol: str, binance: BinanceClient) -> MarketData:
    """
    Збирає додаткові ринкові дані для стратегії:
      trend_1h    — макротренд (UP / DOWN / NEUTRAL)
      atr_current — поточний ATR (останнє значення)
      atr_avg     — середній ATR за ATR_AVG_PERIOD свічок
      atr_ratio   — atr_current / atr_avg (< 1.0 = флет)
    """
    trend_1h = get_trend_direction(symbol, binance)
    atr_current, atr_avg, atr_ratio = _get_atr_metrics(symbol, binance)

    return MarketData(
        trend_1h=trend_1h,
        atr_current=atr_current,
        atr_avg=atr_avg,
        atr_ratio=atr_ratio,
    )


# ── Внутрішні функції ─────────────────────────────────────────────────────────

def _get_atr_metrics(
    symbol: str,
    binance: BinanceClient,
    df: pd.DataFrame | None = None,
) -> tuple[float, float, float]:
    """
    Повертає (atr_current, atr_avg, atr_ratio).

    atr_current — останнє значення ATR(14) на 1m
    atr_avg     — середнє ATR за останні ATR_AVG_PERIOD свічок
    atr_ratio   — atr_current / atr_avg

    Якщо df не передано — завантажує свічки самостійно.
    """
    try:
        if df is None or df.empty:
            df = binance.get_klines(
                symbol, config.TIMEFRAME, config.CANDLES_LIMIT
            )
        if df.empty or len(df) < config.ATR_PERIOD + config.ATR_AVG_PERIOD + 5:
            return 0.0, 0.0, 1.0  # нейтральне значення при помилці

        atr_series = ta.atr(
            df["high"], df["low"], df["close"],
            length=config.ATR_PERIOD,
        )
        if atr_series is None or atr_series.dropna().empty:
            return 0.0, 0.0, 1.0

        atr_vals     = atr_series.dropna()
        atr_current  = float(atr_vals.iloc[-1])
        atr_avg      = float(atr_vals.iloc[-config.ATR_AVG_PERIOD:].mean())

        if atr_avg == 0:
            return atr_current, atr_avg, 1.0

        atr_ratio = atr_current / atr_avg
        return atr_current, atr_avg, atr_ratio

    except Exception as exc:
        logger.error("[%s] Помилка _get_atr_metrics: %s", symbol, exc)
        return 0.0, 0.0, 1.0
