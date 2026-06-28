"""Tests for db.init_db / the in-place migration guard."""
from __future__ import annotations

from app import db


async def test_init_db_creates_tables_and_skips_pg_migration():
    # On SQLite (the test DB) create_all runs and the Postgres-only migration is
    # skipped by the dialect guard — init_db must complete without error.
    await db.init_db()
    assert db.engine.dialect.name == "sqlite"


def test_migrate_is_noop_when_channel_id_present():
    """The migration short-circuits once seen_items already has channel_id."""
    from unittest.mock import MagicMock

    conn = MagicMock()
    conn.execute.return_value.first.return_value = (1,)  # column already exists

    db._migrate_seen_items_channel_id(conn)

    # Only the existence probe ran — no ALTER/UPDATE statements.
    assert conn.execute.call_count == 1
