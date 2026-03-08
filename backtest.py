"""
Binance USDT-M Futures Momentum Bot — Portföy Backtester
=========================================================

Özellikler:
- Multi-symbol portföy seviyesi (BTCUSDT + ETHUSDT aynı zaman dilimiyle)
- Aynı strateji kuralları (SignalEngine) live ile özdeş
- Tüm global risk kuralları: günlük limitler, streak, cooldown, BE bekleme, seans
- Komisyon + slippage simülasyonu (config üzerinden)
- Mum içi SL/TP: long için low, short için high
- Muhafazakâr intrabar çakışma: SL önce alınır (config ile değiştirilebilir)
- Trailing stop backteste uyarlanmış (intrabar peak ile güncellenir)
- Veri kaynağı: "synthetic" (varsayılan) | "csv" | "ccxt"

Çalıştırma:
    python backtest.py
    python backtest.py --source csv
    python backtest.py --source ccxt
    python backtest.py --start 2024-01-01 --end 2025-01-01
"""

import argparse
import csv
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bot.config import STRATEGY, RISK, BACKTEST
from bot.logger_setup import setup_logger
from bot.risk_manager import GlobalRiskState
from bot.strategy import SignalEngine

setup_logger(log_file="logs/backtest.log")
logger = logging.getLogger(__name__)


# ── Sentetik veri üretimi ─────────────────────────────────────────────────────

def _generate_synthetic(
    start_price: float,
    n_minutes: int,
    start_dt: datetime,
    seed: int = 42,
    symbol_id: int = 0,
) -> pd.DataFrame:
    """
    GBM + GARCH benzeri volatilite kümelenmesi + trend rejimleri ile
    gerçekçi 5m sentetik OHLCV verisi üretir.
    """
    rng = np.random.default_rng(seed + symbol_id * 137)

    annual_drift = 0.35
    base_annual_vol = 0.70
    dt_frac = 1.0 / (365 * 24 * 12)   # 5m (yıl cinsinden: 365*24*60/5)
    base_sigma = base_annual_vol * np.sqrt(dt_frac)
    mu = annual_drift * dt_frac

    prices = np.zeros(n_minutes)
    volumes = np.zeros(n_minutes)
    prices[0] = start_price

    vol_state = base_sigma
    vol_persistence = 0.97
    vol_reaction = 0.04

    regime_dur = 0
    regime_drift = mu

    for i in range(1, n_minutes):
        regime_dur += 1
        if regime_dur > int(rng.exponential(5000)):   # 5000 × 5m ≈ 17 gün
            regime_dur = 0
            r = rng.random()
            if r < 0.40:
                regime_drift = abs(mu) * rng.uniform(1.5, 3.5)
            elif r < 0.70:
                regime_drift = -abs(mu) * rng.uniform(1.5, 3.5)
            else:
                regime_drift = mu * 0.1

        shock = rng.standard_normal()
        vol_state = (
            vol_persistence * vol_state
            + vol_reaction * abs(shock) * base_sigma
            + (1 - vol_persistence - vol_reaction) * base_sigma
        )
        vol_state = np.clip(vol_state, base_sigma * 0.2, base_sigma * 5.0)

        ret = regime_drift + vol_state * shock
        prices[i] = max(prices[i - 1] * np.exp(ret), 0.01)

        hour_frac = (i % 1440) / 60.0
        session_mult = 1.0 + 0.6 * np.exp(-((hour_frac - 14.5) ** 2) / 10)
        vol_mult = vol_state / base_sigma
        volumes[i] = max(1.0, rng.lognormal(
            mean=np.log(600 * session_mult * vol_mult), sigma=0.7
        ))

    volumes[0] = float(np.median(volumes[1:200]))

    opens = np.zeros(n_minutes)
    highs = np.zeros(n_minutes)
    lows = np.zeros(n_minutes)
    opens[0] = prices[0]

    for i in range(n_minutes):
        if i > 0:
            opens[i] = prices[i - 1]
        body = abs(prices[i] - opens[i])
        uw = body * rng.uniform(0.05, 0.9)
        lw = body * rng.uniform(0.05, 0.9)
        highs[i] = max(opens[i], prices[i]) + uw
        lows[i] = min(opens[i], prices[i]) - lw

    timestamps = [start_dt + timedelta(minutes=5 * i) for i in range(n_minutes)]
    return pd.DataFrame({
        "open_time": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": volumes,
    })


