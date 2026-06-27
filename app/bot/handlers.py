"""Chat control: set the RSS feed, trigger a run, check status.

The admin drives the bot either with slash commands or with a persistent
emoji keyboard (the buttons below the input field). Both paths share the same
logic; the keyboard just makes the common actions one tap away.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app.config import settings
from app.pipeline import run_once
from app.storage import get_rss_url, set_rss_url

log = logging.getLogger("handlers")

router = Router()
# Only the admin may control the bot.
router.message.filter(F.from_user.id == settings.admin_id)

# --- Keyboard button labels (also matched as incoming text) --------------------
BTN_RUN = "🚀 Запустить"
BTN_PREVIEW = "👁 Предпросмотр"
BTN_RSS = "📡 Лента"
BTN_STATUS = "📊 Статус"
BTN_SETRSS = "📝 Сменить ленту"
BTN_HELP = "❓ Помощь"
BTN_CANCEL = "❌ Отмена"


def main_kb() -> ReplyKeyboardMarkup:
    """Persistent keyboard with the everyday actions."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_RUN), KeyboardButton(text=BTN_PREVIEW)],
            [KeyboardButton(text=BTN_RSS), KeyboardButton(text=BTN_STATUS)],
            [KeyboardButton(text=BTN_SETRSS), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие на клавиатуре…",
    )


