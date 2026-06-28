"""Chat control: set the RSS feed, trigger a run, check status.

The admin drives the bot either with slash commands or with a persistent
emoji keyboard (the buttons below the input field). Both paths share the same
logic; the keyboard just makes the common actions one tap away.
"""
from __future__ import annotations

import logging
from typing import cast
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.config import settings
from app.pipeline import run_once
from app.scheduler.worker import reschedule
from app.storage import (
    get_rss_url,
    get_run_hours,
    get_stored_rss_url,
    get_stored_run_hours,
    get_stored_tone,
    get_tone,
    get_tone_preset,
    parse_run_hours,
    set_rss_url,
    set_run_hours,
    set_tone,
)
from app.tone import TONES

log = logging.getLogger("handlers")

router = Router()
# Only the admin may control the bot — both messages and inline-button taps.
router.message.filter(F.from_user.id == settings.admin_id)
router.callback_query.filter(F.from_user.id == settings.admin_id)

# Callback-data prefix for the hour toggle grid: "hour:9" toggles 09:00.
HOUR_CB = "hour:"
HOURS_DONE_CB = "hours_done"
# Callback-data for the tone picker: "tone:expert" selects the preset.
TONE_CB = "tone:"
TONE_DONE_CB = "tone_done"

# --- Keyboard button labels (also matched as incoming text) --------------------
BTN_RUN = "🚀 Запустить"
BTN_PREVIEW = "👁 Предпросмотр"
BTN_RSS = "📡 Лента"
BTN_STATUS = "📊 Статус"
BTN_SETRSS = "📝 Сменить ленту"
BTN_SETHOURS = "🕒 Часы"
BTN_TONE = "🎨 Тон"
BTN_HELP = "❓ Помощь"
BTN_CANCEL = "❌ Отмена"


def main_kb() -> ReplyKeyboardMarkup:
    """Persistent keyboard with the everyday actions."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_RUN), KeyboardButton(text=BTN_PREVIEW)],
            [KeyboardButton(text=BTN_RSS), KeyboardButton(text=BTN_STATUS)],
            [KeyboardButton(text=BTN_SETRSS), KeyboardButton(text=BTN_SETHOURS)],
            [KeyboardButton(text=BTN_TONE), KeyboardButton(text=BTN_HELP)],
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


def hours_inline_kb(selected: set[int]) -> InlineKeyboardMarkup:
    """24-hour toggle grid: tap an hour to switch publishing on/off for it.

    Enabled hours are marked ✅, disabled ▫️. The last «Готово» row closes the
    editor. Four columns keep all 24 buttons readable on a phone.
    """
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for h in range(24):
        mark = "✅" if h in selected else "▫️"
        row.append(InlineKeyboardButton(text=f"{mark} {h:02d}", callback_data=f"{HOUR_CB}{h}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data=HOURS_DONE_CB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def tone_inline_kb(current: str) -> InlineKeyboardMarkup:
    """Radio-style tone picker: one row per preset, the active one marked ✅."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅ ' if key == current else ''}{t.label}",
                callback_data=f"{TONE_CB}{key}",
            )
        ]
        for key, t in TONES.items()
    ]
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data=TONE_DONE_CB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
    "• 🕒 <code>/hours</code> — показать часы публикации\n"
    "• 🕒 <code>/sethours</code> — сетка часов: тапай, чтобы включать/выключать "
    f"(в {settings.timezone}); можно и текстом: <code>/sethours 9,13,18</code>\n"
    "• 🎨 <code>/tone</code> — показать тон постов\n"
    "• 🎨 <code>/settone</code> — выбрать тон постов кнопками "
    "(или текстом: <code>/settone expert</code>)\n"
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
    await message.answer(f"✅ RSS-лента сохранена:\n{url}", reply_markup=main_kb())


async def _rss_source_suffix() -> str:
    """Label the effective feed's origin: DB value (set via chat) wins; the ENV
    var only kicks in as a fallback when nothing is stored."""
    return "" if await get_stored_rss_url() else " (из ENV — фолбэк)"


def _fmt_hours(hours: list[int]) -> str:
    return ", ".join(f"{h:02d}:00" for h in hours)


