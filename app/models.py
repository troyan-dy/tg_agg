from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Setting(Base):
    """Simple key/value store (used for the admin's currently selected channel)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Channel(Base):
    """A Telegram channel the bot runs on autopilot.

    Each channel carries its own feed, tone and schedule — what used to be the
    single global settings now lives per row. `chat_id` is the Telegram target
    (a numeric id like -100… or a @username) and is what we send posts to.
    """

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    rss_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    tone: Mapped[str] = mapped_column(String(32), default="news")
    run_hours: Mapped[str] = mapped_column(String(128), default="9,13,18")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # When True, only entries carrying an image or video are published; text-only
    # entries are skipped. server_default keeps existing rows valid after the
    # manual ALTER (no Alembic — see CLAUDE.md).
    require_media: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def hours_list(self) -> list[int]:
        return [int(h) for h in self.run_hours.split(",") if h.strip()]


class SeenItem(Base):
    """A feed entry already evaluated for a given channel, so we never repost it.

    Dedup is per channel: the same story may legitimately go out to several
    channels, so the primary key is (channel_id, entry_id).
    """

    __tablename__ = "seen_items"

    channel_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("channels.id", ondelete="CASCADE"), primary_key=True
    )
    # Stable entry id: feed's <guid>/<id> or, as a fallback, the link.
    entry_id: Mapped[str] = mapped_column(String(512), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
