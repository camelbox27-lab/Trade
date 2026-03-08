import os
from dotenv import load_dotenv

load_dotenv()

# Binance API
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# Trading parameters
SYMBOL = "BTCUSDT"
INTERVAL = "1m"  # 1 dakikalık mumlar
CAPITAL = 100.0  # Başlangıç sermayesi (USD)

# Momentum strategy
MOMENTUM_PERIOD = 10  # Son 10 mum üzerinden momentum hesapla
MOMENTUM_BUY_THRESHOLD = 0.3  # %0.3 üzeri pozitif momentum -> AL
MOMENTUM_SELL_THRESHOLD = -0.2  # %-0.2 altı negatif momentum -> SAT

# Risk management
DAILY_PROFIT_TARGET_PCT = 10.0  # Günlük %10 kar hedefi
DAILY_LOSS_LIMIT_PCT = 5.0  # Günlük %5 zarar limiti
STOP_LOSS_PCT = 1.5  # İşlem başı %1.5 stop-loss
TAKE_PROFIT_PCT = 3.0  # İşlem başı %3 take-profit
MAX_POSITION_PCT = 95.0  # Sermayenin max %95'i ile pozisyon aç

# Bot settings
CHECK_INTERVAL_SECONDS = 10  # Her 10 saniyede kontrol et
LOG_FILE = "logs/trade_bot.log"
