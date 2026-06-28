"""Chat control: manage channels and, for the selected one, set the feed/tone/
schedule, trigger a run, check status.

The bot runs several channels; the admin picks an «active» channel by tapping it
on the channels screen and the everyday buttons — 📡 Лента, 🎨 Тон, 🕒 Часы,
🚀 Запустить — all act on it. A channel is added by sending its link/@username:
the bot resolves the chat and checks it is an admin there with the right to post.

The bot is driven entirely by a persistent reply keyboard — no slash commands
(only the built-in /start bootstraps the menu) and no inline buttons. The
«active channel» lives in the DB and the only conversational state (waiting for
a pasted link/url) is persisted via the DB-backed FSM storage, so a restart
never drops context.
"""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
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
    get_channel_by_chat,
    get_selected_channel,
    list_channels,
    set_selected_channel,
    update_channel,
)
from app.tone import TONES, get_preset

log = logging.getLogger("handlers")

router = Router()
# Only the admin may control the bot.
router.message.filter(F.from_user.id == settings.admin_id)


# --- Keyboard button labels (also matched as incoming text) --------------------
BTN_RUN = "🚀 Запустить"
BTN_PREVIEW = "👁 Предпросмотр"
BTN_ADDCHANNEL = "➕ Добавить канал"
BTN_RSS = "📡 Лента"
BTN_SETRSS = "📝 Сменить ленту"
BTN_TONE = "🎨 Тон"
BTN_SETHOURS = "🕒 Часы"
BTN_STATUS = "📊 Статус"
BTN_HELP = "❓ Помощь"
BTN_CANCEL = "❌ Отмена"
BTN_BACK = "⬅️ К каналам"
BTN_ENABLE = "▶️ Включить"
BTN_DISABLE = "⏸ Выключить"
BTN_DELETE = "🗑 Удалить"
BTN_MEDIA_REQUIRE = "🖼 Только с медиа"   # turn the require-media filter on
BTN_MEDIA_ANY = "📝 Разрешить текст"      # turn it off (text-only allowed again)
BTN_DONE = "✅ Готово"                     # close the hours/tone editor
BTN_DELYES = "🗑 Да, удалить"
BTN_DELNO = "↩️ Отмена"

# An hour toggle button looks like «✅ 09» / «▫️ 18»; this matches a tap on one.
# Alternation (not a char class) because ▫️ is two code points (▫ + VS16).
HOUR_BTN_RE = re.compile(r"^(?:✅|▫️) (\d{2})$")


async def home_kb() -> ReplyKeyboardMarkup:
    """Screen 1 keyboard: one reply button per channel, then «add» + global
    actions. Tapping a channel opens its settings (Screen 2, см. channel_kb).

    Channel buttons carry the channel's label as text; the catch-all handler
    matches that text back to the channel (см. fallback)."""
    channels = await list_channels()
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text=_ch_label(c))] for c in channels
    ]
    rows.append([KeyboardButton(text=BTN_ADDCHANNEL)])
    rows.append([KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_HELP)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выбери канал или добавь новый…",
    )


def channel_kb(channel: Channel) -> ReplyKeyboardMarkup:
    """Per-channel keyboard: everyday actions on the active channel plus its
    on/off toggle, media filter, delete and a «back to channels» row."""
    toggle = BTN_DISABLE if channel.enabled else BTN_ENABLE
    media = BTN_MEDIA_ANY if channel.require_media else BTN_MEDIA_REQUIRE
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_RUN), KeyboardButton(text=BTN_PREVIEW)],
            [KeyboardButton(text=BTN_RSS), KeyboardButton(text=BTN_SETRSS)],
            [KeyboardButton(text=BTN_TONE), KeyboardButton(text=BTN_SETHOURS)],
            [KeyboardButton(text=media)],
            [KeyboardButton(text=toggle), KeyboardButton(text=BTN_DELETE)],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=f"Настройка канала: {_ch_label(channel)}",
    )


