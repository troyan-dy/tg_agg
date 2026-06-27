"""Fetch and normalize RSS/Atom entries."""
from __future__ import annotations

import asyncio
import html
import logging
import re

import feedparser

log = logging.getLogger("rss")

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str, limit: int = 400) -> str:
    text = html.unescape(_TAG_RE.sub("", text or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _entry_id(entry) -> str:
    return entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title", "")


def _normalize(entry) -> dict:
    return {
        "id": _entry_id(entry),
        "title": _clean(entry.get("title", ""), 300),
        "summary": _clean(entry.get("summary", "")),
        "link": entry.get("link", ""),
    }


async def fetch_entries(url: str, limit: int = 50) -> list[dict]:
    """Parse the feed (off the event loop) and return normalized entries."""
    parsed = await asyncio.to_thread(feedparser.parse, url)
    if getattr(parsed, "bozo", False):
        log.warning("Feed parse issue for %s: %s", url, getattr(parsed, "bozo_exception", None))
    entries = [_normalize(e) for e in parsed.entries[:limit]]
    # Drop entries without a usable id.
    result = [e for e in entries if e["id"]]
    log.info("Fetched %d entries (%d usable) from %s", len(parsed.entries), len(result), url)
    return result
