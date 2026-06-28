"""Post tone presets.

The voice/format of a generated post is a per-channel setting (stored in the DB,
ENV is only a fallback — same pattern as the RSS url and run hours). Each preset
carries the prompt fragment injected into `deepseek.generate_post` plus a
temperature that fits its style. The constant requirements (Telegram HTML, source
link, language) live in generate_post and are the same for every tone.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tone:
    key: str
    label: str  # button caption (with emoji)
    description: str  # one line shown to the admin
    fragment: str  # injected into the generation prompt
    temperature: float


TONES: dict[str, Tone] = {
    "news": Tone(
        key="news",
        label="📰 Новостной",
        description="Сухо и нейтрально, только факты, без оценок.",
        fragment=(
            "Пиши нейтрально и информативно, как новостная заметка: только факты, "
            "без оценок, эмоций и обращений к читателю."
        ),
        temperature=0.4,
    ),
    "expert": Tone(
        key="expert",
        label="🎓 Экспертный",
        description="Факты плюс короткий вывод «почему это важно».",
        fragment=(
            "Подай как аналитик: факты и 1–2 предложения о том, почему это важно и "
            "какие могут быть последствия. Спокойный экспертный тон."
        ),
        temperature=0.6,
    ),
    "casual": Tone(
        key="casual",
        label="💬 Разговорный",
        description="Простой язык, обращение к читателю, лёгкая подача.",
        fragment=(
            "Пиши простым разговорным языком, можно обращаться к читателю на «ты», "
            "легко и дружелюбно, но без панибратства."
        ),
        temperature=0.7,
    ),
    "ironic": Tone(
        key="ironic",
        label="😏 Ироничный",
        description="Остроумно, с лёгким сарказмом; факты не искажаются.",
        fragment=(
            "Добавь лёгкую иронию и остроумные формулировки. Сарказм уместен, но факты "
            "передавай точно и без передёргиваний; не оскорбляй людей и компании."
        ),
        temperature=0.8,
    ),
    "hype": Tone(
        key="hype",
        label="🔥 Энергичный",
        description="Динамично, цепляющий первый абзац, акцент на значимости.",
        fragment=(
            "Подай энергично: цепляющий первый абзац, акцент на значимости события. "
            "Без вранья и кликбейта — интрига строится на реальной сути."
        ),
        temperature=0.8,
    ),
    "digest": Tone(
        key="digest",
        label="📋 Тезисный",
        description="3–5 коротких пунктов списком, максимально кратко.",
        fragment=(
            "Сформулируй суть как список из 3–5 коротких пунктов (каждый с «• »). "
            "Минимум вводных слов, только главное."
        ),
        temperature=0.4,
    ),
}

DEFAULT_TONE = "news"


def get_preset(key: str | None) -> Tone:
    """The preset for `key`, or the default when it's unknown/None."""
    return TONES.get(key or "", TONES[DEFAULT_TONE])
