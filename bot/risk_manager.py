"""
Global risk durumu yöneticisi.

GlobalRiskState:
    - Günlük limitler (UTC bazlı)
    - Loss streak koruması + cooldown
    - Sembol bazlı pozisyon sayacı
    - BE cooldown (whipsaw koruması)
    - Pozisyon boyutu hesabı
    - Seans filtresi

Backtest için simulated_now_ts parametresi desteklenir.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from bot.config import RISK, STRATEGY

logger = logging.getLogger(__name__)


# ── State dataclass'ları ──────────────────────────────────────────────────────

@dataclass
class SymbolState:
    """Sembol bazlı durum (pozisyon + BE cooldown)."""
    in_position: bool = False
    be_cooldown_long: int = 0    # Long giriş yasaklı kalan mum sayısı
    be_cooldown_short: int = 0   # Short giriş yasaklı kalan mum sayısı


@dataclass
class DailyState:
    """UTC gününe ait tüm sayaçlar."""
    date_str: str = ""            # "YYYY-MM-DD"
    trade_count: int = 0
    net_pnl: float = 0.0
    peak_pnl: float = 0.0
    consecutive_losses: int = 0
    day_ended: bool = False       # Gün bitti mi (streak veya zarar limiti)
    cooldown_until_ts: float = 0.0   # Unix timestamp; 0 = cooldown yok


# ── Ana sınıf ─────────────────────────────────────────────────────────────────

class GlobalRiskState:
    """
    Tüm semboller için ortak risk state.

    Live modda tek thread üzerinde çalışır (thread-safe değil).
    Backtest modda simulated_now_ts parametreleri ile zaman simülasyonu yapılır.
    """

    def __init__(self, symbols: Optional[list] = None):
        self._symbols = symbols or STRATEGY.SYMBOLS
        self._daily = DailyState()
        self._sym: Dict[str, SymbolState] = {
            s: SymbolState() for s in self._symbols
        }
        self._ensure_day()

    # ── Gün yönetimi ──────────────────────────────────────────────────────────

    def _today_utc(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _ensure_day(self, current_date: Optional[str] = None) -> bool:
        """Gün değiştiyse sıfırla. Değiştiyse True döner."""
        today = current_date or self._today_utc()
        if self._daily.date_str == today:
            return False

        if self._daily.date_str:   # İlk çağrı değilse özetle
            logger.info(
                f"[DAY_RESET] {self._daily.date_str} → {today} | "
                f"trades={self._daily.trade_count} "
                f"net_pnl={self._daily.net_pnl:+.4f} "
                f"peak={self._daily.peak_pnl:.4f}"
            )
        self._daily = DailyState(date_str=today)
        return True

    def on_candle_close(self, current_date: Optional[str] = None):
        """
        Her kapanmış 5m mum sonrası çağrılır.
        Gün sıfırı ve BE cooldown azaltma yapılır.
        """
        self._ensure_day(current_date)

        for sym, state in self._sym.items():
            if state.be_cooldown_long > 0:
                state.be_cooldown_long -= 1
                logger.debug(f"[BE_COOLDOWN] {sym} long kalan={state.be_cooldown_long}")
            if state.be_cooldown_short > 0:
                state.be_cooldown_short -= 1
                logger.debug(f"[BE_COOLDOWN] {sym} short kalan={state.be_cooldown_short}")

    # ── Pozisyon sayacı ───────────────────────────────────────────────────────

    def total_open_positions(self) -> int:
        return sum(1 for s in self._sym.values() if s.in_position)

    def mark_position_open(self, symbol: str):
        if symbol in self._sym:
            self._sym[symbol].in_position = True

    def mark_position_closed(self, symbol: str):
        if symbol in self._sym:
            self._sym[symbol].in_position = False

    # ── Giriş onayı ───────────────────────────────────────────────────────────

    def can_open(
        self,
        symbol: str,
        direction: str,
        simulated_now_ts: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        Yeni pozisyon açılabilir mi?

        direction: "LONG" veya "SHORT"
        simulated_now_ts: Backtest için simüle edilmiş Unix timestamp.
        Döner: (bool, red_sebebi)
        """
        d = self._daily
        sym = self._sym.get(symbol)
        now_ts = simulated_now_ts or time.time()

        if d.day_ended:
            return False, "day_ended"

        if d.trade_count >= RISK.DAILY_MAX_TRADES:
            logger.info(f"[REJECT] daily_trade_limit {d.trade_count}/{RISK.DAILY_MAX_TRADES}")
            return False, "daily_trade_limit"

        if d.net_pnl <= RISK.DAILY_LOSS_LIMIT_USDT:
            logger.info(f"[REJECT] daily_loss_limit net={d.net_pnl:.4f}")
            return False, "daily_loss_limit"

        # Peak PnL drawdown koruması
        if (
            d.peak_pnl >= RISK.DAILY_PROFIT_PEAK_TRIGGER
            and d.peak_pnl - d.net_pnl >= RISK.DAILY_PEAK_DRAWBACK_USDT
        ):
            logger.info(
                f"[REJECT] peak_pnl_drawdown peak={d.peak_pnl:.4f} "
                f"net={d.net_pnl:.4f} geri={d.peak_pnl - d.net_pnl:.4f}"
            )
            return False, "peak_pnl_drawdown"

        if d.cooldown_until_ts > now_ts:
            remaining = int(d.cooldown_until_ts - now_ts)
            logger.info(f"[REJECT] cooldown_active {remaining}s kaldı")
            return False, "cooldown_active"

        if self.total_open_positions() >= RISK.MAX_TOTAL_POSITIONS:
            logger.info(f"[REJECT] max_positions {self.total_open_positions()}")
            return False, "max_positions"

        if sym and sym.in_position:
            logger.info(f"[REJECT] symbol_in_position {symbol}")
            return False, "symbol_in_position"

        if sym:
            if direction == "LONG" and sym.be_cooldown_long > 0:
                logger.info(f"[REJECT] be_cooldown_long {symbol} {sym.be_cooldown_long} mum")
                return False, "be_cooldown_long"
            if direction == "SHORT" and sym.be_cooldown_short > 0:
                logger.info(f"[REJECT] be_cooldown_short {symbol} {sym.be_cooldown_short} mum")
                return False, "be_cooldown_short"

        return True, ""

    # ── İşlem kaydı ───────────────────────────────────────────────────────────

    def on_trade_opened(self, symbol: str):
        """Pozisyon açıldığında çağrılır."""
        self._daily.trade_count += 1
        self.mark_position_open(symbol)
        logger.info(f"[TRADE_COUNT] {symbol} {self._daily.trade_count}/{RISK.DAILY_MAX_TRADES}")

    def on_trade_closed(
        self,
        symbol: str,
        direction: str,
        pnl: float,
        exit_type: str,
        simulated_now_ts: Optional[float] = None,
    ):
        """
        Pozisyon kapandığında çağrılır.
        exit_type: "TP", "SL", "BE_STOP", "TRAILING", "SIGNAL", "CLOSE_END"
        """
        self.mark_position_closed(symbol)
        self._daily.net_pnl += pnl

        # Peak PnL güncelle
        if self._daily.net_pnl > self._daily.peak_pnl:
            self._daily.peak_pnl = self._daily.net_pnl

        now_ts = simulated_now_ts or time.time()

        # Loss streak yönetimi
        if pnl < 0:
            self._daily.consecutive_losses += 1
            logger.info(f"[LOSS_STREAK] {self._daily.consecutive_losses} ardışık kayıp | PnL={pnl:+.4f}")

            if self._daily.consecutive_losses >= RISK.LOSS_STREAK_DAY_END_COUNT:
                self._daily.day_ended = True
                logger.warning(
                    f"[DAY_END] {RISK.LOSS_STREAK_DAY_END_COUNT} ardışık kayıp → "
                    "gün bitirildi"
                )
            elif self._daily.consecutive_losses >= RISK.LOSS_STREAK_COOLDOWN_COUNT:
                self._daily.cooldown_until_ts = now_ts + RISK.COOLDOWN_DURATION_SECONDS
                logger.warning(
                    f"[COOLDOWN] {RISK.COOLDOWN_DURATION_SECONDS}s cooldown başladı"
                )
        else:
            if self._daily.consecutive_losses > 0:
                logger.info(
                    f"[STREAK_RESET] Win → streak {self._daily.consecutive_losses} → 0"
                )
            self._daily.consecutive_losses = 0

        # BE cooldown
        if exit_type == "BE_STOP":
            sym = self._sym.get(symbol)
            if sym:
                if direction == "LONG":
                    sym.be_cooldown_long = STRATEGY.BE_COOLDOWN_CANDLES
                    logger.info(f"[BE_COOLDOWN] {symbol} long: {STRATEGY.BE_COOLDOWN_CANDLES} mum bekleniyor")
                elif direction == "SHORT":
                    sym.be_cooldown_short = STRATEGY.BE_COOLDOWN_CANDLES
                    logger.info(f"[BE_COOLDOWN] {symbol} short: {STRATEGY.BE_COOLDOWN_CANDLES} mum bekleniyor")

        logger.info(
            f"[TRADE_CLOSED] {symbol} {direction} {exit_type} | "
            f"PnL={pnl:+.4f} | günlük_net={self._daily.net_pnl:+.4f} | "
            f"peak={self._daily.peak_pnl:.4f} | streak={self._daily.consecutive_losses}"
        )

    # ── Pozisyon boyutu ────────────────────────────────────────────────────────

    def calc_position_size(self, sl_dist_pct: float) -> float:
        """
        ATR bazlı risk yönetimi ile pozisyon notional (USDT) hesaplar.

        Formül: position = RISK_PER_TRADE × LEVERAGE / sl_dist_pct
        Sonuç [POSITION_MIN, POSITION_MAX] aralığına kırpılır.

        sl_dist_pct: SL mesafesi ondalık (0.012 = %1.2)
        """
        cfg = RISK
        if sl_dist_pct <= 0:
            logger.warning("[POSITION_SIZE] sl_dist_pct <= 0, minimum kullanılıyor")
            return cfg.POSITION_MIN_USDT

        position = (cfg.RISK_PER_TRADE_USDT * cfg.LEVERAGE) / sl_dist_pct
        clamped = max(cfg.POSITION_MIN_USDT, min(cfg.POSITION_MAX_USDT, position))

        if clamped != position:
            logger.debug(
                f"[POSITION_SIZE] ham={position:.2f} → kırpıldı={clamped:.2f} "
                f"[{cfg.POSITION_MIN_USDT},{cfg.POSITION_MAX_USDT}]"
            )
        return round(clamped, 2)

    # ── Seans filtresi ─────────────────────────────────────────────────────────

    @staticmethod
    def is_session_allowed(dt: datetime) -> Tuple[bool, str]:
        """
        UTC saatine göre işlem seansı kontrolü.
        dt: timezone-aware datetime (UTC).
        """
        cfg = STRATEGY
        if cfg.SESSION_NO_WEEKENDS and dt.weekday() >= 5:   # 5=Cmt, 6=Paz
            return False, "weekend"

        h = dt.hour
        start = cfg.SESSION_NO_TRADE_START_HOUR
        end = cfg.SESSION_NO_TRADE_END_HOUR
        if start <= h < end:
            return False, f"no_trade_session_{start:02d}_{end:02d}utc"

        return True, ""

    # ── State erişimi ─────────────────────────────────────────────────────────

    @property
    def daily(self) -> DailyState:
        return self._daily

    def symbol_state(self, symbol: str) -> SymbolState:
        return self._sym.get(symbol, SymbolState())

    def reset_for_backtest(self, date_str: str = ""):
        """Backtest için tüm state'i sıfırlar."""
        self._daily = DailyState(date_str=date_str)
        self._sym = {s: SymbolState() for s in self._symbols}
