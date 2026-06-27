"""Thin data-access helpers over the DB."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import SessionLocal
from app.models import SeenItem, Setting

RSS_KEY = "rss_url"


async def get_rss_url() -> str | None:
    async with SessionLocal() as session:
        setting = await session.get(Setting, RSS_KEY)
        return setting.value if setting else None


async def set_rss_url(url: str) -> None:
    async with SessionLocal() as session:
        setting = await session.get(Setting, RSS_KEY)
        if setting:
            setting.value = url
        else:
            session.add(Setting(key=RSS_KEY, value=url))
        await session.commit()


async def filter_unseen(entry_ids: list[str]) -> set[str]:
    """Return the subset of entry_ids that are NOT yet in the DB."""
    if not entry_ids:
        return set()
    async with SessionLocal() as session:
        result = await session.execute(
            select(SeenItem.entry_id).where(SeenItem.entry_id.in_(entry_ids))
        )
        known = set(result.scalars().all())
    return set(entry_ids) - known


async def mark_seen(items: list[dict], published_id: str | None = None) -> None:
    """Persist evaluated entries. `items` are dicts with id/title/link.

    The entry equal to `published_id` is flagged as published. Uses an
    idempotent upsert so concurrent runs never raise on duplicates.
    """
    if not items:
        return
    now = datetime.now(UTC)
    rows = [
        {
            "entry_id": it["id"],
            "title": it.get("title"),
            "link": it.get("link"),
            "published": it["id"] == published_id,
            "posted_at": now if it["id"] == published_id else None,
        }
        for it in items
    ]
    async with SessionLocal() as session:
        stmt = pg_insert(SeenItem).values(rows)
        stmt = stmt.on_conflict_do_nothing(index_elements=[SeenItem.entry_id])
        await session.execute(stmt)
        await session.commit()
