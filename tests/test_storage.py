"""Storage tests.

get/set/filter run against a real in-memory SQLite (via the `sqlite_session`
fixture). mark_seen uses a PostgreSQL-only upsert, so its row-building logic is
verified with the pg insert + session mocked instead.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

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


# --- Run hours -----------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("9,13,18", [9, 13, 18]),
        (" 9 , 13 ", [9, 13]),
        ("18,9,9,13", [9, 13, 18]),  # sorted + deduped
        ("0,23", [0, 23]),
        ("9,,13", [9, 13]),  # empty tokens skipped
    ],
)
def test_parse_run_hours_ok(text, expected):
    assert storage.parse_run_hours(text) == expected


@pytest.mark.parametrize("text", ["", " , ", "abc", "9,24", "-1", "9.5"])
def test_parse_run_hours_rejects(text):
    with pytest.raises(ValueError):
        storage.parse_run_hours(text)


async def test_run_hours_env_fallback_when_unset(sqlite_session, monkeypatch):
    monkeypatch.setattr(storage.settings, "run_hours", "7,21")
    assert await storage.get_stored_run_hours() is None
    assert await storage.get_run_hours() == [7, 21]


async def test_set_then_get_run_hours_wins_over_env(sqlite_session, monkeypatch):
    monkeypatch.setattr(storage.settings, "run_hours", "7,21")
    await storage.set_run_hours([8, 12, 20])
    assert await storage.get_stored_run_hours() == [8, 12, 20]
    assert await storage.get_run_hours() == [8, 12, 20]


async def test_set_run_hours_updates_existing(sqlite_session):
    await storage.set_run_hours([9, 13])
    await storage.set_run_hours([10, 14])
    assert await storage.get_run_hours() == [10, 14]


# --- Tone ----------------------------------------------------------------------
async def test_tone_env_fallback_when_unset(sqlite_session, monkeypatch):
    monkeypatch.setattr(storage.settings, "post_tone", "expert")
    assert await storage.get_stored_tone() is None
    assert await storage.get_tone() == "expert"


async def test_tone_unknown_env_degrades_to_default(sqlite_session, monkeypatch):
    monkeypatch.setattr(storage.settings, "post_tone", "bogus")
    assert await storage.get_tone() == storage.get_preset(None).key


async def test_set_then_get_tone_wins_over_env(sqlite_session, monkeypatch):
    monkeypatch.setattr(storage.settings, "post_tone", "news")
    await storage.set_tone("hype")
    assert await storage.get_stored_tone() == "hype"
    assert await storage.get_tone() == "hype"


async def test_set_tone_rejects_unknown(sqlite_session):
    with pytest.raises(ValueError):
        await storage.set_tone("nope")


async def test_get_tone_preset_returns_object(sqlite_session):
    await storage.set_tone("digest")
    preset = await storage.get_tone_preset()
    assert preset.key == "digest"


async def test_db_value_wins_over_env(sqlite_session, monkeypatch):
    await storage.set_rss_url("https://from-db/feed")
    monkeypatch.setattr(storage.settings, "rss_url", "https://from-env/feed")
    assert await storage.get_rss_url() == "https://from-db/feed"


async def test_env_used_as_fallback_when_db_empty(sqlite_session, monkeypatch):
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


async def test_published_among_returns_only_published(sqlite_session):
    async with sqlite_session() as session:
        session.add(SeenItem(entry_id="pub", title="P", published=True))
        session.add(SeenItem(entry_id="seen", title="S", published=False))
        await session.commit()

    result = await storage.published_among(["pub", "seen", "unknown"])
    assert result == {"pub"}


async def test_published_among_empty_input(sqlite_session):
    assert await storage.published_among([]) == set()


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
    executed: list = []

    class _Rec(_FakeSession):
        async def execute(self, stmt):
            executed.append(stmt)

    monkeypatch.setattr(storage, "SessionLocal", lambda: _Rec())

    await storage.mark_seen([{"id": "a", "title": "A", "link": "la"}])
    assert all(r["published"] is False for r in captured["rows"])
    assert all(r["posted_at"] is None for r in captured["rows"])
    # No published_id → only the bulk insert runs, no UPDATE.
    from sqlalchemy.sql.dml import Update

    assert not any(isinstance(s, Update) for s in executed)


async def test_mark_seen_forces_published_flag_on_conflict(monkeypatch):
    """Re-posting an already-seen entry must still record the publication via an
    explicit UPDATE (on_conflict_do_nothing would leave published=False)."""
    _capture_pg_insert(monkeypatch)
    executed: list = []

    class _Rec(_FakeSession):
        async def execute(self, stmt):
            executed.append(stmt)

    monkeypatch.setattr(storage, "SessionLocal", lambda: _Rec())

    await storage.mark_seen([{"id": "b", "title": "B", "link": "lb"}], published_id="b")

    from sqlalchemy.sql.dml import Update

    assert any(isinstance(s, Update) for s in executed)
