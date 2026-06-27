"""Chat control: set the RSS feed, trigger a run, check status."""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.config import settings
from app.pipeline import run_once
from app.storage import get_rss_url, set_rss_url

log = logging.getLogger("handlers")

router = Router()
# Only the admin may control the bot.
router.message.filter(F.from_user.id == settings.admin_id)

HELP = (
    f"Я веду канал <b>{settings.channel_id}</b>: периодически читаю RSS, выбираю через DeepSeek "
    "самую важную новость и публикую пост.\n\n"
    "Команды:\n"
    "• <code>/setrss &lt;url&gt;</code> — задать RSS-ленту\n"
    "• <code>/rss</code> — показать текущую ленту\n"
    "• <code>/run</code> — запустить разбор прямо сейчас\n"
    "• <code>/status</code> — настройки и расписание\n"
    "• <code>/help</code> — эта справка"
)


def _valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("setrss"))
async def cmd_setrss(message: Message, command: CommandObject) -> None:
    url = (command.args or "").strip()
    if not _valid_url(url):
        await message.answer("Укажи корректный URL: <code>/setrss https://example.com/feed.xml</code>")
        return
    await set_rss_url(url)
    log.info("Admin set RSS url: %s", url)
    await message.answer(f"✅ RSS-лента сохранена:\n{url}")


@router.message(Command("rss"))
async def cmd_rss(message: Message) -> None:
    url = await get_rss_url()
    await message.answer(
        f"Текущая лента:\n{url}" if url else "RSS-лента ещё не задана. /setrss <url>"
    )


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    url = await get_rss_url()
    await message.answer(
        "<b>Статус</b>\n"
        f"Канал: {settings.channel_id}\n"
        f"RSS: {url or '—'}\n"
        f"Запуски: {', '.join(f'{h:02d}:00' for h in settings.run_hours_list)}"
        f" ({settings.timezone})\n"
        f"Модель: {settings.deepseek_model}"
    )


@router.message(Command("run"))
async def cmd_run(message: Message, bot: Bot) -> None:
    log.info("Manual /run triggered by admin")
    await message.answer("⏳ Запускаю разбор ленты…")
    result = await run_once(bot)
    replies = {
        "posted": f"✅ Опубликовано: {result.detail}",
        "no_feed": "⚠️ RSS-лента не задана. /setrss <url>",
        "no_new": "ℹ️ Новых новостей нет.",
        "error": f"❌ Ошибка: {result.detail}",
    }
    await message.answer(replies.get(result.status, str(result)))
