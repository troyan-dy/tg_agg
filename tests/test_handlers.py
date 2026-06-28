"""Tests for the bot command handlers (called directly, sans aiogram dispatch)."""
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
    enabled: bool = True,
) -> Channel:
    return Channel(
        id=id, chat_id=chat_id, title=title, rss_url=rss_url,
        tone=tone, run_hours=run_hours, enabled=enabled,
    )


def _message():
    return SimpleNamespace(answer=AsyncMock())


def _callback(data: str):
    message = SimpleNamespace(
        edit_reply_markup=AsyncMock(), edit_text=AsyncMock(), answer=AsyncMock()
    )
    return SimpleNamespace(data=data, answer=AsyncMock(), message=message)


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


# --- Channels list / select / delete -------------------------------------------
async def test_show_channels_lists(monkeypatch):
    chans = [_channel(id=1, title="A"), _channel(id=2, title="B")]
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=chans))
    _select(monkeypatch, chans[0])
    msg = _message()

    await handlers._show_channels(msg)

    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.InlineKeyboardMarkup)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any("✅" in t and "A" in t for t in texts)


async def test_show_channels_empty(monkeypatch):
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    msg = _message()
    await handlers._show_channels(msg)
    assert "Каналов пока нет" in msg.answer.await_args.args[0]


async def test_cb_select_channel(monkeypatch):
    ch = _channel(id=3, title="C")
    monkeypatch.setattr(handlers, "get_channel", AsyncMock(return_value=ch))
    sel = AsyncMock()
    monkeypatch.setattr(handlers, "set_selected_channel", sel)
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[ch]))
    _select(monkeypatch, ch)
    cb = _callback("chsel:3")

    await handlers.cb_select_channel(cb)

    sel.assert_awaited_once_with(3)
    cb.message.edit_reply_markup.assert_awaited_once()
    # Screen 2 opens: a header message with the per-channel keyboard.
    cb.message.answer.assert_awaited_once()
    kb = cb.message.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.ReplyKeyboardMarkup)
    assert handlers.BTN_BACK in {b.text for row in kb.keyboard for b in row}


async def test_cb_delete_ask_confirms(monkeypatch):
    ch = _channel(id=4, title="D")
    monkeypatch.setattr(handlers, "get_channel", AsyncMock(return_value=ch))
    cb = _callback("chdel:4")

    await handlers.cb_delete_channel_ask(cb)

    cb.message.edit_text.assert_awaited_once()
    assert "Удалить" in cb.message.edit_text.await_args.args[0]


async def test_cb_delete_yes_deletes(monkeypatch):
    ch = _channel(id=4, title="D")
    monkeypatch.setattr(handlers, "get_channel", AsyncMock(return_value=ch))
    dele = AsyncMock()
    monkeypatch.setattr(handlers, "delete_channel", dele)
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    _select(monkeypatch, None)
    cb = _callback("chdelyes:4")

    await handlers.cb_delete_channel_yes(cb)

    dele.assert_awaited_once_with(4)
    # Keyboard is resynced after deletion (no channels left → home keyboard).
    cb.message.answer.assert_awaited_once()


# --- Two-screen navigation: back / toggle / delete -----------------------------
async def test_btn_back_shows_channels(monkeypatch):
    monkeypatch.setattr(handlers, "list_channels", AsyncMock(return_value=[]))
    _select(monkeypatch, None)
    msg = _message()

    await handlers.btn_back(msg)

    # First message carries the top-level (home) keyboard.
    kb = msg.answer.await_args_list[0].kwargs["reply_markup"]
    assert isinstance(kb, handlers.ReplyKeyboardMarkup)
    labels = {b.text for row in kb.keyboard for b in row}
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
    assert handlers.BTN_ENABLE in {b.text for row in kb.keyboard for b in row}


async def test_btn_delete_asks_confirmation(monkeypatch):
    _select(monkeypatch, _channel(id=7, title="G"))
    msg = _message()

    await handlers.btn_delete(msg)

    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.InlineKeyboardMarkup)
    cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"{handlers.CH_DELYES_CB}7" in cbs


# --- Add-channel FSM -----------------------------------------------------------
async def test_start_add_channel_sets_state():
    msg = _message()
    state = SimpleNamespace(set_state=AsyncMock())
    await handlers._start_add_channel(msg, state)
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


async def test_addchannel_cancel_clears_state():
    msg = _message()
    state = SimpleNamespace(clear=AsyncMock())
    await handlers.addchannel_cancel(msg, state)
    state.clear.assert_awaited_once()
    assert "Отменено" in msg.answer.await_args.args[0]


# --- RSS -----------------------------------------------------------------------
async def test_cmd_setrss_rejects_bad_url(monkeypatch):
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message()
    await handlers.cmd_setrss(msg, SimpleNamespace(args="garbage"))
    upd.assert_not_called()
    assert "корректный URL" in msg.answer.await_args.args[0]


