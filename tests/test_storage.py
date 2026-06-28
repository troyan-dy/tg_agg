"""Storage tests.

Channel CRUD / selection / channel-scoped dedup run against a real in-memory
SQLite (via the `sqlite_session` fixture). mark_seen uses a PostgreSQL-only
upsert, so its row-building logic is verified with the pg insert + session
mocked instead.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import storage
from app.models import SeenItem


# --- Run hours parsing ---------------------------------------------------------
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


# --- Channels CRUD -------------------------------------------------------------
async def test_add_channel_seeds_defaults(sqlite_session):
    ch = await storage.add_channel("-100123", title="My channel")
    assert ch.id is not None
    assert ch.chat_id == "-100123"
    assert ch.title == "My channel"
    assert ch.run_hours == "9,13,18"  # from the env default
    assert ch.tone == "news"
    assert ch.enabled is True


async def test_list_channels_orders_by_id(sqlite_session):
    a = await storage.add_channel("-1")
    b = await storage.add_channel("-2")
    assert [c.id for c in await storage.list_channels()] == [a.id, b.id]


async def test_get_channel_by_chat(sqlite_session):
    a = await storage.add_channel("-100777")
    found = await storage.get_channel_by_chat("-100777")
    assert found is not None and found.id == a.id
    assert await storage.get_channel_by_chat("-999") is None


async def test_update_channel_patches_fields(sqlite_session):
    a = await storage.add_channel("-1")
    await storage.update_channel(a.id, rss_url="https://f", tone="hype", run_hours="8,20")
    got = await storage.get_channel(a.id)
    assert got.rss_url == "https://f"
    assert got.tone == "hype"
    assert got.run_hours == "8,20"
    assert got.hours_list == [8, 20]


async def test_update_channel_noop_without_fields(sqlite_session):
    a = await storage.add_channel("-1")
    await storage.update_channel(a.id)  # nothing to patch
    assert (await storage.get_channel(a.id)).rss_url is None


async def test_delete_channel_removes_it_and_its_seen_items(sqlite_session):
    a = await storage.add_channel("-1")
    async with sqlite_session() as session:
        session.add(SeenItem(channel_id=a.id, entry_id="x"))
        await session.commit()
    await storage.delete_channel(a.id)
    assert await storage.get_channel(a.id) is None
    # Its seen-history is gone too → the entry is "unseen" again.
    assert await storage.filter_unseen(a.id, ["x"]) == {"x"}


# --- Selected channel ----------------------------------------------------------
async def test_selected_none_when_no_channels(sqlite_session):
    assert await storage.get_selected_channel() is None


async def test_selected_defaults_to_first_channel(sqlite_session):
    a = await storage.add_channel("-1")
    await storage.add_channel("-2")
    sel = await storage.get_selected_channel()
    assert sel is not None and sel.id == a.id


async def test_set_and_get_selected_channel(sqlite_session):
    await storage.add_channel("-1")
    b = await storage.add_channel("-2")
    await storage.set_selected_channel(b.id)
    assert (await storage.get_selected_channel()).id == b.id


async def test_set_selected_updates_existing_pointer(sqlite_session):
    a = await storage.add_channel("-1")
    b = await storage.add_channel("-2")
    await storage.set_selected_channel(a.id)
    await storage.set_selected_channel(b.id)
    assert (await storage.get_selected_channel()).id == b.id


async def test_selected_falls_back_when_pointer_dangles(sqlite_session):
    a = await storage.add_channel("-1")
    await storage.set_selected_channel(999)  # points at a non-existent channel
    sel = await storage.get_selected_channel()
    assert sel is not None and sel.id == a.id


# --- Dedup (channel-scoped) ----------------------------------------------------
async def test_filter_unseen_empty_input_short_circuits(sqlite_session):
    assert await storage.filter_unseen(1, []) == set()


async def test_filter_unseen_is_scoped_per_channel(sqlite_session):
    async with sqlite_session() as session:
        session.add(SeenItem(channel_id=1, entry_id="a"))
        session.add(SeenItem(channel_id=2, entry_id="b"))
        await session.commit()
    # channel 1 has seen "a" but not "b"; channel 2 the opposite.
    assert await storage.filter_unseen(1, ["a", "b"]) == {"b"}
    assert await storage.filter_unseen(2, ["a", "b"]) == {"a"}


async def test_recent_published_titles_scoped_filters_and_orders(sqlite_session):
    now = datetime.now(UTC)
    async with sqlite_session() as session:
        session.add(SeenItem(
            channel_id=1, entry_id="p-old", title="Старее",
            published=True, posted_at=now - timedelta(hours=5),
        ))
        session.add(SeenItem(
            channel_id=1, entry_id="p-new", title="Новее",
            published=True, posted_at=now - timedelta(hours=1),
        ))
        session.add(SeenItem(
            channel_id=1, entry_id="p-stale", title="Давнее",
            published=True, posted_at=now - timedelta(hours=30),  # too old
        ))
        session.add(SeenItem(channel_id=1, entry_id="seen", title="Невышедшее", published=False))
        session.add(SeenItem(
            channel_id=2, entry_id="other", title="Чужое",
            published=True, posted_at=now - timedelta(hours=1),  # another channel
        ))
        await session.commit()
    assert await storage.recent_published_titles(1) == ["Новее", "Старее"]
    assert await storage.recent_published_titles(2) == ["Чужое"]


async def test_recent_published_titles_empty(sqlite_session):
    assert await storage.recent_published_titles(1) == []


async def test_published_among_returns_only_published_for_channel(sqlite_session):
    async with sqlite_session() as session:
        session.add(SeenItem(channel_id=1, entry_id="pub", title="P", published=True))
        session.add(SeenItem(channel_id=1, entry_id="seen", title="S", published=False))
        session.add(SeenItem(channel_id=2, entry_id="pub", title="P2", published=True))
        await session.commit()
    assert await storage.published_among(1, ["pub", "seen", "unknown"]) == {"pub"}
    assert await storage.published_among(2, ["pub", "seen"]) == {"pub"}


async def test_published_among_empty_input(sqlite_session):
    assert await storage.published_among(1, []) == set()


# --- mark_seen (PostgreSQL upsert, mocked) -------------------------------------
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
    from unittest.mock import MagicMock

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
    await storage.mark_seen(1, [])


async def test_mark_seen_flags_published_entry_with_channel(monkeypatch):
    captured = _capture_pg_insert(monkeypatch)
    session = _FakeSession()
    monkeypatch.setattr(storage, "SessionLocal", lambda: session)

    items = [
        {"id": "a", "title": "A", "link": "la"},
        {"id": "b", "title": "B", "link": "lb"},
    ]
    await storage.mark_seen(7, items, published_id="b")

    rows = {r["entry_id"]: r for r in captured["rows"]}
    assert all(r["channel_id"] == 7 for r in captured["rows"])
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

    await storage.mark_seen(1, [{"id": "a", "title": "A", "link": "la"}])
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

    await storage.mark_seen(1, [{"id": "b", "title": "B", "link": "lb"}], published_id="b")

    from sqlalchemy.sql.dml import Update

    assert any(isinstance(s, Update) for s in executed)
