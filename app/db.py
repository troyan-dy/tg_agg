import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

log = logging.getLogger("db")

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    """Create any missing tables; leave an existing schema untouched.

    No Alembic and no in-place migrations: create_all only adds tables that are
    absent, so a fresh DB gets the full schema while an existing one is left as
    is. Schema changes to existing tables must be handled manually.
    """
    import app.models  # noqa: F401 - ensure models are registered

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