# ── CSV / ccxt yükleyiciler ────────────────────────────────────────────────────

def load_csv_data(symbol: str, csv_dir: str, start: datetime, end: datetime) -> pd.DataFrame:
    """
    CSV'den 5m OHLCV yükler.
    Dosya adı: <csv_dir>/<symbol>_5m.csv
    Başlık zorunlu: timestamp,open,high,low,close,volume
    timestamp: Unix ms (int) veya ISO8601 string
    """
    path = os.path.join(csv_dir, f"{symbol}_5m.csv")
    if not os.path.exists(path):
        raise FileNotFoundError(f"CSV bulunamadı: {path}")

    df = pd.read_csv(path)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV başlıkları eksik: {missing}")

    ts_sample = df["timestamp"].iloc[0]
    try:
        if isinstance(ts_sample, (int, float)) and float(ts_sample) > 1e12:
            df["open_time"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        else:
            df["open_time"] = pd.to_datetime(df["timestamp"], utc=True)
    except Exception as exc:
        raise ValueError(f"CSV timestamp parse hatası: {exc}")

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["open", "high", "low", "close", "volume"])
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    df = df[(df["open_time"] >= start_ts) & (df["open_time"] < end_ts)]
    df = df.sort_values("open_time").reset_index(drop=True)
    logger.info(f"[CSV] {symbol}: {len(df)} mum ({path})")
    return df


def load_ccxt_data(symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    """ccxt aracılığıyla Binance Futures'tan 5m veri çeker."""
    try:
        import ccxt
    except ImportError:
        raise ImportError("ccxt yüklü değil. Kurun: pip install ccxt")

    exchange = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": "future"}})
    ccxt_symbol = symbol.replace("USDT", "/USDT")
    since = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    limit = 1000
    all_ohlcv: list = []

    logger.info(f"[CCXT] {symbol} çekiliyor: {start.date()} → {end.date()}")
    while since < end_ms:
        try:
            batch = exchange.fetch_ohlcv(ccxt_symbol, "5m", since=since, limit=limit)
        except Exception as exc:
            logger.error(f"[CCXT] {symbol} hata: {exc}")
            time.sleep(3)
            continue
        if not batch:
            break
        all_ohlcv.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < limit:
            break
        time.sleep(0.35)

    if not all_ohlcv:
        raise RuntimeError(f"[CCXT] {symbol} için veri alınamadı")

    df = pd.DataFrame(all_ohlcv, columns=["ts_ms", "open", "high", "low", "close", "volume"])
    df["open_time"] = pd.to_datetime(df["ts_ms"], unit="ms", utc=True)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    end_ts = pd.Timestamp(end, tz="UTC")
    df = df[df["open_time"] < end_ts].sort_values("open_time").reset_index(drop=True)
    logger.info(f"[CCXT] {symbol}: {len(df)} mum")
    return df


def fetch_all_data(
    symbols: List[str], source: str, start: datetime, end: datetime
) -> Dict[str, pd.DataFrame]:
    """Her sembol için veri yükler."""
    n_minutes = int((end - start).total_seconds() / 300)  # 5m mumlar
    start_prices = {
        "BTCUSDT": BACKTEST.SYNTHETIC_BTC_START,
        "ETHUSDT": BACKTEST.SYNTHETIC_ETH_START,
    }
    result: Dict[str, pd.DataFrame] = {}

    for i, symbol in enumerate(symbols):
        if source == "synthetic":
            sp = start_prices.get(symbol, 1000.0)
            df = _generate_synthetic(sp, n_minutes, start, BACKTEST.SYNTHETIC_SEED, i)
            s, e = df["close"].iloc[0], df["close"].iloc[-1]
            logger.info(
                f"[SYNTHETIC] {symbol}: {len(df)} mum | "
                f"${s:,.0f} → ${e:,.0f} ({(e/s - 1)*100:+.1f}%)"
            )
        elif source == "csv":
            df = load_csv_data(symbol, BACKTEST.CSV_DIR, start, end)
        elif source == "ccxt":
            df = load_ccxt_data(symbol, start, end)
        else:
            raise ValueError(f"Bilinmeyen kaynak: {source}")
        result[symbol] = df

    return result


# ── Backtest pozisyon ve simülasyon ───────────────────────────────────────────

@dataclass
class BacktestTrade:
    symbol: str
    direction: str
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime]
    exit_price: Optional[float]
    qty: float
    notional: float
    sl_price: float
    tp_price: float
    atr_val: float
    exit_type: Optional[str]
    gross_pnl: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    net_pnl: float = 0.0


