"""
Binance Momentum Trade Bot - 1 Yıllık Backtest
================================================
BTCUSDT 1dk verisi ile strateji performans testi.

Veri kaynağı:
  - Öncelik: Binance API (public endpoint)
  - Yedek: Gerçekçi sentetik BTC verisi (GBM + volatilite kümelenmesi)

Kullanım:
  python backtest.py              # Önce API dener, başarısız olursa sentetik
  python backtest.py --synthetic  # Direkt sentetik veri kullan
"""

import sys
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from bot.config import (
    CAPITAL, MOMENTUM_PERIOD, MOMENTUM_BUY_THRESHOLD, MOMENTUM_SELL_THRESHOLD,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, MAX_POSITION_PCT,
    DAILY_PROFIT_TARGET_PCT, DAILY_LOSS_LIMIT_PCT
)


def fetch_from_binance() -> pd.DataFrame:
    """Binance API'den 1 yıllık 1dk BTCUSDT verisini çeker."""
    from binance.client import Client
    client = Client("", "")
    symbol = "BTCUSDT"
    interval = Client.KLINE_INTERVAL_1MINUTE

    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=365)

    all_data = []
    current_start = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    batch = 0

    print(f"Binance API'den veri çekiliyor: {start_dt.date()} -> {end_dt.date()}")

    while current_start < end_ms:
        klines = client.get_klines(
            symbol=symbol, interval=interval,
            startTime=current_start, limit=1000
        )
        if not klines:
            break
        all_data.extend(klines)
        current_start = klines[-1][6] + 1
        batch += 1
        if batch % 100 == 0:
            print(f"  ... {len(all_data):,} mum çekildi")
        time.sleep(0.1)

    df = pd.DataFrame(all_data, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df = df.drop_duplicates(subset=["open_time"]).reset_index(drop=True)
    return df


def generate_synthetic_btc_data() -> pd.DataFrame:
    """Gerçekçi 1 yıllık sentetik BTC 1dk verisi üretir.

    Geometric Brownian Motion + GARCH benzeri volatilite kümelenmesi +
    trend rejim değişimleri ile gerçekçi BTC fiyat hareketi simüle eder.
    """
    np.random.seed(42)
    n_minutes = 365 * 24 * 60  # 525,600 dakika

    start_price = 65000.0  # BTC başlangıç fiyatı
    annual_drift = 0.40  # Yıllık %40 beklenen getiri (kripto)
    base_annual_vol = 0.70  # Yıllık %70 volatilite (BTC tipik)

    dt = 1 / (365 * 24 * 60)  # 1 dakika zaman adımı (yıl cinsinden)
    mu = annual_drift * dt
    base_sigma = base_annual_vol * np.sqrt(dt)

    prices = np.zeros(n_minutes)
    volumes = np.zeros(n_minutes)
    prices[0] = start_price

    # Volatilite kümelenmesi (basit GARCH benzeri)
    vol_state = base_sigma
    vol_persistence = 0.98
    vol_reaction = 0.05

    # Trend rejimleri (bull/bear/sideways)
    regime_duration = 0
    regime_type = "bull"  # bull, bear, sideways
    regime_drift = mu

    for i in range(1, n_minutes):
        # Rejim değişimi (ortalama 2-4 hafta süren trendler)
        regime_duration += 1
        if regime_duration > np.random.exponential(20000):  # ~14 günde bir değiş
            regime_duration = 0
            r = np.random.random()
            if r < 0.4:
                regime_type = "bull"
                regime_drift = abs(mu) * np.random.uniform(1, 3)
            elif r < 0.7:
                regime_type = "bear"
                regime_drift = -abs(mu) * np.random.uniform(1, 3)
            else:
                regime_type = "sideways"
                regime_drift = mu * 0.1

        # GARCH benzeri volatilite güncelleme
        shock = np.random.standard_normal()
        vol_state = (vol_persistence * vol_state +
                     vol_reaction * abs(shock) * base_sigma +
                     (1 - vol_persistence - vol_reaction) * base_sigma)
        vol_state = np.clip(vol_state, base_sigma * 0.3, base_sigma * 4.0)

        # Fiyat hareketi (GBM)
        ret = regime_drift + vol_state * shock
        prices[i] = prices[i - 1] * np.exp(ret)

        # Hacim (volatilite ile korelasyonlu + gün içi pattern)
        hour = (i % 1440) / 60  # Saat (0-24)
        # NY/London session'da yüksek hacim
        session_factor = 1.0 + 0.5 * np.exp(-((hour - 15) ** 2) / 8)
        vol_factor = (vol_state / base_sigma)
        volumes[i] = max(10, np.random.lognormal(
            mean=np.log(500 * session_factor * vol_factor), sigma=0.6
        ))

    volumes[0] = np.median(volumes[1:100])

    # OHLC oluştur
    start_dt = datetime.utcnow() - timedelta(days=365)
    timestamps = [start_dt + timedelta(minutes=i) for i in range(n_minutes)]

    # High/Low hesapla (gerçekçi wicks)
    highs = np.zeros(n_minutes)
    lows = np.zeros(n_minutes)
    opens = np.zeros(n_minutes)
    opens[0] = prices[0]

    for i in range(n_minutes):
        if i > 0:
            opens[i] = prices[i - 1]  # Open = önceki close
        wick_size = abs(prices[i] - opens[i]) * np.random.uniform(0.1, 0.8)
        highs[i] = max(opens[i], prices[i]) + abs(wick_size) * np.random.uniform(0, 1)
        lows[i] = min(opens[i], prices[i]) - abs(wick_size) * np.random.uniform(0, 1)

    df = pd.DataFrame({
        "open_time": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": prices,
        "volume": volumes,
    })

    print(f"Sentetik BTC verisi üretildi: {n_minutes:,} mum")
    print(f"Tarih aralığı: {df['open_time'].iloc[0].date()} -> {df['open_time'].iloc[-1].date()}")
    print(f"Fiyat aralığı: ${df['close'].min():,.0f} - ${df['close'].max():,.0f}")
    print(f"Başlangıç: ${prices[0]:,.0f} -> Bitiş: ${prices[-1]:,.0f} "
          f"(%{((prices[-1]-prices[0])/prices[0]*100):+.1f})")
    return df


def fetch_1year_data(force_synthetic: bool = False) -> pd.DataFrame:
    """Veri kaynağını seçer: API veya sentetik."""
    if not force_synthetic:
        try:
            print("Binance API deneniyor...")
            df = fetch_from_binance()
            print(f"Toplam {len(df):,} mum verisi çekildi.")
            return df
        except Exception as e:
            print(f"Binance API erişilemedi: {type(e).__name__}")
            print("Sentetik veriye geçiliyor...\n")

    return generate_synthetic_btc_data()


class Backtester:
    """Stratejiyi geçmiş veri üzerinde test eder."""

    def __init__(self, df: pd.DataFrame):
        self.df = df
        self.capital = CAPITAL
        self.starting_capital = CAPITAL
        self.in_position = False
        self.entry_price = 0.0
        self.position_qty = 0.0
        self.position_cost = 0.0

        # Günlük takip
        self.daily_pnl = 0.0
        self.current_day = None
        self.daily_trade_blocked = False

        # İstatistikler
        self.trades = []
        self.equity_curve = []
        self.daily_returns = []
        self.signals_count = {"BUY": 0, "SELL": 0, "HOLD": 0}

    def _calc_momentum(self, window: pd.DataFrame) -> float:
        if len(window) < MOMENTUM_PERIOD:
            return 0.0
        cur = window["close"].iloc[-1]
        past = window["close"].iloc[-MOMENTUM_PERIOD]
        if past == 0:
            return 0.0
        return ((cur - past) / past) * 100

    def _calc_rsi(self, window: pd.DataFrame, period: int = 14) -> float:
        if len(window) < period + 1:
            return 50.0
        delta = window["close"].diff()
        gain = delta.where(delta > 0, 0.0).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window=period).mean()
        last_loss = loss.iloc[-1]
        if last_loss == 0:
            return 100.0
        rs = gain.iloc[-1] / last_loss
        return 100 - (100 / (1 + rs))

    def _calc_volume_spike(self, window: pd.DataFrame, period: int = 20) -> bool:
        if len(window) < period:
            return False
        avg_vol = window["volume"].iloc[-period:].mean()
        cur_vol = window["volume"].iloc[-1]
        return cur_vol > avg_vol * 1.5

    def _get_signal(self, window: pd.DataFrame) -> str:
        momentum = self._calc_momentum(window)
        rsi = self._calc_rsi(window)
        vol_spike = self._calc_volume_spike(window)

        if momentum > MOMENTUM_BUY_THRESHOLD and rsi < 70:
            if vol_spike or momentum > MOMENTUM_BUY_THRESHOLD * 2:
                return "BUY"
        if momentum < MOMENTUM_SELL_THRESHOLD or rsi > 80:
            return "SELL"
        return "HOLD"

    def _check_daily_reset(self, current_time):
        day = current_time.date()
        if self.current_day != day:
            if self.current_day is not None:
                self.daily_returns.append({
                    "date": self.current_day,
                    "pnl": self.daily_pnl,
                    "pnl_pct": (self.daily_pnl / self.starting_capital) * 100
                })
            self.daily_pnl = 0.0
            self.current_day = day
            self.daily_trade_blocked = False

    def _check_daily_limits(self) -> bool:
        if self.starting_capital == 0:
            return False
        pnl_pct = (self.daily_pnl / self.starting_capital) * 100
        if pnl_pct >= DAILY_PROFIT_TARGET_PCT:
            return False
        if pnl_pct <= -DAILY_LOSS_LIMIT_PCT:
            self.daily_trade_blocked = True
            return False
        return True

    def run(self):
        """Backtest çalıştır."""
        lookback = 50  # Sinyal hesabı için gereken minimum mum sayısı
        total = len(self.df)

        print(f"\nBacktest başlatılıyor...")
        print(f"Sermaye: ${self.capital:.2f}")
        print(f"Toplam mum: {total:,}\n")

        for i in range(lookback, total):
            window = self.df.iloc[i - lookback:i + 1].copy()
            row = self.df.iloc[i]
            price = row["close"]
            ts = row["open_time"]

            self._check_daily_reset(ts)

            # Equity kaydet (her 60 mumda bir = 1 saat)
            if i % 60 == 0:
                unrealized = 0.0
                if self.in_position:
                    unrealized = self.position_qty * price - self.position_cost
                self.equity_curve.append({
                    "time": ts,
                    "equity": self.capital + unrealized
                })

            # Stop-loss / take-profit kontrol
            if self.in_position:
                pnl_pct = ((price - self.entry_price) / self.entry_price) * 100
                if pnl_pct <= -STOP_LOSS_PCT:
                    pnl = self.position_qty * price - self.position_cost
                    self.capital += pnl
                    self.daily_pnl += pnl
                    self.trades.append({
                        "type": "STOP_LOSS", "entry": self.entry_price,
                        "exit": price, "pnl": pnl, "pnl_pct": pnl_pct, "time": ts
                    })
                    self.in_position = False
                    continue

                if pnl_pct >= TAKE_PROFIT_PCT:
                    pnl = self.position_qty * price - self.position_cost
                    self.capital += pnl
                    self.daily_pnl += pnl
                    self.trades.append({
                        "type": "TAKE_PROFIT", "entry": self.entry_price,
                        "exit": price, "pnl": pnl, "pnl_pct": pnl_pct, "time": ts
                    })
                    self.in_position = False
                    continue

            # Sinyal al
            signal = self._get_signal(window)
            self.signals_count[signal] += 1

            # AL
            if signal == "BUY" and not self.in_position:
                if not self.daily_trade_blocked and self._check_daily_limits():
                    size = self.capital * (MAX_POSITION_PCT / 100)
                    if size > 1.0:
                        self.position_qty = size / price
                        self.position_cost = size
                        self.entry_price = price
                        self.in_position = True

            # SAT
            elif signal == "SELL" and self.in_position:
                pnl_pct_trade = ((price - self.entry_price) / self.entry_price) * 100
                pnl = self.position_qty * price - self.position_cost
                self.capital += pnl
                self.daily_pnl += pnl
                self.trades.append({
                    "type": "SIGNAL_SELL", "entry": self.entry_price,
                    "exit": price, "pnl": pnl, "pnl_pct": pnl_pct_trade, "time": ts
                })
                self.in_position = False

            # İlerleme göster
            if i % 100000 == 0 and i > 0:
                pct = (i / total) * 100
                print(f"  İlerleme: %{pct:.0f} ({i:,}/{total:,}) | Sermaye: ${self.capital:.2f}")

        # Son açık pozisyonu kapat
        if self.in_position:
            last_price = self.df["close"].iloc[-1]
            pnl = self.position_qty * last_price - self.position_cost
            self.capital += pnl
            self.daily_pnl += pnl
            self.trades.append({
                "type": "CLOSE_END", "entry": self.entry_price,
                "exit": last_price, "pnl": pnl,
                "pnl_pct": ((last_price - self.entry_price) / self.entry_price) * 100,
                "time": self.df["open_time"].iloc[-1]
            })
            self.in_position = False

        # Son günü kaydet
        if self.current_day:
            self.daily_returns.append({
                "date": self.current_day,
                "pnl": self.daily_pnl,
                "pnl_pct": (self.daily_pnl / self.starting_capital) * 100
            })

    def print_results(self):
        """Backtest sonuçlarını yazdır."""
        trades_df = pd.DataFrame(self.trades)
        daily_df = pd.DataFrame(self.daily_returns)
        equity_df = pd.DataFrame(self.equity_curve)

        total_return = ((self.capital - CAPITAL) / CAPITAL) * 100
        winning = trades_df[trades_df["pnl"] > 0] if len(trades_df) > 0 else pd.DataFrame()
        losing = trades_df[trades_df["pnl"] <= 0] if len(trades_df) > 0 else pd.DataFrame()

        print("\n" + "=" * 65)
        print("           BACKTEST SONUÇLARI - 1 YILLIK BTCUSDT")
        print("=" * 65)

        # BTC fiyat değişimi
        btc_start = self.df["close"].iloc[0]
        btc_end = self.df["close"].iloc[-1]
        btc_return = ((btc_end - btc_start) / btc_start) * 100

        print(f"\n{'BTC Fiyat Başlangıç:':<30} ${btc_start:,.2f}")
        print(f"{'BTC Fiyat Bitiş:':<30} ${btc_end:,.2f}")
        print(f"{'BTC Getiri (Buy&Hold):':<30} %{btc_return:.2f}")

        print(f"\n{'─' * 65}")
        print(f"{'SERMAYE':<30}")
        print(f"{'─' * 65}")
        print(f"{'Başlangıç Sermayesi:':<30} ${CAPITAL:.2f}")
        print(f"{'Son Sermaye:':<30} ${self.capital:.2f}")
        print(f"{'Toplam Getiri:':<30} %{total_return:.2f}")
        print(f"{'Net Kar/Zarar:':<30} ${self.capital - CAPITAL:.2f}")

        if len(trades_df) > 0:
            print(f"\n{'─' * 65}")
            print(f"{'İŞLEM İSTATİSTİKLERİ':<30}")
            print(f"{'─' * 65}")
            print(f"{'Toplam İşlem:':<30} {len(trades_df)}")
            print(f"{'Kazanan İşlem:':<30} {len(winning)} (%{len(winning)/len(trades_df)*100:.1f})")
            print(f"{'Kaybeden İşlem:':<30} {len(losing)} (%{len(losing)/len(trades_df)*100:.1f})")

            # İşlem türü dağılımı
            type_counts = trades_df["type"].value_counts()
            print(f"\n  İşlem Türü Dağılımı:")
            for t, c in type_counts.items():
                print(f"    {t:<20} {c}")

            if len(winning) > 0:
                print(f"\n{'Ort. Kazanç/İşlem:':<30} ${winning['pnl'].mean():.2f}")
                print(f"{'Max Kazanç:':<30} ${winning['pnl'].max():.2f}")
            if len(losing) > 0:
                print(f"{'Ort. Kayıp/İşlem:':<30} ${losing['pnl'].mean():.2f}")
                print(f"{'Max Kayıp:':<30} ${losing['pnl'].min():.2f}")

            avg_win = winning["pnl"].mean() if len(winning) > 0 else 0
            avg_loss = abs(losing["pnl"].mean()) if len(losing) > 0 else 1
            profit_factor = (winning["pnl"].sum() / abs(losing["pnl"].sum())) if len(losing) > 0 and losing["pnl"].sum() != 0 else float("inf")

            print(f"\n{'Risk/Ödül Oranı:':<30} {avg_win/avg_loss:.2f}" if avg_loss > 0 else "")
            print(f"{'Profit Factor:':<30} {profit_factor:.2f}")

        if len(daily_df) > 0:
            print(f"\n{'─' * 65}")
            print(f"{'GÜNLÜK PERFORMANS':<30}")
            print(f"{'─' * 65}")
            profitable_days = daily_df[daily_df["pnl"] > 0]
            losing_days = daily_df[daily_df["pnl"] < 0]
            target_hit_days = daily_df[daily_df["pnl_pct"] >= DAILY_PROFIT_TARGET_PCT]
            loss_limit_days = daily_df[daily_df["pnl_pct"] <= -DAILY_LOSS_LIMIT_PCT]

            print(f"{'Toplam Gün:':<30} {len(daily_df)}")
            print(f"{'Karlı Gün:':<30} {len(profitable_days)} (%{len(profitable_days)/len(daily_df)*100:.1f})")
            print(f"{'Zararlı Gün:':<30} {len(losing_days)} (%{len(losing_days)/len(daily_df)*100:.1f})")
            print(f"{'%10 Hedefe Ulaşan Gün:':<30} {len(target_hit_days)}")
            print(f"{'%5 Zarar Limiti Gün:':<30} {len(loss_limit_days)}")
            print(f"{'Ort. Günlük Getiri:':<30} %{daily_df['pnl_pct'].mean():.3f}")
            print(f"{'En İyi Gün:':<30} %{daily_df['pnl_pct'].max():.2f}")
            print(f"{'En Kötü Gün:':<30} %{daily_df['pnl_pct'].min():.2f}")

        if len(equity_df) > 0:
            print(f"\n{'─' * 65}")
            print(f"{'RİSK METRİKLERİ':<30}")
            print(f"{'─' * 65}")
            # Max drawdown
            equity_df["peak"] = equity_df["equity"].cummax()
            equity_df["drawdown"] = (equity_df["equity"] - equity_df["peak"]) / equity_df["peak"] * 100
            max_dd = equity_df["drawdown"].min()
            print(f"{'Max Drawdown:':<30} %{max_dd:.2f}")

            # Sharpe ratio (günlük bazda)
            if len(daily_df) > 1:
                daily_std = daily_df["pnl_pct"].std()
                if daily_std > 0:
                    sharpe = (daily_df["pnl_pct"].mean() / daily_std) * np.sqrt(365)
                    print(f"{'Sharpe Ratio (yıllık):':<30} {sharpe:.2f}")

        print(f"\n{'─' * 65}")
        print(f"{'SİNYAL DAĞILIMI':<30}")
        print(f"{'─' * 65}")
        for s, c in self.signals_count.items():
            print(f"  {s:<10} {c:>10,}")

        print("\n" + "=" * 65)

        # Aylık performans tablosu
        if len(daily_df) > 0:
            print(f"\n{'AYLIK PERFORMANS TABLOSU'}")
            print(f"{'─' * 40}")
            daily_df["date"] = pd.to_datetime(daily_df["date"])
            daily_df["month"] = daily_df["date"].dt.to_period("M")
            monthly = daily_df.groupby("month").agg(
                pnl=("pnl", "sum"),
                trades_days=("pnl", "count")
            )
            monthly["pnl_pct"] = (monthly["pnl"] / CAPITAL) * 100
            for idx, row in monthly.iterrows():
                marker = "+" if row["pnl"] >= 0 else ""
                print(f"  {str(idx):<10} {marker}${row['pnl']:.2f}  ({marker}{row['pnl_pct']:.1f}%)")

        print()


def main():
    force_synthetic = "--synthetic" in sys.argv

    print("=" * 65)
    print("  BINANCE MOMENTUM TRADE BOT - BACKTEST (1 YIL)")
    print("=" * 65)

    df = fetch_1year_data(force_synthetic=force_synthetic)

    bt = Backtester(df)
    bt.run()
    bt.print_results()


if __name__ == "__main__":
    main()
