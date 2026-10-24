"""One end-to-end run: RSS -> dedup -> DeepSeek pick -> generate -> publish."""
from __future__ import annotations

from aiogram import Bot
from aiogram.types import (
    InputMediaAudio,
    InputMediaDocument,
    InputMediaLivePhoto,
    InputMediaPhoto,
    InputMediaVideo,
)
from loguru import logger as log

from app.config import settings
from app.models import Channel
from app.services import deepseek, rss
from app.storage import (
    filter_unseen,
    mark_seen,
    published_among,
    recent_published_titles,
)
from app.tone import get_preset

# Telegram caps photo captions at 1024 chars; plain messages allow more.
_CAPTION_LIMIT = 1024
# Telegram allows at most 10 items in a media group.
_MEDIA_GROUP_LIMIT = 10


# Fetch extra entries so dedup/media filters still leave enough candidates.
_MIN_FETCH = 50
# Entry fields that carry media (see rss._normalize).
_MEDIA_FIELDS = ("images", "image", "video")

_MediaGroup = list[
    InputMediaAudio
    | InputMediaDocument
    | InputMediaLivePhoto
    | InputMediaPhoto
    | InputMediaVideo
]


def _has_media(entry: dict) -> bool:
    """Whether an entry carries an image or a video (used by require_media)."""
    return any(entry.get(field) for field in _MEDIA_FIELDS)


def _entry_images(entry: dict) -> list[str]:
    """All images of an entry, falling back to the single primary image."""
    if entry.get("images"):
        return entry["images"]
    return [entry["image"]] if entry.get("image") else []


async def _send_video(bot: Bot, target: int | str, video: str, caption: str | None) -> None:
    if caption is None:
        await bot.send_video(chat_id=target, video=video)
    else:
        await bot.send_video(chat_id=target, video=video, caption=caption)


async def _send_photo(bot: Bot, target: int | str, photo: str, caption: str | None) -> None:
    if caption is None:
        await bot.send_photo(chat_id=target, photo=photo)
    else:
        await bot.send_photo(chat_id=target, photo=photo, caption=caption)


async def _send_gallery(
    bot: Bot, target: int | str, images: list[str], caption: str | None
) -> None:
    """Up to _MEDIA_GROUP_LIMIT photos as one media group; caption rides the first."""
    group: _MediaGroup = [
        InputMediaPhoto(media=url, caption=caption if num == 0 else None)
        for num, url in enumerate(images[:_MEDIA_GROUP_LIMIT])
    ]
    await bot.send_media_group(chat_id=target, media=group)


async def _send_media(
    bot: Bot, target: int | str, text: str, images: list[str], video: str
) -> bool:
    """Send the entry media; True when the text already went out as its caption."""
    caption = text if len(text) <= _CAPTION_LIMIT else None
    if video:
        await _send_video(bot, target, video, caption)
    elif len(images) > 1:
        await _send_gallery(bot, target, images, caption)
    elif images:
        await _send_photo(bot, target, images[0], caption)
    else:
        return False
    return caption is not None


async def _publish(
    bot: Bot,
    target: int | str,
    text: str,
    images: list[str] | None = None,
    video: str = "",
) -> None:
    """Send the post, attaching the article media when there is any.

    A single image rides as a photo (with the text as caption when short
    enough). Several images go out as one media group (gallery). A video is sent
    as a video. When the text is longer than a caption allows, the media goes
    first on its own and the full text follows, so nothing gets truncated. Any
    media failure degrades gracefully to a plain text message.
    """
    try:
        captioned = await _send_media(bot, target, text, images or [], video)
    except Exception as exc:  # noqa: BLE001
        # A dead/unsupported media URL must not block the post itself.
        log.warning("Could not send media: {} — posting text only", exc)
        captioned = False
    if not captioned:
        await bot.send_message(chat_id=target, text=text, disable_web_page_preview=True)


class RunResult:
    def __init__(self, status: str, detail: str = ""):
        self.status = status  # posted | no_feed | no_new | error
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.status}: {self.detail}" if self.detail else self.status


