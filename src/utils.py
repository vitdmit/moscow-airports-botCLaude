"""Вспомогательные функции."""
import logging
import sys
from datetime import date, datetime, timedelta

from src.config import MSK


def get_logger(name: str) -> logging.Logger:
    """Простой логгер в stderr.

    GitHub Actions сохраняет stderr в логе задачи — этого достаточно.
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


def now_msk() -> datetime:
    """Текущее время в Москве."""
    return datetime.now(tz=MSK)


def yesterday_msk() -> date:
    """Вчерашняя календарная дата по Москве."""
    return (now_msk() - timedelta(days=1)).date()