@dataclass
class SimPos:
    """Backtest açık pozisyonu."""
    symbol: str
    direction: str
    entry_time: datetime
    entry_price: float
    qty: float
    notional: float
    atr_val: float
    sl_price: float
    tp_price: float
    trailing_level_idx: int = -1
    sl_is_breakeven: bool = False


def _sim_trailing(pos: SimPos, ref_price: float) -> Optional[float]:
    """
    Backtest trailing stop güncelleme.
    Yeni SL döner ya da None.
    """
    levels = STRATEGY.TRAILING_LEVELS
    new_sl = None
    if pos.atr_val <= 0:
        return None

    if pos.direction == "LONG":
        profit_atr = (ref_price - pos.entry_price) / pos.atr_val
    else:
        profit_atr = (pos.entry_price - ref_price) / pos.atr_val

    for idx, (threshold, offset) in enumerate(levels):
        if idx <= pos.trailing_level_idx:
            continue
        if profit_atr < threshold:
            break

        if pos.direction == "LONG":
            candidate = pos.entry_price + offset * pos.atr_val
            if candidate > pos.sl_price:
                pos.sl_price = candidate
                pos.trailing_level_idx = idx
                pos.sl_is_breakeven = (offset == 0.0)
                new_sl = candidate
        else:
            candidate = pos.entry_price - offset * pos.atr_val
            if candidate < pos.sl_price:
                pos.sl_price = candidate
                pos.trailing_level_idx = idx
                pos.sl_is_breakeven = (offset == 0.0)
                new_sl = candidate
    return new_sl


def _intrabar_exit(
    pos: SimPos, candle_high: float, candle_low: float
) -> Optional[Tuple[str, float]]:
    """
    Mum içi SL/TP kontrolü.
    (exit_type, exit_price) döner; tetikleme yoksa None.

    Long: SL → low, TP → high
    Short: SL → high, TP → low
    Her ikisi tetiklenirse: CONSERVATIVE_INTRABAR → SL önce
    """
    if pos.direction == "LONG":
        sl_hit = candle_low <= pos.sl_price
        tp_hit = candle_high >= pos.tp_price
    else:
        sl_hit = candle_high >= pos.sl_price
        tp_hit = candle_low <= pos.tp_price

    if sl_hit and tp_hit:
        if BACKTEST.CONSERVATIVE_INTRABAR:
            return "SL", pos.sl_price
        return "TP", pos.tp_price

    if sl_hit:
        return "SL", pos.sl_price
    if tp_hit:
        return "TP", pos.tp_price
    return None


def _pnl(pos: SimPos, exit_price: float) -> Tuple[float, float, float, float]:
    """(gross, commission, slippage, net) döner."""
    if pos.direction == "LONG":
        gross = (exit_price - pos.entry_price) * pos.qty
    else:
        gross = (pos.entry_price - exit_price) * pos.qty
    comm = pos.notional * (BACKTEST.MAKER_FEE + BACKTEST.TAKER_FEE)
    slip = pos.notional * BACKTEST.SLIPPAGE_PCT
    return gross, comm, slip, gross - comm - slip


def precompute_1h(df_5m: pd.DataFrame) -> pd.DataFrame:
    """
    5m DataFrame'den tam 1H OHLCV hesaplar (başta bir kez çalışır).
    Her 1H satırına 5m'deki son index bilgisi eklenir (lookup için).
    """
    df = df_5m.copy()
    df = df.set_index("open_time")
    df_1h = df[["open", "high", "low", "close", "volume"]].resample("1h").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna()
    return df_1h.reset_index()


