"""DeepSeek (OpenAI-compatible) calls: pick the top story and write a post."""

from __future__ import annotations

import json
from functools import cache

from loguru import logger as log
from openai import AsyncOpenAI

from app.config import settings
from app.tone import Tone, get_preset

# Low temperature: picking a story is a ranking task, not a creative one.
_PICK_TEMPERATURE = 0.3


@cache
def _get_client() -> AsyncOpenAI:
    return AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)


def _build_pick_prompt(candidates: list[dict], recent_titles: list[str] | None) -> str:
    listing = "\n".join(
        f"{num}. {cand['title']}\n   {cand.get('summary', '')}"
        for num, cand in enumerate(candidates)
    )
    published = (
        "\n".join(f"- {title}" for title in recent_titles)
        if recent_titles
        else "(за последние сутки ничего не публиковалось)"
    )
    return (
        "Ты редактор Telegram-канала и подбираешь следующую публикацию.\n\n"
        "Уже опубликовано за последние сутки:\n"
        f"{published}\n\n"
        "Свежие новости-кандидаты (ещё не публиковались):\n"
        f"{listing}\n\n"
        "Выбери ОДНУ новость для публикации прямо сейчас. Она должна быть важной и "
        "актуальной, но при этом РАЗНООБРАЗИТЬ ленту: по теме и сюжету отличаться "
        "от уже опубликованного выше — не повторяй недавние темы. Если несколько "
        "кандидатов про одно и то же, предпочти новость из другой тематики.\n"
        'Ответь строго JSON-объектом вида {"index": <число>, "reason": "<кратко>"}.'
    )


def _extract_pick(answer: str) -> tuple[int, str] | None:
    """(index, reason) from the model's JSON answer, or None if it can't be parsed."""
    try:
        payload = json.loads(answer)
    except ValueError:  # json.JSONDecodeError is a ValueError
        return None
    try:
        return int(payload["index"]), payload.get("reason", "")
    except (KeyError, ValueError, TypeError):
        return None


def _parse_pick(answer: str, count: int) -> int:
    """Picked index; 0 when the answer is unparsable or out of range."""
    pick = _extract_pick(answer)
    if pick is None:
        log.warning("Could not parse DeepSeek pick, defaulting to 0")
        return 0
    index, reason = pick
    if 0 <= index < count:
        log.info("Picked #{}: {}", index, reason)
        return index
    return 0


async def pick_most_relevant(candidates: list[dict], recent_titles: list[str] | None = None) -> int:
    """Return the index of the best candidate to publish next.

    `candidates` is a list of {title, summary} that have NOT been published yet.
    `recent_titles` are headlines already posted in the last day — passed to the
    model so it can diversify topics and avoid repeating a recent story.
    Falls back to 0 on any parsing problem so the pipeline still posts something.
    """
    if len(candidates) == 1:
        return 0

    resp = await _get_client().chat.completions.create(
        model=settings.deepseek_model,
        messages=[{"role": "user", "content": _build_pick_prompt(candidates, recent_titles)}],
        response_format={"type": "json_object"},
        temperature=_PICK_TEMPERATURE,
    )
    return _parse_pick(resp.choices[0].message.content or "", len(candidates))


async def generate_post(entry: dict, tone: Tone | None = None) -> str:
    """Generate a ready-to-send Telegram post (HTML) for a news entry.

    `tone` controls the voice/format (the per-channel preset); the structural
    requirements below stay constant. Defaults to the default preset.
    """
    tone = tone or get_preset(None)
    prompt = (
        f"Напиши пост для Telegram-канала СТРОГО на {settings.post_language} языке "
        "по этой новости.\n"
        f"Заголовок: {entry['title']}\n"
        f"Описание: {entry.get('summary', '')}\n\n"
        f"Тон и подача: {tone.fragment}\n\n"
        "Требования:\n"
        "- 2–5 коротких абзацев, без воды и кликбейта.\n"
        "- Допустима только Telegram HTML-разметка: <b>, <i>. Без markdown.\n"
        "- НЕ добавляй ссылки и не упоминай источник.\n"
        "- Не используй заголовки markdown и эмодзи-спам (1–2 уместных эмодзи максимум).\n"
        "Верни только текст поста, без пояснений."
    )
    resp = await _get_client().chat.completions.create(
        model=settings.deepseek_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=tone.temperature,
    )
    return (resp.choices[0].message.content or "").strip()