async def _active_kb() -> ReplyKeyboardMarkup:
    """Keyboard matching the current screen: channel actions if one is active,
    otherwise the top-level keyboard."""
    channel = await get_selected_channel()
    return channel_kb(channel) if channel else await home_kb()


def cancel_kb(placeholder: str) -> ReplyKeyboardMarkup:
    """One-button keyboard shown while waiting for a url/link."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
        input_field_placeholder=placeholder,
    )


def _hour_btn_text(hour: int, on: bool) -> str:
    return f"{'✅' if on else '▫️'} {hour:02d}"


def hours_reply_kb(selected: set[int]) -> ReplyKeyboardMarkup:
    """24-hour toggle grid as a reply keyboard: tap an hour to switch publishing
    on/off for it. Enabled hours are marked ✅, disabled ▫️. The «Готово» row
    closes the editor. Four columns keep all 24 buttons readable on a phone."""
    rows: list[list[KeyboardButton]] = []
    row: list[KeyboardButton] = []
    for h in range(24):
        row.append(KeyboardButton(text=_hour_btn_text(h, h in selected)))
        if len(row) == 4:
            rows.append(row)
            row = []
    rows.append([KeyboardButton(text=BTN_DONE)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Тапай по часам — ✅ публикую, ▫️ нет",
    )


def _tone_btn_text(key: str, active: bool) -> str:
    return f"{'✅ ' if active else ''}{TONES[key].label}"


# Every possible tone-button caption (with and without the active ✅) → its key,
# so a tap on a tone button can be matched and resolved back to the preset.
TONE_BTN_TEXTS: dict[str, str] = {}
for _key in TONES:
    TONE_BTN_TEXTS[_tone_btn_text(_key, False)] = _key
    TONE_BTN_TEXTS[_tone_btn_text(_key, True)] = _key


def tone_reply_kb(current: str) -> ReplyKeyboardMarkup:
    """Radio-style tone picker as a reply keyboard: one button per preset, the
    active one marked ✅. «Готово» closes the editor."""
    rows = [[KeyboardButton(text=_tone_btn_text(key, key == current))] for key in TONES]
    rows.append([KeyboardButton(text=BTN_DONE)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выбери тон — применяется сразу",
    )


def delete_confirm_kb() -> ReplyKeyboardMarkup:
    """Two-button reply keyboard confirming channel deletion."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_DELYES), KeyboardButton(text=BTN_DELNO)]],
        resize_keyboard=True,
        input_field_placeholder="Подтверди удаление",
    )


class SetRss(StatesGroup):
    """Conversational flow for the «📝 Сменить ленту» button."""

    waiting_for_url = State()


class AddChannel(StatesGroup):
    """Conversational flow for adding a channel by its link/@username."""

    waiting_for_link = State()


HELP = (
    "Я веду несколько Telegram-каналов на автопилоте: периодически читаю их RSS, "
    "через DeepSeek выбираю важную новость и публикую пост.\n\n"
    "Всё управление — кнопками на клавиатуре, команды и кнопки под сообщениями не "
    "нужны.\n\n"
    "Навигация в два шага. Сначала экран каналов: тапни по названию, чтобы открыть "
    "канал, или «➕ Добавить канал» (пришли ссылку — бот должен быть админом канала "
    "с правом публикации). Затем экран настройки канала:\n"
    "• 📡 Лента / 📝 Сменить ленту — RSS-источник\n"
    "• 🎨 Тон — стиль постов (кнопки)\n"
    "• 🕒 Часы — сетка часов публикации (тапай, чтобы вкл/выкл)\n"
    "• 🖼 Только с медиа — публиковать лишь новости с картинкой или видео "
    "(несколько картинок — постит все); 📝 Разрешить текст возвращает обратно\n"
    "• 🚀 Запустить — разобрать ленту и опубликовать сейчас\n"
    "• 👁 Предпросмотр — пробный прогон сюда, без публикации и записи в базу\n"
    "• ⏸/▶️ — включить/выключить автопостинг канала\n"
    "• 🗑 Удалить — удалить канал\n"
    "• ⬅️ К каналам — назад к выбору\n\n"
    "📊 Статус — все каналы и их настройки."
)


