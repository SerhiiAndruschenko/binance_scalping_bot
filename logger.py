import logging
import sys
from pathlib import Path


def setup_logger(name: str = "scalping_bot", level: int = logging.INFO) -> logging.Logger:
    """
    Налаштовує і повертає логер з форматуванням у консоль.
    Назва сервісу: scalping_bot
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # вже налаштований

    logger.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Консольний хендлер
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    logger.propagate = False
    return logger


# Єдиний глобальний логер для всього проєкту
logger = setup_logger("scalping_bot")
