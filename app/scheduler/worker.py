"""APScheduler setup: an hourly tick that runs each due channel.

A single cron job fires at the top of every hour (in `TIMEZONE`). It looks at
the current hour and runs the pipeline for every enabled channel whose
`run_hours` include it. Because the schedule is read live on each tick, editing
a channel's hours from the chat takes effect immediately — no rescheduling.
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.pipeline import run_once
from app.storage import list_channels

log = logging.getLogger("scheduler")


async def _tick(bot: Bot) -> None:
    """Run every channel whose schedule includes the current hour."""
    hour = datetime.now(ZoneInfo(settings.timezone)).hour
    channels = await list_channels()
    due = [c for c in channels if c.enabled and c.rss_url and hour in c.hours_list]
    log.info("Tick at %02d:00 (%s): %d of %d channels due", hour, settings.timezone,
             len(due), len(channels))
    for channel in due:
        result = await run_once(bot, channel)
        log.info("Channel %s (%s) run finished: %s", channel.id, channel.chat_id, result)


async def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.add_job(
        _tick,
        trigger=CronTrigger(minute=0),
        args=[bot],
        id="pipeline_tick",
        replace_existing=True,
        misfire_grace_time=600,
    )
    log.info("Scheduler configured: hourly tick (%s)", settings.timezone)
    return scheduler
