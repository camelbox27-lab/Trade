import time
import logging
from datetime import datetime, date

from bot.logger_setup import setup_logger
from bot.config import CHECK_INTERVAL_SECONDS, CAPITAL
from bot.data_fetcher import BinanceDataFetcher
from bot.strategy import MomentumStrategy
from bot.risk_manager import RiskManager
from bot.executor import TradeExecutor

logger = setup_logger()


class TradeBot:
    """Ana trade bot sınıfı.

    1dk'lık momentum değişimlerine göre Binance üzerinden
    otomatik alım/satım yapar.
    """

    def __init__(self):
        self.fetcher = BinanceDataFetcher()
        self.strategy = MomentumStrategy()
        self.risk = RiskManager()
        self.executor = TradeExecutor()
        self.current_capital = CAPITAL
        self.current_date = date.today()

    def check_daily_reset(self):
        """Yeni güne geçildiyse günlük sayaçları sıfırla."""
        today = date.today()
        if today != self.current_date:
            logger.info(f"Yeni gün: {today}")
            self.risk.reset_daily()
            self.current_date = today

    def run_cycle(self):
        """Tek bir işlem döngüsü çalıştırır."""
        self.check_daily_reset()

        # Günlük limitler kontrol
        if not self.risk.can_trade() and not self.executor.in_position:
            return

        # Veri çek
        df = self.fetcher.get_klines(limit=50)
        current_price = float(df["close"].iloc[-1])

        # Açık pozisyon varsa stop-loss / take-profit kontrol
        if self.executor.in_position:
            if self.risk.check_stop_loss(self.executor.entry_price, current_price):
                logger.info("STOP-LOSS tetiklendi!")
                pnl = self.executor.sell(current_price)
                self.risk.record_trade(pnl)
                self.current_capital += pnl
                return

            if self.risk.check_take_profit(self.executor.entry_price, current_price):
                logger.info("TAKE-PROFIT tetiklendi!")
                pnl = self.executor.sell(current_price)
                self.risk.record_trade(pnl)
                self.current_capital += pnl
                return

        # Strateji sinyali al
        signal = self.strategy.get_signal(df)
        momentum = self.strategy.calculate_momentum(df)
        rsi = self.strategy.calculate_rsi(df)

        logger.info(
            f"Fiyat: ${current_price:.2f} | Momentum: %{momentum:.3f} | "
            f"RSI: {rsi:.1f} | Sinyal: {signal} | "
            f"Pozisyon: {'AÇIK' if self.executor.in_position else 'YOK'} | "
            f"Sermaye: ${self.current_capital:.2f} | "
            f"Günlük PnL: %{self.risk.daily_pnl_pct:.2f}"
        )

        if signal == "BUY" and not self.executor.in_position:
            if self.risk.can_trade():
                size = self.risk.get_position_size(self.current_capital)
                self.executor.buy(size, current_price)

        elif signal == "SELL" and self.executor.in_position:
            pnl = self.executor.sell(current_price)
            self.risk.record_trade(pnl)
            self.current_capital += pnl

    def run(self):
        """Bot ana döngüsü."""
        logger.info("=" * 60)
        logger.info("Trade Bot başlatıldı")
        logger.info(f"Sermaye: ${self.current_capital:.2f}")
        logger.info(f"Hedef: Günlük %10 kar | Limit: Günlük %5 zarar")
        logger.info("=" * 60)

        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                logger.info("Bot kullanıcı tarafından durduruldu.")
                if self.executor.in_position:
                    logger.warning("DİKKAT: Açık pozisyon var!")
                break
            except Exception as e:
                logger.error(f"Hata: {e}")

            time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    bot = TradeBot()
    bot.run()
