"""Chat control: manage channels and, for the selected one, set the feed/tone/
schedule, trigger a run, check status.

The bot runs several channels; the admin picks an «active» channel (📺 Каналы)
and the everyday buttons — 📡 Лента, 🎨 Тон, 🕒 Часы, 🚀 Запустить — all act on
it. A channel is added by sending its link/@username: the bot resolves the chat
and checks it is an admin there with the right to post.

The admin drives the bot either with slash commands or with a persistent emoji
keyboard; both paths share the same logic.
"""
from __future__ import annotations

import logging
import re
from typing import cast

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
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
from app.models import Channel
from app.pipeline import run_once
from app.storage import (
    add_channel,
    delete_channel,
    get_channel,
    get_channel_by_chat,
    get_selected_channel,
    list_channels,
    parse_run_hours,
    set_selected_channel,
    update_channel,
)
from app.tone import TONES, get_preset

log = logging.getLogger("handlers")

router = Router()
# Only the admin may control the bot — both messages and inline-button taps.
router.message.filter(F.from_user.id == settings.admin_id)
router.callback_query.filter(F.from_user.id == settings.admin_id)

# Callback-data prefixes.
HOUR_CB = "hour:"           # "hour:9" toggles 09:00 for the selected channel
HOURS_DONE_CB = "hours_done"
TONE_CB = "tone:"           # "tone:expert" selects the preset
TONE_DONE_CB = "tone_done"
CH_SEL_CB = "chsel:"        # "chsel:3" makes channel #3 active
CH_DEL_CB = "chdel:"        # ask to delete channel #3
CH_DELYES_CB = "chdelyes:"  # confirm deletion of channel #3
CH_ADD_CB = "chadd"         # start the add-channel flow

# --- Keyboard button labels (also matched as incoming text) --------------------
BTN_RUN = "🚀 Запустить"
BTN_PREVIEW = "👁 Предпросмотр"
BTN_CHANNELS = "📺 Каналы"
BTN_ADDCHANNEL = "➕ Добавить канал"
BTN_RSS = "📡 Лента"
BTN_SETRSS = "📝 Сменить ленту"
BTN_TONE = "🎨 Тон"
BTN_SETHOURS = "🕒 Часы"
BTN_STATUS = "📊 Статус"
BTN_HELP = "❓ Помощь"
BTN_CANCEL = "❌ Отмена"


def main_kb() -> ReplyKeyboardMarkup:
    """Persistent keyboard with the everyday actions."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_RUN), KeyboardButton(text=BTN_PREVIEW)],
            [KeyboardButton(text=BTN_CHANNELS), KeyboardButton(text=BTN_ADDCHANNEL)],
            [KeyboardButton(text=BTN_RSS), KeyboardButton(text=BTN_SETRSS)],
            [KeyboardButton(text=BTN_TONE), KeyboardButton(text=BTN_SETHOURS)],
            [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери действие на клавиатуре…",
    )


def cancel_kb(placeholder: str) -> ReplyKeyboardMarkup:
    """One-button keyboard shown while waiting for a url/link."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        input_field_placeholder=placeholder,
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