async def _hours_source_suffix() -> str:
    """Same ENV-fallback labelling as RSS, but for the run hours."""
    return "" if await get_stored_run_hours() else " (из ENV — фолбэк)"


async def _save_hours(message: Message, hours: list[int]) -> None:
    """Persist the new hours and rebuild the live cron schedule at once."""
    await set_run_hours(hours)
    await reschedule()
    log.info("Admin set run hours: %s", hours)
    await message.answer(
        f"✅ Часы публикации сохранены: {_fmt_hours(hours)} ({settings.timezone})",
        reply_markup=main_kb(),
    )


async def _show_hours(message: Message) -> None:
    hours = await get_run_hours()
    src = await _hours_source_suffix()
    await message.answer(
        f"🕒 Часы публикации{src}:\n{_fmt_hours(hours)} ({settings.timezone})",
        reply_markup=main_kb(),
    )


HOURS_GRID_PROMPT = (
    "🕒 Часы публикации — тапай по часам, чтобы включать/выключать.\n"
    "✅ — публикую, ▫️ — нет. Изменения применяются сразу."
)


async def _show_hours_grid(message: Message) -> None:
    """Open the interactive 0–23 toggle grid seeded with the current hours."""
    selected = set(await get_run_hours())
    await message.answer(HOURS_GRID_PROMPT, reply_markup=hours_inline_kb(selected))


async def _tone_source_suffix() -> str:
    """ENV-fallback label for the tone, same convention as RSS/hours."""
    return "" if await get_stored_tone() else " (из ENV — фолбэк)"


async def _show_tone(message: Message) -> None:
    tone = await get_tone_preset()
    src = await _tone_source_suffix()
    await message.answer(
        f"🎨 Тон постов{src}: {tone.label}\n{tone.description}", reply_markup=main_kb()
    )


async def _apply_tone(message: Message, key: str) -> None:
    await set_tone(key)
    tone = TONES[key]
    log.info("Admin set tone: %s", key)
    await message.answer(
        f"✅ Тон постов: {tone.label}\n{tone.description}", reply_markup=main_kb()
    )


def _tone_grid_prompt() -> str:
    legend = "\n".join(f"{t.label} — {t.description}" for t in TONES.values())
    return "🎨 Тон постов — выбери пресет (применяется сразу):\n\n" + legend


async def _show_tone_grid(message: Message) -> None:
    """Open the radio-style tone picker seeded with the current preset."""
    current = await get_tone()
    await message.answer(_tone_grid_prompt(), reply_markup=tone_inline_kb(current))


async def _show_rss(message: Message) -> None:
    url = await get_rss_url()
    src = await _rss_source_suffix() if url else ""
    await message.answer(
        f"📡 Текущая лента{src}:\n{url}"
        if url
        else "📡 RSS-лента ещё не задана. Нажми «📝 Сменить ленту» или /setrss <url>",
        reply_markup=main_kb(),
    )


async def _status_body() -> str:
    """The shared «what's configured right now» block, reused by /status and the
    startup notice."""
    url = await get_rss_url()
    src = await _rss_source_suffix() if url else ""
    hours = await get_run_hours()
    hours_src = await _hours_source_suffix()
    tone = await get_tone_preset()
    tone_src = await _tone_source_suffix()
    return (
        f"📣 Канал: {settings.channel_id}\n"
        f"📡 RSS: {(url or '—')}{src}\n"
        f"🕒 Запуски: {_fmt_hours(hours)} ({settings.timezone}){hours_src}\n"
        f"🎨 Тон: {tone.label}{tone_src}\n"
        f"🤖 Модель: {settings.deepseek_model}"
    )


async def _show_status(message: Message) -> None:
    await message.answer(
        "📊 <b>Статус</b>\n" + await _status_body(), reply_markup=main_kb()
    )


async def startup_notice() -> str:
    """Message sent to the admin every time the service (re)starts: a heads-up
    plus the current settings, so a silent crash-restart is never a surprise."""
    return "♻️ <b>Сервис перезапущен</b>\n" + await _status_body()


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


