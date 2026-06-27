"""Tests for the bot command handlers (called directly, sans aiogram dispatch)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import handlers
from app.pipeline import RunResult


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


def _message():
    return SimpleNamespace(answer=AsyncMock())


async def test_cmd_setrss_rejects_bad_url(monkeypatch):
    set_rss = AsyncMock()
    monkeypatch.setattr(handlers, "set_rss_url", set_rss)
    msg = _message()

    await handlers.cmd_setrss(msg, SimpleNamespace(args="garbage"))

    set_rss.assert_not_called()
    assert "корректный URL" in msg.answer.await_args.args[0]


async def test_cmd_setrss_saves_valid_url(monkeypatch):
    set_rss = AsyncMock()
    monkeypatch.setattr(handlers, "set_rss_url", set_rss)
    msg = _message()

    await handlers.cmd_setrss(msg, SimpleNamespace(args="  https://ex.com/feed  "))

    set_rss.assert_awaited_once_with("https://ex.com/feed")
    assert "https://ex.com/feed" in msg.answer.await_args.args[0]


async def test_cmd_setrss_handles_none_args(monkeypatch):
    monkeypatch.setattr(handlers, "set_rss_url", AsyncMock())
    msg = _message()
    await handlers.cmd_setrss(msg, SimpleNamespace(args=None))
    assert "корректный URL" in msg.answer.await_args.args[0]


async def test_cmd_rss_shows_url(monkeypatch):
    monkeypatch.setattr(handlers, "get_rss_url", AsyncMock(return_value="https://ex.com/f"))
    monkeypatch.setattr(handlers, "get_stored_rss_url", AsyncMock(return_value="https://ex.com/f"))
    msg = _message()
    await handlers.cmd_rss(msg)
    assert "https://ex.com/f" in msg.answer.await_args.args[0]


async def test_cmd_rss_marks_env_fallback(monkeypatch):
    # url comes from ENV (nothing stored) -> the reply flags it as a fallback.
    monkeypatch.setattr(handlers, "get_rss_url", AsyncMock(return_value="https://env/f"))
    monkeypatch.setattr(handlers, "get_stored_rss_url", AsyncMock(return_value=None))
    msg = _message()
    await handlers.cmd_rss(msg)
    assert "ENV" in msg.answer.await_args.args[0]


async def test_cmd_rss_when_unset(monkeypatch):
    monkeypatch.setattr(handlers, "get_rss_url", AsyncMock(return_value=None))
    msg = _message()
    await handlers.cmd_rss(msg)
    assert "не задана" in msg.answer.await_args.args[0]


async def test_cmd_hours_shows_schedule(monkeypatch):
    monkeypatch.setattr(handlers, "get_run_hours", AsyncMock(return_value=[9, 13, 18]))
    monkeypatch.setattr(handlers, "get_stored_run_hours", AsyncMock(return_value=[9, 13, 18]))
    msg = _message()
    await handlers.cmd_hours(msg)
    out = msg.answer.await_args.args[0]
    assert "09:00" in out and "13:00" in out and "18:00" in out
    assert "ENV" not in out


async def test_cmd_hours_marks_env_fallback(monkeypatch):
    monkeypatch.setattr(handlers, "get_run_hours", AsyncMock(return_value=[9]))
    monkeypatch.setattr(handlers, "get_stored_run_hours", AsyncMock(return_value=None))
    msg = _message()
    await handlers.cmd_hours(msg)
    assert "ENV" in msg.answer.await_args.args[0]


async def test_cmd_sethours_saves_and_reschedules(monkeypatch):
    set_hours = AsyncMock()
    resched = AsyncMock()
    monkeypatch.setattr(handlers, "set_run_hours", set_hours)
    monkeypatch.setattr(handlers, "reschedule", resched)
    msg = _message()

    await handlers.cmd_sethours(msg, SimpleNamespace(args="18,9,9"))

    set_hours.assert_awaited_once_with([9, 18])  # parsed, sorted, deduped
    resched.assert_awaited_once()
    assert "09:00" in msg.answer.await_args.args[0]


async def test_cmd_sethours_rejects_bad_input(monkeypatch):
    set_hours = AsyncMock()
    monkeypatch.setattr(handlers, "set_run_hours", set_hours)
    monkeypatch.setattr(handlers, "reschedule", AsyncMock())
    msg = _message()

    await handlers.cmd_sethours(msg, SimpleNamespace(args="25,foo"))

    set_hours.assert_not_called()
    assert "0–23" in msg.answer.await_args.args[0]


async def test_cmd_sethours_no_args_opens_grid(monkeypatch):
    monkeypatch.setattr(handlers, "get_run_hours", AsyncMock(return_value=[9, 13]))
    msg = _message()
    await handlers.cmd_sethours(msg, SimpleNamespace(args=None))
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.InlineKeyboardMarkup)


async def test_btn_sethours_opens_grid(monkeypatch):
    monkeypatch.setattr(handlers, "get_run_hours", AsyncMock(return_value=[9, 13]))
    msg = _message()
    await handlers.btn_sethours(msg)
    kb = msg.answer.await_args.kwargs["reply_markup"]
    assert isinstance(kb, handlers.InlineKeyboardMarkup)


# --- Hours toggle grid (inline keyboard + callbacks) ---------------------------
def test_hours_inline_kb_marks_selected():
    kb = handlers.hours_inline_kb({9, 13})
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "✅ 09" in texts and "✅ 13" in texts
    assert "▫️ 10" in texts
    # 24 hour buttons + a final «Готово» row.
    assert sum("Готово" in t for t in texts) == 1


def _callback(data: str):
    message = SimpleNamespace(edit_reply_markup=AsyncMock(), edit_text=AsyncMock())
    return SimpleNamespace(data=data, answer=AsyncMock(), message=message)


async def test_cb_toggle_hour_enables(monkeypatch):
    set_hours = AsyncMock()
    resched = AsyncMock()
    monkeypatch.setattr(handlers, "get_run_hours", AsyncMock(return_value=[9, 13]))
    monkeypatch.setattr(handlers, "set_run_hours", set_hours)
    monkeypatch.setattr(handlers, "reschedule", resched)
    cb = _callback("hour:18")

    await handlers.cb_toggle_hour(cb)

    set_hours.assert_awaited_once_with([9, 13, 18])  # added + sorted
    resched.assert_awaited_once()
    cb.message.edit_reply_markup.assert_awaited_once()
    assert "вкл" in cb.answer.await_args.args[0]


async def test_cb_toggle_hour_disables(monkeypatch):
    set_hours = AsyncMock()
    monkeypatch.setattr(handlers, "get_run_hours", AsyncMock(return_value=[9, 13]))
    monkeypatch.setattr(handlers, "set_run_hours", set_hours)
    monkeypatch.setattr(handlers, "reschedule", AsyncMock())
    cb = _callback("hour:9")

    await handlers.cb_toggle_hour(cb)

    set_hours.assert_awaited_once_with([13])  # removed
    assert "выкл" in cb.answer.await_args.args[0]


async def test_cb_toggle_hour_keeps_last_one(monkeypatch):
    set_hours = AsyncMock()
    monkeypatch.setattr(handlers, "get_run_hours", AsyncMock(return_value=[9]))
    monkeypatch.setattr(handlers, "set_run_hours", set_hours)
    monkeypatch.setattr(handlers, "reschedule", AsyncMock())
    cb = _callback("hour:9")

    await handlers.cb_toggle_hour(cb)

    set_hours.assert_not_called()  # refuse to empty the schedule
    assert cb.answer.await_args.kwargs.get("show_alert") is True


async def test_cb_hours_done_closes_grid(monkeypatch):
    monkeypatch.setattr(handlers, "get_run_hours", AsyncMock(return_value=[9, 13]))
    cb = _callback("hours_done")

    await handlers.cb_hours_done(cb)

    cb.message.edit_text.assert_awaited_once()
    assert "09:00" in cb.message.edit_text.await_args.args[0]


async def test_cmd_run_reports_posted(monkeypatch):
    monkeypatch.setattr(
        handlers, "run_once", AsyncMock(return_value=RunResult("posted", "Заголовок"))
    )
    msg = _message()
    bot = AsyncMock()

    await handlers.cmd_run(msg, bot)

    # First a "starting" message, then the result.
    assert msg.answer.await_count == 2
    assert "Заголовок" in msg.answer.await_args_list[-1].args[0]


async def test_cmd_run_reports_error(monkeypatch):
    monkeypatch.setattr(handlers, "run_once", AsyncMock(return_value=RunResult("error", "boom")))
    msg = _message()
    await handlers.cmd_run(msg, AsyncMock())
    assert "boom" in msg.answer.await_args_list[-1].args[0]


async def test_cmd_preview_runs_dry(monkeypatch):
    run = AsyncMock(return_value=RunResult("posted", "Заголовок"))
    monkeypatch.setattr(handlers, "run_once", run)
    monkeypatch.setattr(handlers.settings, "admin_id", 777)
    msg = _message()
    bot = AsyncMock()

    await handlers.cmd_preview(msg, bot)

    # Pipeline invoked against the admin chat, without persisting.
    run.assert_awaited_once_with(bot, chat_id=777, persist=False)
    assert msg.answer.await_count == 2  # "starting" + summary


async def test_cmd_help_answers(monkeypatch):
    msg = _message()
    await handlers.cmd_help(msg)
    msg.answer.assert_awaited_once()


# --- Keyboard buttons / FSM ----------------------------------------------------
async def test_btn_run_delegates_to_run(monkeypatch):
    monkeypatch.setattr(
        handlers, "run_once", AsyncMock(return_value=RunResult("posted", "Заголовок"))
    )
    msg = _message()
    await handlers.btn_run(msg, AsyncMock())
    assert "Заголовок" in msg.answer.await_args_list[-1].args[0]


async def test_btn_status_shows_status(monkeypatch):
    monkeypatch.setattr(handlers, "get_rss_url", AsyncMock(return_value="https://ex.com/f"))
    monkeypatch.setattr(handlers, "get_stored_rss_url", AsyncMock(return_value="https://ex.com/f"))
    monkeypatch.setattr(handlers, "get_run_hours", AsyncMock(return_value=[9, 13, 18]))
    monkeypatch.setattr(handlers, "get_stored_run_hours", AsyncMock(return_value=[9, 13, 18]))
    msg = _message()
    await handlers.btn_status(msg)
    out = msg.answer.await_args.args[0]
    assert "https://ex.com/f" in out
    assert "09:00" in out


async def test_btn_setrss_enters_waiting_state():
    msg = _message()
    state = SimpleNamespace(set_state=AsyncMock())
    await handlers.btn_setrss(msg, state)
    state.set_state.assert_awaited_once_with(handlers.SetRss.waiting_for_url)
    msg.answer.assert_awaited_once()


async def test_setrss_receive_saves_valid_url(monkeypatch):
    set_rss = AsyncMock()
    monkeypatch.setattr(handlers, "set_rss_url", set_rss)
    msg = SimpleNamespace(answer=AsyncMock(), text="  https://ex.com/feed  ")
    state = SimpleNamespace(clear=AsyncMock())

    await handlers.setrss_receive(msg, state)

    state.clear.assert_awaited_once()
    set_rss.assert_awaited_once_with("https://ex.com/feed")


async def test_setrss_receive_rejects_bad_url(monkeypatch):
    set_rss = AsyncMock()
    monkeypatch.setattr(handlers, "set_rss_url", set_rss)
    msg = SimpleNamespace(answer=AsyncMock(), text="garbage")
    state = SimpleNamespace(clear=AsyncMock())

    await handlers.setrss_receive(msg, state)

    set_rss.assert_not_called()
    state.clear.assert_not_called()  # stay in the waiting state for a retry


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
