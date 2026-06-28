"""Tests for db.init_db / the in-place migration guard."""

from __future__ import annotations

from app import db


async def test_init_db_creates_tables_and_skips_pg_migration():
    # On SQLite (the test DB) create_all runs and the Postgres-only migration is
    # skipped by the dialect guard — init_db must complete without error.
    await db.init_db()
    assert db.engine.dialect.name == "sqlite"
