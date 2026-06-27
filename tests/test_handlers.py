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
    msg = _message()
    await handlers.cmd_rss(msg)
    assert "https://ex.com/f" in msg.answer.await_args.args[0]


async def test_cmd_rss_when_unset(monkeypatch):
    monkeypatch.setattr(handlers, "get_rss_url", AsyncMock(return_value=None))
    msg = _message()
    await handlers.cmd_rss(msg)
    assert "не задана" in msg.answer.await_args.args[0]


async def test_cmd_run_reports_posted(monkeypatch):
    monkeypatch.setattr(handlers, "run_once", AsyncMock(return_value=RunResult("posted", "Заголовок")))
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


async def test_cmd_help_answers(monkeypatch):
    msg = _message()
    await handlers.cmd_help(msg)
    msg.answer.assert_awaited_once()
