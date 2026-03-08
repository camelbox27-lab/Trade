"""
Teknik indikatör hesaplamaları.

Tüm fonksiyonlar pandas DataFrame/Series alır, Series döner.
NaN yönetimi: Wilder smoothing (EWM com=period-1) kullanılır.
Yeterli veri yoksa başlangıç değerleri NaN olur — çağıran taraf kontrol etmeli.
"""

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average (EWM, adjust=False)."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Wilder RSI.
    Yeterli veri yoksa NaN döner (başlangıç mumları).
    """
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))

    # Wilder smoothing = EWM com = period - 1
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range (Wilder smoothing).
    df sütunları: high, low, close
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    return tr.ewm(com=period - 1, adjust=False).mean()


def volume_sma(series: pd.Series, period: int = 20) -> pd.Series:
    """Hacim basit hareketli ortalaması."""
    return series.rolling(window=period, min_periods=period).mean()