def get_1h_window(df_1h_full: pd.DataFrame, candle_time: datetime, n: int = 60) -> pd.DataFrame:
    """Verilen 5m mum zamanına kadar olan son n adet 1H mumu döner."""
    # candle_time'dan küçük olan 1H mumları al
    if hasattr(candle_time, "tzinfo") and candle_time.tzinfo is None:
        ct = candle_time
    else:
        ct = candle_time.replace(tzinfo=None) if hasattr(candle_time, "replace") else candle_time

    col = df_1h_full["open_time"]
    # tz-aware vs tz-naive uyumu
    try:
        mask = col < ct
    except TypeError:
        col_naive = col.dt.tz_localize(None) if col.dt.tz is not None else col
        ct_naive = ct.replace(tzinfo=None) if hasattr(ct, "tzinfo") and ct.tzinfo else ct
        mask = col_naive < ct_naive

    window = df_1h_full[mask].iloc[-n:]
    return window.reset_index(drop=True)


# ── Ana Backtester ────────────────────────────────────────────────────────────

class Backtester:
    """
    Portföy seviyeli backtest motoru.
    Tüm sembolleri aynı zaman dilimiyle işler, global risk state paylaşır.
    """

    def __init__(self, data: Dict[str, pd.DataFrame]):
        self.data = data
        self._risk = GlobalRiskState(symbols=list(data.keys()))
        self._strategy = SignalEngine()
        self._open: Dict[str, Optional[SimPos]] = {s: None for s in data}
        self.trades: List[BacktestTrade] = []
        self._equity = RISK.INITIAL_CAPITAL
        self._equity_curve: List[Tuple[datetime, float]] = []
        # 1H veri başta bir kez hesaplanır — her adımda lookup
        self._df_1h: Dict[str, pd.DataFrame] = {
            sym: precompute_1h(df) for sym, df in data.items()
        }
        logger.info(f"[BACKTEST] 1H precompute tamamlandı: "
                    f"{', '.join(f'{s}={len(v)} mum' for s, v in self._df_1h.items())}")

    def run(self) -> None:
        symbols = list(self.data.keys())
        min_len = min(len(df) for df in self.data.values())

        warmup = max(
            STRATEGY.ATR_PERIOD,
            STRATEGY.EMA_SLOW,
            STRATEGY.RSI_PERIOD,
            STRATEGY.VOL_SMA_PERIOD,
        ) + 5

        total = min_len - warmup
        logger.info(
            f"[BACKTEST] Başlatılıyor | warmup={warmup} | işlenecek={total} mum | {symbols}"
        )
        print(f"\nBacktest çalışıyor: {total:,} mum işlenecek...")

        for i in range(warmup, min_len):
            raw_time = self.data[symbols[0]]["open_time"].iloc[i]
            candle_time = (
                raw_time.to_pydatetime()
                if hasattr(raw_time, "to_pydatetime")
                else raw_time
            )
            date_str = candle_time.strftime("%Y-%m-%d")

            # Gün sıfırı + BE cooldown
            self._risk.on_candle_close(date_str)

            sim_ts = (
                candle_time.replace(tzinfo=timezone.utc).timestamp()
                if candle_time.tzinfo is None
                else candle_time.timestamp()
            )

            for symbol in symbols:
                self._step(symbol, i, candle_time, sim_ts)

            # Equity kaydet (her 12 mum = 1 saat)
            if i % 12 == 0:
                unrealized = sum(self._unrealized(s, i) for s in symbols)
                self._equity_curve.append((candle_time, self._equity + unrealized))

            # İlerleme (her 50k mum)
            progress_i = i - warmup
            if progress_i > 0 and progress_i % 50000 == 0:
                pct = progress_i / total * 100
                print(f"  %{pct:.0f} ({progress_i:,}/{total:,}) | sermaye={self._equity:.2f}")

        # Son açık pozisyonları kapat
        for symbol in symbols:
            self._force_close(symbol, min_len - 1)

        logger.info(
            f"[BACKTEST] Bitti | {len(self.trades)} işlem | son_sermaye={self._equity:.4f}"
        )

    def _step(self, symbol: str, idx: int, candle_time: datetime, sim_ts: float) -> None:
        df = self.data[symbol]
        row = df.iloc[idx]
        c_high = float(row["high"])
        c_low = float(row["low"])
        c_close = float(row["close"])

        pos = self._open[symbol]

        # ── Açık pozisyon yönetimi ────────────────────────────────────────────
        if pos:
            # Trailing: long için high, short için low (intrabar peak)
            peak = c_high if pos.direction == "LONG" else c_low
            _sim_trailing(pos, peak)

            exit_result = _intrabar_exit(pos, c_high, c_low)
            if exit_result:
                exit_type_raw, exit_price = exit_result
                exit_type = (
                    "BE_STOP" if exit_type_raw == "SL" and pos.sl_is_breakeven
                    else exit_type_raw
                )
                self._close(symbol, idx, candle_time, exit_price, exit_type, sim_ts)
            return

        # ── Seans filtresi ─────────────────────────────────────────────────────
        dt_utc = (
            candle_time.replace(tzinfo=timezone.utc)
            if candle_time.tzinfo is None else candle_time
        )
        session_ok, _ = GlobalRiskState.is_session_allowed(dt_utc)
        if not session_ok:
            return

        # ── Sinyal değerlendirme ───────────────────────────────────────────────
        window = df.iloc[max(0, idx - STRATEGY.PRIMARY_CANDLES + 1): idx + 1].copy()
        df_1h = get_1h_window(self._df_1h[symbol], candle_time, n=STRATEGY.HTF_CANDLES)

        signal = self._strategy.get_signal(window, df_1h)
        if signal.signal == "HOLD":
            return

        direction = signal.signal
        can_open, _ = self._risk.can_open(symbol, direction, simulated_now_ts=sim_ts)
        if not can_open:
            return

        sl_dist_pct = abs(c_close - signal.sl_price) / c_close
        notional = self._risk.calc_position_size(sl_dist_pct)
        qty = notional / c_close

        pos = SimPos(
            symbol=symbol,
            direction=direction,
            entry_time=candle_time,
            entry_price=c_close,
            qty=qty,
            notional=notional,
            atr_val=signal.atr_value,
            sl_price=signal.sl_price,
            tp_price=signal.tp_price,
        )
        self._open[symbol] = pos
        self._risk.on_trade_opened(symbol)

    def _close(
        self,
        symbol: str,
        idx: int,
        candle_time: datetime,
        exit_price: float,
        exit_type: str,
        sim_ts: float,
    ) -> None:
        pos = self._open[symbol]
        if not pos:
            return

        gross, comm, slip, net = _pnl(pos, exit_price)
        self._equity += net
        self._risk.on_trade_closed(
            symbol, pos.direction, net, exit_type, simulated_now_ts=sim_ts
        )

        self.trades.append(BacktestTrade(
            symbol=symbol,
            direction=pos.direction,
            entry_time=pos.entry_time,
            entry_price=pos.entry_price,
            exit_time=candle_time,
            exit_price=exit_price,
            qty=pos.qty,
            notional=pos.notional,
            sl_price=pos.sl_price,
            tp_price=pos.tp_price,
            atr_val=pos.atr_val,
            exit_type=exit_type,
            gross_pnl=gross,
            commission=comm,
            slippage=slip,
            net_pnl=net,
        ))
        self._open[symbol] = None

    def _force_close(self, symbol: str, idx: int) -> None:
        pos = self._open[symbol]
        if not pos:
            return
        df = self.data[symbol]
        exit_price = float(df["close"].iloc[idx])
        raw_time = df["open_time"].iloc[idx]
        ct = raw_time.to_pydatetime() if hasattr(raw_time, "to_pydatetime") else raw_time
        sim_ts = (
            ct.replace(tzinfo=timezone.utc).timestamp()
            if ct.tzinfo is None else ct.timestamp()
        )
        self._close(symbol, idx, ct, exit_price, "CLOSE_END", sim_ts)

    def _unrealized(self, symbol: str, idx: int) -> float:
        pos = self._open[symbol]
        if not pos:
            return 0.0
        price = float(self.data[symbol]["close"].iloc[idx])
        if pos.direction == "LONG":
            return (price - pos.entry_price) * pos.qty
        return (pos.entry_price - price) * pos.qty


