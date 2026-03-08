"""
Strateji sinyal motoru (SignalEngine).

5m kapanmış mumlar + 1H EMA50 filtresi ile LONG/SHORT/HOLD sinyali üretir.
Live ve backtest modda aynı kod çalışır — dışarıdan veri (df) verilir.

Tüm giriş kontrolleri yalnızca kapanmış mum bazlıdır.
"""

import logging
from dataclasses import dataclass
from typing import Literal, Optional

import numpy as np
import pandas as pd

from bot.config import STRATEGY
from bot.indicators import atr, ema, rsi, volume_sma

logger = logging.getLogger(__name__)

Signal = Literal["LONG", "SHORT", "HOLD"]


@dataclass
class SignalResult:
    signal: Signal
    atr_value: float          # Hesaplanan ATR (SL/TP için kullanılır)
    sl_price: float           # İlk stop fiyatı
    tp_price: float           # Take-profit fiyatı
    reject_reason: Optional[str] = None   # Reddetme sebebi (loglama için)


def _clamp_dist(raw_dist: float, price: float, min_pct: float, max_pct: float) -> float:
    """
    ATR bazlı ham mesafeyi yüzde clamp ile sınırlar.

    1. raw_dist → yüzde: pct = raw_dist / price
    2. clamp: pct = max(min_pct, min(max_pct, pct))
    3. Fiyat mesafesine geri çevir: clamped = price × pct

    SL/TP clamp günlüğü üst seviyede yapılır.
    """
    pct = raw_dist / price
    clamped_pct = max(min_pct, min(max_pct, pct))
    return price * clamped_pct


