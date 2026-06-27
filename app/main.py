from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    Message,
    TelegramObject,
)

from app.bot.handlers import router, startup_notice
from app.config import settings
from app.db import init_db
from app.pipeline import run_once
from app.scheduler.worker import build_scheduler

log = logging.getLogger("main")


async def log_incoming(
    handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
    event: TelegramObject,
    data: dict[str, Any],
) -> Any:
    """Log every incoming message before the admin filter runs (diagnostic)."""
    if isinstance(event, Message):
        uid = event.from_user.id if event.from_user else None
        log.info(
            "Incoming message from id=%s (admin_id=%s, allowed=%s): %r",
            uid, settings.admin_id, uid == settings.admin_id, event.text,
        )
    return await handler(event, data)


async def _set_commands(bot: Bot) -> None:
    """Populate the «/» menu in the admin's chat with friendly labels."""
    commands = [
        BotCommand(command="menu", description="🏠 Меню"),
        BotCommand(command="run", description="🚀 Запустить разбор"),
        BotCommand(command="preview", description="👁 Предпросмотр"),
        BotCommand(command="rss", description="📡 Текущая лента"),
        BotCommand(command="setrss", description="📝 Сменить ленту"),
        BotCommand(command="hours", description="🕒 Часы публикации"),
        BotCommand(command="sethours", description="🕒 Сменить часы"),
        BotCommand(command="status", description="📊 Статус"),
        BotCommand(command="help", description="❓ Помощь"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id=settings.admin_id))


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
    dp.message.outer_middleware(log_incoming)
    dp.include_router(router)

    await _set_commands(bot)

    scheduler = await build_scheduler(bot)
    scheduler.start()

    # Best-effort heads-up to the admin that the service (re)started, with the
    # current settings. Never let a failed DM (e.g. chat not opened yet) crash
    # startup.
    try:
        await bot.send_message(settings.admin_id, await startup_notice())
    except Exception:
        log.warning("Could not send startup notice to admin", exc_info=True)

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