async def test_cmd_setrss_saves_valid_url(monkeypatch):
    _select(monkeypatch, _channel(id=1))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message()
    await handlers.cmd_setrss(msg, SimpleNamespace(args="  https://ex.com/feed  "))
    upd.assert_awaited_once_with(1, rss_url="https://ex.com/feed")


async def test_cmd_setrss_handles_none_args(monkeypatch):
    monkeypatch.setattr(handlers, "update_channel", AsyncMock())
    msg = _message()
    await handlers.cmd_setrss(msg, SimpleNamespace(args=None))
    assert "корректный URL" in msg.answer.await_args.args[0]


async def test_cmd_rss_shows_url(monkeypatch):
    _select(monkeypatch, _channel(rss_url="https://ex.com/f"))
    msg = _message()
    await handlers.cmd_rss(msg)
    assert "https://ex.com/f" in msg.answer.await_args.args[0]


async def test_cmd_rss_when_unset(monkeypatch):
    _select(monkeypatch, _channel(rss_url=None))
    msg = _message()
    await handlers.cmd_rss(msg)
    assert "не задана" in msg.answer.await_args.args[0]


async def test_cmd_rss_without_channel_nudges(monkeypatch):
    _select(monkeypatch, None)
    msg = _message()
    await handlers.cmd_rss(msg)
    assert "добавь канал" in msg.answer.await_args.args[0].lower()


# --- Hours ---------------------------------------------------------------------
async def test_cmd_hours_shows_schedule(monkeypatch):
    _select(monkeypatch, _channel(run_hours="9,13,18"))
    msg = _message()
    await handlers.cmd_hours(msg)
    out = msg.answer.await_args.args[0]
    assert "09:00" in out and "13:00" in out and "18:00" in out


async def test_cmd_sethours_saves(monkeypatch):
    _select(monkeypatch, _channel(id=1))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message()
    await handlers.cmd_sethours(msg, SimpleNamespace(args="18,9,9"))
    upd.assert_awaited_once_with(1, run_hours="9,18")  # parsed, sorted, deduped
    assert "09:00" in msg.answer.await_args.args[0]


async def test_cmd_sethours_rejects_bad_input(monkeypatch):
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message()
    await handlers.cmd_sethours(msg, SimpleNamespace(args="25,foo"))
    upd.assert_not_called()
    assert "0–23" in msg.answer.await_args.args[0]


async def test_cmd_sethours_no_args_opens_grid(monkeypatch):
    _select(monkeypatch, _channel())
    msg = _message()
    await handlers.cmd_sethours(msg, SimpleNamespace(args=None))
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.InlineKeyboardMarkup)


async def test_btn_sethours_opens_grid(monkeypatch):
    _select(monkeypatch, _channel())
    msg = _message()
    await handlers.btn_sethours(msg)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.InlineKeyboardMarkup)


def test_hours_inline_kb_marks_selected():
    kb = handlers.hours_inline_kb({9, 13})
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "✅ 09" in texts and "✅ 13" in texts
    assert "▫️ 10" in texts
    assert sum("Готово" in t for t in texts) == 1


async def test_cb_toggle_hour_enables(monkeypatch):
    _select(monkeypatch, _channel(id=1, run_hours="9,13"))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    cb = _callback("hour:18")

    await handlers.cb_toggle_hour(cb)

    upd.assert_awaited_once_with(1, run_hours="9,13,18")
    cb.message.edit_reply_markup.assert_awaited_once()
    assert "вкл" in cb.answer.await_args.args[0]


async def test_cb_toggle_hour_disables(monkeypatch):
    _select(monkeypatch, _channel(id=1, run_hours="9,13"))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    cb = _callback("hour:9")

    await handlers.cb_toggle_hour(cb)

    upd.assert_awaited_once_with(1, run_hours="13")
    assert "выкл" in cb.answer.await_args.args[0]


async def test_cb_toggle_hour_keeps_last_one(monkeypatch):
    _select(monkeypatch, _channel(id=1, run_hours="9"))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    cb = _callback("hour:9")

    await handlers.cb_toggle_hour(cb)

    upd.assert_not_called()
    assert cb.answer.await_args.kwargs.get("show_alert") is True


async def test_cb_toggle_hour_without_channel(monkeypatch):
    _select(monkeypatch, None)
    cb = _callback("hour:9")
    await handlers.cb_toggle_hour(cb)
    assert cb.answer.await_args.kwargs.get("show_alert") is True


async def test_cb_hours_done_closes_grid(monkeypatch):
    _select(monkeypatch, _channel(run_hours="9,13"))
    cb = _callback("hours_done")
    await handlers.cb_hours_done(cb)
    cb.message.edit_text.assert_awaited_once()
    assert "09:00" in cb.message.edit_text.await_args.args[0]


# --- Tone ----------------------------------------------------------------------
async def test_cmd_tone_shows_current(monkeypatch):
    _select(monkeypatch, _channel(tone="expert"))
    msg = _message()
    await handlers.cmd_tone(msg)
    assert "Экспертный" in msg.answer.await_args.args[0]


