"""Structured JSON logging via structlog + stdlib logging.

Every log record is emitted as a JSON object to both a daily-rotated file
(`logs/app.jsonl`) and stderr. Callers import `log` from this module and use
it like:

    from pim_studio.logging import log
    log.info("scraping started", channel="storefront", page=1)

The first call to `configure_logging()` sets up the handlers; subsequent calls
are no-ops. Scrapers and the FastAPI app both call it at startup.
"""

from __future__ import annotations

import logging as stdlib_logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from .config import settings

_configured = False


def configure_logging(logs_root: Path | None = None, level: str = "INFO") -> None:
    """Wire up structlog + stdlib logging. Idempotent."""
    global _configured
    if _configured:
        return

    root = logs_root or settings.logs_root
    root.mkdir(parents=True, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        root / "app.jsonl",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=True,
    )
    stream_handler = stdlib_logging.StreamHandler(sys.stderr)

    stdlib_logging.basicConfig(
        format="%(message)s",
        level=getattr(stdlib_logging, level),
        handlers=[file_handler, stream_handler],
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _configured = True


def get_logger(**initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a logger, optionally bound with persistent context fields."""
    if not _configured:
        configure_logging()
    return structlog.get_logger().bind(**initial_values)


# A module-level default logger — `from pim_studio.logging import log`.
log = get_logger()
