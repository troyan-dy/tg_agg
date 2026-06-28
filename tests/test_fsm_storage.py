"""Tests for the DB-backed FSM storage (round-trips on real in-memory SQLite)."""
from __future__ import annotations

import pytest
from aiogram.fsm.storage.base import StorageKey

from app.bot import fsm_storage
from app.bot.handlers import AddChannel


@pytest.fixture
async def storage(sqlite_session, monkeypatch):
    """A DBStorage bound to the shared in-memory SQLite session factory."""
    monkeypatch.setattr(fsm_storage, "SessionLocal", sqlite_session)
    return fsm_storage.DBStorage()


def _key() -> StorageKey:
    return StorageKey(bot_id=1, chat_id=2, user_id=3)


async def test_state_roundtrip(storage):
    key = _key()
    assert await storage.get_state(key) is None

    await storage.set_state(key, "AddChannel:waiting_for_link")
    assert await storage.get_state(key) == "AddChannel:waiting_for_link"


async def test_state_accepts_state_object(storage):
    key = _key()
    await storage.set_state(key, AddChannel.waiting_for_link)
    assert await storage.get_state(key) == "AddChannel:waiting_for_link"


async def test_data_roundtrip_and_update(storage):
    key = _key()
    assert await storage.get_data(key) == {}

    await storage.set_data(key, {"url": "https://x"})
    assert await storage.get_data(key) == {"url": "https://x"}

    await storage.update_data(key, {"extra": 1})
    assert await storage.get_data(key) == {"url": "https://x", "extra": 1}


async def test_clearing_state_and_data_drops_row(storage):
    key = _key()
    await storage.set_state(key, "X")
    await storage.set_data(key, {"a": 1})

    # Mimic FSMContext.clear(): drop state, then data.
    await storage.set_state(key, None)
    await storage.set_data(key, {})

    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {}


async def test_keys_are_isolated(storage):
    a = StorageKey(bot_id=1, chat_id=2, user_id=3)
    b = StorageKey(bot_id=1, chat_id=2, user_id=4)
    await storage.set_state(a, "AState")
    assert await storage.get_state(b) is None