HOURS_HINT = (
    "Укажи часы через запятую (0–23), напр. "
    "<code>/sethours 9,13,18</code>"
)


@router.message(Command("hours"))
async def cmd_hours(message: Message) -> None:
    await _show_hours(message)


@router.message(Command("sethours"))
async def cmd_sethours(message: Message, command: CommandObject) -> None:
    # No args → open the interactive grid; args → parse them directly.
    if not (command.args or "").strip():
        await _show_hours_grid(message)
        return
    try:
        hours = parse_run_hours(command.args or "")
    except ValueError:
        await message.answer(HOURS_HINT)
        return
    await _save_hours(message, hours)


TONE_HINT = "Доступные пресеты: " + ", ".join(f"<code>{k}</code>" for k in TONES)


@router.message(Command("tone"))
async def cmd_tone(message: Message) -> None:
    await _show_tone(message)


@router.message(Command("settone"))
async def cmd_settone(message: Message, command: CommandObject) -> None:
    # No args → open the picker; an arg must be a known preset key.
    arg = (command.args or "").strip().lower()
    if not arg:
        await _show_tone_grid(message)
        return
    if arg not in TONES:
        await message.answer(TONE_HINT)
        return
    await _apply_tone(message, arg)


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


# --- «Часы» toggle grid (inline buttons) ---------------------------------------
@router.message(F.text == BTN_SETHOURS)
async def btn_sethours(message: Message) -> None:
    await _show_hours_grid(message)


@router.callback_query(F.data.startswith(HOUR_CB))
async def cb_toggle_hour(callback: CallbackQuery) -> None:
    """Toggle one hour on/off, persist, reschedule, and refresh the grid."""
    if not callback.data:  # data is guaranteed by the filter; narrows for mypy
        return
    hour = int(callback.data.removeprefix(HOUR_CB))
    selected = set(await get_run_hours())
    if hour in selected:
        if len(selected) == 1:
            # Never leave an empty schedule — that would silently fall back to ENV.
            await callback.answer("Должен остаться хотя бы один час", show_alert=True)
            return
        selected.discard(hour)
        toggled_on = False
    else:
        selected.add(hour)
        toggled_on = True
    await set_run_hours(sorted(selected))
    await reschedule()
    if callback.message is not None:
        await cast(Message, callback.message).edit_reply_markup(
            reply_markup=hours_inline_kb(selected)
        )
    await callback.answer(f"{hour:02d}:00 — {'вкл' if toggled_on else 'выкл'}")


@router.callback_query(F.data == HOURS_DONE_CB)
async def cb_hours_done(callback: CallbackQuery) -> None:
    """Close the grid: replace it with a plain summary of the saved hours."""
    hours = await get_run_hours()
    if callback.message is not None:
        await cast(Message, callback.message).edit_text(
            f"🕒 Часы публикации: {_fmt_hours(hours)} ({settings.timezone})"
        )
    await callback.answer("Сохранено")


# --- «Тон» picker (inline buttons) ---------------------------------------------
@router.message(F.text == BTN_TONE)
async def btn_tone(message: Message) -> None:
    await _show_tone_grid(message)


@router.callback_query(F.data.startswith(TONE_CB))
async def cb_select_tone(callback: CallbackQuery) -> None:
    """Select a tone preset, persist it, and refresh the picker."""
    if not callback.data:  # guaranteed by the filter; narrows for mypy
        return
    key = callback.data.removeprefix(TONE_CB)
    if key not in TONES:
        await callback.answer()
        return
    await set_tone(key)
    if callback.message is not None:
        await cast(Message, callback.message).edit_reply_markup(
            reply_markup=tone_inline_kb(key)
        )
    await callback.answer(f"Тон: {TONES[key].label}")


@router.callback_query(F.data == TONE_DONE_CB)
async def cb_tone_done(callback: CallbackQuery) -> None:
    """Close the picker: replace it with a plain summary of the chosen tone."""
    tone = await get_tone_preset()
    if callback.message is not None:
        await cast(Message, callback.message).edit_text(
            f"🎨 Тон постов: {tone.label}\n{tone.description}"
        )
    await callback.answer("Сохранено")


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
