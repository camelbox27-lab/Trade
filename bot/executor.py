import logging
from binance.client import Client
from bot.config import BINANCE_API_KEY, BINANCE_API_SECRET, SYMBOL

logger = logging.getLogger("trade_bot")


class TradeExecutor:
    """Binance üzerinde alım/satım emirleri yürütür."""

    def __init__(self):
        self.client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
        self.in_position = False
        self.entry_price = 0.0
        self.position_qty = 0.0
        self.position_cost = 0.0  # Pozisyona giriş maliyeti (USD)

    def buy(self, usdt_amount: float, current_price: float) -> bool:
        """Market buy emri gönderir."""
        try:
            qty = round(usdt_amount / current_price, 6)
            if qty <= 0:
                logger.warning("Yetersiz miktar, alım yapılamadı.")
                return False

            order = self.client.order_market_buy(
                symbol=SYMBOL,
                quantity=qty
            )
            self.in_position = True
            self.entry_price = current_price
            self.position_qty = qty
            self.position_cost = usdt_amount
            logger.info(
                f"ALIM yapıldı: {qty} {SYMBOL} @ ${current_price:.2f} "
                f"(Toplam: ${usdt_amount:.2f}) | OrderID: {order['orderId']}"
            )
            return True
        except Exception as e:
            logger.error(f"Alım hatası: {e}")
            return False

    def sell(self, current_price: float) -> float:
        """Mevcut pozisyonu market sell ile kapatır. PnL döndürür."""
        if not self.in_position:
            logger.warning("Satılacak pozisyon yok.")
            return 0.0

        try:
            order = self.client.order_market_sell(
                symbol=SYMBOL,
                quantity=self.position_qty
            )
            sell_value = self.position_qty * current_price
            pnl = sell_value - self.position_cost
            logger.info(
                f"SATIM yapıldı: {self.position_qty} {SYMBOL} @ ${current_price:.2f} "
                f"| PnL: ${pnl:.2f} | OrderID: {order['orderId']}"
            )
            self.in_position = False
            self.entry_price = 0.0
            self.position_qty = 0.0
            self.position_cost = 0.0
            return pnl
        except Exception as e:
            logger.error(f"Satım hatası: {e}")
            return 0.0

    def get_unrealized_pnl(self, current_price: float) -> float:
        """Açık pozisyonun gerçekleşmemiş kar/zararı."""
        if not self.in_position:
            return 0.0
        current_value = self.position_qty * current_price
        return current_value - self.position_cost
