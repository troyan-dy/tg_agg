"""A tiny aiogram FSM storage backed by the `settings` table.

The default `MemoryStorage` keeps conversational state (which input we are
waiting for — a channel link or an RSS url) in process memory, so a restart
loses it: a half-finished «add channel» flow silently breaks. Persisting that
state in the DB means the bot can be restarted at any point without dropping
context. State+data are stored as one JSON value per FSM key in the same
key/value `settings` table the rest of the app already uses.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from sqlalchemy import delete

from app.db import SessionLocal
from app.models import Setting

_PREFIX = "fsm:"


def _key(key: StorageKey) -> str:
    """A stable string key for the settings row (one admin chat in practice)."""
    return (
        f"{_PREFIX}{key.bot_id}:{key.chat_id}:{key.user_id}:"
        f"{key.thread_id}:{key.destiny}"
    )


class DBStorage(BaseStorage):
    """FSM storage that persists state and data in the `settings` table."""

    async def _load(self, key: StorageKey) -> dict[str, Any]:
        async with SessionLocal() as session:
            row = await session.get(Setting, _key(key))
            return json.loads(row.value) if row else {}

    async def _save(self, key: StorageKey, payload: dict[str, Any]) -> None:
        skey = _key(key)
        async with SessionLocal() as session:
            # Empty state and data → drop the row entirely (keeps the table tidy).
            if not payload.get("state") and not payload.get("data"):
                await session.execute(delete(Setting).where(Setting.key == skey))
                await session.commit()
                return
            row = await session.get(Setting, skey)
            value = json.dumps(payload)
            if row:
                row.value = value
            else:
                session.add(Setting(key=skey, value=value))
            await session.commit()

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        payload = await self._load(key)
        payload["state"] = state.state if isinstance(state, State) else state
        await self._save(key, payload)

    async def get_state(self, key: StorageKey) -> str | None:
        return (await self._load(key)).get("state")

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        payload = await self._load(key)
        payload["data"] = dict(data)
        await self._save(key, payload)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return (await self._load(key)).get("data", {})

    async def close(self) -> None:
        return None
