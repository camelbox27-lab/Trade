"""
Binance USDT-M Futures veri çekici.

Özellikler:
- 5m kapanmış mum verisi (son açık mum hariç tutulur)
- 1H mum verisi ile cache mekanizması (gereksiz API çağrısını önler)
- Market metadata (lot size, tick size, precision)
- Futures hesap bakiyesi
"""

import logging
import time
from typing import Dict, Optional

import pandas as pd

from bot.config import STRATEGY

logger = logging.getLogger(__name__)


def _parse_klines(raw: list) -> pd.DataFrame:
    """Binance klines listesini DataFrame'e çevirir."""
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = df["open_time"].astype(int)
    df["close_time"] = df["close_time"].astype(int)
    df = df.drop_duplicates(subset=["open_time"]).reset_index(drop=True)
    return df


class DataFetcher:
    """Binance Futures API'den OHLCV ve hesap verisi çeker."""

    def __init__(self, client):
        self._client = client
        # {symbol: {"df": DataFrame, "fetched_at": float}}
        self._htf_cache: Dict[str, dict] = {}

    # ── 5m mum verisi ─────────────────────────────────────────────────────────

    def get_candles(
        self,
        symbol: str,
        interval: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Kapanmış mumları döner.
        Son açık (kapanmamış) mum hariç tutulur.
        """
        interval = interval or STRATEGY.TF_PRIMARY
        limit = limit or STRATEGY.PRIMARY_CANDLES

        try:
            raw = self._client.futures_klines(
                symbol=symbol,
                interval=interval,
                limit=limit + 1,  # Son açık mum için +1 ekstra çek
            )
        except Exception as exc:
            logger.error(f"[DATA] {symbol} {interval} klines hata: {exc}")
            raise

        df = _parse_klines(raw)

        # Son mum henüz kapanmamışsa çıkar
        now_ms = int(time.time() * 1000)
        df = df[df["close_time"] < now_ms].copy()
        df = df.reset_index(drop=True)
        return df

    # ── 1H mum verisi (cache'li) ──────────────────────────────────────────────

    def get_htf_candles(self, symbol: str) -> pd.DataFrame:
        """
        1H kapanmış mumları cache ile döner.
        Aynı 1H mum kapanmadan önce cache'ten okunur.
        HTF_CACHE_SECONDS dolunca ya da yok ise yeniden çekilir.
        """
        cfg = STRATEGY
        now = time.time()
        cached = self._htf_cache.get(symbol)

        if cached:
            age = now - cached["fetched_at"]
            if age < cfg.HTF_CACHE_SECONDS:
                logger.debug(f"[CACHE_HIT] {symbol} 1H ({age:.0f}s önce çekilmişti)")
                return cached["df"]
            logger.debug(f"[CACHE_MISS] {symbol} 1H süresi doldu, yenileniyor")
        else:
            logger.debug(f"[CACHE_MISS] {symbol} 1H ilk yükleme")

        df = self.get_candles(symbol, interval=STRATEGY.TF_HTF, limit=STRATEGY.HTF_CANDLES)
        self._htf_cache[symbol] = {"df": df, "fetched_at": now}
        logger.info(f"[CACHE_REFRESH] {symbol} 1H {len(df)} mum yüklendi")
        return df

    # ── Anlık fiyat ───────────────────────────────────────────────────────────

    def get_current_price(self, symbol: str) -> float:
        try:
            ticker = self._client.futures_symbol_ticker(symbol=symbol)
            return float(ticker["price"])
        except Exception as exc:
            logger.error(f"[DATA] {symbol} fiyat çekilemedi: {exc}")
            raise

    # ── Futures bakiye ────────────────────────────────────────────────────────

    def get_futures_balance(self, asset: str = "USDT") -> float:
        try:
            balances = self._client.futures_account_balance()
            for b in balances:
                if b["asset"] == asset:
                    return float(b["availableBalance"])
            return 0.0
        except Exception as exc:
            logger.error(f"[DATA] bakiye çekilemedi: {exc}")
            raise

    # ── Market metadata ───────────────────────────────────────────────────────

    def get_market_info(self, symbol: str) -> dict:
        """
        Binance exchange bilgilerinden sembol kısıtlarını çeker.
        Dönen dict: qty_precision, price_precision, min_notional,
                    min_qty, step_size, tick_size
        """
        try:
            info = self._client.futures_exchange_info()
        except Exception as exc:
            logger.error(f"[DATA] exchange info hatası: {exc}")
            raise

        for sym_info in info.get("symbols", []):
            if sym_info["symbol"] != symbol:
                continue

            result = {
                "qty_precision": int(sym_info.get("quantityPrecision", 3)),
                "price_precision": int(sym_info.get("pricePrecision", 2)),
                "min_notional": 5.0,   # Varsayılan (filtrede yoksa)
                "min_qty": 0.001,
                "step_size": 0.001,
                "tick_size": 0.01,
            }

            for f in sym_info.get("filters", []):
                ft = f.get("filterType", "")
                if ft == "MIN_NOTIONAL":
                    result["min_notional"] = float(f.get("notional", 5.0))
                elif ft == "LOT_SIZE":
                    result["min_qty"] = float(f.get("minQty", 0.001))
                    result["step_size"] = float(f.get("stepSize", 0.001))
                elif ft == "PRICE_FILTER":
                    result["tick_size"] = float(f.get("tickSize", 0.01))

            logger.debug(f"[MARKET_INFO] {symbol}: {result}")
            return result

        raise ValueError(f"[DATA] {symbol} exchange info bulunamadı")
