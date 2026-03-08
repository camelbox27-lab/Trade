import pandas as pd
import numpy as np
from bot.config import MOMENTUM_PERIOD, MOMENTUM_BUY_THRESHOLD, MOMENTUM_SELL_THRESHOLD


class MomentumStrategy:
    """1 dakikalık momentum değişimlerine dayalı strateji.

    Momentum = (son fiyat - N periyot önceki fiyat) / N periyot önceki fiyat * 100
    RSI filtresi ile birlikte kullanılır.
    """

    def calculate_momentum(self, df: pd.DataFrame) -> float:
        """Son N mumdaki momentum yüzdesini hesaplar."""
        if len(df) < MOMENTUM_PERIOD:
            return 0.0
        current_close = df["close"].iloc[-1]
        past_close = df["close"].iloc[-MOMENTUM_PERIOD]
        if past_close == 0:
            return 0.0
        return ((current_close - past_close) / past_close) * 100

    def calculate_rsi(self, df: pd.DataFrame, period: int = 14) -> float:
        """RSI hesaplar (ek filtre olarak)."""
        if len(df) < period + 1:
            return 50.0
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window=period).mean()
        last_loss = loss.iloc[-1]
        if last_loss == 0:
            return 100.0
        rs = gain.iloc[-1] / last_loss
        return 100 - (100 / (1 + rs))

    def calculate_volume_spike(self, df: pd.DataFrame, period: int = 20) -> bool:
        """Hacim ortalamanın üzerinde mi kontrol eder."""
        if len(df) < period:
            return False
        avg_volume = df["volume"].iloc[-period:].mean()
        current_volume = df["volume"].iloc[-1]
        return current_volume > avg_volume * 1.5

    def get_signal(self, df: pd.DataFrame) -> str:
        """Alım/satım sinyali üretir.

        Returns:
            "BUY"  - Pozisyon aç
            "SELL" - Pozisyon kapat
            "HOLD" - Bekle
        """
        momentum = self.calculate_momentum(df)
        rsi = self.calculate_rsi(df)
        volume_spike = self.calculate_volume_spike(df)

        # AL sinyali: Momentum pozitif eşiği aştı + RSI aşırı satım bölgesinden çıkıyor
        if momentum > MOMENTUM_BUY_THRESHOLD and rsi < 70:
            if volume_spike:
                return "BUY"
            # Hacim desteği olmasa da güçlü momentum varsa al
            if momentum > MOMENTUM_BUY_THRESHOLD * 2:
                return "BUY"

        # SAT sinyali: Momentum negatif eşiğin altına düştü veya RSI aşırı alım
        if momentum < MOMENTUM_SELL_THRESHOLD or rsi > 80:
            return "SELL"

        return "HOLD"
