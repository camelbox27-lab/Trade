"""
Binance USDT-M Futures Momentum Trade Bot
==========================================
Ana giriş noktası.

Desteklenen semboller: BTCUSDT, ETHUSDT (config.STRATEGY.SYMBOLS)
Timeframe: 5m kapanmış mumlar
Kaldıraç: 3x Isolated (config.RISK.LEVERAGE)

Çalıştırma:
    python main.py
"""

import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from dotenv import load_dotenv
from binance.client import Client

from bot.config import STRATEGY, RISK, EXECUTION
from bot.logger_setup import setup_logger
from bot.data_fetcher import DataFetcher
from bot.strategy import SignalEngine
from bot.risk_manager import GlobalRiskState
from bot.executor import FuturesExecutor

load_dotenv()
setup_logger()
logger = logging.getLogger(__name__)


# ── Yardımcı fonksiyonlar ─────────────────────────────────────────────────────

def check_position_mode(client: Client) -> bool:
    """
    Hesabın One-Way Mode'da olduğunu doğrular.
    Hedge Mode'da çalışmak güvensizdir; bot durur.
    """
    try:
        res = client.futures_get_position_mode()
        is_hedge = res.get("dualSidePosition", False)
        if is_hedge:
            logger.error(
                "[POSITION_MODE] UYARI: Hesap Hedge Mode'da! "
                "Bot One-Way Mode gerektirir. "
                "Binance Futures > Tercihler > Pozisyon Modu > One-Way Mode seçin."
            )
            return False
        logger.info("[POSITION_MODE] One-Way Mode doğrulandı")
        return True
    except Exception as exc:
        logger.error(f"[POSITION_MODE] Kontrol hatası: {exc}")
        return False


def seconds_to_next_5m() -> float:
    """Sonraki 5 dakikalık mum kapanışına kalan saniyeyi döner."""
    now = time.time()
    interval = 300  # 5 × 60
    next_close = math.ceil(now / interval) * interval
    return max(0.0, next_close - now)


# ── Ana bot sınıfı ────────────────────────────────────────────────────────────