class SignalEngine:
    """
    5m kapanmış mumlar üzerinde çalışan sinyal motoru.

    get_signal(df_5m, df_1h) → SignalResult

    df_5m: sütunlar open, high, low, close, volume (son satır = son kapanmış mum)
    df_1h: sütunlar open, high, low, close, volume (1H kapanmış mumlar)
    """

    def get_signal(
        self,
        df_5m: pd.DataFrame,
        df_1h: pd.DataFrame,
    ) -> SignalResult:
        """Ana sinyal fonksiyonu."""
        cfg = STRATEGY

        def hold(reason: str) -> SignalResult:
            return SignalResult("HOLD", 0.0, 0.0, 0.0, reject_reason=reason)

        # ── Minimum veri kontrolü ─────────────────────────────────────────────
        min_rows = max(
            cfg.ATR_PERIOD + 1,
            cfg.EMA_SLOW + 1,
            cfg.RSI_PERIOD + 1,
            cfg.VOL_SMA_PERIOD + 1,
            3,   # Giriş tetik için en az 2 mum gerekli
        )
        if len(df_5m) < min_rows:
            return hold("insufficient_data")

        closes = df_5m["close"]
        highs = df_5m["high"]
        lows = df_5m["low"]
        volumes = df_5m["volume"]
        last_close = closes.iloc[-1]

        # ── ATR hesabı ve volatilite filtresi ─────────────────────────────────
        atr_series = atr(df_5m, cfg.ATR_PERIOD)
        atr_val = atr_series.iloc[-1]
        if np.isnan(atr_val) or atr_val <= 0:
            logger.debug("[REJECT] atr_nan")
            return hold("atr_nan")

        atr_ratio = atr_val / last_close
        if atr_ratio < cfg.ATR_MIN_RATIO:
            logger.debug(f"[REJECT] volatility_low ATR/price={atr_ratio:.5f} < {cfg.ATR_MIN_RATIO}")
            return hold("volatility_low")

        # ── RSI ───────────────────────────────────────────────────────────────
        rsi_series = rsi(closes, cfg.RSI_PERIOD)
        rsi_val = rsi_series.iloc[-1]
        if np.isnan(rsi_val):
            logger.debug("[REJECT] rsi_nan")
            return hold("rsi_nan")

        # ── EMA20 / EMA50 (5m) ────────────────────────────────────────────────
        ema20_series = ema(closes, cfg.EMA_FAST)
        ema50_series = ema(closes, cfg.EMA_SLOW)
        ema20_last = ema20_series.iloc[-1]
        ema50_last = ema50_series.iloc[-1]

        if np.isnan(ema20_last) or np.isnan(ema50_last):
            return hold("ema_nan")

        # ── 1H EMA50 yakınlık filtresi ────────────────────────────────────────
        htf_ok = True
        if len(df_1h) >= cfg.EMA_HTF_PERIOD:
            htf_ema50 = ema(df_1h["close"], cfg.EMA_HTF_PERIOD).iloc[-1]
            if not np.isnan(htf_ema50) and htf_ema50 > 0:
                htf_dist = abs(last_close - htf_ema50) / htf_ema50
                if htf_dist < cfg.EMA_HTF_MIN_DIST_PCT:
                    logger.debug(
                        f"[REJECT] htf_ema50_proximity dist={htf_dist:.4f} "
                        f"< {cfg.EMA_HTF_MIN_DIST_PCT}"
                    )
                    return hold("htf_ema50_proximity")
            else:
                htf_ok = False
        else:
            htf_ok = False

        if not htf_ok:
            logger.debug("[STRATEGY] 1H veri yetersiz, HTF EMA filtresi atlandı")

        # ── Hacim filtresi ────────────────────────────────────────────────────
        vol_sma = volume_sma(volumes, cfg.VOL_SMA_PERIOD)
        vol_sma_last = vol_sma.iloc[-1]
        if np.isnan(vol_sma_last) or vol_sma_last <= 0:
            return hold("vol_sma_nan")

        current_vol = volumes.iloc[-1]
        prev_vol = volumes.iloc[-2]
        vol_ok = (
            current_vol > vol_sma_last * cfg.VOL_CURRENT_MULT
            and prev_vol < vol_sma_last * cfg.VOL_PREV_MULT
        )

        # ── EMA20 temas kuralı ────────────────────────────────────────────────
        # Son N kapanmış mumdan en az birinde EMA20 teması
        lb = cfg.EMA_TOUCH_LOOKBACK
        ema20_win = ema20_series.iloc[-lb:]
        lows_win = lows.iloc[-lb:]
        highs_win = highs.iloc[-lb:]

        ema20_long_touch = any(
            lows_win.iloc[i] <= ema20_win.iloc[i] for i in range(lb)
        )
        ema20_short_touch = any(
            highs_win.iloc[i] >= ema20_win.iloc[i] for i in range(lb)
        )

        # ── Giriş tetik: kapanmış mum bazlı kırılım ──────────────────────────
        # Long: son kapanış > bir önceki mumun high
        # Short: son kapanış < bir önceki mumun low
        prev_high = highs.iloc[-2]
        prev_low = lows.iloc[-2]
        long_break = last_close > prev_high
        short_break = last_close < prev_low

        # ── LONG sinyali ─────────────────────────────────────────────────────
        if long_break:
            if not (cfg.RSI_LONG_MIN <= rsi_val <= cfg.RSI_LONG_MAX):
                logger.debug(f"[REJECT] rsi_long RSI={rsi_val:.1f} dışı [{cfg.RSI_LONG_MIN},{cfg.RSI_LONG_MAX}]")
                return hold("rsi_out_of_range")

            if not ema20_long_touch:
                logger.debug("[REJECT] ema20_touch_miss (long)")
                return hold("ema20_touch_miss")

            if not vol_ok:
                logger.debug(
                    f"[REJECT] volume_filter (long) cur={current_vol:.0f} "
                    f"prev={prev_vol:.0f} sma={vol_sma_last:.0f}"
                )
                return hold("volume_filter")

            # SL/TP hesapla ve clamp uygula
            raw_sl_dist = atr_val * cfg.ATR_SL_MULT
            raw_tp_dist = atr_val * cfg.ATR_TP_MULT
            sl_dist = _clamp_dist(raw_sl_dist, last_close, cfg.SL_MIN_PCT, cfg.SL_MAX_PCT)
            tp_dist = _clamp_dist(raw_tp_dist, last_close, cfg.TP_MIN_PCT, cfg.TP_MAX_PCT)

            _log_sltp_clamp("LONG", last_close, atr_val, raw_sl_dist, sl_dist, raw_tp_dist, tp_dist)

            sl_price = last_close - sl_dist
            tp_price = last_close + tp_dist

            logger.info(
                f"[SIGNAL] LONG close={last_close:.4f} SL={sl_price:.4f} "
                f"TP={tp_price:.4f} ATR={atr_val:.4f} RSI={rsi_val:.1f}"
            )
            return SignalResult("LONG", atr_val, sl_price, tp_price)

        # ── SHORT sinyali ─────────────────────────────────────────────────────
        if short_break:
            if not (cfg.RSI_SHORT_MIN <= rsi_val <= cfg.RSI_SHORT_MAX):
                logger.debug(f"[REJECT] rsi_short RSI={rsi_val:.1f} dışı [{cfg.RSI_SHORT_MIN},{cfg.RSI_SHORT_MAX}]")
                return hold("rsi_out_of_range")

            if not ema20_short_touch:
                logger.debug("[REJECT] ema20_touch_miss (short)")
                return hold("ema20_touch_miss")

            if not vol_ok:
                logger.debug(
                    f"[REJECT] volume_filter (short) cur={current_vol:.0f} "
                    f"prev={prev_vol:.0f} sma={vol_sma_last:.0f}"
                )
                return hold("volume_filter")

            raw_sl_dist = atr_val * cfg.ATR_SL_MULT
            raw_tp_dist = atr_val * cfg.ATR_TP_MULT
            sl_dist = _clamp_dist(raw_sl_dist, last_close, cfg.SL_MIN_PCT, cfg.SL_MAX_PCT)
            tp_dist = _clamp_dist(raw_tp_dist, last_close, cfg.TP_MIN_PCT, cfg.TP_MAX_PCT)

            _log_sltp_clamp("SHORT", last_close, atr_val, raw_sl_dist, sl_dist, raw_tp_dist, tp_dist)

            sl_price = last_close + sl_dist
            tp_price = last_close - tp_dist

            logger.info(
                f"[SIGNAL] SHORT close={last_close:.4f} SL={sl_price:.4f} "
                f"TP={tp_price:.4f} ATR={atr_val:.4f} RSI={rsi_val:.1f}"
            )
            return SignalResult("SHORT", atr_val, sl_price, tp_price)

        return hold("no_entry_trigger")


def _log_sltp_clamp(
    direction: str,
    price: float,
    atr_val: float,
    raw_sl: float,
    clamped_sl: float,
    raw_tp: float,
    clamped_tp: float,
):
    """SL/TP clamp uygulandıysa loglar."""
    sl_changed = abs(raw_sl - clamped_sl) > 1e-8
    tp_changed = abs(raw_tp - clamped_tp) > 1e-8
    if sl_changed or tp_changed:
        logger.info(
            f"[SL_TP_CLAMP] {direction} ATR={atr_val:.4f} "
            f"SL: raw={raw_sl/price*100:.3f}% → {clamped_sl/price*100:.3f}% | "
            f"TP: raw={raw_tp/price*100:.3f}% → {clamped_tp/price*100:.3f}%"
        )
    else:
        logger.debug(
            f"[SL_TP] {direction} SL_dist={clamped_sl/price*100:.3f}% "
            f"TP_dist={clamped_tp/price*100:.3f}%"
        )
