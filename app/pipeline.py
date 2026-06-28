"""One end-to-end run: RSS -> dedup -> DeepSeek pick -> generate -> publish."""
from __future__ import annotations

import logging

from aiogram import Bot

from app.config import settings
from app.services import deepseek, rss
from app.storage import (
    filter_unseen,
    get_rss_url,
    get_tone_preset,
    mark_seen,
    published_among,
    recent_published_titles,
)

log = logging.getLogger("pipeline")

# Telegram caps photo captions at 1024 chars; plain messages allow more.
_CAPTION_LIMIT = 1024


async def _publish(bot: Bot, target: int | str, text: str, image: str) -> None:
    """Send the post, attaching the article image when there is one.

    If the post is longer than a photo caption allows, the image goes first as
    its own message and the full text follows, so nothing gets truncated.
    """
    if image:
        try:
            if len(text) <= _CAPTION_LIMIT:
                await bot.send_photo(chat_id=target, photo=image, caption=text)
                return
            await bot.send_photo(chat_id=target, photo=image)
        except Exception as exc:  # noqa: BLE001
            # A dead/unsupported image URL must not block the post itself.
            log.warning("Could not send image %s: %s — posting text only", image, exc)
    await bot.send_message(chat_id=target, text=text, disable_web_page_preview=True)


class RunResult:
    def __init__(self, status: str, detail: str = ""):
        self.status = status  # posted | no_feed | no_new | error
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.status}: {self.detail}" if self.detail else self.status


async def run_once(
    bot: Bot, *, chat_id: int | str | None = None, persist: bool = True
) -> RunResult:
    """One pipeline run.

    chat_id — where to send the post; defaults to the channel.
    persist — whether to mark evaluated entries as seen. Set False for a
        preview/dry-run so it stays repeatable and touches nothing in the DB.
    """
    target = chat_id if chat_id is not None else settings.channel_id
    url = await get_rss_url()
    if not url:
        log.info("No RSS url configured")
        return RunResult("no_feed", "RSS-ссылка не задана. Установи её: /setrss <url>")

    entries = await rss.fetch_entries(url, limit=max(settings.max_candidates * 2, 50))
    if not entries:
        return RunResult("no_new", "Лента пуста или недоступна.")

    ids = [e["id"] for e in entries]
    unseen_ids = await filter_unseen(ids)
    candidates = [e for e in entries if e["id"] in unseen_ids][: settings.max_candidates]
    if not candidates:
        # No fresh entries. Rather than give up, re-surface entries already seen
        # but never published (may break chronological order — acceptable here),
        # so a manual run still has something to post.
        published_ids = await published_among(ids)
        candidates = [e for e in entries if e["id"] not in published_ids][
            : settings.max_candidates
        ]
        if candidates:
            log.info("No fresh entries; falling back to %d seen-but-unposted", len(candidates))
    if not candidates:
        log.info("Nothing to post — every feed entry is already published")
        return RunResult("no_new", "В ленте нет ничего, что ещё не было опубликовано.")

    recent = await recent_published_titles()
    log.info(
        "%d new candidates, %d posted in last 24h, asking DeepSeek to pick",
        len(candidates), len(recent),
    )
    index = await deepseek.pick_most_relevant(candidates, recent)
    chosen = candidates[index]

    try:
        tone = await get_tone_preset()
        text = await deepseek.generate_post(chosen, tone)
        await _publish(bot, target, text, chosen.get("image", ""))
    except Exception as exc:  # noqa: BLE001
        log.exception("Publishing failed")
        # Mark the rest as seen but NOT the failed one, so we can retry it later.
        if persist:
            await mark_seen([c for c in candidates if c["id"] != chosen["id"]])
        return RunResult("error", str(exc))

    if persist:
        await mark_seen(candidates, published_id=chosen["id"])
    log.info("Posted: %s", chosen["title"])
    return RunResult("posted", chosen["title"])
