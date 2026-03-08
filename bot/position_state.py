"""
Açık pozisyon durumu.

Her sembol için en fazla bir PositionState örneği tutulur.
Trailing stop mantığı bu sınıf içinde yönetilir.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class PositionState:
    """Tek bir açık futures pozisyonunu temsil eder."""

    symbol: str
    direction: str       # "LONG" veya "SHORT"
    entry_price: float
    quantity: float      # Base asset miktarı (ör. BTC)
    notional: float      # USDT notional değeri
    atr_at_entry: float
    sl_price: float      # Güncel internal stop fiyatı
    tp_price: float

    # Trailing stop takibi
    trailing_level_idx: int = -1   # Ulaşılan son trailing seviye indeksi (-1 = henüz yok)
    sl_is_breakeven: bool = False   # Stop şu an entry fiyatında mı?

    # Emir takibi
    order_id: Optional[str] = None
    is_partial_fill: bool = False

    # ── SL/TP tetik kontrolü ──────────────────────────────────────────────────

    def is_sl_hit(self, price: float) -> bool:
        """Fiyat stop seviyesine değdi mi?"""
        if self.direction == "LONG":
            return price <= self.sl_price
        return price >= self.sl_price  # SHORT

    def is_tp_hit(self, price: float) -> bool:
        """Fiyat take-profit seviyesine ulaştı mı?"""
        if self.direction == "LONG":
            return price >= self.tp_price
        return price <= self.tp_price  # SHORT

    # ── Trailing stop güncelleme ──────────────────────────────────────────────

    def update_trailing(
        self,
        reference_price: float,
        levels: List[Tuple[float, float]],
    ) -> Optional[float]:
        """
        Kâr miktarına göre trailing stop seviyesini günceller.

        reference_price:
            Live modda anlık fiyat,
            Backtest modda long için mum.high, short için mum.low.

        levels: [(kâr_atr_çarpanı, stop_atr_çarpanı), ...]
            Sıralı olmalı (küçükten büyüğe).

        Yeni stop fiyatı döner; değişmediyse None.
        Stop hiçbir zaman geri genişletilmez.
        """
        profit_atr = self._profit_in_atr(reference_price)
        new_sl: Optional[float] = None

        for idx, (profit_threshold, stop_offset) in enumerate(levels):
            if idx <= self.trailing_level_idx:
                # Bu seviye zaten geçildi
                continue
            if profit_atr < profit_threshold:
                # Henüz bu seviyeye ulaşılmadı
                break

            # Yeni stop adayı hesapla
            if self.direction == "LONG":
                candidate = self.entry_price + stop_offset * self.atr_at_entry
                # Stop sadece daha avantajlı (yüksek) yöne gider
                if candidate > self.sl_price:
                    self.sl_price = candidate
                    self.trailing_level_idx = idx
                    self.sl_is_breakeven = (stop_offset == 0.0)
                    new_sl = candidate
            else:  # SHORT
                candidate = self.entry_price - stop_offset * self.atr_at_entry
                # Stop sadece daha avantajlı (düşük) yöne gider
                if candidate < self.sl_price:
                    self.sl_price = candidate
                    self.trailing_level_idx = idx
                    self.sl_is_breakeven = (stop_offset == 0.0)
                    new_sl = candidate

        return new_sl

    # ── Yardımcı ─────────────────────────────────────────────────────────────

    def _profit_in_atr(self, current_price: float) -> float:
        """Anlık kârı ATR cinsinden döner."""
        if self.atr_at_entry <= 0:
            return 0.0
        if self.direction == "LONG":
            return (current_price - self.entry_price) / self.atr_at_entry
        return (self.entry_price - current_price) / self.atr_at_entry

    def current_pnl_raw(self, current_price: float) -> float:
        """Komisyon/slippage hariç anlık PnL (USDT)."""
        if self.direction == "LONG":
            return (current_price - self.entry_price) * self.quantity
        return (self.entry_price - current_price) * self.quantity