# ── Sonuç raporlama ───────────────────────────────────────────────────────────

def print_results(
    bt: Backtester, data: Dict[str, pd.DataFrame], start: datetime, end: datetime
) -> None:
    trades = bt.trades
    initial = RISK.INITIAL_CAPITAL
    final = bt._equity
    total_ret_pct = (final - initial) / initial * 100

    print("\n" + "=" * 70)
    print("       BACKTEST SONUÇLARI — PORTFÖY (BTCUSDT + ETHUSDT)")
    print("=" * 70)
    print(f"  Tarih      : {start.date()} → {end.date()}")
    print(f"  Kaynak     : {BACKTEST.DATA_SOURCE}")
    print(f"  Komisyon   : Maker {BACKTEST.MAKER_FEE*100:.2f}% + Taker {BACKTEST.TAKER_FEE*100:.2f}%")
    print(f"  Slippage   : {BACKTEST.SLIPPAGE_PCT*100:.2f}%  |  Kaldıraç: {RISK.LEVERAGE}x")

    print(f"\n{'─' * 70}")
    print("  BENCHMARK (Buy & Hold)")
    print(f"{'─' * 70}")
    for sym, df in data.items():
        s_p, e_p = df["close"].iloc[0], df["close"].iloc[-1]
        print(f"  {sym:<12} ${s_p:>10,.2f} → ${e_p:>10,.2f}  ({(e_p/s_p-1)*100:+.1f}%)")

    print(f"\n{'─' * 70}")
    print("  SERMAYE")
    print(f"{'─' * 70}")
    print(f"  Başlangıç  : ${initial:.2f}")
    print(f"  Son        : ${final:.4f}")
    print(f"  Getiri     : {total_ret_pct:+.2f}%  |  Net: ${final-initial:+.4f}")

    if not trades:
        print("\n  [!] İşlem bulunamadı.")
        print("=" * 70)
        return

    df_t = pd.DataFrame([t.__dict__ for t in trades])
    winning = df_t[df_t["net_pnl"] > 0]
    losing = df_t[df_t["net_pnl"] <= 0]
    wr = len(winning) / len(df_t) * 100

    print(f"\n{'─' * 70}")
    print("  İŞLEM İSTATİSTİKLERİ")
    print(f"{'─' * 70}")
    print(f"  Toplam işlem      : {len(df_t)}")
    print(f"  Kazanan           : {len(winning)} ({wr:.1f}%)")
    print(f"  Kaybeden          : {len(losing)} ({100-wr:.1f}%)")

    print(f"\n  Çıkış türleri:")
    for et, cnt in df_t["exit_type"].value_counts().items():
        print(f"    {et:<15} {cnt}")

    if len(winning):
        print(f"\n  Ort. kazanç/işlem : ${winning['net_pnl'].mean():.4f}")
        print(f"  Max kazanç        : ${winning['net_pnl'].max():.4f}")
    if len(losing):
        print(f"  Ort. kayıp/işlem  : ${losing['net_pnl'].mean():.4f}")
        print(f"  Max kayıp         : ${losing['net_pnl'].min():.4f}")

    gw = winning["net_pnl"].sum() if len(winning) else 0.0
    gl = abs(losing["net_pnl"].sum()) if len(losing) else 0.0
    pf = gw / gl if gl > 0 else float("inf")
    aw = winning["net_pnl"].mean() if len(winning) else 0.0
    al = abs(losing["net_pnl"].mean()) if len(losing) else 1.0

    print(f"\n  Profit factor     : {pf:.2f}")
    print(f"  Risk/ödül oranı   : {aw/al:.2f}")
    print(f"  Toplam komisyon   : ${df_t['commission'].sum():.4f}")
    print(f"  Toplam slippage   : ${df_t['slippage'].sum():.4f}")

    print(f"\n{'─' * 70}")
    print("  SEMBOL BAZLI DAĞILIM")
    print(f"{'─' * 70}")
    for sym in data.keys():
        st = df_t[df_t["symbol"] == sym]
        if len(st) == 0:
            print(f"  {sym:<12} işlem yok")
            continue
        sym_wr = (st["net_pnl"] > 0).mean() * 100
        print(
            f"  {sym:<12} {len(st):>4} işlem | "
            f"net={st['net_pnl'].sum():+.4f} | WR={sym_wr:.1f}%"
        )

    print(f"\n{'─' * 70}")
    print("  RİSK METRİKLERİ")
    print(f"{'─' * 70}")
    eq_curve = bt._equity_curve
    if eq_curve:
        eq_arr = np.array([e for _, e in eq_curve])
        peak_arr = np.maximum.accumulate(eq_arr)
        dd = (eq_arr - peak_arr) / peak_arr * 100
        print(f"  Max drawdown      : {dd.min():.2f}%")

    exit_times = pd.to_datetime([t.exit_time for t in trades])
    df_t["exit_date"] = exit_times.normalize()
    daily_pnl = df_t.groupby("exit_date")["net_pnl"].sum()
    if len(daily_pnl) > 1:
        std = daily_pnl.std()
        if std > 0:
            sharpe = (daily_pnl.mean() / std) * np.sqrt(365)
            print(f"  Sharpe (yıllık)   : {sharpe:.2f}")

    # Aylık tablo
    print(f"\n{'─' * 70}")
    print("  AYLIK PERFORMANS")
    print(f"{'─' * 70}")
    df_t["exit_month"] = exit_times.to_period("M")
    monthly = df_t.groupby("exit_month")["net_pnl"].sum()
    for month, pnl_val in monthly.items():
        bar = "▲" if pnl_val >= 0 else "▼"
        pct_m = pnl_val / initial * 100
        print(f"  {bar} {str(month):<8}  ${pnl_val:+.4f}  ({pct_m:+.2f}%)")

    print("\n" + "=" * 70)


