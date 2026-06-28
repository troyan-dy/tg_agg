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


def _has_media(entry: dict) -> bool:
    """Whether an entry carries an image or a video (used by require_media)."""
    return bool(entry.get("images") or entry.get("image") or entry.get("video"))


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
    images = images or []
    short = len(text) <= _CAPTION_LIMIT
    try:
        if video:
            if short:
                await bot.send_video(chat_id=target, video=video, caption=text)
                return
            await bot.send_video(chat_id=target, video=video)
        elif len(images) > 1:
            group: list[
                InputMediaAudio
                | InputMediaDocument
                | InputMediaLivePhoto
                | InputMediaPhoto
                | InputMediaVideo
            ] = [
                InputMediaPhoto(media=url, caption=text if i == 0 and short else None)
                for i, url in enumerate(images[:_MEDIA_GROUP_LIMIT])
            ]
            await bot.send_media_group(chat_id=target, media=group)
            if short:
                return
        elif images:
            if short:
                await bot.send_photo(chat_id=target, photo=images[0], caption=text)
                return
            await bot.send_photo(chat_id=target, photo=images[0])
    except Exception as exc:  # noqa: BLE001
        # A dead/unsupported media URL must not block the post itself.
        log.warning("Could not send media: {} — posting text only", exc)
    await bot.send_message(chat_id=target, text=text, disable_web_page_preview=True)


class RunResult:
    def __init__(self, status: str, detail: str = ""):
        self.status = status  # posted | no_feed | no_new | error
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.status}: {self.detail}" if self.detail else self.status


async def run_once(
    bot: Bot, channel: Channel, *, chat_id: int | str | None = None, persist: bool = True
) -> RunResult:
    """One pipeline run for a single channel.

    channel — whose feed/tone/target to use, and what dedup is scoped to.
    chat_id — where to send the post; defaults to the channel's chat_id.
    persist — whether to mark evaluated entries as seen. Set False for a
        preview/dry-run so it stays repeatable and touches nothing in the DB.
    """
    target = chat_id if chat_id is not None else channel.chat_id
    url = channel.rss_url
    if not url:
        log.info("Channel {} has no RSS url configured", channel.id)
        return RunResult("no_feed", "RSS-ссылка не задана. Установи её: /setrss <url>")

    entries = await rss.fetch_entries(url, limit=max(settings.max_candidates * 2, 50))
    if not entries:
        return RunResult("no_new", "Лента пуста или недоступна.")

    # When the channel requires media, drop text-only entries up front so they
    # never take a candidate slot nor get picked.
    if channel.require_media:
        entries = [e for e in entries if _has_media(e)]
        if not entries:
            log.info("Channel {} requires media but no entry carries any", channel.id)
            return RunResult("no_new", "Нет новостей с картинкой или видео для публикации.")

    ids = [e["id"] for e in entries]
    unseen_ids = await filter_unseen(channel.id, ids)
    candidates = [e for e in entries if e["id"] in unseen_ids][: settings.max_candidates]
    if not candidates:
        # No fresh entries. Rather than give up, re-surface entries already seen
        # but never published (may break chronological order — acceptable here),
        # so a manual run still has something to post.
        published_ids = await published_among(channel.id, ids)
        candidates = [e for e in entries if e["id"] not in published_ids][
            : settings.max_candidates
        ]
        if candidates:
            log.info("No fresh entries; falling back to {} seen-but-unposted", len(candidates))
    if not candidates:
        log.info("Nothing to post — every feed entry is already published")
        return RunResult("no_new", "В ленте нет ничего, что ещё не было опубликовано.")

    recent = await recent_published_titles(channel.id)
    log.info(
        "{} new candidates, {} posted in last 24h, asking DeepSeek to pick",
        len(candidates), len(recent),
    )
    index = await deepseek.pick_most_relevant(candidates, recent)
    chosen = candidates[index]

    try:
        tone = get_preset(channel.tone)
        text = await deepseek.generate_post(chosen, tone)
        images = chosen.get("images") or (
            [chosen["image"]] if chosen.get("image") else []
        )
        await _publish(bot, target, text, images, chosen.get("video", ""))
    except Exception as exc:  # noqa: BLE001
        log.exception("Publishing failed")
        # Mark the rest as seen but NOT the failed one, so we can retry it later.
        if persist:
            await mark_seen(channel.id, [c for c in candidates if c["id"] != chosen["id"]])
        return RunResult("error", str(exc))

    if persist:
        await mark_seen(channel.id, candidates, published_id=chosen["id"])
    log.info("Posted to {}: {}", channel.chat_id, chosen["title"])
    return RunResult("posted", chosen["title"])
