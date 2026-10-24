"""Fetch and normalize RSS/Atom entries."""
from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Iterator
from itertools import chain

import feedparser
from loguru import logger as log

_TAG_RE = re.compile(r"<[^>]+>")
_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
_TITLE_LIMIT = 300
# Feed fields that may carry a stable entry id, in priority order.
_ID_FIELDS = ("id", "guid", "link")


def _clean(text: str, limit: int = 400) -> str:
    text = html.unescape(_TAG_RE.sub("", text or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _entry_id(entry) -> str:
    for field in _ID_FIELDS:
        if entry.get(field):
            return entry[field]
    return entry.get("title", "")


def _is_video(media) -> bool:
    return (
        str(media.get("medium", "")).lower() == "video"
        or str(media.get("type", "")).lower().startswith("video")
    )


def _media_image_urls(entry) -> Iterator[str | None]:
    """Image URLs from media_content (non-video), media_thumbnail and enclosures."""
    for media in entry.get("media_content", []) or []:
        if not _is_video(media):
            yield media.get("url")
    for thumb in entry.get("media_thumbnail", []) or []:
        yield thumb.get("url")
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image/"):
            yield enc.get("href")


def _html_image_urls(entry) -> Iterator[str]:
    """Every <img> src found in the summary/content HTML."""
    blobs = [entry.get("summary", "")]
    parts = entry.get("content", []) or []
    blobs.extend(part.get("value", "") for part in parts)
    for blob in blobs:
        for match in _IMG_RE.finditer(blob or ""):
            yield html.unescape(match.group(1))


def _extract_images(entry) -> list[str]:
    """All image URLs from common RSS/Atom places, in priority order, deduped.

    media_content (non-video) first, then media_thumbnail, image enclosures, and
    finally every <img> found in summary/content HTML. The first element is the
    "primary" image (see _extract_image); the full list lets us post galleries.
    """
    urls: list[str] = []
    for raw_url in chain(_media_image_urls(entry), _html_image_urls(entry)):
        url = (raw_url or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _extract_image(entry) -> str:
    """Best-effort single (primary) image URL, else ''."""
    images = _extract_images(entry)
    return images[0] if images else ""


def _extract_video(entry) -> str:
    """Best-effort video URL from media_content/enclosures, else ''."""
    for media in entry.get("media_content", []) or []:
        video_url = media.get("url")
        if _is_video(media) and video_url:
            return str(video_url)
    for enc in entry.get("enclosures", []) or []:
        enc_type = str(enc.get("type", ""))
        if enc_type.startswith("video/") and enc.get("href"):
            return enc["href"]
    return ""


def _normalize(entry) -> dict:
    images = _extract_images(entry)
    return {
        "id": _entry_id(entry),
        "title": _clean(entry.get("title", ""), _TITLE_LIMIT),
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
    entries = [_normalize(raw) for raw in parsed.entries[:limit]]
    # Drop entries without a usable id.
    usable = [entry for entry in entries if entry["id"]]
    log.info("Fetched {} entries ({} usable) from {}", len(parsed.entries), len(usable), url)
    return usable
