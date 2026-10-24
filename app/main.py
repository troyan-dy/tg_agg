from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommandScopeChat,
    Message,
    TelegramObject,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger as log

from app.bot.fsm_storage import DBStorage
from app.bot.handlers import router, startup_notice
from app.config import settings
from app.db import run_migrations
from app.logging_setup import setup_logging
from app.pipeline import run_once
from app.scheduler.worker import build_scheduler
from app.storage import list_channels


async def _run_all_on_startup(bot: Bot) -> None:
    """Optional kick-off: run every enabled channel that has a feed configured."""
    for channel in await list_channels():
        if channel.enabled and channel.rss_url:
            # Sequential on purpose: one channel at a time, no burst of API calls.
            outcome = await run_once(bot, channel)  # noqa: WPS476
            log.info("Startup run for {}: {}", channel.chat_id, outcome)


async def log_incoming(
    next_handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    middleware_data: dict[str, Any],
) -> Any:
    """Log every incoming message before the admin filter runs (diagnostic)."""
    if isinstance(event, Message):
        uid = event.from_user.id if event.from_user else None
        log.info(
            "Incoming message from id={} (admin_id={}, allowed={}): {!r}",
            uid, settings.admin_id, uid == settings.admin_id, event.text,
        )
    return await next_handler(event, middleware_data)


async def _set_commands(bot: Bot) -> None:
    """Clear the «/» command menu — the bot is driven entirely by the keyboard.

    Telegram still surfaces a built-in «Start» button (sends /start) in a fresh
    chat, which the handlers treat as «open the menu»; everything else lives on
    the persistent reply keyboard, so we leave the «/» menu empty.
    """
    await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=settings.admin_id))


def _build_dispatcher() -> Dispatcher:
    # DB-backed FSM storage so a restart never drops a half-finished input flow.
    dp = Dispatcher(storage=DBStorage())
    dp.message.outer_middleware(log_incoming)
    dp.include_router(router)
    return dp


async def _notify_admin(bot: Bot) -> None:
    """Best-effort heads-up to the admin that the service (re)started, with the
    current settings. Never let a failed DM (e.g. chat not opened yet) crash
    startup."""
    try:
        await bot.send_message(settings.admin_id, await startup_notice())
    except Exception:
        log.opt(exception=True).warning("Could not send startup notice to admin")


async def _poll(bot: Bot, dp: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


async def _shutdown(bot: Bot, scheduler: AsyncIOScheduler) -> None:
    log.info("Shutting down")
    scheduler.shutdown(wait=False)
    await bot.session.close()


async def _prepare() -> None:
    setup_logging()
    log.info("Starting bot (admin_id={})", settings.admin_id)

    await run_migrations()
    # Alembic's env.py calls fileConfig(), which resets the stdlib root handlers.
    # loguru itself is untouched, but reinstall the stdlib→loguru intercept so
    # third-party logs keep flowing for the rest of the process.
    setup_logging()


async def _start_services(bot: Bot) -> AsyncIOScheduler:
    """Command menu, scheduler and the startup notice; returns the running scheduler."""
    await _set_commands(bot)
    scheduler = await build_scheduler(bot)
    scheduler.start()
    await _notify_admin(bot)
    return scheduler


async def main() -> None:
    await _prepare()

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = _build_dispatcher()
    scheduler = await _start_services(bot)

    if settings.run_on_startup:
        asyncio.create_task(_run_all_on_startup(bot))

    try:  # noqa: WPS501 - plain cleanup, errors propagate as before
        await _poll(bot, dp)
    finally:
        await _shutdown(bot, scheduler)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(main())
