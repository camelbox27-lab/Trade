# Binance Momentum Trade Bot

1 dakikalık momentum değişimlerine göre otomatik alım/satım yapan trade bot.

## Özellikler

- **Strateji**: 1dk momentum + RSI + hacim filtresi
- **Risk Yönetimi**: Günlük %10 kar hedefi, %5 zarar limiti
- **Pozisyon Yönetimi**: İşlem başı %1.5 stop-loss, %3 take-profit
- **Sermaye**: $100 başlangıç

## Kurulum

```bash
pip install -r requirements.txt
cp .env.example .env
# .env dosyasına Binance API anahtarlarınızı girin
```

## Çalıştırma

```bash
python main.py
```

## Yapılandırma

Tüm parametreler `bot/config.py` dosyasından ayarlanabilir.