def save_trade_log(trades: List[BacktestTrade], path: str) -> None:
    if not trades:
        return
    fields = list(trades[0].__dict__.keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for t in trades:
            writer.writerow(t.__dict__)
    logger.info(f"[OUTPUT] Trade log: {path}")


def save_daily_log(trades: List[BacktestTrade], path: str) -> None:
    if not trades:
        return
    df = pd.DataFrame([t.__dict__ for t in trades])
    exit_times = pd.to_datetime([t.exit_time for t in trades])
    df["date"] = exit_times.normalize()
    daily = df.groupby("date").agg(
        trades=("net_pnl", "count"),
        net_pnl=("net_pnl", "sum"),
        gross_pnl=("gross_pnl", "sum"),
    ).reset_index()
    daily.to_csv(path, index=False)
    logger.info(f"[OUTPUT] Günlük log: {path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Futures Momentum Bot Backtester")
    p.add_argument(
        "--source", choices=["synthetic", "csv", "ccxt"],
        default=BACKTEST.DATA_SOURCE, help="Veri kaynağı (varsayılan: %(default)s)"
    )
    p.add_argument("--start", default=BACKTEST.START_DATE, help="YYYY-MM-DD")
    p.add_argument("--end", default=BACKTEST.END_DATE, help="YYYY-MM-DD")
    return p.parse_args()


def main():
    args = parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")

    print("=" * 70)
    print("  FUTURES MOMENTUM BOT — BACKTEST")
    print(f"  Kaynak: {args.source} | {start.date()} → {end.date()}")
    print("=" * 70)

    data = fetch_all_data(STRATEGY.SYMBOLS, args.source, start, end)
    bt = Backtester(data)
    bt.run()
    print_results(bt, data, start, end)
    save_trade_log(bt.trades, BACKTEST.TRADE_LOG_CSV)
    save_daily_log(bt.trades, BACKTEST.DAILY_LOG_CSV)
    print(f"Trade log: {BACKTEST.TRADE_LOG_CSV}")
    print(f"Günlük log: {BACKTEST.DAILY_LOG_CSV}\n")


if __name__ == "__main__":
    main()
