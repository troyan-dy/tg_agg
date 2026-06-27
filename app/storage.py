"""Thin data-access helpers over the DB."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db import SessionLocal
from app.models import SeenItem, Setting

log = logging.getLogger("storage")

RSS_KEY = "rss_url"


async def get_rss_url() -> str | None:
    """Effective RSS url: the RSS_URL env override wins, else the stored value."""
    if settings.rss_url:
        return settings.rss_url
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
    log.info("RSS url set to %s", url)


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


async def recent_published_titles(within_hours: int = 24) -> list[str]:
    """Titles of entries published to the channel within the last `within_hours`.

    Fed to DeepSeek so it can diversify topics and avoid repeating a story
    that already went out recently. Newest first.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=within_hours)
    async with SessionLocal() as session:
        result = await session.execute(
            select(SeenItem.title)
            .where(SeenItem.published.is_(True), SeenItem.posted_at >= cutoff)
            .order_by(SeenItem.posted_at.desc())
        )
        return [t for t in result.scalars().all() if t]


async def published_among(entry_ids: list[str]) -> set[str]:
    """Return the subset of entry_ids already published to the channel."""
    if not entry_ids:
        return set()
    async with SessionLocal() as session:
        result = await session.execute(
            select(SeenItem.entry_id).where(
                SeenItem.entry_id.in_(entry_ids), SeenItem.published.is_(True)
            )
        )
        return set(result.scalars().all())


async def mark_seen(items: list[dict], published_id: str | None = None) -> None:
    """Persist evaluated entries. `items` are dicts with id/title/link.

    The entry equal to `published_id` is flagged as published. Uses an
    idempotent upsert so concurrent runs never raise on duplicates. The
    published flag is then forced on with a separate UPDATE so that re-posting
    an already-seen entry (the fallback path) still records the publication —
    on_conflict_do_nothing alone would leave the old published=False row intact.
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
        if published_id is not None:
            await session.execute(
                update(SeenItem)
                .where(SeenItem.entry_id == published_id)
                .values(published=True, posted_at=now)
            )
        await session.commit()
    log.debug("Marked %d entries seen (published=%s)", len(rows), published_id)
