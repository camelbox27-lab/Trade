import logging
from bot.config import (
    CAPITAL, DAILY_PROFIT_TARGET_PCT, DAILY_LOSS_LIMIT_PCT,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, MAX_POSITION_PCT
)

logger = logging.getLogger("trade_bot")


class RiskManager:
    """Günlük kar/zarar limitleri ve pozisyon bazlı risk yönetimi."""

    def __init__(self):
        self.starting_capital = CAPITAL
        self.daily_pnl = 0.0  # Günlük kar/zarar (USD)
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0

    @property
    def daily_pnl_pct(self) -> float:
        """Günlük kar/zarar yüzdesi."""
        if self.starting_capital == 0:
            return 0.0
        return (self.daily_pnl / self.starting_capital) * 100

    def can_trade(self) -> bool:
        """İşlem açılabilir mi kontrol eder."""
        # Günlük %10 kar hedefine ulaşıldıysa dur
        if self.daily_pnl_pct >= DAILY_PROFIT_TARGET_PCT:
            logger.info(
                f"Günlük kar hedefine ulaşıldı: %{self.daily_pnl_pct:.2f}. "
                "İşlem durduruldu."
            )
            return False

        # Günlük %5 zarar limitine ulaşıldıysa dur
        if self.daily_pnl_pct <= -DAILY_LOSS_LIMIT_PCT:
            logger.warning(
                f"Günlük zarar limitine ulaşıldı: %{self.daily_pnl_pct:.2f}. "
                "İşlem durduruldu."
            )
            return False

        return True

    def get_position_size(self, current_capital: float) -> float:
        """Açılacak pozisyon büyüklüğünü hesaplar (USD)."""
        max_size = current_capital * (MAX_POSITION_PCT / 100)
        return round(max_size, 2)

    def check_stop_loss(self, entry_price: float, current_price: float) -> bool:
        """Stop-loss tetiklendi mi?"""
        if entry_price == 0:
            return False
        pnl_pct = ((current_price - entry_price) / entry_price) * 100
        return pnl_pct <= -STOP_LOSS_PCT

    def check_take_profit(self, entry_price: float, current_price: float) -> bool:
        """Take-profit tetiklendi mi?"""
        if entry_price == 0:
            return False
        pnl_pct = ((current_price - entry_price) / entry_price) * 100
        return pnl_pct >= TAKE_PROFIT_PCT

    def record_trade(self, pnl: float):
        """Tamamlanan işlemi kaydet."""
        self.daily_pnl += pnl
        self.total_trades += 1
        if pnl >= 0:
            self.winning_trades += 1
        else:
            self.losing_trades += 1
        logger.info(
            f"İşlem kaydedildi: PnL=${pnl:.2f} | "
            f"Günlük PnL=${self.daily_pnl:.2f} (%{self.daily_pnl_pct:.2f})"
        )

    def reset_daily(self):
        """Günlük sayaçları sıfırla (her gün başında çağrılır)."""
        logger.info(
            f"Gün sonu özeti: Toplam={self.total_trades}, "
            f"Kazanan={self.winning_trades}, Kaybeden={self.losing_trades}, "
            f"PnL=${self.daily_pnl:.2f}"
        )
        self.daily_pnl = 0.0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
