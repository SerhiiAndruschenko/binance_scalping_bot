from __future__ import annotations

import sys
import time
from typing import Any

import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException

import config
from logger import logger


class BinanceClient:
    """
    Обгортка над python-binance для роботи з USDT-M Futures.
    Підтримує testnet і mainnet режими.
    """

    def __init__(self) -> None:
        if not config.API_KEY or not config.API_SECRET:
            logger.error(
                "API_KEY або API_SECRET не задано. "
                "Створіть файл .env (скопіюйте з .env.example) і заповніть ключі."
            )
            sys.exit(1)

        self.client = Client(
            api_key=config.API_KEY,
            api_secret=config.API_SECRET,
            testnet=config.TESTNET,
        )
        if config.TESTNET:
            self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
        logger.info(
            "BinanceClient ініціалізовано | testnet=%s", config.TESTNET
        )

    # ── Баланс ────────────────────────────────────────────────────────────────

    def get_usdt_balance(self) -> float:
        """Повертає доступний USDT баланс на futures акаунті."""
        try:
            account = self.client.futures_account()
            for asset in account["assets"]:
                if asset["asset"] == "USDT":
                    return float(asset["availableBalance"])
        except BinanceAPIException as exc:
            logger.error("Помилка отримання балансу: %s", exc)
        return 0.0

    def get_total_wallet_balance(self) -> float:
        """Повертає загальний USDT баланс гаманця (walletBalance)."""
        try:
            account = self.client.futures_account()
            for asset in account["assets"]:
                if asset["asset"] == "USDT":
                    return float(asset["walletBalance"])
        except BinanceAPIException as exc:
            logger.error("Помилка отримання walletBalance: %s", exc)
        return 0.0

    def get_unrealized_pnl(self) -> float:
        """Повертає суму нереалізованого PnL по всіх відкритих позиціях."""
        try:
            account = self.client.futures_account()
            return float(account.get("totalUnrealizedProfit", 0))
        except BinanceAPIException as exc:
            logger.error("Помилка отримання unrealizedPnL: %s", exc)
        return 0.0

    def get_income_history(self, start_ms: int, end_ms: int) -> float:
        """
        Повертає реалізований PnL (REALIZED_PNL) за вказаний період.
        start_ms / end_ms — Unix timestamp в мілісекундах.
        """
        total = 0.0
        try:
            result = self.client.futures_income_history(
                incomeType="REALIZED_PNL",
                startTime=start_ms,
                endTime=end_ms,
                limit=1000,
            )
            for entry in result:
                total += float(entry.get("income", 0))
        except BinanceAPIException as exc:
            logger.error("Помилка отримання income history: %s", exc)
        return total

    # ── Ринкові дані ──────────────────────────────────────────────────────────

    def get_klines(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        """
        Завантажує OHLCV свічки і повертає DataFrame з колонками:
        open_time, open, high, low, close, volume.
        """
        try:
            raw = self.client.futures_klines(
                symbol=symbol, interval=interval, limit=limit
            )
        except BinanceAPIException as exc:
            logger.error("[%s] Помилка отримання klines: %s", symbol, exc)
            return pd.DataFrame()

        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "number_of_trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        return df

    def get_current_price(self, symbol: str) -> float:
        """Повертає поточну ринкову ціну символу."""
        try:
            ticker = self.client.futures_symbol_ticker(symbol=symbol)
            return float(ticker["price"])
        except BinanceAPIException as exc:
            logger.error("[%s] Помилка отримання ціни: %s", symbol, exc)
            return 0.0

    def get_order_book(self, symbol: str, limit: int = 10) -> dict:
        """
        Повертає стакан ордерів (bids/asks) для символу.
        GET /fapi/v1/depth?symbol=BTCUSDT&limit=10
        """
        try:
            return self.client.futures_order_book(symbol=symbol, limit=limit)
        except BinanceAPIException as exc:
            logger.error("[%s] Помилка отримання order book: %s", symbol, exc)
            return {"bids": [], "asks": []}

    # ── Позиції і ордери ──────────────────────────────────────────────────────

    def get_open_positions(self) -> list[dict]:
        """Повертає всі відкриті futures позиції (positionAmt != 0)."""
        try:
            positions = self.client.futures_position_information()
            return [p for p in positions if float(p["positionAmt"]) != 0]
        except BinanceAPIException as exc:
            logger.error("Помилка отримання позицій: %s", exc)
            return []

    def get_all_positions_info(self) -> list[dict]:
        """Повертає інформацію по всіх символах (включаючи нульові позиції)."""
        try:
            return self.client.futures_position_information()
        except BinanceAPIException as exc:
            logger.error("Помилка отримання position_information: %s", exc)
            return []

    def get_position_info(self, symbol: str) -> dict | None:
        """Повертає інформацію по конкретній позиції."""
        try:
            positions = self.client.futures_position_information(symbol=symbol)
            for p in positions:
                if p["symbol"] == symbol:
                    return p
        except BinanceAPIException as exc:
            logger.error("[%s] Помилка отримання позиції: %s", symbol, exc)
        return None

    def get_open_orders(self, symbol: str) -> list[dict]:
        """Повертає відкриті ордери для символу."""
        try:
            return self.client.futures_get_open_orders(symbol=symbol)
        except BinanceAPIException as exc:
            logger.error("[%s] Помилка отримання ордерів: %s", symbol, exc)
            return []

    # ── Торгові операції ──────────────────────────────────────────────────────

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Встановлює плече для символу."""
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=leverage)
            logger.info("[%s] Плече встановлено: x%d", symbol, leverage)
            return True
        except BinanceAPIException as exc:
            logger.error("[%s] Помилка встановлення плеча: %s", symbol, exc)
            return False

    def set_margin_type(self, symbol: str, margin_type: str = "ISOLATED") -> bool:
        """Встановлює тип маржі: ISOLATED або CROSSED."""
        try:
            self.client.futures_change_margin_type(
                symbol=symbol, marginType=margin_type
            )
            logger.info("[%s] Тип маржі: %s", symbol, margin_type)
            return True
        except BinanceAPIException as exc:
            # Якщо вже встановлено — не помилка
            if "No need to change margin type" in str(exc):
                return True
            logger.warning("[%s] Помилка зміни типу маржі: %s", symbol, exc)
            return False

    def place_market_order(
        self,
        symbol: str,
        side: str,       # "BUY" або "SELL"
        quantity: float,
        reduce_only: bool = False,
    ) -> dict | None:
        """Розміщує ринковий ордер на futures."""
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
        }
        if reduce_only:
            params["reduceOnly"] = True

        try:
            order = self.client.futures_create_order(**params)
            logger.info(
                "[%s] Ордер розміщено | side=%s qty=%.4f orderId=%s",
                symbol, side, quantity, order.get("orderId"),
            )
            return order
        except BinanceAPIException as exc:
            # -2022 пробрасуємо вгору — trader.py обробляє цей кейс окремо
            if exc.code == -2022:
                raise
            logger.error("[%s] Помилка розміщення ордеру: %s", symbol, exc)
            return None

    def cancel_all_open_orders(self, symbol: str) -> bool:
        """Скасовує всі відкриті ордери для символу."""
        try:
            self.client.futures_cancel_all_open_orders(symbol=symbol)
            logger.info("[%s] Всі ордери скасовані", symbol)
            return True
        except BinanceAPIException as exc:
            logger.error("[%s] Помилка скасування ордерів: %s", symbol, exc)
            return False

    def get_symbol_info(self, symbol: str) -> dict | None:
        """Повертає торгові параметри символу (точність, мінімальна кількість)."""
        try:
            info = self.client.futures_exchange_info()
            for s in info["symbols"]:
                if s["symbol"] == symbol:
                    return s
        except BinanceAPIException as exc:
            logger.error("[%s] Помилка отримання symbol info: %s", symbol, exc)
        return None

    def get_quantity_precision(self, symbol: str) -> int:
        """Повертає точність кількості для символу."""
        info = self.get_symbol_info(symbol)
        if info:
            return int(info.get("quantityPrecision", 3))
        return 3

    def get_price_precision(self, symbol: str) -> int:
        """Повертає точність ціни для символу."""
        info = self.get_symbol_info(symbol)
        if info:
            return int(info.get("pricePrecision", 2))
        return 2
