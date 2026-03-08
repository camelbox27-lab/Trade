import logging
import os
from bot.config import LOG_FILE


def setup_logger():
    """Bot için logging yapılandırması."""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    logger = logging.getLogger("trade_bot")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Dosyaya yaz
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Konsola yaz
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger
