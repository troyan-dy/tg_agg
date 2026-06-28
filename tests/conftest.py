"""Shared test setup.

`app.config.Settings` is instantiated at import time and requires the
Telegram/DeepSeek env vars, so we populate them here *before* any `app.*`
module is imported (conftest is loaded by pytest first).
"""
from __future__ import annotations

import os

os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("ADMIN_ID", "42")
os.environ.setdefault("DEEPSEEK_API_KEY", "sk-test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import storage  # noqa: E402
from app.db import Base  # noqa: E402
from app.models import Channel, SeenItem, Setting  # noqa: F401,E402  (register tables)


@pytest.fixture
async def sqlite_session(monkeypatch):
    """A real, isolated in-memory SQLite session factory bound into storage.

    Note: pg-specific code paths (mark_seen's ON CONFLICT) are not exercised
    here — those are covered with mocks in test_storage.py.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(storage, "SessionLocal", session_factory)
    yield session_factory
    await engine.dispose()
