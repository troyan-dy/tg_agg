"""APScheduler setup: run the pipeline a few times a day.

The hours come from `storage.get_run_hours` — the value set via chat (DB) wins,
the RUN_HOURS env var is only a fallback. When the admin changes the hours,
`reschedule` rebuilds the cron jobs in place without restarting the process.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.pipeline import run_once
from app.storage import get_run_hours

log = logging.getLogger("scheduler")

# The single live scheduler + its bot, so handlers can trigger a reschedule
# after the admin edits the hours.
_scheduler: AsyncIOScheduler | None = None
_bot: Bot | None = None


async def _job(bot: Bot) -> None:
    log.info("Scheduled run starting")
    result = await run_once(bot)
    log.info("Scheduled run finished: %s", result)


async def _install_jobs(scheduler: AsyncIOScheduler, bot: Bot) -> list[int]:
    """(Re)install the per-hour pipeline jobs to match the effective hours."""
    for job in scheduler.get_jobs():
        if job.id.startswith("pipeline_"):
            scheduler.remove_job(job.id)
    hours = await get_run_hours()
    for hour in hours:
        scheduler.add_job(
            _job,
            trigger=CronTrigger(hour=hour, minute=0),
            args=[bot],
            id=f"pipeline_{hour}",
            replace_existing=True,
            misfire_grace_time=600,
        )
    log.info("Scheduler configured for hours %s (%s)", hours, settings.timezone)
    return hours


async def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    global _scheduler, _bot
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    await _install_jobs(scheduler, bot)
    _scheduler, _bot = scheduler, bot
    return scheduler


async def reschedule() -> list[int]:
    """Rebuild the cron jobs from the current (DB-backed) hours. Returns the
    hours now in effect. No-op-safe if the scheduler isn't built yet."""
    if _scheduler is None or _bot is None:
        return await get_run_hours()
    return await _install_jobs(_scheduler, _bot)