def _valid_url(url: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _ch_label(channel: Channel) -> str:
    return channel.title or channel.chat_id


def _fmt_hours(hours: list[int]) -> str:
    return ", ".join(f"{h:02d}:00" for h in hours)


def _channel_header(channel: Channel) -> str:
    """Screen 2 header: the active channel and its current settings at a glance."""
    tone = get_preset(channel.tone)
    state = "▶️ включён" if channel.enabled else "⏸ выключен"
    media = "🖼 только с медиа" if channel.require_media else "📝 любые посты"
    return (
        f"⚙️ Настройка канала: <b>{_ch_label(channel)}</b>\n"
        f"📡 {channel.rss_url or '— лента не задана'}\n"
        f"🕒 {_fmt_hours(channel.hours_list)} ({settings.timezone}) · "
        f"🎨 {tone.label} · {state} · {media}"
    )


async def _require_channel(message: Message) -> Channel | None:
    """The selected channel, or a friendly nudge to add one first."""
    channel = await get_selected_channel()
    if channel is None:
        await message.answer(
            "Сначала добавь канал кнопкой «➕ Добавить канал».",
            reply_markup=await home_kb(),
        )
    return channel


# --- Channels: list / select / delete / add ------------------------------------
CHANNELS_PROMPT = (
    "📺 Каналы — выбери канал на клавиатуре ниже, чтобы открыть его настройки, "
    "или «➕ Добавить канал»."
)


async def _show_channels(message: Message) -> None:
    """Screen 1: the reply keyboard lists every channel as a button (tap to open
    its settings) plus «➕ Добавить канал»."""
    channels = await list_channels()
    await message.answer(
        CHANNELS_PROMPT if channels else "📺 Каналов пока нет. Добавь первый:",
        reply_markup=await home_kb(),
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
        await message.answer("ℹ️ Этот канал уже добавлен.", reply_markup=await home_kb())
        return

    channel = await add_channel(chat_id, title=chat.title)
    await set_selected_channel(channel.id)
    log.info("Admin added channel %s (%s)", chat_id, chat.title)
    await message.answer(
        "✅ Канал добавлен и выбран активным.\n"
        "Задай ему ленту (📝 «Сменить ленту»), часы и тон.\n\n"
        + _channel_header(channel),
        reply_markup=channel_kb(channel),
    )


# --- RSS (selected channel) ----------------------------------------------------
async def _save_rss(message: Message, url: str) -> None:
    channel = await _require_channel(message)
    if channel is None:
        return
    await update_channel(channel.id, rss_url=url)
    log.info("Admin set RSS url for channel %s: %s", channel.id, url)
    channel.rss_url = url
    await message.answer(
        f"✅ RSS-лента канала <b>{_ch_label(channel)}</b> сохранена:\n{url}",
        reply_markup=channel_kb(channel),
    )


async def _show_rss(message: Message) -> None:
    channel = await _require_channel(message)
    if channel is None:
        return
    await message.answer(
        f"📡 Лента канала <b>{_ch_label(channel)}</b>:\n{channel.rss_url}"
        if channel.rss_url
        else f"📡 У канала <b>{_ch_label(channel)}</b> лента не задана. "
        "Нажми «📝 Сменить ленту».",
        reply_markup=channel_kb(channel),
    )


# --- Hours (selected channel) --------------------------------------------------
HOURS_GRID_PROMPT = (
    "🕒 Часы публикации — тапай по часам, чтобы включать/выключать.\n"
    "✅ — публикую, ▫️ — нет. Изменения применяются сразу; «✅ Готово» — назад."
)


async def _show_hours_grid(message: Message) -> None:
    """Open the interactive 0–23 toggle grid seeded with the channel's hours."""
    channel = await _require_channel(message)
    if channel is None:
        return
    await message.answer(
        f"{HOURS_GRID_PROMPT}\nКанал: <b>{_ch_label(channel)}</b>",
        reply_markup=hours_reply_kb(set(channel.hours_list)),
    )


# --- Tone (selected channel) ---------------------------------------------------
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
        reply_markup=tone_reply_kb(channel.tone),
    )


# --- Status --------------------------------------------------------------------
async def _status_body() -> str:
    """A summary of every channel and its settings, marking the active one."""
    channels = await list_channels()
    if not channels:
        return "Каналов пока нет. Добавь первый: «➕ Добавить канал»."
    selected = await get_selected_channel()
    sel_id = selected.id if selected else None
    blocks = []
    for c in channels:
        head = "▶️" if c.id == sel_id else "•"
        tone = get_preset(c.tone)
        off = "" if c.enabled else "  ⏸ выключен"
        media = "  🖼 только с медиа" if c.require_media else ""
        blocks.append(
            f"{head} <b>{_ch_label(c)}</b>{off}{media}\n"
            f"   📡 {c.rss_url or '— не задана'}\n"
            f"   🕒 {_fmt_hours(c.hours_list)} ({settings.timezone})\n"
            f"   🎨 {tone.label}"
        )
    return f"🤖 Модель: {settings.deepseek_model}\n\n" + "\n\n".join(blocks)


async def _show_status(message: Message) -> None:
    await message.answer(
        "📊 <b>Статус</b>\n" + await _status_body(), reply_markup=await _active_kb()
    )


async def startup_notice() -> str:
    """Message sent to the admin every time the service (re)starts: a heads-up
    plus the current settings, so a silent crash-restart is never a surprise."""
    return "♻️ <b>Сервис перезапущен</b>\n" + await _status_body()


# --- Run / preview (selected channel) ------------------------------------------
async def _do_run(message: Message, bot: Bot) -> None:
    channel = await _require_channel(message)
    if channel is None:
        return
    log.info("Manual run for channel %s triggered by admin", channel.id)
    await message.answer(f"⏳ Запускаю разбор ленты канала <b>{_ch_label(channel)}</b>…")
    result = await run_once(bot, channel)
    replies = {
        "posted": f"✅ Опубликовано в <b>{_ch_label(channel)}</b>: {result.detail}",
        "no_feed": "⚠️ RSS-лента не задана. Нажми «📝 Сменить ленту».",
        "no_new": "ℹ️ Новых новостей нет.",
        "error": f"❌ Ошибка: {result.detail}",
    }
    await message.answer(
        replies.get(result.status, str(result)), reply_markup=channel_kb(channel)
    )


async def _do_preview(message: Message, bot: Bot) -> None:
    """Dry-run: full pipeline, but the post lands in this chat and nothing is
    written to the DB (entries are not marked seen — the run is repeatable)."""
    channel = await _require_channel(message)
    if channel is None:
        return
    log.info("Manual preview for channel %s triggered by admin", channel.id)
    await message.answer(
        f"⏳ Пробный прогон <b>{_ch_label(channel)}</b>: соберу пост сюда, "
        "без публикации и записи в базу…"
    )
    result = await run_once(bot, channel, chat_id=settings.admin_id, persist=False)
    replies = {
        "posted": "☝️ Так выглядел бы пост. В канал не отправлено, база не тронута.",
        "no_feed": "⚠️ RSS-лента не задана. Нажми «📝 Сменить ленту».",
        "no_new": "ℹ️ Новых новостей нет.",
        "error": f"❌ Ошибка: {result.detail}",
    }
    await message.answer(
        replies.get(result.status, str(result)), reply_markup=channel_kb(channel)
    )


# --- /start bootstrap ----------------------------------------------------------
@router.message(Command("start", "menu"))
async def cmd_start(message: Message) -> None:
    """The only command: Telegram's built-in «Start» button bootstraps the menu."""
    await message.answer(HELP, reply_markup=await _active_kb())


# --- Keyboard buttons ----------------------------------------------------------
@router.message(F.text == BTN_RUN)
async def btn_run(message: Message, bot: Bot) -> None:
    await _do_run(message, bot)


@router.message(F.text == BTN_PREVIEW)
async def btn_preview(message: Message, bot: Bot) -> None:
    await _do_preview(message, bot)


@router.message(F.text == BTN_BACK)
async def btn_back(message: Message) -> None:
    """«⬅️ К каналам» — back to Screen 1 (the channel list)."""
    await _show_channels(message)


@router.message(F.text.in_({BTN_ENABLE, BTN_DISABLE}))
async def btn_toggle(message: Message) -> None:
    """Flip the active channel's enabled flag and refresh Screen 2."""
    channel = await _require_channel(message)
    if channel is None:
        return
    new_state = not channel.enabled
    await update_channel(channel.id, enabled=new_state)
    channel.enabled = new_state
    log.info("Admin set enabled=%s for channel %s", new_state, channel.id)
    word = "включён ▶️" if new_state else "выключен ⏸"
    await message.answer(
        f"Канал <b>{_ch_label(channel)}</b> {word}.", reply_markup=channel_kb(channel)
    )


@router.message(F.text.in_({BTN_MEDIA_REQUIRE, BTN_MEDIA_ANY}))
async def btn_toggle_media(message: Message) -> None:
    """Flip the active channel's require_media filter and refresh Screen 2."""
    channel = await _require_channel(message)
    if channel is None:
        return
    new_state = not channel.require_media
    await update_channel(channel.id, require_media=new_state)
    channel.require_media = new_state
    log.info("Admin set require_media=%s for channel %s", new_state, channel.id)
    word = (
        "только посты с картинкой или видео 🖼"
        if new_state
        else "любые посты, включая текстовые 📝"
    )
    await message.answer(
        f"Канал <b>{_ch_label(channel)}</b>: теперь публикуются {word}.",
        reply_markup=channel_kb(channel),
    )


@router.message(F.text == BTN_DELETE)
async def btn_delete(message: Message) -> None:
    """Ask to delete the active channel (confirmation via a reply keyboard)."""
    channel = await _require_channel(message)
    if channel is None:
        return
    await message.answer(
        f"Удалить канал <b>{_ch_label(channel)}</b> и всю его историю публикаций?",
        reply_markup=delete_confirm_kb(),
    )


@router.message(F.text == BTN_DELYES)
async def btn_delete_yes(message: Message) -> None:
    """Confirm deletion: drop the active channel and return to the channels screen."""
    channel = await _require_channel(message)
    if channel is None:
        return
    label = _ch_label(channel)
    await delete_channel(channel.id)
    log.info("Admin deleted channel %s", channel.id)
    await message.answer(f"🗑 Канал <b>{label}</b> удалён.", reply_markup=await home_kb())


@router.message(F.text == BTN_DELNO)
async def btn_delete_no(message: Message) -> None:
    """Cancel deletion — back to the channel screen."""
    channel = await get_selected_channel()
    if channel is None:
        await _show_channels(message)
        return
    await message.answer("Отменено — канал не удалён.", reply_markup=channel_kb(channel))


@router.message(F.text == BTN_RSS)
async def btn_rss(message: Message) -> None:
    await _show_rss(message)


@router.message(F.text == BTN_STATUS)
async def btn_status(message: Message) -> None:
    await _show_status(message)


@router.message(F.text == BTN_HELP)
async def btn_help(message: Message) -> None:
    await message.answer(HELP, reply_markup=await _active_kb())


@router.message(F.text == BTN_SETHOURS)
async def btn_sethours(message: Message) -> None:
    await _show_hours_grid(message)


@router.message(F.text == BTN_TONE)
async def btn_tone(message: Message) -> None:
    await _show_tone_grid(message)


# --- «Часы» toggle grid (reply keyboard) ---------------------------------------
@router.message(F.text.regexp(HOUR_BTN_RE))
async def btn_toggle_hour(message: Message) -> None:
    """Toggle one hour on/off for the selected channel and refresh the grid."""
    channel = await _require_channel(message)
    if channel is None:
        return
    m = HOUR_BTN_RE.match(message.text or "")
    if m is None:  # guaranteed by the filter; narrows for type-checkers
        return
    hour = int(m.group(1))
    selected = set(channel.hours_list)
    if hour in selected:
        if len(selected) == 1:
            # Never leave an empty schedule — a channel with no hours never runs.
            await message.answer(
                "Должен остаться хотя бы один час.",
                reply_markup=hours_reply_kb(selected),
            )
            return
        selected.discard(hour)
    else:
        selected.add(hour)
    await update_channel(channel.id, run_hours=",".join(str(h) for h in sorted(selected)))
    await message.answer(
        f"🕒 {_fmt_hours(sorted(selected))} ({settings.timezone})",
        reply_markup=hours_reply_kb(selected),
    )


# --- «Тон» picker (reply keyboard) ---------------------------------------------
@router.message(F.text.in_(set(TONE_BTN_TEXTS)))
async def btn_select_tone(message: Message) -> None:
    """Select a tone preset for the selected channel and refresh the picker."""
    channel = await _require_channel(message)
    if channel is None:
        return
    key = TONE_BTN_TEXTS[message.text or ""]
    await update_channel(channel.id, tone=key)
    channel.tone = key
    log.info("Admin set tone=%s for channel %s", key, channel.id)
    await message.answer(
        f"🎨 Тон: {TONES[key].label}\n{TONES[key].description}",
        reply_markup=tone_reply_kb(key),
    )


@router.message(F.text == BTN_DONE)
async def btn_editor_done(message: Message) -> None:
    """«✅ Готово» closes the hours/tone editor and returns to the channel screen."""
    channel = await get_selected_channel()
    if channel is None:
        await _show_channels(message)
        return
    await message.answer(_channel_header(channel), reply_markup=channel_kb(channel))


# --- «Добавить канал» conversational flow --------------------------------------
ADD_CHANNEL_PROMPT = (
    "➕ Пришли ссылку на канал одним сообщением: <code>@имя_канала</code>, "
    "<code>https://t.me/имя_канала</code> или числовой id.\n\n"
    "Важно: бот должен быть <b>администратором</b> канала с правом публиковать "
    "сообщения — иначе добавить не получится."
)


@router.message(F.text == BTN_ADDCHANNEL)
async def btn_addchannel(message: Message, state: FSMContext) -> None:
    await state.set_state(AddChannel.waiting_for_link)
    await message.answer(ADD_CHANNEL_PROMPT, reply_markup=cancel_kb("Пришли ссылку на канал…"))


@router.message(AddChannel.waiting_for_link, F.text == BTN_CANCEL)
async def addchannel_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=await _active_kb())


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
    await message.answer("Отменено.", reply_markup=await _active_kb())


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


# --- Channel pick / fallback ---------------------------------------------------
@router.message()
async def fallback(message: Message) -> None:
    """Catch-all: a tap on a channel button (its label as text) opens Screen 2;
    anything else is an unknown input.

    Channel buttons live on the home keyboard and carry the channel label as
    plain text, so they have no dedicated handler — we match the text against the
    channel list here, after every reserved button has had its turn."""
    text = (message.text or "").strip()
    if text:
        channel = next(
            (c for c in await list_channels() if _ch_label(c) == text), None
        )
        if channel is not None:
            await set_selected_channel(channel.id)
            log.info("Admin opened channel %s", channel.id)
            await message.answer(
                _channel_header(channel), reply_markup=channel_kb(channel)
            )
            return
    await message.answer(
        "Не понял 🤔 Выбери действие на клавиатуре ниже.",
        reply_markup=await _active_kb(),
    )
