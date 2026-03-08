"""
Binance USDT-M Futures emir yöneticisi.

Özellikler:
- Limit giriş emri (maker komisyon avantajı)
- Fill timeout ve iptal mantığı
- Partial fill yönetimi
- Internal stop takibi (exchange cancel/replace döngüsü olmadan)
- Reduce-only market kapatma emri
- Kaldıraç ve margin ayarı
"""

import logging
import math
import time
from typing import Optional, Tuple

from bot.config import EXECUTION, RISK, BACKTEST
from bot.position_state import PositionState
from bot.config import STRATEGY

logger = logging.getLogger(__name__)


class FuturesExecutor:
    """Tek bir futures sembolü için emir ve pozisyon yönetimi."""

    def __init__(self, client, symbol: str, market_info: dict):
        self._client = client
        self.symbol = symbol
        self._minfo = market_info
        self.position: Optional[PositionState] = None

    # ── Başlangıç kurulumu ────────────────────────────────────────────────────

    def setup_leverage(self):
        """Kaldıraç ve margin modunu ayarlar."""
        try:
            self._client.futures_change_leverage(
                symbol=self.symbol,
                leverage=RISK.LEVERAGE,
            )
            logger.info(f"[SETUP] {self.symbol} kaldıraç={RISK.LEVERAGE}x")
        except Exception as exc:
            if "already" in str(exc).lower() or "no need" in str(exc).lower():
                logger.debug(f"[SETUP] {self.symbol} kaldıraç zaten {RISK.LEVERAGE}x")
            else:
                logger.error(f"[SETUP] {self.symbol} kaldıraç hatası: {exc}")

        try:
            self._client.futures_change_margin_type(
                symbol=self.symbol,
                marginType=RISK.MARGIN_MODE,
            )
            logger.info(f"[SETUP] {self.symbol} margin={RISK.MARGIN_MODE}")
        except Exception as exc:
            if "already" in str(exc).lower() or "no need" in str(exc).lower():
                logger.debug(f"[SETUP] {self.symbol} margin zaten {RISK.MARGIN_MODE}")
            else:
                logger.error(f"[SETUP] {self.symbol} margin hatası: {exc}")

    # ── Pozisyon açma ─────────────────────────────────────────────────────────

    def open_position(
        self,
        direction: str,
        notional_usdt: float,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        atr_val: float,
    ) -> Optional[PositionState]:
        """
        Limit emir ile pozisyon açar.
        - Fill olmazsa FILL_TIMEOUT_SECONDS içinde emir iptal edilir.
        - Partial fill minimum boyutu karşılıyorsa kabul edilir.
        - Çok küçük partial fill → reduce-only market ile kapatılır.
        """
        cfg = EXECUTION
        side = "BUY" if direction == "LONG" else "SELL"

        # Limit fiyat (maker avantajı: long için biraz aşağı, short için biraz yukarı)
        if direction == "LONG":
            limit_price = entry_price * (1.0 - cfg.LIMIT_PRICE_OFFSET_PCT)
        else:
            limit_price = entry_price * (1.0 + cfg.LIMIT_PRICE_OFFSET_PCT)

        limit_price = self._round_price(limit_price)
        qty = self._calc_qty(notional_usdt, limit_price)

        if qty <= 0:
            logger.error(f"[EXEC] {self.symbol} {direction} geçersiz miktar: {qty}")
            return None

        # Minimum notional kontrolü
        actual_notional = qty * limit_price
        min_notional = self._minfo.get("min_notional", 5.0)
        if actual_notional < min_notional:
            logger.error(
                f"[EXEC] {self.symbol} notional {actual_notional:.2f} < min {min_notional}"
            )
            return None

        logger.info(
            f"[ORDER_SEND] {self.symbol} {direction} LIMIT qty={qty} "
            f"price={limit_price:.4f} SL={sl_price:.4f} TP={tp_price:.4f}"
        )

        try:
            order = self._client.futures_create_order(
                symbol=self.symbol,
                side=side,
                type="LIMIT",
                quantity=qty,
                price=limit_price,
                timeInForce="GTC",
                reduceOnly=False,
            )
            order_id = str(order["orderId"])
        except Exception as exc:
            logger.error(f"[EXEC] {self.symbol} emir gönderilemedi: {exc}")
            return None

        # Fill bekle
        filled_qty, avg_price = self._wait_for_fill(order_id, cfg.FILL_TIMEOUT_SECONDS)

        if filled_qty <= 0:
            logger.warning(f"[ORDER_TIMEOUT] {self.symbol} {cfg.FILL_TIMEOUT_SECONDS}s dolmadı, iptal")
            self._cancel_order(order_id)
            return None

        is_partial = filled_qty < qty - 1e-8
        if is_partial:
            filled_notional = filled_qty * avg_price
            if filled_notional < RISK.POSITION_MIN_USDT:
                logger.warning(
                    f"[PARTIAL_FILL] {self.symbol} çok küçük fill "
                    f"{filled_qty} ({filled_notional:.2f} USDT) — kapatılıyor"
                )
                self._cancel_order(order_id)
                self._close_small_position(direction, filled_qty)
                return None
            logger.info(
                f"[PARTIAL_FILL] {self.symbol} {filled_qty}/{qty} doldu "
                f"({filled_qty * avg_price:.2f} USDT)"
            )

        pos = PositionState(
            symbol=self.symbol,
            direction=direction,
            entry_price=avg_price,
            quantity=filled_qty,
            notional=filled_qty * avg_price,
            atr_at_entry=atr_val,
            sl_price=sl_price,
            tp_price=tp_price,
            order_id=order_id,
            is_partial_fill=is_partial,
        )
        self.position = pos

        logger.info(
            f"[POSITION_OPEN] {self.symbol} {direction} "
            f"qty={filled_qty} entry={avg_price:.4f} "
            f"SL={sl_price:.4f} TP={tp_price:.4f} ATR={atr_val:.4f}"
        )
        return pos

    # ── Pozisyon kapatma ──────────────────────────────────────────────────────

    def close_position(self, reason: str) -> Optional[float]:
        """
        Reduce-only market emri ile pozisyonu kapatır.
        Net PnL döner (komisyon + slippage düşülmüş tahmini).
        Dönen değer None ise emir gönderilemedi.
        """
        pos = self.position
        if not pos:
            logger.warning(f"[CLOSE] {self.symbol} kapatılacak pozisyon yok")
            return None

        side = "SELL" if pos.direction == "LONG" else "BUY"
        logger.info(
            f"[CLOSE_SEND] {self.symbol} {pos.direction} {reason} qty={pos.quantity}"
        )

        try:
            order = self._client.futures_create_order(
                symbol=self.symbol,
                side=side,
                type="MARKET",
                quantity=pos.quantity,
                reduceOnly=True,
            )
            order_id = str(order["orderId"])
        except Exception as exc:
            logger.error(f"[CLOSE] {self.symbol} kapatma emri hatası: {exc}")
            return None

        # Gerçek çıkış fiyatını çek (kısa bekleme ile)
        time.sleep(0.5)
        exit_price = self._get_avg_price(order_id)
        if exit_price <= 0:
            # Fallback: son close fiyatı (tahmini)
            exit_price = pos.entry_price
            logger.warning(f"[CLOSE] {self.symbol} avg_price alınamadı, entry kullanıldı")

        net_pnl = self._calc_net_pnl(pos, exit_price)

        logger.info(
            f"[POSITION_CLOSED] {self.symbol} {pos.direction} {reason} "
            f"exit={exit_price:.4f} net_pnl={net_pnl:+.4f}"
        )
        self.position = None
        return net_pnl

    # ── Trailing stop güncelleme ──────────────────────────────────────────────

    def check_and_update_trailing(self, reference_price: float) -> Optional[float]:
        """
        Trailing stop seviyesini günceller.
        Yeni stop fiyatı döner; değişmediyse None.
        """
        if not self.position:
            return None
        new_sl = self.position.update_trailing(reference_price, STRATEGY.TRAILING_LEVELS)
        if new_sl is not None:
            logger.info(
                f"[TRAILING] {self.symbol} {self.position.direction} "
                f"SL → {new_sl:.4f} "
                f"(seviye {self.position.trailing_level_idx + 1}/"
                f"{len(STRATEGY.TRAILING_LEVELS)})"
            )
        return new_sl

    # ── Yardımcı fonksiyonlar ─────────────────────────────────────────────────

    def _wait_for_fill(self, order_id: str, timeout: int) -> Tuple[float, float]:
        """
        Emir dolana veya timeout dolana kadar bekler.
        (filled_qty, avg_price) döner.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                order = self._client.futures_get_order(
                    symbol=self.symbol, orderId=int(order_id)
                )
                filled = float(order.get("executedQty", 0))
                status = order.get("status", "")

                if filled > 0 and status in ("FILLED", "PARTIALLY_FILLED"):
                    avg = float(order.get("avgPrice", 0))
                    if avg <= 0:
                        avg = float(order.get("price", 0))
                    return filled, avg

                if status in ("CANCELED", "EXPIRED", "REJECTED"):
                    logger.warning(f"[FILL] {self.symbol} order {order_id} durum: {status}")
                    return 0.0, 0.0

            except Exception as exc:
                logger.warning(f"[FILL] {self.symbol} order sorgu hatası: {exc}")

            time.sleep(2)

        return 0.0, 0.0

    def _cancel_order(self, order_id: str):
        try:
            self._client.futures_cancel_order(
                symbol=self.symbol, orderId=int(order_id)
            )
            logger.info(f"[CANCEL] {self.symbol} order {order_id} iptal edildi")
        except Exception as exc:
            # Zaten dolmuş olabilir
            logger.debug(f"[CANCEL] {self.symbol} order {order_id} iptal hatası: {exc}")

    def _close_small_position(self, direction: str, qty: float):
        """Çok küçük partial fill'i reduce-only market ile kapatır."""
        if qty <= 0:
            return
        side = "SELL" if direction == "LONG" else "BUY"
        try:
            self._client.futures_create_order(
                symbol=self.symbol,
                side=side,
                type="MARKET",
                quantity=qty,
                reduceOnly=True,
            )
            logger.info(f"[PARTIAL_CLOSE] {self.symbol} küçük pozisyon kapatıldı qty={qty}")
        except Exception as exc:
            logger.error(f"[PARTIAL_CLOSE] {self.symbol} hata: {exc}")

    def _get_avg_price(self, order_id: str) -> float:
        try:
            order = self._client.futures_get_order(
                symbol=self.symbol, orderId=int(order_id)
            )
            return float(order.get("avgPrice", 0))
        except Exception as exc:
            logger.warning(f"[PRICE] {self.symbol} avg_price alınamadı: {exc}")
            return 0.0

    def _round_price(self, price: float) -> float:
        tick = self._minfo.get("tick_size", 0.01)
        prec = self._minfo.get("price_precision", 2)
        if tick > 0:
            rounded = round(round(price / tick) * tick, prec)
        else:
            rounded = round(price, prec)
        return rounded

    def _calc_qty(self, notional: float, price: float) -> float:
        if price <= 0:
            return 0.0
        raw = notional / price
        step = self._minfo.get("step_size", 0.001)
        prec = self._minfo.get("qty_precision", 3)
        if step > 0:
            raw = math.floor(raw / step) * step
        return round(raw, prec)

    def _calc_net_pnl(self, pos: PositionState, exit_price: float) -> float:
        """Tahmini net PnL (komisyon + slippage dahil)."""
        if pos.direction == "LONG":
            raw = (exit_price - pos.entry_price) * pos.quantity
        else:
            raw = (pos.entry_price - exit_price) * pos.quantity

        # Giriş maker komisyonu + çıkış taker komisyonu + slippage
        entry_fee = pos.notional * BACKTEST.MAKER_FEE
        exit_fee = pos.notional * BACKTEST.TAKER_FEE
        slippage = pos.notional * BACKTEST.SLIPPAGE_PCT
        return raw - entry_fee - exit_fee - slippage
