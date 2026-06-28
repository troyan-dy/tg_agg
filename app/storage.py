"""Thin data-access helpers over the DB.

Everything is scoped to a channel now: each `Channel` row carries its own feed,
tone and schedule, and dedup in `seen_items` is keyed by (channel_id, entry_id).
The admin's currently selected channel (what the chat buttons act on) is kept as
a single `settings` row.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db import SessionLocal
from app.models import Channel, SeenItem, Setting
from app.tone import get_preset

log = logging.getLogger("storage")

SELECTED_KEY = "selected_channel"


def parse_run_hours(text: str) -> list[int]:
    """Parse a "9,13,18" string into a sorted, unique list of valid hours.

    Raises ValueError if a token is not an integer in 0..23 or nothing is left
    after cleaning — callers surface that to the admin as a usage hint.
    """
    hours: list[int] = []
    for tok in text.replace(" ", "").split(","):
        if not tok:
            continue
        h = int(tok)  # ValueError on non-numeric tokens
        if not 0 <= h <= 23:
            raise ValueError(f"час вне диапазона 0–23: {h}")
        hours.append(h)
    if not hours:
        raise ValueError("не указан ни один час")
    return sorted(set(hours))


# --- Channels CRUD -------------------------------------------------------------
async def list_channels() -> list[Channel]:
    """All channels, oldest first (stable order for lists/menus)."""
    async with SessionLocal() as session:
        result = await session.execute(select(Channel).order_by(Channel.id))
        return list(result.scalars().all())


async def get_channel(channel_id: int) -> Channel | None:
    async with SessionLocal() as session:
        return await session.get(Channel, channel_id)


async def get_channel_by_chat(chat_id: str) -> Channel | None:
    async with SessionLocal() as session:
        result = await session.execute(select(Channel).where(Channel.chat_id == chat_id))
        return result.scalar_one_or_none()


async def add_channel(chat_id: str, title: str | None = None) -> Channel:
    """Create a channel, seeding its schedule/tone from the env defaults."""
    channel = Channel(
        chat_id=chat_id,
        title=title,
        run_hours=",".join(str(h) for h in settings.run_hours_list),
        tone=get_preset(settings.post_tone).key,
    )
    async with SessionLocal() as session:
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
    log.info("Channel added: %s (%s)", chat_id, title)
    return channel


async def update_channel(channel_id: int, **fields: object) -> None:
    """Patch one channel's columns (rss_url / tone / run_hours / enabled / title)."""
    if not fields:
        return
    async with SessionLocal() as session:
        await session.execute(
            update(Channel).where(Channel.id == channel_id).values(**fields)
        )
        await session.commit()
    log.info("Channel %s updated: %s", channel_id, fields)


async def delete_channel(channel_id: int) -> None:
    """Delete a channel and (via FK cascade) its seen_items."""
    async with SessionLocal() as session:
        await session.execute(delete(SeenItem).where(SeenItem.channel_id == channel_id))
        await session.execute(delete(Channel).where(Channel.id == channel_id))
        await session.commit()
    log.info("Channel %s deleted", channel_id)


# --- Selected channel (what the chat controls act on) --------------------------
async def get_selected_channel() -> Channel | None:
    """The admin's selected channel. Falls back to the first channel (and stores
    that choice) so the chat always has a sensible target after a restart."""
    async with SessionLocal() as session:
        setting = await session.get(Setting, SELECTED_KEY)
        if setting and setting.value.isdigit():
            channel = await session.get(Channel, int(setting.value))
            if channel:
                return channel
        # Nothing selected (or it was deleted) — default to the first channel.
        result = await session.execute(select(Channel).order_by(Channel.id).limit(1))
        return result.scalar_one_or_none()


async def set_selected_channel(channel_id: int) -> None:
    value = str(channel_id)
    async with SessionLocal() as session:
        setting = await session.get(Setting, SELECTED_KEY)
        if setting:
            setting.value = value
        else:
            session.add(Setting(key=SELECTED_KEY, value=value))
        await session.commit()
    log.info("Selected channel set to %s", channel_id)


# --- Dedup (channel-scoped) ----------------------------------------------------
async def filter_unseen(channel_id: int, entry_ids: list[str]) -> set[str]:
    """Return the subset of entry_ids not yet seen for this channel."""
    if not entry_ids:
        return set()
    async with SessionLocal() as session:
        result = await session.execute(
            select(SeenItem.entry_id).where(
                SeenItem.channel_id == channel_id, SeenItem.entry_id.in_(entry_ids)
            )
        )
        known = set(result.scalars().all())
    return set(entry_ids) - known


async def recent_published_titles(channel_id: int, within_hours: int = 24) -> list[str]:
    """Titles published to this channel within the last `within_hours`.

    Fed to DeepSeek so it can diversify topics and avoid repeating a story
    that already went out recently. Newest first.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=within_hours)
    async with SessionLocal() as session:
        result = await session.execute(
            select(SeenItem.title)
            .where(
                SeenItem.channel_id == channel_id,
                SeenItem.published.is_(True),
                SeenItem.posted_at >= cutoff,
            )
            .order_by(SeenItem.posted_at.desc())
        )
        return [t for t in result.scalars().all() if t]


async def published_among(channel_id: int, entry_ids: list[str]) -> set[str]:
    """Return the subset of entry_ids already published to this channel."""
    if not entry_ids:
        return set()
    async with SessionLocal() as session:
        result = await session.execute(
            select(SeenItem.entry_id).where(
                SeenItem.channel_id == channel_id,
                SeenItem.entry_id.in_(entry_ids),
                SeenItem.published.is_(True),
            )
        )
        return set(result.scalars().all())


async def mark_seen(
    channel_id: int, items: list[dict], published_id: str | None = None
) -> None:
    """Persist evaluated entries for a channel. `items` are dicts with id/title/link.

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
            "channel_id": channel_id,
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
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[SeenItem.channel_id, SeenItem.entry_id]
        )
        await session.execute(stmt)
        if published_id is not None:
            await session.execute(
                update(SeenItem)
                .where(
                    SeenItem.channel_id == channel_id,
                    SeenItem.entry_id == published_id,
                )
                .values(published=True, posted_at=now)
            )
        await session.commit()
    log.debug(
        "Marked %d entries seen for channel %s (published=%s)",
        len(rows), channel_id, published_id,
    )
