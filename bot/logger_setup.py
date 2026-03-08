"""Logging yapılandırması."""

import logging
import os

LOG_FILE = "logs/trade_bot.log"
LOG_LEVEL = logging.INFO


def setup_logger(log_file: str = LOG_FILE, level: int = LOG_LEVEL) -> logging.Logger:
    """
    Root logger'ı dosyaya + konsola bağlar.
    Tekrar çağrıda handler çakışmasını önler.
    """
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    root = logging.getLogger()
    if root.handlers:
        return root   # Zaten kurulmuş

    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    return root
