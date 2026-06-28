"""Loguru as the single logging sink, with stdlib logging funneled into it.

Our own modules log via `from loguru import logger`; third-party libraries
(aiogram, apscheduler, httpx, openai, alembic) use stdlib `logging`, so an
`InterceptHandler` on the root logger forwards their records into loguru too —
one format, one place. Because loguru is independent of stdlib logging, our own
logs are no longer silenced when Alembic's `env.py` runs `fileConfig()` during
migrations (the historical pain point); only the stdlib intercept needs
reinstalling afterwards, which `setup_logging()` does idempotently.
"""
from __future__ import annotations

import logging
import sys
from types import FrameType

from loguru import logger

from app.config import settings

# Third-party loggers we keep at WARNING so their INFO/DEBUG chatter stays out.
_NOISY = ("aiogram.event", "apscheduler", "httpx", "httpcore", "openai")

_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{name}</cyan>: {message}"
)


class InterceptHandler(logging.Handler):
    """Forward stdlib `logging` records into loguru, preserving level and origin."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        # Walk back to the frame that actually issued the log so loguru reports
        # the real caller (file/line/module), not this handler.
        frame: FrameType | None = logging.currentframe()
        depth = 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logging() -> None:
    """Configure loguru and route stdlib logging into it.

    Idempotent: Alembic's `fileConfig()` resets the stdlib root handlers during
    migrations, so calling this again after `run_migrations()` reinstalls the
    intercept and keeps third-party logs flowing to loguru.
    """
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level.upper(), format=_FORMAT)
    logging.basicConfig(handlers=[InterceptHandler()], level=logging.NOTSET, force=True)
    for noisy in _NOISY:
        logging.getLogger(noisy).setLevel(logging.WARNING)
