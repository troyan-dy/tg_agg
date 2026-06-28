import asyncio
from pathlib import Path

from loguru import logger as log
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Alembic config lives at the repo root (one level above the app package).
_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


class Base(DeclarativeBase):
    pass


def _alembic_config():
    from alembic.config import Config

    return Config(str(_ALEMBIC_INI))


async def run_migrations() -> None:
    """Bring the schema up to the latest Alembic revision (`upgrade head`).

    Alembic is the single source of truth for the schema (no more create_all).
    `command.upgrade` is synchronous and spins its own event loop inside
    `env.py`, so it runs in a worker thread to stay off the running loop.
    """
    from alembic import command

    import app.models  # noqa: F401 - ensure models are registered

    log.info("Applying database migrations (alembic upgrade head)…")
    await asyncio.to_thread(command.upgrade, _alembic_config(), "head")