def channels_inline_kb(channels: list[Channel], selected_id: int | None) -> InlineKeyboardMarkup:
    """List of channels: tap a name to make it active (✅), 🗑 to delete, plus an
    «add» row at the bottom."""
    rows: list[list[InlineKeyboardButton]] = []
    for c in channels:
        mark = "✅ " if c.id == selected_id else ""
        name = mark + (c.title or c.chat_id)
        rows.append(
            [
                InlineKeyboardButton(text=name, callback_data=f"{CH_SEL_CB}{c.id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"{CH_DEL_CB}{c.id}"),
            ]
        )
    rows.append([InlineKeyboardButton(text=BTN_ADDCHANNEL, callback_data=CH_ADD_CB)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class SetRss(StatesGroup):
    """Conversational flow for the «📝 Сменить ленту» button."""

    waiting_for_url = State()


class AddChannel(StatesGroup):
    """Conversational flow for adding a channel by its link/@username."""

    waiting_for_link = State()


HELP = (
    "Я веду несколько Telegram-каналов на автопилоте: периодически читаю их RSS, "
    "через DeepSeek выбираю важную новость и публикую пост.\n\n"
    "Сначала добавь канал (📺 «Каналы» → ➕, или пришли ссылку через "
    "«➕ Добавить канал»). Бот должен быть админом канала с правом публикации. "
    "Затем выбери активный канал — кнопки 📡 Лента, 🎨 Тон, 🕒 Часы, 🚀 Запустить "
    "действуют на него.\n\n"
    "Команды:\n"
    "• 📺 <code>/channels</code> — список каналов, выбор активного, удаление\n"
    "• ➕ <code>/addchannel</code> — добавить канал по ссылке\n"
    "• 🚀 <code>/run</code> — запустить разбор активного канала сейчас\n"
    "• 👁 <code>/preview</code> — пробный прогон: пост придёт сюда, в канал НЕ "
    "публикуется и в базу не пишется\n"
    "• 📡 <code>/rss</code> — показать ленту активного канала\n"
    "• 📝 <code>/setrss &lt;url&gt;</code> — задать RSS-ленту активного канала\n"
    "• 🕒 <code>/hours</code> — часы публикации активного канала\n"
    "• 🕒 <code>/sethours</code> — сетка часов: тапай, чтобы включать/выключать "
    f"(в {settings.timezone}); можно и текстом: <code>/sethours 9,13,18</code>\n"
    "• 🎨 <code>/tone</code> — тон постов активного канала\n"
    "• 🎨 <code>/settone</code> — выбрать тон кнопками "
    "(или текстом: <code>/settone expert</code>)\n"
    "• 📊 <code>/status</code> — все каналы и их настройки\n"
    "• ❓ <code>/help</code> — эта справка"
)


def _valid_url(url: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _ch_label(channel: Channel) -> str:
    return channel.title or channel.chat_id


def _fmt_hours(hours: list[int]) -> str:
    return ", ".join(f"{h:02d}:00" for h in hours)


async def _require_channel(message: Message) -> Channel | None:
    """The selected channel, or a friendly nudge to add one first."""
    channel = await get_selected_channel()
    if channel is None:
        await message.answer(
            "Сначала добавь канал: «➕ Добавить канал» или /addchannel.",
            reply_markup=main_kb(),
        )
    return channel


# --- Channels: list / select / delete / add ------------------------------------
CHANNELS_PROMPT = "📺 Каналы — тапни по названию, чтобы сделать активным (✅); 🗑 — удалить."


async def _show_channels(message: Message) -> None:
    channels = await list_channels()
    if not channels:
        await message.answer(
            "📺 Каналов пока нет. Добавь первый:",
            reply_markup=channels_inline_kb([], None),
        )
        return
    selected = await get_selected_channel()
    await message.answer(
        CHANNELS_PROMPT,
        reply_markup=channels_inline_kb(channels, selected.id if selected else None),
    )


_REF_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/(?P<rest>.+)", re.IGNORECASE)


def _parse_channel_ref(text: str) -> str | None:
    """Turn admin input into something bot.get_chat accepts: a @username or a
    numeric id. Returns None for invite links / unrecognised input."""
    text = text.strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+", text):  # raw numeric id, e.g. -1001234567890
        return text
    if text.startswith("@"):
        return text
    m = _REF_RE.fullmatch(text)
    if m:
        rest = m.group("rest").strip("/")
        priv = re.match(r"c/(\d+)", rest)  # private link t.me/c/<id>/<msg>
        if priv:
            return f"-100{priv.group(1)}"
        first = rest.split("/")[0].split("?")[0]
        if not first or first == "joinchat" or first.startswith("+"):
            return None  # invite link — can't be resolved via the API
        return f"@{first}"
    # Bare username without @, e.g. "mychannel"
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,}", text):
        return f"@{text}"
    return None


async def _try_add_channel(message: Message, bot: Bot, raw: str) -> None:
    """Resolve a channel reference, verify the bot can post there, and store it."""
    ref = _parse_channel_ref(raw)
    if ref is None:
        await message.answer(
            "Не разобрал ссылку. Пришли @username, ссылку вида https://t.me/имя_канала "
            "или числовой id. Пригласительные ссылки (t.me/+…) не подходят — добавь "
            "бота в канал и пришли его @username."
        )
        return
    try:
        chat = await bot.get_chat(ref)
    except Exception as exc:  # noqa: BLE001
        log.warning("get_chat(%s) failed: %s", ref, exc)
        await message.answer(
            "❌ Не нашёл такой канал. Проверь ссылку и что бот уже добавлен в канал."
        )
        return

    me = await bot.me()
    try:
        member = await bot.get_chat_member(chat.id, me.id)
    except Exception as exc:  # noqa: BLE001
        log.warning("get_chat_member failed for %s: %s", chat.id, exc)
        await message.answer("❌ Бот не состоит в этом канале. Добавь его админом и повтори.")
        return

    if member.status == ChatMemberStatus.CREATOR:
        can_post = True
    elif member.status == ChatMemberStatus.ADMINISTRATOR:
        can_post = bool(getattr(member, "can_post_messages", False))
    else:
        can_post = False
    if not can_post:
        await message.answer(
            "❌ Бот не админ канала или у него нет права публиковать сообщения. "
            "Дай боту права администратора с публикацией и повтори."
        )
        return

    chat_id = str(chat.id)
    if await get_channel_by_chat(chat_id):
        await message.answer("ℹ️ Этот канал уже добавлен.", reply_markup=main_kb())
        return

    channel = await add_channel(chat_id, title=chat.title)
    await set_selected_channel(channel.id)
    log.info("Admin added channel %s (%s)", chat_id, chat.title)
    await message.answer(
        f"✅ Канал добавлен и выбран активным: <b>{_ch_label(channel)}</b>\n"
        "Теперь задай ему ленту (📝 «Сменить ленту»), часы и тон.",
        reply_markup=main_kb(),
    )


# --- RSS (selected channel) ----------------------------------------------------
async def _save_rss(message: Message, url: str) -> None:
    channel = await _require_channel(message)
    if channel is None:
        return
    await update_channel(channel.id, rss_url=url)
    log.info("Admin set RSS url for channel %s: %s", channel.id, url)
    await message.answer(
        f"✅ RSS-лента канала <b>{_ch_label(channel)}</b> сохранена:\n{url}",
        reply_markup=main_kb(),
    )


async def _show_rss(message: Message) -> None:
    channel = await _require_channel(message)
    if channel is None:
        return
    await message.answer(
        f"📡 Лента канала <b>{_ch_label(channel)}</b>:\n{channel.rss_url}"
        if channel.rss_url
        else f"📡 У канала <b>{_ch_label(channel)}</b> лента не задана. "
        "Нажми «📝 Сменить ленту» или /setrss <url>",
        reply_markup=main_kb(),
    )


# --- Hours (selected channel) --------------------------------------------------
async def _show_hours(message: Message) -> None:
    channel = await _require_channel(message)
    if channel is None:
        return
    await message.answer(
        f"🕒 Часы публикации <b>{_ch_label(channel)}</b>:\n"
        f"{_fmt_hours(channel.hours_list)} ({settings.timezone})",
        reply_markup=main_kb(),
    )


HOURS_GRID_PROMPT = (
    "🕒 Часы публикации — тапай по часам, чтобы включать/выключать.\n"
    "✅ — публикую, ▫️ — нет. Изменения применяются сразу."
)


async def _show_hours_grid(message: Message) -> None:
    """Open the interactive 0–23 toggle grid seeded with the channel's hours."""
    channel = await _require_channel(message)
    if channel is None:
        return
    await message.answer(
        f"{HOURS_GRID_PROMPT}\nКанал: <b>{_ch_label(channel)}</b>",
        reply_markup=hours_inline_kb(set(channel.hours_list)),
    )


async def _save_hours(message: Message, hours: list[int]) -> None:
    channel = await _require_channel(message)
    if channel is None:
        return
    await update_channel(channel.id, run_hours=",".join(str(h) for h in hours))
    log.info("Admin set run hours for channel %s: %s", channel.id, hours)
    await message.answer(
        f"✅ Часы <b>{_ch_label(channel)}</b>: {_fmt_hours(hours)} ({settings.timezone})",
        reply_markup=main_kb(),
    )


# --- Tone (selected channel) ---------------------------------------------------
async def _show_tone(message: Message) -> None:
    channel = await _require_channel(message)
    if channel is None:
        return
    tone = get_preset(channel.tone)
    await message.answer(
        f"🎨 Тон <b>{_ch_label(channel)}</b>: {tone.label}\n{tone.description}",
        reply_markup=main_kb(),
    )


def _tone_grid_prompt() -> str:
    legend = "\n".join(f"{t.label} — {t.description}" for t in TONES.values())
    return "🎨 Тон постов — выбери пресет (применяется сразу):\n\n" + legend


async def _show_tone_grid(message: Message) -> None:
    """Open the radio-style tone picker seeded with the channel's preset."""
    channel = await _require_channel(message)
    if channel is None:
        return
    await message.answer(
        f"{_tone_grid_prompt()}\n\nКанал: <b>{_ch_label(channel)}</b>",
        reply_markup=tone_inline_kb(channel.tone),
    )


# --- Status --------------------------------------------------------------------
async def _status_body() -> str:
    """A summary of every channel and its settings, marking the active one."""
    channels = await list_channels()
    if not channels:
        return "Каналов пока нет. Добавь первый: «➕ Добавить канал» или /addchannel."
    selected = await get_selected_channel()
    sel_id = selected.id if selected else None
    blocks = []
    for c in channels:
        head = "▶️" if c.id == sel_id else "•"
        tone = get_preset(c.tone)
        off = "" if c.enabled else "  ⏸ выключен"
        blocks.append(
            f"{head} <b>{_ch_label(c)}</b>{off}\n"
            f"   📡 {c.rss_url or '— не задана'}\n"
            f"   🕒 {_fmt_hours(c.hours_list)} ({settings.timezone})\n"
            f"   🎨 {tone.label}"
        )
    return f"🤖 Модель: {settings.deepseek_model}\n\n" + "\n\n".join(blocks)


async def _show_status(message: Message) -> None:
    await message.answer("📊 <b>Статус</b>\n" + await _status_body(), reply_markup=main_kb())


async def startup_notice() -> str:
    """Message sent to the admin every time the service (re)starts: a heads-up
    plus the current settings, so a silent crash-restart is never a surprise."""
    return "♻️ <b>Сервис перезапущен</b>\n" + await _status_body()


# --- Run / preview (selected channel) ------------------------------------------
async def _do_run(message: Message, bot: Bot) -> None:
    channel = await _require_channel(message)
    if channel is None:
        return
    log.info("Manual /run for channel %s triggered by admin", channel.id)
    await message.answer(f"⏳ Запускаю разбор ленты канала <b>{_ch_label(channel)}</b>…")
    result = await run_once(bot, channel)
    replies = {
        "posted": f"✅ Опубликовано в <b>{_ch_label(channel)}</b>: {result.detail}",
        "no_feed": "⚠️ RSS-лента не задана. Нажми «📝 Сменить ленту» или /setrss <url>",
        "no_new": "ℹ️ Новых новостей нет.",
        "error": f"❌ Ошибка: {result.detail}",
    }
    await message.answer(replies.get(result.status, str(result)), reply_markup=main_kb())


async def _do_preview(message: Message, bot: Bot) -> None:
    """Dry-run: full pipeline, but the post lands in this chat and nothing is
    written to the DB (entries are not marked seen — the run is repeatable)."""
    channel = await _require_channel(message)
    if channel is None:
        return
    log.info("Manual /preview for channel %s triggered by admin", channel.id)
    await message.answer(
        f"⏳ Пробный прогон <b>{_ch_label(channel)}</b>: соберу пост сюда, "
        "без публикации и записи в базу…"
    )
    result = await run_once(bot, channel, chat_id=settings.admin_id, persist=False)
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


@router.message(Command("channels"))
async def cmd_channels(message: Message) -> None:
    await _show_channels(message)


@router.message(Command("addchannel"))
async def cmd_addchannel(message: Message, state: FSMContext) -> None:
    await _start_add_channel(message, state)


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


HOURS_HINT = "Укажи часы через запятую (0–23), напр. <code>/sethours 9,13,18</code>"


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
    channel = await _require_channel(message)
    if channel is None:
        return
    await update_channel(channel.id, tone=arg)
    tone = TONES[arg]
    await message.answer(
        f"✅ Тон <b>{_ch_label(channel)}</b>: {tone.label}\n{tone.description}",
        reply_markup=main_kb(),
    )


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


@router.message(F.text == BTN_CHANNELS)
async def btn_channels(message: Message) -> None:
    await _show_channels(message)


@router.message(F.text == BTN_RSS)
async def btn_rss(message: Message) -> None:
    await _show_rss(message)


@router.message(F.text == BTN_STATUS)
async def btn_status(message: Message) -> None:
    await _show_status(message)


@router.message(F.text == BTN_HELP)
async def btn_help(message: Message) -> None:
    await cmd_help(message)


@router.message(F.text == BTN_SETHOURS)
async def btn_sethours(message: Message) -> None:
    await _show_hours_grid(message)


@router.message(F.text == BTN_TONE)
async def btn_tone(message: Message) -> None:
    await _show_tone_grid(message)


# --- «Часы» toggle grid (inline buttons) ---------------------------------------
@router.callback_query(F.data.startswith(HOUR_CB))
async def cb_toggle_hour(callback: CallbackQuery) -> None:
    """Toggle one hour on/off for the selected channel and refresh the grid."""
    if not callback.data:  # data is guaranteed by the filter; narrows for mypy
        return
    channel = await get_selected_channel()
    if channel is None:
        await callback.answer("Нет активного канала", show_alert=True)
        return
    hour = int(callback.data.removeprefix(HOUR_CB))
    selected = set(channel.hours_list)
    if hour in selected:
        if len(selected) == 1:
            # Never leave an empty schedule — a channel with no hours never runs.
            await callback.answer("Должен остаться хотя бы один час", show_alert=True)
            return
        selected.discard(hour)
        toggled_on = False
    else:
        selected.add(hour)
        toggled_on = True
    await update_channel(channel.id, run_hours=",".join(str(h) for h in sorted(selected)))
    if callback.message is not None:
        await cast(Message, callback.message).edit_reply_markup(
            reply_markup=hours_inline_kb(selected)
        )
    await callback.answer(f"{hour:02d}:00 — {'вкл' if toggled_on else 'выкл'}")


@router.callback_query(F.data == HOURS_DONE_CB)
async def cb_hours_done(callback: CallbackQuery) -> None:
    """Close the grid: replace it with a plain summary of the saved hours."""
    channel = await get_selected_channel()
    hours = channel.hours_list if channel else []
    if callback.message is not None:
        await cast(Message, callback.message).edit_text(
            f"🕒 Часы публикации: {_fmt_hours(hours)} ({settings.timezone})"
        )
    await callback.answer("Сохранено")


# --- «Тон» picker (inline buttons) ---------------------------------------------
@router.callback_query(F.data.startswith(TONE_CB))
async def cb_select_tone(callback: CallbackQuery) -> None:
    """Select a tone preset for the selected channel and refresh the picker."""
    if not callback.data:  # guaranteed by the filter; narrows for mypy
        return
    channel = await get_selected_channel()
    if channel is None:
        await callback.answer("Нет активного канала", show_alert=True)
        return
    key = callback.data.removeprefix(TONE_CB)
    if key not in TONES:
        await callback.answer()
        return
    await update_channel(channel.id, tone=key)
    if callback.message is not None:
        await cast(Message, callback.message).edit_reply_markup(
            reply_markup=tone_inline_kb(key)
        )
    await callback.answer(f"Тон: {TONES[key].label}")


@router.callback_query(F.data == TONE_DONE_CB)
async def cb_tone_done(callback: CallbackQuery) -> None:
    """Close the picker: replace it with a plain summary of the chosen tone."""
    channel = await get_selected_channel()
    tone = get_preset(channel.tone if channel else None)
    if callback.message is not None:
        await cast(Message, callback.message).edit_text(
            f"🎨 Тон постов: {tone.label}\n{tone.description}"
        )
    await callback.answer("Сохранено")


# --- Channels (inline buttons) -------------------------------------------------
async def _refresh_channels(callback: CallbackQuery) -> None:
    channels = await list_channels()
    selected = await get_selected_channel()
    sel_id = selected.id if selected else None
    if callback.message is not None:
        await cast(Message, callback.message).edit_reply_markup(
            reply_markup=channels_inline_kb(channels, sel_id)
        )


@router.callback_query(F.data.startswith(CH_SEL_CB))
async def cb_select_channel(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    channel = await get_channel(int(callback.data.removeprefix(CH_SEL_CB)))
    if channel is None:
        await callback.answer("Канал не найден", show_alert=True)
        await _refresh_channels(callback)
        return
    await set_selected_channel(channel.id)
    await _refresh_channels(callback)
    await callback.answer(f"Активный канал: {_ch_label(channel)}")


@router.callback_query(F.data.startswith(CH_DELYES_CB))
async def cb_delete_channel_yes(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    channel_id = int(callback.data.removeprefix(CH_DELYES_CB))
    channel = await get_channel(channel_id)
    await delete_channel(channel_id)
    await _refresh_channels(callback)
    await callback.answer(f"Удалён: {_ch_label(channel)}" if channel else "Удалён")


@router.callback_query(F.data.startswith(CH_DEL_CB))
async def cb_delete_channel_ask(callback: CallbackQuery) -> None:
    if not callback.data:
        return
    channel_id = int(callback.data.removeprefix(CH_DEL_CB))
    channel = await get_channel(channel_id)
    if channel is None:
        await callback.answer("Канал не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 Удалить", callback_data=f"{CH_DELYES_CB}{channel_id}"
                ),
                InlineKeyboardButton(text="↩️ Отмена", callback_data="chlist"),
            ]
        ]
    )
    if callback.message is not None:
        await cast(Message, callback.message).edit_text(
            f"Удалить канал <b>{_ch_label(channel)}</b> и всю его историю публикаций?",
            reply_markup=kb,
        )
    await callback.answer()


@router.callback_query(F.data == "chlist")
async def cb_back_to_channels(callback: CallbackQuery) -> None:
    channels = await list_channels()
    selected = await get_selected_channel()
    sel_id = selected.id if selected else None
    if callback.message is not None:
        await cast(Message, callback.message).edit_text(
            CHANNELS_PROMPT, reply_markup=channels_inline_kb(channels, sel_id)
        )
    await callback.answer()


@router.callback_query(F.data == CH_ADD_CB)
async def cb_add_channel(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.message is not None:
        await _start_add_channel(cast(Message, callback.message), state)
    await callback.answer()


# --- «Добавить канал» conversational flow --------------------------------------
ADD_CHANNEL_PROMPT = (
    "➕ Пришли ссылку на канал одним сообщением: <code>@имя_канала</code>, "
    "<code>https://t.me/имя_канала</code> или числовой id.\n\n"
    "Важно: бот должен быть <b>администратором</b> канала с правом публиковать "
    "сообщения — иначе добавить не получится."
)


async def _start_add_channel(message: Message, state: FSMContext) -> None:
    await state.set_state(AddChannel.waiting_for_link)
    await message.answer(ADD_CHANNEL_PROMPT, reply_markup=cancel_kb("Пришли ссылку на канал…"))


@router.message(F.text == BTN_ADDCHANNEL)
async def btn_addchannel(message: Message, state: FSMContext) -> None:
    await _start_add_channel(message, state)


@router.message(AddChannel.waiting_for_link, F.text == BTN_CANCEL)
async def addchannel_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_kb())


@router.message(AddChannel.waiting_for_link)
async def addchannel_receive(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await _try_add_channel(message, bot, message.text or "")


# --- «Сменить ленту» conversational flow ---------------------------------------
@router.message(F.text == BTN_SETRSS)
async def btn_setrss(message: Message, state: FSMContext) -> None:
    channel = await _require_channel(message)
    if channel is None:
        return
    await state.set_state(SetRss.waiting_for_url)
    await message.answer(
        f"📝 Пришли ссылку на RSS-ленту для <b>{_ch_label(channel)}</b> одним сообщением.\n"
        "Например: <code>https://example.com/feed.xml</code>",
        reply_markup=cancel_kb("Пришли ссылку на ленту…"),
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