async def test_cmd_settone_no_args_opens_grid(monkeypatch):
    _select(monkeypatch, _channel(tone="news"))
    msg = _message()
    await handlers.cmd_settone(msg, SimpleNamespace(args=None))
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.InlineKeyboardMarkup)


async def test_cmd_settone_text_applies(monkeypatch):
    _select(monkeypatch, _channel(id=1))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message()
    await handlers.cmd_settone(msg, SimpleNamespace(args="  EXPERT "))
    upd.assert_awaited_once_with(1, tone="expert")  # lowercased
    assert "Экспертный" in msg.answer.await_args.args[0]


async def test_cmd_settone_rejects_unknown(monkeypatch):
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    msg = _message()
    await handlers.cmd_settone(msg, SimpleNamespace(args="bogus"))
    upd.assert_not_called()
    assert "пресет" in msg.answer.await_args.args[0]


async def test_btn_tone_opens_grid(monkeypatch):
    _select(monkeypatch, _channel(tone="news"))
    msg = _message()
    await handlers.btn_tone(msg)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.InlineKeyboardMarkup)


def test_tone_inline_kb_marks_current():
    kb = handlers.tone_inline_kb("expert")
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any(t.startswith("✅") and "Экспертный" in t for t in texts)
    assert sum("Готово" in t for t in texts) == 1


async def test_cb_select_tone_persists(monkeypatch):
    _select(monkeypatch, _channel(id=1))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    cb = _callback("tone:hype")

    await handlers.cb_select_tone(cb)

    upd.assert_awaited_once_with(1, tone="hype")
    cb.message.edit_reply_markup.assert_awaited_once()


async def test_cb_select_tone_ignores_unknown(monkeypatch):
    _select(monkeypatch, _channel(id=1))
    upd = AsyncMock()
    monkeypatch.setattr(handlers, "update_channel", upd)
    cb = _callback("tone:bogus")

    await handlers.cb_select_tone(cb)

    upd.assert_not_called()


async def test_cb_tone_done_closes_picker(monkeypatch):
    _select(monkeypatch, _channel(tone="digest"))
    cb = _callback("tone_done")
    await handlers.cb_tone_done(cb)
    cb.message.edit_text.assert_awaited_once()
    assert "Тезисный" in cb.message.edit_text.await_args.args[0]


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
async def test_cmd_run_reports_posted(monkeypatch):
    _select(monkeypatch, _channel())
    monkeypatch.setattr(
        handlers, "run_once", AsyncMock(return_value=RunResult("posted", "Заголовок"))
    )
    msg = _message()

    await handlers.cmd_run(msg, AsyncMock())

    assert msg.answer.await_count == 2  # "starting" then result
    assert "Заголовок" in msg.answer.await_args_list[-1].args[0]


async def test_cmd_run_without_channel(monkeypatch):
    _select(monkeypatch, None)
    run = AsyncMock()
    monkeypatch.setattr(handlers, "run_once", run)
    msg = _message()
    await handlers.cmd_run(msg, AsyncMock())
    run.assert_not_called()
    assert "добавь канал" in msg.answer.await_args.args[0].lower()


async def test_cmd_run_reports_error(monkeypatch):
    _select(monkeypatch, _channel())
    monkeypatch.setattr(handlers, "run_once", AsyncMock(return_value=RunResult("error", "boom")))
    msg = _message()
    await handlers.cmd_run(msg, AsyncMock())
    assert "boom" in msg.answer.await_args_list[-1].args[0]


async def test_cmd_preview_runs_dry(monkeypatch):
    ch = _channel()
    _select(monkeypatch, ch)
    run = AsyncMock(return_value=RunResult("posted", "Заголовок"))
    monkeypatch.setattr(handlers, "run_once", run)
    monkeypatch.setattr(handlers.settings, "admin_id", 777)
    msg = _message()
    bot = AsyncMock()

    await handlers.cmd_preview(msg, bot)

    run.assert_awaited_once_with(bot, ch, chat_id=777, persist=False)
    assert msg.answer.await_count == 2  # "starting" + summary


async def test_cmd_help_answers():
    msg = _message()
    await handlers.cmd_help(msg)
    msg.answer.assert_awaited_once()


async def test_btn_run_delegates_to_run(monkeypatch):
    _select(monkeypatch, _channel())
    monkeypatch.setattr(
        handlers, "run_once", AsyncMock(return_value=RunResult("posted", "Заголовок"))
    )
    msg = _message()
    await handlers.btn_run(msg, AsyncMock())
    assert "Заголовок" in msg.answer.await_args_list[-1].args[0]


# --- Set-RSS FSM ---------------------------------------------------------------
async def test_btn_setrss_requires_channel(monkeypatch):
    _select(monkeypatch, None)
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


async def test_setrss_cancel_clears_state():
    msg = _message()
    state = SimpleNamespace(clear=AsyncMock())
    await handlers.setrss_cancel(msg, state)
    state.clear.assert_awaited_once()
    assert "Отменено" in msg.answer.await_args.args[0]


async def test_fallback_answers():
    msg = _message()
    await handlers.fallback(msg)
    msg.answer.assert_awaited_once()
