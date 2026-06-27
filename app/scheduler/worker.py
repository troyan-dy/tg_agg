"""APScheduler setup: run the pipeline a few times a day."""
from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.pipeline import run_once

log = logging.getLogger("scheduler")


async def _job(bot: Bot) -> None:
    log.info("Scheduled run starting")
    result = await run_once(bot)
    log.info("Scheduled run finished: %s", result)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    for hour in settings.run_hours_list:
        scheduler.add_job(
            _job,
            trigger=CronTrigger(hour=hour, minute=0),
            args=[bot],
            id=f"pipeline_{hour}",
            replace_existing=True,
            misfire_grace_time=600,
        )
    log.info("Scheduler configured for hours %s (%s)", settings.run_hours_list, settings.timezone)
    return scheduler
