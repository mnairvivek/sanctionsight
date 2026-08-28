"""Unit tests for Phase 5 language detection helper.

The helper is intentionally conservative: short samples return None (rather
than guessing "en") so we don't mis-badge ambiguous content. These tests
lock that behaviour in so an over-eager tweak doesn't silently regress.
"""
from __future__ import annotations

import pytest

pytest.importorskip("langdetect")

from sanctions_engine import detect_language  # noqa: E402 — after importorskip


def test_english_paragraph_detected_as_en() -> None:
    text = (
        "The compliance team reviewed the sanctions list and confirmed that the "
        "entity does not appear on any current OFAC designations. The investigator "
        "recommends no further action pending the next list refresh cycle."
    )
    assert detect_language(text) == "en"


def test_short_sample_returns_none() -> None:
    # Below the 40-char threshold — detection is too unreliable to trust.
    assert detect_language("OFAC cleared") is None


def test_empty_or_none_returns_none() -> None:
    assert detect_language(None) is None
    assert detect_language("") is None
    assert detect_language("   ") is None


def test_non_english_paragraph_flagged() -> None:
    # Russian — written out so we verify the detector returns a non-en code
    # without hard-coding which non-en code (langdetect sometimes picks
    # close neighbours like "bg"/"uk" for short Cyrillic samples).
    russian = (
        "Компания подпадает под санкционные ограничения Европейского союза в "
        "отношении финансовых операций и не имеет права осуществлять переводы "
        "через корреспондентские счета в долларах США."
    )
    lang = detect_language(russian)
    assert lang is not None
    assert lang != "en"
