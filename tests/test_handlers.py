"""Tests for the bot handlers (called directly, sans aiogram dispatch)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus

from app.bot import handlers
from app.models import Channel
from app.pipeline import RunResult


def _channel(
    *, id: int = 1, chat_id: str = "@chan", title: str | None = "Chan",
    rss_url: str | None = "https://ex.com/f", tone: str = "news", run_hours: str = "9,13,18",
    enabled: bool = True, require_media: bool = False,
) -> Channel:
    return Channel(
        id=id, chat_id=chat_id, title=title, rss_url=rss_url,
        tone=tone, run_hours=run_hours, enabled=enabled, require_media=require_media,
    )


def _message(text: str | None = None):
    return SimpleNamespace(text=text, answer=AsyncMock())


def _kb_texts(kb) -> set[str]:
    return {b.text for row in kb.keyboard for b in row}


def _select(monkeypatch, channel: Channel | None):
    monkeypatch.setattr(handlers, "get_selected_channel", AsyncMock(return_value=channel))


# --- URL / channel-ref parsing -------------------------------------------------
@pytest.mark.parametrize(
    "url,ok",
    [
        ("https://example.com/feed.xml", True),
        ("http://example.com/rss", True),
        ("ftp://example.com/feed", False),
        ("example.com/feed", False),
        ("https://", False),
        ("", False),
        ("not a url", False),
    ],
)
def test_valid_url(url, ok):
    assert handlers._valid_url(url) is ok


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("@chan", "@chan"),
        ("chan_name", "@chan_name"),
        ("https://t.me/chan", "@chan"),
        ("t.me/chan", "@chan"),
        ("https://t.me/chan/123", "@chan"),
        ("https://t.me/c/123456/78", "-100123456"),
        ("-1001234567890", "-1001234567890"),
        ("https://t.me/+abcDEF", None),
        ("https://t.me/joinchat/xyz", None),
        ("", None),
        ("ab", None),  # too short for a bare username
    ],
)
def test_parse_channel_ref(raw, expected):
    assert handlers._parse_channel_ref(raw) == expected


# --- Add channel flow ----------------------------------------------------------
def _bot_with_member(status, *, can_post=True, title="My Chan", chat_id=-100123):
    bot = AsyncMock()
    bot.get_chat = AsyncMock(return_value=SimpleNamespace(id=chat_id, title=title))
    bot.me = AsyncMock(return_value=SimpleNamespace(id=999))
    bot.get_chat_member = AsyncMock(
        return_value=SimpleNamespace(status=status, can_post_messages=can_post)
    )
    return bot


async def test_try_add_channel_happy(monkeypatch):
    bot = _bot_with_member(ChatMemberStatus.ADMINISTRATOR, can_post=True)
    monkeypatch.setattr(handlers, "get_channel_by_chat", AsyncMock(return_value=None))
    add = AsyncMock(return_value=_channel(id=5, chat_id="-100123", title="My Chan"))
    monkeypatch.setattr(handlers, "add_channel", add)
    sel = AsyncMock()
    monkeypatch.setattr(handlers, "set_selected_channel", sel)
    msg = _message()

    await handlers._try_add_channel(msg, bot, "@MyChan")

    add.assert_awaited_once_with("-100123", title="My Chan")
    sel.assert_awaited_once_with(5)
    assert "добавлен" in msg.answer.await_args.args[0]


async def test_try_add_channel_owner_allowed(monkeypatch):
    bot = _bot_with_member(ChatMemberStatus.CREATOR, can_post=False)
    monkeypatch.setattr(handlers, "get_channel_by_chat", AsyncMock(return_value=None))
    add = AsyncMock(return_value=_channel(id=5, chat_id="-100123"))
    monkeypatch.setattr(handlers, "add_channel", add)
    monkeypatch.setattr(handlers, "set_selected_channel", AsyncMock())
    msg = _message()

    await handlers._try_add_channel(msg, bot, "@MyChan")

    add.assert_awaited_once()  # owner can always post


async def test_try_add_channel_rejects_non_admin(monkeypatch):
    bot = _bot_with_member(ChatMemberStatus.MEMBER)
    add = AsyncMock()
    monkeypatch.setattr(handlers, "add_channel", add)
    msg = _message()

    await handlers._try_add_channel(msg, bot, "@MyChan")

    add.assert_not_called()
    assert "не админ" in msg.answer.await_args.args[0]


async def test_try_add_channel_rejects_admin_without_post_right(monkeypatch):
    bot = _bot_with_member(ChatMemberStatus.ADMINISTRATOR, can_post=False)
    add = AsyncMock()
    monkeypatch.setattr(handlers, "add_channel", add)
    msg = _message()

    await handlers._try_add_channel(msg, bot, "@MyChan")

    add.assert_not_called()


async def test_try_add_channel_bad_ref_skips_resolution(monkeypatch):
    bot = AsyncMock()
    msg = _message()
    await handlers._try_add_channel(msg, bot, "https://t.me/+secret")
    bot.get_chat.assert_not_called()
    assert "ссылк" in msg.answer.await_args.args[0].lower()


async def test_try_add_channel_duplicate(monkeypatch):
    bot = _bot_with_member(ChatMemberStatus.ADMINISTRATOR)
    monkeypatch.setattr(
        handlers, "get_channel_by_chat", AsyncMock(return_value=_channel())
    )
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    add = AsyncMock()
    monkeypatch.setattr(handlers, "add_channel", add)
    msg = _message()

    await handlers._try_add_channel(msg, bot, "@MyChan")

    add.assert_not_called()
    assert "уже добавлен" in msg.answer.await_args.args[0]


async def test_try_add_channel_unresolvable(monkeypatch):
    bot = AsyncMock()
    bot.get_chat = AsyncMock(side_effect=RuntimeError("no such chat"))
    add = AsyncMock()
    monkeypatch.setattr(handlers, "add_channel", add)
    msg = _message()

    await handlers._try_add_channel(msg, bot, "@nope")

    add.assert_not_called()
    assert "Не нашёл" in msg.answer.await_args.args[0]


# --- Channels list / select ----------------------------------------------------
async def test_show_channels_lists(monkeypatch):
    chans = [_channel(id=1, title="A"), _channel(id=2, title="B")]
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=chans))
    _select(monkeypatch, chans[0])
    msg = _message()

    await handlers._show_channels(msg)

    # Screen 1 is a reply keyboard: one button per channel + «add».
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.ReplyKeyboardMarkup)
    assert {"A", "B", handlers.BTN_ADDCHANNEL} <= _kb_texts(kb)


async def test_show_channels_empty(monkeypatch):
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    msg = _message()
    await handlers._show_channels(msg)
    assert "Каналов пока нет" in msg.answer.await_args.args[0]


async def test_tap_channel_opens_settings(monkeypatch):
    """Tapping a channel button (its label as text) selects it and opens Screen 2."""
    ch = _channel(id=3, title="C")
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[ch]))
    sel = AsyncMock()
    monkeypatch.setattr(handlers, "set_selected_channel", sel)
    msg = _message(text="C")

    await handlers.fallback(msg)

    sel.assert_awaited_once_with(3)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.ReplyKeyboardMarkup)
    assert handlers.BTN_BACK in _kb_texts(kb)


# --- Two-screen navigation: back / toggle / media ------------------------------
async def test_btn_back_shows_channels(monkeypatch):
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    _select(monkeypatch, None)
    msg = _message()

    await handlers.btn_back(msg)

    kb = msg.answer.await_args_list[0].kwargs["reply_markup"]
    assert isinstance(kb, handlers.ReplyKeyboardMarkup)
    labels = _kb_texts(kb)
    assert handlers.BTN_ADDCHANNEL in labels and handlers.BTN_BACK not in labels


async def test_btn_toggle_flips_enabled(monkeypatch):
    _select(monkeypatch, _channel(id=1, enabled=True))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message()

    await handlers.btn_toggle(msg)

    upd.assert_awaited_once_with(1, enabled=False)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    # Now disabled → keyboard offers the «enable» label.
    assert handlers.BTN_ENABLE in _kb_texts(kb)


async def test_btn_toggle_media_turns_on(monkeypatch):
    _select(monkeypatch, _channel(id=2, require_media=False))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message()

    await handlers.btn_toggle_media(msg)

    upd.assert_awaited_once_with(2, require_media=True)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    # Filter is now on → keyboard offers the «allow text» label to turn it off.
    assert handlers.BTN_MEDIA_ANY in _kb_texts(kb)


# --- Delete (reply-keyboard confirmation) --------------------------------------
async def test_btn_delete_asks_confirmation(monkeypatch):
    _select(monkeypatch, _channel(id=7, title="G"))
    msg = _message()

    await handlers.btn_delete(msg)

    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.ReplyKeyboardMarkup)
    assert handlers.BTN_DELYES in _kb_texts(kb)


async def test_btn_delete_yes_deletes(monkeypatch):
    _select(monkeypatch, _channel(id=4, title="D"))
    dele = AsyncMock()
    monkeypatch.setattr(handlers, "delete_channel", dele)
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    msg = _message()

    await handlers.btn_delete_yes(msg)

    dele.assert_awaited_once_with(4)
    # Back to the home keyboard after deletion.
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert handlers.BTN_ADDCHANNEL in _kb_texts(kb)


async def test_btn_delete_no_cancels(monkeypatch):
    _select(monkeypatch, _channel(id=4, title="D"))
    msg = _message()

    await handlers.btn_delete_no(msg)

    assert "не удалён" in msg.answer.await_args.args[0]
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert handlers.BTN_BACK in _kb_texts(kb)


# --- Add-channel FSM -----------------------------------------------------------
async def test_btn_addchannel_sets_state():
    msg = _message()
    state = SimpleNamespace(set_state=AsyncMock())
    await handlers.btn_addchannel(msg, state)
    state.set_state.assert_awaited_once_with(handlers.AddChannel.waiting_for_link)


async def test_addchannel_receive_calls_try(monkeypatch):
    seen = {}

    async def fake_try(message, bot, raw):
        seen["raw"] = raw

    monkeypatch.setattr(handlers, "_try_add_channel", fake_try)
    msg = SimpleNamespace(answer=AsyncMock(), text="@chan")
    state = SimpleNamespace(clear=AsyncMock())

    await handlers.addchannel_receive(msg, state, AsyncMock())

    state.clear.assert_awaited_once()
    assert seen["raw"] == "@chan"


async def test_addchannel_cancel_clears_state(monkeypatch):
    _select(monkeypatch, None)
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    msg = _message()
    state = SimpleNamespace(clear=AsyncMock())
    await handlers.addchannel_cancel(msg, state)
    state.clear.assert_awaited_once()
    assert "Отменено" in msg.answer.await_args.args[0]


# --- RSS -----------------------------------------------------------------------
async def test_show_rss_shows_url(monkeypatch):
    _select(monkeypatch, _channel(rss_url="https://ex.com/f"))
    msg = _message()
    await handlers._show_rss(msg)
    assert "https://ex.com/f" in msg.answer.await_args.args[0]


async def test_show_rss_when_unset(monkeypatch):
    _select(monkeypatch, _channel(rss_url=None))
    msg = _message()
    await handlers._show_rss(msg)
    assert "не задана" in msg.answer.await_args.args[0]


async def test_show_rss_without_channel_nudges(monkeypatch):
    _select(monkeypatch, None)
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    msg = _message()
    await handlers._show_rss(msg)
    assert "добавь канал" in msg.answer.await_args.args[0].lower()


# --- Hours (reply-keyboard toggle grid) ----------------------------------------
def test_hours_reply_kb_marks_selected():
    kb = handlers.hours_reply_kb({9, 13})
    texts = _kb_texts(kb)
    assert "✅ 09" in texts and "✅ 13" in texts
    assert "▫️ 10" in texts
    assert handlers.BTN_DONE in texts


async def test_btn_sethours_opens_grid(monkeypatch):
    _select(monkeypatch, _channel())
    msg = _message()
    await handlers.btn_sethours(msg)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.ReplyKeyboardMarkup)
    assert handlers.BTN_DONE in _kb_texts(kb)


async def test_btn_toggle_hour_enables(monkeypatch):
    _select(monkeypatch, _channel(id=1, run_hours="9,13"))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message(text="▫️ 18")

    await handlers.btn_toggle_hour(msg)

    upd.assert_awaited_once_with(1, run_hours="9,13,18")
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.ReplyKeyboardMarkup)


async def test_btn_toggle_hour_disables(monkeypatch):
    _select(monkeypatch, _channel(id=1, run_hours="9,13"))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message(text="✅ 09")

    await handlers.btn_toggle_hour(msg)

    upd.assert_awaited_once_with(1, run_hours="13")


async def test_btn_toggle_hour_keeps_last_one(monkeypatch):
    _select(monkeypatch, _channel(id=1, run_hours="9"))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message(text="✅ 09")

    await handlers.btn_toggle_hour(msg)

    upd.assert_not_called()
    assert "хотя бы один" in msg.answer.await_args.args[0]


async def test_btn_toggle_hour_without_channel(monkeypatch):
    _select(monkeypatch, None)
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message(text="✅ 09")

    await handlers.btn_toggle_hour(msg)

    upd.assert_not_called()


async def test_btn_editor_done_returns_to_channel(monkeypatch):
    _select(monkeypatch, _channel(id=1, title="C"))
    msg = _message()
    await handlers.btn_editor_done(msg)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.ReplyKeyboardMarkup)
    assert handlers.BTN_BACK in _kb_texts(kb)


# --- Tone (reply-keyboard picker) ----------------------------------------------
def test_tone_reply_kb_marks_current():
    kb = handlers.tone_reply_kb("expert")
    texts = _kb_texts(kb)
    assert any(t.startswith("✅") and "Экспертный" in t for t in texts)
    assert handlers.BTN_DONE in texts


def test_tone_btn_texts_map_both_variants():
    # Both the plain and the active (✅) caption resolve back to the preset key.
    assert handlers.TONE_BTN_TEXTS[handlers._tone_btn_text("hype", False)] == "hype"
    assert handlers.TONE_BTN_TEXTS[handlers._tone_btn_text("hype", True)] == "hype"


async def test_btn_tone_opens_grid(monkeypatch):
    _select(monkeypatch, _channel(tone="news"))
    msg = _message()
    await handlers.btn_tone(msg)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.ReplyKeyboardMarkup)
    assert handlers.BTN_DONE in _kb_texts(kb)


async def test_btn_select_tone_persists(monkeypatch):
    _select(monkeypatch, _channel(id=1))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message(text=handlers._tone_btn_text("hype", False))

    await handlers.btn_select_tone(msg)

    upd.assert_awaited_once_with(1, tone="hype")
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.ReplyKeyboardMarkup)


# --- Status / startup ----------------------------------------------------------
async def test_status_body_lists_channels(monkeypatch):
    chans = [
        _channel(id=1, title="A", rss_url="https://a", tone="news", run_hours="9"),
        _channel(id=2, title="B", rss_url=None, tone="expert", run_hours="10,20"),
    ]
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=chans))
    _select(monkeypatch, chans[0])

    text = await handlers._status_body()

    assert "A" in text and "B" in text
    assert "https://a" in text
    assert "Новостной" in text and "Экспертный" in text
    assert "▶️" in text  # active marker


async def test_status_body_empty(monkeypatch):
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    text = await handlers._status_body()
    assert "Каналов пока нет" in text


async def test_startup_notice_includes_channels(monkeypatch):
    monkeypatch.setattr(
        handlers, "list_channels", AsyncMock(return_value=[_channel(rss_url="https://ex.com/f")])
    )
    _select(monkeypatch, _channel(rss_url="https://ex.com/f"))

    text = await handlers.startup_notice()

    assert "перезапущен" in text
    assert "https://ex.com/f" in text


async def test_btn_status_shows_status(monkeypatch):
    monkeypatch.setattr(
        handlers, "list_channels", AsyncMock(return_value=[_channel(rss_url="https://ex.com/f")])
    )
    _select(monkeypatch, _channel(rss_url="https://ex.com/f"))
    msg = _message()
    await handlers.btn_status(msg)
    out = msg.answer.await_args.args[0]
    assert "https://ex.com/f" in out


# --- Run / preview -------------------------------------------------------------
async def test_btn_run_reports_posted(monkeypatch):
    _select(monkeypatch, _channel())
    monkeypatch.setattr(
        handlers, "run_once", AsyncMock(return_value=RunResult("posted", "Заголовок"))
    )
    msg = _message()

    await handlers.btn_run(msg, AsyncMock())

    assert msg.answer.await_count == 2  # "starting" then result
    assert "Заголовок" in msg.answer.await_args_list[-1].args[0]


async def test_btn_run_without_channel(monkeypatch):
    _select(monkeypatch, None)
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    run = AsyncMock()
    monkeypatch.setattr(handlers, "run_once", run)
    msg = _message()
    await handlers.btn_run(msg, AsyncMock())
    run.assert_not_called()
    assert "добавь канал" in msg.answer.await_args.args[0].lower()


async def test_btn_run_reports_error(monkeypatch):
    _select(monkeypatch, _channel())
    monkeypatch.setattr(handlers, "run_once", AsyncMock(return_value=RunResult("error", "boom")))
    msg = _message()
    await handlers.btn_run(msg, AsyncMock())
    assert "boom" in msg.answer.await_args_list[-1].args[0]


async def test_btn_preview_runs_dry(monkeypatch):
    ch = _channel()
    _select(monkeypatch, ch)
    run = AsyncMock(return_value=RunResult("posted", "Заголовок"))
    monkeypatch.setattr(handlers, "run_once", run)
    monkeypatch.setattr(handlers.settings, "admin_id", 777)
    msg = _message()
    bot = AsyncMock()

    await handlers.btn_preview(msg, bot)

    run.assert_awaited_once_with(bot, ch, chat_id=777, persist=False)
    assert msg.answer.await_count == 2  # "starting" + summary


async def test_cmd_start_answers(monkeypatch):
    _select(monkeypatch, None)
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    msg = _message()
    await handlers.cmd_start(msg)
    msg.answer.assert_awaited_once()


# --- Set-RSS FSM ---------------------------------------------------------------
async def test_btn_setrss_requires_channel(monkeypatch):
    _select(monkeypatch, None)
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    msg = _message()
    state = SimpleNamespace(set_state=AsyncMock())
    await handlers.btn_setrss(msg, state)
    state.set_state.assert_not_called()


async def test_btn_setrss_enters_waiting_state(monkeypatch):
    _select(monkeypatch, _channel())
    msg = _message()
    state = SimpleNamespace(set_state=AsyncMock())
    await handlers.btn_setrss(msg, state)
    state.set_state.assert_awaited_once_with(handlers.SetRss.waiting_for_url)
    msg.answer.assert_awaited_once()


async def test_setrss_receive_saves_valid_url(monkeypatch):
    _select(monkeypatch, _channel(id=1))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = SimpleNamespace(answer=AsyncMock(), text="  https://ex.com/feed  ")
    state = SimpleNamespace(clear=AsyncMock())

    await handlers.setrss_receive(msg, state)

    state.clear.assert_awaited_once()
    upd.assert_awaited_once_with(1, rss_url="https://ex.com/feed")


async def test_setrss_receive_rejects_bad_url(monkeypatch):
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = SimpleNamespace(answer=AsyncMock(), text="garbage")
    state = SimpleNamespace(clear=AsyncMock())

    await handlers.setrss_receive(msg, state)

    upd.assert_not_called()
    state.clear.assert_not_called()  # stay waiting for a retry


async def test_setrss_cancel_clears_state(monkeypatch):
    _select(monkeypatch, None)
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    msg = _message()
    state = SimpleNamespace(clear=AsyncMock())
    await handlers.setrss_cancel(msg, state)
    state.clear.assert_awaited_once()
    assert "Отменено" in msg.answer.await_args.args[0]


async def test_fallback_answers(monkeypatch):
    _select(monkeypatch, None)
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    msg = _message(text="что-то непонятное")
    await handlers.fallback(msg)
    msg.answer.assert_awaited_once()
