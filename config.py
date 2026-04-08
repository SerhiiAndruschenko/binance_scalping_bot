import os
from pathlib import Path

# ── Режим роботи ─────────────────────────────────────────────────────────────
TESTNET: bool = True

# ── Binance API ───────────────────────────────────────────────────────────────
API_KEY: str    = os.getenv("API_KEY", "")
API_SECRET: str = os.getenv("API_SECRET", "")

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_THREAD_ID: str = os.getenv("TELEGRAM_THREAD_ID", "")

# Тільки топ-3 пари з найвищою ліквідністю — решта має широкий спред
SYMBOLS: list[str] = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
]

# ── Таймфрейм і свічки ────────────────────────────────────────────────────────
TIMEFRAME: str     = "1m"   # 1-хвилинні свічки
CANDLES_LIMIT: int = 200    # більше даних для VWAP

# ── EMA (швидші для скальпінгу) ───────────────────────────────────────────────
EMA_FAST: int = 9
EMA_SLOW: int = 21

# ── RSI ───────────────────────────────────────────────────────────────────────
RSI_PERIOD: int      = 7     # коротший для швидшої реакції
RSI_LONG_MIN: float  = 35.0
RSI_LONG_MAX: float  = 65.0
RSI_SHORT_MIN: float = 35.0
RSI_SHORT_MAX: float = 65.0

# ── VWAP — ключовий індикатор для скальпінгу ─────────────────────────────────
VWAP_PERIOD: int = 20  # кількість свічок для розрахунку VWAP

# ── MACD ──────────────────────────────────────────────────────────────────────
MACD_FAST: int      = 6
MACD_SLOW: int      = 13
MACD_SIGNAL: int    = 5
MACD_MIN_DIFF: float = 0.0003  # менше ніж в основному боті

# ── Order Book Imbalance ──────────────────────────────────────────────────────
# 0.60 = bid_volume має бути мінімум 60% від сумарного об'єму
ORDER_BOOK_IMBALANCE: float = 0.60
ORDER_BOOK_DEPTH: int = 10  # аналізуємо топ-10 рівнів стакану

# ── Управління ризиками ───────────────────────────────────────────────────────
LEVERAGE: int           = 3      # менше плече ніж в основному боті
RISK_PER_TRADE: float   = 0.01   # 1% на угоду
TAKE_PROFIT_PCT: float  = 0.005  # +0.5%
STOP_LOSS_PCT: float    = 0.002  # -0.2%
DAILY_LOSS_LIMIT: float = 0.03   # зупинка при -3%

MAX_TRADING_BALANCE: float   = float(os.getenv("MAX_TRADING_BALANCE", "300"))
MAX_OPEN_TRADES_GLOBAL: int  = int(os.getenv("MAX_OPEN_TRADES_GLOBAL", "2"))

# Комісія Taker на Binance Futures
TAKER_FEE: float = 0.0005  # 0.05%

# Мінімальний notional на Binance Futures (вимога біржі)
MIN_NOTIONAL: float = 100.0  # USDT

# ── Швидкість циклу ───────────────────────────────────────────────────────────
SCAN_INTERVAL: int = 15  # кожні 15 секунд

# ── Ідентифікатор у логах і Telegram ─────────────────────────────────────────
BOT_PREFIX: str = "[SCALP]"

# ── Персистентність стану ─────────────────────────────────────────────────────
STATE_DIR: str  = os.getenv("STATE_DIR", str(Path(__file__).parent))
STATE_FILE: str = str(Path(STATE_DIR) / "scalp_state.json")
