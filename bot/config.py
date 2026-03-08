"""
Merkezi strateji konfigürasyonu.

.env  → Sadece API key/secret gibi hassas bilgiler.
config.py → Tüm strateji, risk ve sistem parametreleri.

Kullanım:
    from bot.config import STRATEGY, RISK, EXECUTION, BACKTEST
"""

from dataclasses import dataclass, field
from typing import List, Tuple


# ── STRATEJI ──────────────────────────────────────────────────────────────────

@dataclass
class StrategyConfig:
    # İşlem yapılacak semboller (Binance USDT-M Futures)
    SYMBOLS: List[str] = field(default_factory=lambda: ["BTCUSDT", "ETHUSDT"])

    # Timeframe
    TF_PRIMARY: str = "5m"
    TF_HTF: str = "1h"
    PRIMARY_CANDLES: int = 120   # İndikatörler için yeterli 5m mum
    HTF_CANDLES: int = 60        # EMA50 hesabı için yeterli 1H mum

    # 1H veri cache (saniye) — aynı 1H mum kapanmadan veri değişmez
    HTF_CACHE_SECONDS: int = 300  # 1 adet 5m döngüsü ömrü

    # ATR (5m ATR-14)
    ATR_PERIOD: int = 14
    ATR_SL_MULT: float = 1.3        # SL mesafesi = ATR × bu çarpan
    ATR_TP_MULT: float = 2.5        # TP mesafesi = ATR × bu çarpan
    ATR_MIN_RATIO: float = 0.0005   # ATR/fiyat < bu → no-trade (düşük volatilite)

    # SL/TP yüzde bazlı clamp (ondalık: 0.007 = %0.7)
    SL_MIN_PCT: float = 0.007    # %0.7
    SL_MAX_PCT: float = 0.018    # %1.8
    TP_MIN_PCT: float = 0.012    # %1.2
    TP_MAX_PCT: float = 0.032    # %3.2

    # EMA (5m üzerinde)
    EMA_FAST: int = 20
    EMA_SLOW: int = 50

    # 1H EMA50 filtresi
    EMA_HTF_PERIOD: int = 50
    EMA_HTF_MIN_DIST_PCT: float = 0.004  # fiyat ile 1H EMA50 arası min mesafe (%0.4)

    # EMA20 temas kuralı: son N kapanmış mumdan en az 1'i temas etmeli
    EMA_TOUCH_LOOKBACK: int = 3

    # RSI (5m RSI-14)
    RSI_PERIOD: int = 14
    RSI_LONG_MIN: float = 41.0
    RSI_LONG_MAX: float = 66.0
    RSI_SHORT_MIN: float = 34.0
    RSI_SHORT_MAX: float = 59.0

    # Hacim filtresi
    VOL_SMA_PERIOD: int = 20
    VOL_CURRENT_MULT: float = 1.2   # Son mum hacmi > SMA × bu
    VOL_PREV_MULT: float = 1.5      # Önceki mum hacmi < SMA × bu

    # Trailing stop seviyeleri: (kâr_atr_çarpanı, stop_atr_çarpanı)
    # Long:  stop = entry + stop_offset × ATR
    # Short: stop = entry - stop_offset × ATR
    TRAILING_LEVELS: List[Tuple[float, float]] = field(default_factory=lambda: [
        (1.0, 0.0),   # +1.0 ATR kâr → stop = entry (breakeven)
        (1.5, 0.5),   # +1.5 ATR kâr → stop = entry + 0.5 ATR
        (2.0, 1.0),   # +2.0 ATR kâr → stop = entry + 1.0 ATR
    ])

    # Seans filtresi (UTC saat)
    SESSION_NO_TRADE_START_HOUR: int = 0    # 00:00 UTC
    SESSION_NO_TRADE_END_HOUR: int = 4      # 04:00 UTC (dahil değil)
    SESSION_NO_WEEKENDS: bool = True         # Cmt/Paz no-trade

    # Breakeven stop çıkışı sonrası aynı yön bekleme (kapanmış mum sayısı)
    BE_COOLDOWN_CANDLES: int = 3  # 3 × 5m = 15 dakika


