import pandas as pd
from binance.client import Client
from bot.config import BINANCE_API_KEY, BINANCE_API_SECRET, SYMBOL, INTERVAL


class BinanceDataFetcher:
    """Binance'den fiyat verisi çeken modül."""

    def __init__(self):
        self.client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

    def get_klines(self, limit: int = 50) -> pd.DataFrame:
        """Son `limit` adet 1dk'lık mum verisini çeker."""
        raw = self.client.get_klines(symbol=SYMBOL, interval=INTERVAL, limit=limit)
        df = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])
        df["close"] = df["close"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["open"] = df["open"].astype(float)
        df["volume"] = df["volume"].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        return df

    def get_current_price(self) -> float:
        """Anlık fiyatı döndürür."""
        ticker = self.client.get_symbol_ticker(symbol=SYMBOL)
        return float(ticker["price"])

    def get_account_balance(self, asset: str = "USDT") -> float:
        """Hesaptaki belirli varlığın bakiyesini döndürür."""
        balance = self.client.get_asset_balance(asset=asset)
        if balance:
            return float(balance["free"])
        return 0.0
