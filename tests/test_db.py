"""Tests for db.run_migrations (Alembic upgrade head)."""

from __future__ import annotations

from app import db


async def test_run_migrations_applies_head():
    # Against the test DB (SQLite), `alembic upgrade head` must apply both
    # revisions and complete without error.
    await db.run_migrations()
    assert db.engine.dialect.name == "sqlite"