async def _unpublished_fallback(
    channel: Channel, entries: list[dict], ids: list[str]
) -> list[dict]:
    """Entries already seen but never published to this channel.

    Used when there are no fresh entries: rather than give up, re-surface them
    (may break chronological order — acceptable here), so a manual run still
    has something to post.
    """
    published_ids = await published_among(channel.id, ids)
    stale = [
        entry
        for entry, entry_id in zip(entries, ids, strict=True)
        if entry_id not in published_ids
    ][: settings.max_candidates]
    if stale:
        log.info("No fresh entries; falling back to {} seen-but-unposted", len(stale))
    return stale


async def _select_candidates(channel: Channel, entries: list[dict]) -> list[dict]:
    """Unseen entries; if none, seen-but-never-published ones (fallback)."""
    ids = [entry["id"] for entry in entries]
    unseen_ids = await filter_unseen(channel.id, ids)
    fresh = [
        entry for entry, entry_id in zip(entries, ids, strict=True) if entry_id in unseen_ids
    ]
    if fresh:
        return fresh[: settings.max_candidates]
    return await _unpublished_fallback(channel, entries, ids)


async def _collect_candidates(channel: Channel, url: str) -> list[dict] | RunResult:
    """Candidates to show DeepSeek, or the RunResult explaining why there are none."""
    fetch_limit = max(settings.max_candidates * 2, _MIN_FETCH)
    entries = await rss.fetch_entries(url, limit=fetch_limit)
    if not entries:
        return RunResult("no_new", "Лента пуста или недоступна.")

    # When the channel requires media, drop text-only entries up front so they
    # never take a candidate slot nor get picked.
    if channel.require_media:
        entries = [entry for entry in entries if _has_media(entry)]
        if not entries:
            log.info("Channel {} requires media but no entry carries any", channel.id)
            return RunResult("no_new", "Нет новостей с картинкой или видео для публикации.")

    candidates = await _select_candidates(channel, entries)
    if not candidates:
        log.info("Nothing to post — every feed entry is already published")
        return RunResult("no_new", "В ленте нет ничего, что ещё не было опубликовано.")
    return candidates


async def _choose(channel: Channel, candidates: list[dict]) -> dict:
    """Ask DeepSeek for the best candidate, given what the channel posted recently."""
    recent = await recent_published_titles(channel.id)
    log.info(
        "{} new candidates, {} posted in last 24h, asking DeepSeek to pick",
        len(candidates), len(recent),
    )
    index = await deepseek.pick_most_relevant(candidates, recent)
    return candidates[index]


async def _post_entry(bot: Bot, target: int | str, channel: Channel, chosen: dict) -> None:
    """Generate the post in the channel's tone and publish it with its media."""
    text = await deepseek.generate_post(chosen, get_preset(channel.tone))
    images = _entry_images(chosen)
    await _publish(bot, target, text, images, chosen.get("video", ""))


async def run_once(
    bot: Bot, channel: Channel, *, chat_id: int | str | None = None, persist: bool = True
) -> RunResult:
    """One pipeline run for a single channel.

    channel — whose feed/tone/target to use, and what dedup is scoped to.
    chat_id — where to send the post; defaults to the channel's chat_id.
    persist — whether to mark evaluated entries as seen. Set False for a
        preview/dry-run so it stays repeatable and touches nothing in the DB.
    """
    if not channel.rss_url:
        log.info("Channel {} has no RSS url configured", channel.id)
        return RunResult("no_feed", "RSS-ссылка не задана. Установи её: /setrss <url>")

    candidates = await _collect_candidates(channel, channel.rss_url)
    if isinstance(candidates, RunResult):
        return candidates

    chosen = await _choose(channel, candidates)
    chosen_id = chosen["id"]
    target = channel.chat_id if chat_id is None else chat_id
    try:
        await _post_entry(bot, target, channel, chosen)
    except Exception as exc:  # noqa: BLE001
        log.exception("Publishing failed")
        # Mark the rest as seen but NOT the failed one, so we can retry it later.
        if persist:
            rest = [cand for cand in candidates if cand["id"] != chosen_id]
            await mark_seen(channel.id, rest)
        return RunResult("error", str(exc))

    if persist:
        await mark_seen(channel.id, candidates, published_id=chosen_id)
    log.info("Posted to {}: {}", channel.chat_id, chosen["title"])
    return RunResult("posted", chosen["title"])
