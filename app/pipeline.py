"""One end-to-end run: RSS -> dedup -> DeepSeek pick -> generate -> publish."""
from __future__ import annotations

import logging

from aiogram import Bot

from app.config import settings
from app.services import deepseek, rss
from app.storage import (
    filter_unseen,
    get_rss_url,
    mark_seen,
    published_among,
    recent_published_titles,
)

log = logging.getLogger("pipeline")


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
        text = await deepseek.generate_post(chosen)
        await bot.send_message(chat_id=target, text=text, disable_web_page_preview=False)
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
