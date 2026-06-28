"""Fetch and normalize RSS/Atom entries."""
from __future__ import annotations

import asyncio
import html
import re

import feedparser
from loguru import logger as log

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)


def _clean(text: str, limit: int = 400) -> str:
    text = html.unescape(_TAG_RE.sub("", text or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _entry_id(entry) -> str:
    return entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title", "")


def _is_video(media) -> bool:
    return (
        str(media.get("medium", "")).lower() == "video"
        or str(media.get("type", "")).lower().startswith("video")
    )


def _extract_images(entry) -> list[str]:
    """All image URLs from common RSS/Atom places, in priority order, deduped.

    media_content (non-video) first, then media_thumbnail, image enclosures, and
    finally every <img> found in summary/content HTML. The first element is the
    "primary" image (see _extract_image); the full list lets us post galleries.
    """
    urls: list[str] = []

    def add(url: str | None) -> None:
        url = (url or "").strip()
        if url and url not in urls:
            urls.append(url)

    for media in entry.get("media_content", []) or []:
        if not _is_video(media):
            add(media.get("url"))
    for thumb in entry.get("media_thumbnail", []) or []:
        add(thumb.get("url"))
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image/"):
            add(enc.get("href"))
    html_blobs = [entry.get("summary", "")]
    for c in entry.get("content", []) or []:
        html_blobs.append(c.get("value", ""))
    for blob in html_blobs:
        for m in _IMG_RE.finditer(blob or ""):
            add(html.unescape(m.group(1)))
    return urls


def _extract_image(entry) -> str:
    """Best-effort single (primary) image URL, else ''."""
    images = _extract_images(entry)
    return images[0] if images else ""


def _extract_video(entry) -> str:
    """Best-effort video URL from media_content/enclosures, else ''."""
    for media in entry.get("media_content", []) or []:
        if _is_video(media) and media.get("url"):
            return str(media["url"])
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("video/") and enc.get("href"):
            return enc["href"]
    return ""


def _normalize(entry) -> dict:
    images = _extract_images(entry)
    return {
        "id": _entry_id(entry),
        "title": _clean(entry.get("title", ""), 300),
        "summary": _clean(entry.get("summary", "")),
        "link": entry.get("link", ""),
        "image": images[0] if images else "",
        "images": images,
        "video": _extract_video(entry),
    }


async def fetch_entries(url: str, limit: int = 50) -> list[dict]:
    """Parse the feed (off the event loop) and return normalized entries."""
    parsed = await asyncio.to_thread(feedparser.parse, url)
    if getattr(parsed, "bozo", False):
        log.warning("Feed parse issue for {}: {}", url, getattr(parsed, "bozo_exception", None))
    entries = [_normalize(e) for e in parsed.entries[:limit]]
    # Drop entries without a usable id.
    result = [e for e in entries if e["id"]]
    log.info("Fetched {} entries ({} usable) from {}", len(parsed.entries), len(result), url)
    return result
