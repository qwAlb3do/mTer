from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from bot.config import settings
from bot.error_store import JsonErrorHandler


def configure_logging() -> None:
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    root_logger.handlers.clear()

    # stdout is captured by both an interactive terminal and Docker's logging
    # driver, making the same runtime events visible through `python3 bot.py`
    # and `docker compose logs -f bot`.
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        settings.log_dir / "bot.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    error_handler = RotatingFileHandler(
        settings.log_dir / "errors.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    root_logger.addHandler(error_handler)

    # Machine-readable diagnostic history for admin review. It contains event
    # details and tracebacks, but is independent from users.json.
    root_logger.addHandler(JsonErrorHandler(settings.errors_file))

    logging.getLogger("httpx").setLevel(logging.WARNING)