class TradeBot:
    """
    BTCUSDT + ETHUSDT üzerinde eşzamanlı çalışan momentum botu.

    Mimari:
    - Her 5m mum kapanışında sinyal değerlendirilir (intrabar sinyal yok).
    - Mum içi SL/TP takibi 10 saniyelik polling ile yapılır.
    - Internal stop takibi: exchange'de sürekli cancel/replace döngüsü yok.
    """

    def __init__(self):
        api_key = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_API_SECRET", "")
        self._client = Client(api_key, api_secret)
        self._fetcher = DataFetcher(self._client)
        self._strategy = SignalEngine()
        self._risk = GlobalRiskState()
        self._executors: Dict[str, FuturesExecutor] = {}

    def setup(self):
        """Bot başlangıç kontrolü ve kurulum."""
        logger.info("=" * 65)
        logger.info("  BINANCE USDT-M FUTURES MOMENTUM BOT BAŞLATILIYOR")
        logger.info(f"  Semboller: {STRATEGY.SYMBOLS}")
        logger.info(f"  Kaldıraç: {RISK.LEVERAGE}x {RISK.MARGIN_MODE}")
        logger.info(f"  Risk/trade: {RISK.RISK_PER_TRADE_USDT} USDT")
        logger.info("=" * 65)

        if not check_position_mode(self._client):
            raise SystemExit("One-Way Mode değil — bot durduruluyor.")

        for symbol in STRATEGY.SYMBOLS:
            try:
                minfo = self._fetcher.get_market_info(symbol)
                exec_ = FuturesExecutor(self._client, symbol, minfo)
                exec_.setup_leverage()
                self._executors[symbol] = exec_
                logger.info(f"[SETUP] {symbol} hazır | {minfo}")
            except Exception as exc:
                logger.critical(f"[SETUP] {symbol} hazırlanamadı: {exc}")
                raise

        logger.info("[SETUP] Tüm semboller hazır, ana döngü başlıyor")

    def run(self):
        """Ana bot döngüsü."""
        self.setup()

        while True:
            try:
                # Sonraki 5m kapanışını bekle
                wait = seconds_to_next_5m()
                if wait > 1:
                    logger.debug(f"[WAIT] Mum kapanışına {wait:.0f}s kaldı")
                    # Beklerken mum içi SL takibini çalıştır
                    self._intrabar_monitor(duration=wait - 1)

                # 5m mum kapandı
                candle_time = datetime.now(timezone.utc)
                self._on_candle_close(candle_time)

            except KeyboardInterrupt:
                logger.info("[BOT] Kullanıcı tarafından durduruldu")
                self._report_open_positions()
                break
            except Exception as exc:
                logger.error(f"[BOT] Ana döngü hatası: {exc}", exc_info=True)
                time.sleep(10)

    def _on_candle_close(self, candle_time: datetime):
        """Her 5m kapanmış mum sonrasında çalışır."""
        # Risk state: gün sıfırı + BE cooldown azalt
        self._risk.on_candle_close(candle_time.strftime("%Y-%m-%d"))

        # Seans filtresi
        session_ok, reason = GlobalRiskState.is_session_allowed(candle_time)
        if not session_ok:
            logger.debug(f"[SESSION] {reason} — sinyaller değerlendirilmedi")
            # Açık pozisyonlar yönetilmeye devam eder (return yok)
            self._manage_open_positions(candle_time)
            return

        for symbol in STRATEGY.SYMBOLS:
            try:
                self._process_symbol(symbol, candle_time)
            except Exception as exc:
                logger.error(f"[BOT] {symbol} işlem hatası: {exc}", exc_info=True)

    def _process_symbol(self, symbol: str, candle_time: datetime):
        """Tek sembol için mum kapanış mantığı."""
        executor = self._executors[symbol]

        # Veri çek
        try:
            df_5m = self._fetcher.get_candles(symbol)
            df_1h = self._fetcher.get_htf_candles(symbol)
        except Exception as exc:
            logger.error(f"[DATA] {symbol} veri alınamadı: {exc}")
            return

        if df_5m is None or len(df_5m) < 50:
            logger.warning(f"[DATA] {symbol} yetersiz 5m veri ({len(df_5m) if df_5m is not None else 0})")
            return

        current_price = float(df_5m["close"].iloc[-1])

        # ── Açık pozisyon yönetimi ────────────────────────────────────────────
        if executor.position:
            pos = executor.position

            # Trailing stop güncelle (mum kapanış fiyatıyla)
            new_sl = executor.check_and_update_trailing(current_price)
            if new_sl:
                exit_label = "BE_STOP" if pos.sl_is_breakeven else "TRAILING"
                logger.info(f"[TRAILING] {symbol} new SL={new_sl:.4f} ({exit_label})")

            # SL tetiklendi mi?
            if pos.is_sl_hit(current_price):
                exit_type = "BE_STOP" if pos.sl_is_breakeven else "SL"
                pnl = executor.close_position(exit_type)
                if pnl is not None:
                    self._risk.on_trade_closed(symbol, pos.direction, pnl, exit_type)
                return

            # TP tetiklendi mi?
            if pos.is_tp_hit(current_price):
                pnl = executor.close_position("TP")
                if pnl is not None:
                    self._risk.on_trade_closed(symbol, pos.direction, pnl, "TP")
                return

            # Pozisyon hala açık — yeni giriş değerlendirme
            return

        # ── Sinyal değerlendirme ──────────────────────────────────────────────
        signal_result = self._strategy.get_signal(df_5m, df_1h)

        if signal_result.signal == "HOLD":
            logger.debug(f"[HOLD] {symbol}: {signal_result.reject_reason}")
            return

        direction = signal_result.signal

        # Risk onayı
        can_open, reason = self._risk.can_open(symbol, direction)
        if not can_open:
            logger.info(f"[REJECT] {symbol} {direction}: {reason}")
            return

        # Pozisyon boyutu
        sl_dist_pct = abs(current_price - signal_result.sl_price) / current_price
        notional = self._risk.calc_position_size(sl_dist_pct)

        logger.info(
            f"[ENTRY] {symbol} {direction} price={current_price:.4f} "
            f"SL={signal_result.sl_price:.4f} TP={signal_result.tp_price:.4f} "
            f"notional={notional:.2f} USDT SL%={sl_dist_pct*100:.2f}"
        )

        pos = executor.open_position(
            direction=direction,
            notional_usdt=notional,
            entry_price=current_price,
            sl_price=signal_result.sl_price,
            tp_price=signal_result.tp_price,
            atr_val=signal_result.atr_value,
        )

        if pos:
            self._risk.on_trade_opened(symbol)

    def _manage_open_positions(self, candle_time: datetime):
        """Seans dışında açık pozisyonları kontrol eder (yeni giriş olmaz)."""
        for symbol in STRATEGY.SYMBOLS:
            executor = self._executors.get(symbol)
            if not executor or not executor.position:
                continue
            try:
                price = self._fetcher.get_current_price(symbol)
                pos = executor.position
                executor.check_and_update_trailing(price)
                if pos.is_sl_hit(price):
                    exit_type = "BE_STOP" if pos.sl_is_breakeven else "SL"
                    pnl = executor.close_position(exit_type)
                    if pnl is not None:
                        self._risk.on_trade_closed(symbol, pos.direction, pnl, exit_type)
                elif pos.is_tp_hit(price):
                    pnl = executor.close_position("TP")
                    if pnl is not None:
                        self._risk.on_trade_closed(symbol, pos.direction, pnl, "TP")
            except Exception as exc:
                logger.error(f"[MANAGE] {symbol} hata: {exc}")

    def _intrabar_monitor(self, duration: float):
        """
        Mum içi SL/TP takibi.
        Her SL_POLL_INTERVAL_SECONDS saniyede bir fiyat kontrol eder.
        Toplam duration saniyelik süre boyunca çalışır.
        """
        poll = EXECUTION.SL_POLL_INTERVAL_SECONDS
        deadline = time.time() + duration

        while time.time() < deadline:
            for symbol in STRATEGY.SYMBOLS:
                executor = self._executors.get(symbol)
                if not executor or not executor.position:
                    continue

                pos = executor.position
                try:
                    price = self._fetcher.get_current_price(symbol)
                except Exception as exc:
                    logger.warning(f"[POLL] {symbol} fiyat alınamadı: {exc}")
                    continue

                # Trailing güncelle
                new_sl = executor.check_and_update_trailing(price)
                if new_sl:
                    logger.info(f"[TRAILING_INTRABAR] {symbol} SL → {new_sl:.4f}")

                # SL tetiklendi mi?
                if pos.is_sl_hit(price):
                    exit_type = "BE_STOP" if pos.sl_is_breakeven else "SL"
                    logger.info(f"[SL_HIT] {symbol} intrabar @ {price:.4f}")
                    pnl = executor.close_position(exit_type)
                    if pnl is not None:
                        self._risk.on_trade_closed(symbol, pos.direction, pnl, exit_type)

                # TP tetiklendi mi?
                elif pos.is_tp_hit(price):
                    logger.info(f"[TP_HIT] {symbol} intrabar @ {price:.4f}")
                    pnl = executor.close_position("TP")
                    if pnl is not None:
                        self._risk.on_trade_closed(symbol, pos.direction, pnl, "TP")

            # Sonraki poll'a kalan süre kadar bekle
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(poll, remaining))

    def _report_open_positions(self):
        """Kapanışta açık pozisyonları raporlar."""
        for symbol, executor in self._executors.items():
            if executor.position:
                pos = executor.position
                logger.warning(
                    f"[OPEN_POSITION] {symbol} {pos.direction} "
                    f"entry={pos.entry_price:.4f} qty={pos.quantity} "
                    f"SL={pos.sl_price:.4f} TP={pos.tp_price:.4f}"
                )


# ── Giriş noktası ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot = TradeBot()
    bot.run()
