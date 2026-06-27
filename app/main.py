from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import router
from app.config import settings
from app.db import init_db
from app.pipeline import run_once
from app.scheduler.worker import build_scheduler

log = logging.getLogger("main")


def _setup_logging() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet down noisy third-party loggers; keep our own at the configured level.
    for noisy in ("aiogram.event", "apscheduler", "httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def main() -> None:
    _setup_logging()
    log.info("Starting bot for channel %s", settings.channel_id)

    await init_db()

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = build_scheduler(bot)
    scheduler.start()

    if settings.run_on_startup:
        asyncio.create_task(run_once(bot))

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        log.info("Shutting down")
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
