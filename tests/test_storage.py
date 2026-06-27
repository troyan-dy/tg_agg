"""Storage tests.

get/set/filter run against a real in-memory SQLite (via the `sqlite_session`
fixture). mark_seen uses a PostgreSQL-only upsert, so its row-building logic is
verified with the pg insert + session mocked instead.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from app import storage
from app.models import SeenItem


async def test_get_rss_url_none_when_unset(sqlite_session):
    assert await storage.get_rss_url() is None


async def test_set_then_get_rss_url(sqlite_session):
    await storage.set_rss_url("https://example.com/feed.xml")
    assert await storage.get_rss_url() == "https://example.com/feed.xml"


async def test_set_rss_url_updates_existing(sqlite_session):
    await storage.set_rss_url("https://a/feed")
    await storage.set_rss_url("https://b/feed")
    assert await storage.get_rss_url() == "https://b/feed"


async def test_env_override_wins_over_db(sqlite_session, monkeypatch):
    await storage.set_rss_url("https://from-db/feed")
    monkeypatch.setattr(storage.settings, "rss_url", "https://from-env/feed")
    assert await storage.get_rss_url() == "https://from-env/feed"


async def test_filter_unseen_empty_input_short_circuits(sqlite_session):
    assert await storage.filter_unseen([]) == set()


async def test_filter_unseen_returns_only_unknown(sqlite_session):
    async with sqlite_session() as session:
        session.add(SeenItem(entry_id="known-1"))
        session.add(SeenItem(entry_id="known-2"))
        await session.commit()

    result = await storage.filter_unseen(["known-1", "new-1", "known-2", "new-2"])
    assert result == {"new-1", "new-2"}


async def test_recent_published_titles_filters_and_orders(sqlite_session):
    now = datetime.now(UTC)
    async with sqlite_session() as session:
        # Published recently — included, newest first.
        session.add(SeenItem(
            entry_id="p-old", title="Старее", published=True, posted_at=now - timedelta(hours=5)
        ))
        session.add(SeenItem(
            entry_id="p-new", title="Новее", published=True, posted_at=now - timedelta(hours=1)
        ))
        # Published too long ago — excluded.
        session.add(SeenItem(
            entry_id="p-stale", title="Давнее", published=True, posted_at=now - timedelta(hours=30)
        ))
        # Seen but never published — excluded.
        session.add(SeenItem(entry_id="seen", title="Невышедшее", published=False))
        await session.commit()

    titles = await storage.recent_published_titles()
    assert titles == ["Новее", "Старее"]


async def test_recent_published_titles_empty(sqlite_session):
    assert await storage.recent_published_titles() == []


class _FakeSession:
    """Captures the statement passed to execute()."""

    def __init__(self):
        self.executed = None
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        self.executed = stmt

    async def commit(self):
        self.committed = True


def _capture_pg_insert(monkeypatch):
    """Replace pg_insert so we can read the rows mark_seen builds."""
    captured = {}

    def fake_pg_insert(model):
        stmt = MagicMock()

        def values(rows):
            captured["rows"] = rows
            return stmt

        stmt.values.side_effect = values
        stmt.on_conflict_do_nothing.return_value = stmt
        return stmt

    monkeypatch.setattr(storage, "pg_insert", fake_pg_insert)
    return captured


async def test_mark_seen_empty_is_noop(monkeypatch):
    # Should not even open a session.
    monkeypatch.setattr(
        storage, "SessionLocal", lambda: (_ for _ in ()).throw(AssertionError("opened"))
    )
    await storage.mark_seen([])


async def test_mark_seen_flags_published_entry(monkeypatch):
    captured = _capture_pg_insert(monkeypatch)
    session = _FakeSession()
    monkeypatch.setattr(storage, "SessionLocal", lambda: session)

    items = [
        {"id": "a", "title": "A", "link": "la"},
        {"id": "b", "title": "B", "link": "lb"},
    ]
    await storage.mark_seen(items, published_id="b")

    rows = {r["entry_id"]: r for r in captured["rows"]}
    assert rows["a"]["published"] is False
    assert rows["a"]["posted_at"] is None
    assert rows["b"]["published"] is True
    assert rows["b"]["posted_at"] is not None
    assert session.committed is True


async def test_mark_seen_without_published_id_flags_nothing(monkeypatch):
    captured = _capture_pg_insert(monkeypatch)
    monkeypatch.setattr(storage, "SessionLocal", lambda: _FakeSession())

    await storage.mark_seen([{"id": "a", "title": "A", "link": "la"}])
    assert all(r["published"] is False for r in captured["rows"])
    assert all(r["posted_at"] is None for r in captured["rows"])
