"""Tone preset registry sanity checks."""
from __future__ import annotations

from app import tone


def test_default_tone_is_a_known_preset():
    assert tone.DEFAULT_TONE in tone.TONES


def test_registry_keys_match_dataclass_keys():
    for key, preset in tone.TONES.items():
        assert preset.key == key
        assert preset.label and preset.description and preset.fragment
        assert 0.0 <= preset.temperature <= 1.0


def test_get_preset_known_key():
    assert tone.get_preset("expert") is tone.TONES["expert"]


def test_get_preset_unknown_falls_back_to_default():
    assert tone.get_preset("nope") is tone.TONES[tone.DEFAULT_TONE]
    assert tone.get_preset(None) is tone.TONES[tone.DEFAULT_TONE]