def cancel_kb() -> ReplyKeyboardMarkup:
    """One-button keyboard shown while waiting for the RSS url."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        input_field_placeholder="Пришли ссылку на ленту…",
    )


class SetRss(StatesGroup):
    """Conversational flow for the «📝 Сменить ленту» button."""

    waiting_for_url = State()


HELP = (
    f"Я веду канал <b>{settings.channel_id}</b>: периодически читаю RSS, выбираю через DeepSeek "
    "самую важную новость и публикую пост.\n\n"
    "Жми кнопки на клавиатуре ниже 👇 или используй команды:\n"
    "• 🚀 <code>/run</code> — запустить разбор прямо сейчас\n"
    "• 👁 <code>/preview</code> — пробный прогон: пост придёт сюда, в канал НЕ публикуется "
    "и в базу не пишется\n"
    "• 📡 <code>/rss</code> — показать текущую ленту\n"
    "• 📝 <code>/setrss &lt;url&gt;</code> — задать RSS-ленту\n"
    "• 📊 <code>/status</code> — настройки и расписание\n"
    "• ❓ <code>/help</code> — эта справка"
)


def _valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# --- Shared action logic (reused by commands and keyboard buttons) -------------
async def _save_rss(message: Message, url: str) -> None:
    await set_rss_url(url)
    log.info("Admin set RSS url: %s", url)
    note = (
        "\n\n⚠️ Задан <code>RSS_URL</code> в ENV — он перекрывает это значение, "
        "пока не убран."
        if settings.rss_url
        else ""
    )
    await message.answer(f"✅ RSS-лента сохранена:\n{url}{note}", reply_markup=main_kb())


async def _show_rss(message: Message) -> None:
    url = await get_rss_url()
    src = " (из ENV)" if settings.rss_url else ""
    await message.answer(
        f"📡 Текущая лента{src}:\n{url}"
        if url
        else "📡 RSS-лента ещё не задана. Нажми «📝 Сменить ленту» или /setrss <url>",
        reply_markup=main_kb(),
    )


async def _show_status(message: Message) -> None:
    url = await get_rss_url()
    src = " (из ENV)" if settings.rss_url else ""
    await message.answer(
        "📊 <b>Статус</b>\n"
        f"📣 Канал: {settings.channel_id}\n"
        f"📡 RSS: {(url or '—')}{src}\n"
        f"🕒 Запуски: {', '.join(f'{h:02d}:00' for h in settings.run_hours_list)}"
        f" ({settings.timezone})\n"
        f"🤖 Модель: {settings.deepseek_model}",
        reply_markup=main_kb(),
    )


async def _do_run(message: Message, bot: Bot) -> None:
    log.info("Manual /run triggered by admin")
    await message.answer("⏳ Запускаю разбор ленты…")
    result = await run_once(bot)
    replies = {
        "posted": f"✅ Опубликовано: {result.detail}",
        "no_feed": "⚠️ RSS-лента не задана. Нажми «📝 Сменить ленту» или /setrss <url>",
        "no_new": "ℹ️ Новых новостей нет.",
        "error": f"❌ Ошибка: {result.detail}",
    }
    await message.answer(replies.get(result.status, str(result)), reply_markup=main_kb())


async def _do_preview(message: Message, bot: Bot) -> None:
    """Dry-run: full pipeline, but the post lands in this chat and nothing is
    written to the DB (entries are not marked seen — the run is repeatable)."""
    log.info("Manual /preview (dry-run) triggered by admin")
    await message.answer("⏳ Пробный прогон: соберу пост сюда, без публикации и записи в базу…")
    result = await run_once(bot, chat_id=settings.admin_id, persist=False)
    replies = {
        "posted": "☝️ Так выглядел бы пост. В канал не отправлено, база не тронута.",
        "no_feed": "⚠️ RSS-лента не задана. Нажми «📝 Сменить ленту» или /setrss <url>",
        "no_new": "ℹ️ Новых новостей нет.",
        "error": f"❌ Ошибка: {result.detail}",
    }
    await message.answer(replies.get(result.status, str(result)), reply_markup=main_kb())


# --- Slash commands ------------------------------------------------------------
@router.message(Command("start", "help", "menu"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP, reply_markup=main_kb())


@router.message(Command("setrss"))
async def cmd_setrss(message: Message, command: CommandObject) -> None:
    url = (command.args or "").strip()
    if not _valid_url(url):
        await message.answer(
            "Укажи корректный URL: <code>/setrss https://example.com/feed.xml</code>"
        )
        return
    await _save_rss(message, url)


@router.message(Command("rss"))
async def cmd_rss(message: Message) -> None:
    await _show_rss(message)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    await _show_status(message)


@router.message(Command("preview"))
async def cmd_preview(message: Message, bot: Bot) -> None:
    await _do_preview(message, bot)


@router.message(Command("run"))
async def cmd_run(message: Message, bot: Bot) -> None:
    await _do_run(message, bot)


# --- Keyboard buttons ----------------------------------------------------------
@router.message(F.text == BTN_RUN)
async def btn_run(message: Message, bot: Bot) -> None:
    await _do_run(message, bot)


@router.message(F.text == BTN_PREVIEW)
async def btn_preview(message: Message, bot: Bot) -> None:
    await _do_preview(message, bot)


@router.message(F.text == BTN_RSS)
async def btn_rss(message: Message) -> None:
    await _show_rss(message)


@router.message(F.text == BTN_STATUS)
async def btn_status(message: Message) -> None:
    await _show_status(message)


@router.message(F.text == BTN_HELP)
async def btn_help(message: Message) -> None:
    await cmd_help(message)


# --- «Сменить ленту» conversational flow ---------------------------------------
@router.message(F.text == BTN_SETRSS)
async def btn_setrss(message: Message, state: FSMContext) -> None:
    await state.set_state(SetRss.waiting_for_url)
    await message.answer(
        "📝 Пришли ссылку на RSS-ленту одним сообщением.\n"
        "Например: <code>https://example.com/feed.xml</code>",
        reply_markup=cancel_kb(),
    )


@router.message(SetRss.waiting_for_url, F.text == BTN_CANCEL)
async def setrss_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_kb())


@router.message(SetRss.waiting_for_url)
async def setrss_receive(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not _valid_url(url):
        await message.answer(
            "Это не похоже на ссылку 🤔 Пришли URL вида https://… или нажми «❌ Отмена»."
        )
        return
    await state.clear()
    await _save_rss(message, url)


# --- Fallback ------------------------------------------------------------------
@router.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Не понял 🤔 Выбери действие на клавиатуре ниже или загляни в ❓ /help.",
        reply_markup=main_kb(),
    )
