"""APScheduler setup: an hourly tick that runs each due channel.

A single cron job fires at the top of every hour (in `TIMEZONE`). It looks at
the current hour and runs the pipeline for every enabled channel whose
`run_hours` include it. Because the schedule is read live on each tick, editing
a channel's hours from the chat takes effect immediately — no rescheduling.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger as log

from app.config import settings
from app.models import Channel
from app.pipeline import run_once
from app.storage import list_channels

# How late a missed tick may still fire (e.g. after a restart).
_MISFIRE_GRACE_SECONDS = 600


def _is_due(channel: Channel, hour: int) -> bool:
    return bool(channel.enabled and channel.rss_url) and hour in channel.hours_list


async def _tick(bot: Bot) -> None:
    """Run every channel whose schedule includes the current hour."""
    hour = datetime.now(ZoneInfo(settings.timezone)).hour
    channels = await list_channels()
    due = [channel for channel in channels if _is_due(channel, hour)]
    log.info("Tick at {:02d}:00 ({}): {} of {} channels due", hour, settings.timezone,
             len(due), len(channels))
    for channel in due:
        # Sequential on purpose: one channel at a time, no burst of API calls.
        outcome = await run_once(bot, channel)  # noqa: WPS476
        log.info("Channel {} ({}) run finished: {}", channel.id, channel.chat_id, outcome)


async def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        _tick,
        trigger=CronTrigger(minute=0),
        args=[bot],
        id="pipeline_tick",
        replace_existing=True,
        misfire_grace_time=_MISFIRE_GRACE_SECONDS,
    )
    log.info("Scheduler configured: hourly tick ({})", settings.timezone)
    return scheduler