# ── RİSK ──────────────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    INITIAL_CAPITAL: float = 100.0  # Başlangıç sermayesi (USDT)

    # Futures kaldıraç ve margin
    LEVERAGE: int = 3
    MARGIN_MODE: str = "ISOLATED"

    # Trade başına sabit risk (USDT)
    RISK_PER_TRADE_USDT: float = 2.25

    # Pozisyon notional boyutu sınırları (USDT)
    POSITION_MIN_USDT: float = 30.0
    POSITION_MAX_USDT: float = 80.0

    # Günlük limitler
    DAILY_MAX_TRADES: int = 6
    DAILY_LOSS_LIMIT_USDT: float = -5.0         # Net zarar limiti
    DAILY_PROFIT_PEAK_TRIGGER: float = 10.0      # PeakPnL takibi bu değerden sonra başlar
    DAILY_PEAK_DRAWBACK_USDT: float = 3.0        # Peak'ten bu kadar geri verilince yeni giriş yok

    # Loss streak koruma
    LOSS_STREAK_COOLDOWN_COUNT: int = 2    # Bu ardışık kayıptan sonra cooldown
    LOSS_STREAK_DAY_END_COUNT: int = 3     # Bu ardışık kayıptan sonra gün biter
    COOLDOWN_DURATION_SECONDS: int = 3600  # 1 saat cooldown

    # Pozisyon limitleri
    MAX_TOTAL_POSITIONS: int = 2
    MAX_POSITIONS_PER_SYMBOL: int = 1


# ── EXECUTİON ─────────────────────────────────────────────────────────────────

@dataclass
class ExecutionConfig:
    # Giriş emir tipi
    ENTRY_ORDER_TYPE: str = "LIMIT"          # "LIMIT" veya "MARKET"
    LIMIT_PRICE_OFFSET_PCT: float = 0.0002   # Maker avantajı için offset (%0.02)
    FILL_TIMEOUT_SECONDS: int = 30           # Bu sürede dolmazsa iptal

    # Live modda mum içi SL polling aralığı (saniye)
    SL_POLL_INTERVAL_SECONDS: int = 10


# ── BACKTEST ──────────────────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    # Veri kaynağı: "csv", "ccxt", "synthetic"
    DATA_SOURCE: str = "synthetic"

    # CSV ayarları
    CSV_DIR: str = "data/"
    # Beklenen CSV başlıkları: timestamp,open,high,low,close,volume
    # timestamp: Unix millisaniye (int) veya ISO8601 string

    # ccxt ayarları (DATA_SOURCE="ccxt" ise)
    CCXT_EXCHANGE: str = "binance"

    # Tarih aralığı
    START_DATE: str = "2024-03-08"
    END_DATE: str = "2025-03-08"

    # Komisyon (kesirsiz, ör. 0.0002 = %0.02)
    MAKER_FEE: float = 0.0002    # Limit giriş
    TAKER_FEE: float = 0.0004   # Market çıkış
    SLIPPAGE_PCT: float = 0.0001  # Çıkış slippage (%0.01)

    # Aynı mum içi TP/SL çakışmasında davranış
    CONSERVATIVE_INTRABAR: bool = True  # True → SL önce (kötü senaryo seç)

    # Çıktı dosyaları
    TRADE_LOG_CSV: str = "backtest_trades.csv"
    DAILY_LOG_CSV: str = "backtest_daily.csv"

    # Sentetik veri parametreleri
    SYNTHETIC_BTC_START: float = 65000.0
    SYNTHETIC_ETH_START: float = 3500.0
    SYNTHETIC_SEED: int = 42


# ── SINGLETON INSTANCE'LAR ────────────────────────────────────────────────────

STRATEGY = StrategyConfig()
RISK = RiskConfig()
EXECUTION = ExecutionConfig()
BACKTEST = BacktestConfig()
