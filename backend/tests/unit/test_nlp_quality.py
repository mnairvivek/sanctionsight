"""Regression matrix for the NLP redesign (Tracks A + A8).

These tests lock in behaviour for the specific bugs the redesign set out to
fix. They don't exercise the zero-shot NLI classifier directly — that requires
a ~180MB model download and is disabled by default via
``SANCTIONSIGHT_USE_NLI=false`` in CI — so every assertion here is about the
*deterministic* rules (licensed-activity override, FP categorisation, word-
boundary country matching, bidirectional negation) which run the same way
whether the NLI pipeline is loaded or not.
"""
from __future__ import annotations

import os
import pytest

# Force the classifier off for the test suite so the keyword-fallback path is
# exercised deterministically. Production can still set this back to "true".
os.environ["SANCTIONSIGHT_USE_NLI"] = "false"

from sanctions_engine import (  # noqa: E402  (env must be set before import)
    EnhancedRiskAssessment,
    SanctionsContentAnalyzer,
    get_analyzer,
)


# ---------------------------------------------------------------------------
# A8a — licensed-activity override (humanitarian + OFAC licensing → HIGH)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase",
    [
        "humanitarian aid",
        "humanitarian assistance",
        "disaster relief",
        "medical aid",
        "food aid",
        "ofac license",
        "general license",
        "authorized under license",
    ],
)
def test_licensed_activity_phrases_flag_high(phrase: str) -> None:
    analyzer = get_analyzer("Iran")
    context = f"The NGO provided {phrase} to Tehran last year."
    result = analyzer._analyze_context(context, context)
    assert result["relevant"] is True
    assert result["risk_type"] == "LICENSED_ACTIVITY_MENTION"
    assert result["risk_score"] == 85
    assert phrase in (result.get("note") or "").lower()


def test_licensed_activity_beats_false_positive_rule() -> None:
    """Humanitarian aid phrase sits *before* the FP list, so a sentence that
    happens to contain both (e.g. 'damascus steel') still flags HIGH on the
    licensed-activity rule rather than getting excluded."""
    analyzer = get_analyzer("Syria")
    # Pathological sentence — exists to prove order-of-operations in _analyze_context
    context = "Damascus steel blades were donated as part of the humanitarian aid shipment."
    result = analyzer._analyze_context(context, context)
    assert result["risk_type"] == "LICENSED_ACTIVITY_MENTION"


# ---------------------------------------------------------------------------
# A8b — confirmed false positives become audit-trail findings, not drops
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase,country",
    [
        ("damascus steel", "Syria"),
        ("cuban sandwich", "Cuba"),
        ("cuban link", "Cuba"),
    ],
)
def test_confirmed_fp_returns_excluded_reference(phrase: str, country: str) -> None:
    analyzer = get_analyzer(country)
    sentence = f"Shop our selection of {phrase} knives online."
    result = analyzer._analyze_context(sentence, sentence)
    assert result["relevant"] is False
    assert result["excluded_reference"] is True
    assert result["matched_phrase"] == phrase
    assert result["risk_type"] == "NON_SANCTIONS_REFERENCE"


def test_deferred_review_phrases_still_silently_skip() -> None:
    analyzer = get_analyzer("Iran")
    for phrase in ("shipping policy", "we do not ship", "countries we ship to"):
        sentence = f"See our {phrase} for details on Iran."
        result = analyzer._analyze_context(sentence, sentence)
        assert result == {"relevant": False}


# ---------------------------------------------------------------------------
# A4 — word-boundary country matching (via the country_pattern regex)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "country,should_match,text",
    [
        ("Cuba", True, "We ship to Cuba regularly."),
        ("Cuba", False, "Please incubate the sample overnight."),   # "cuba" ⊂ "incubate"
        ("Cuba", False, "Pass me the habanero sauce."),             # "habana" ⊂ "habanero"
        ("Iran", True, "Do not transfer funds to Iran."),
        ("North Korea", True, "Intercepted a shipment bound for DPRK."),
    ],
)
def test_country_pattern_word_boundary(country: str, should_match: bool, text: str) -> None:
    analyzer = get_analyzer(country)
    hit = bool(analyzer.country_pattern.search(text))
    assert hit is should_match


# ---------------------------------------------------------------------------
# A5 — bidirectional negation (forward AND backward windows)
# ---------------------------------------------------------------------------


def test_forward_negation_is_detected() -> None:
    # Indicator appears BEFORE the negation word — the old backward-only window
    # missed these. "Funding to Iran is prohibited" is the canonical case.
    assert EnhancedRiskAssessment._is_negated(
        "Funding to Iran is prohibited by internal policy.", "funding"
    ) is True


def test_forward_negation_multiword() -> None:
    assert EnhancedRiskAssessment._is_negated(
        "Payment to the Tehran office shall not be processed.", "payment"
    ) is True


def test_backward_negation_still_works() -> None:
    # Regression guard: don't lose the backward case while adding forward.
    assert EnhancedRiskAssessment._is_negated(
        "We do not process payments for Iranian entities.", "payment"
    ) is True


def test_unnegated_mention_is_not_flagged() -> None:
    assert EnhancedRiskAssessment._is_negated(
        "We process payments for Iranian entities weekly.", "payment"
    ) is False


# ---------------------------------------------------------------------------
# A6 — currency short-circuit removed
# ---------------------------------------------------------------------------


def test_currency_symbol_no_longer_short_circuits() -> None:
    # Currency goes through negation now. With "not" preceding "$" within the
    # window, the indicator is suppressed.
    assert EnhancedRiskAssessment.has_active_financial_indicator(
        "We do not accept $ payments from sanctioned regions.", ["$"]
    ) is False


# ---------------------------------------------------------------------------
# A7 / custom-country — user-supplied variations override the built-in dict
# ---------------------------------------------------------------------------


def test_custom_country_variations_override() -> None:
    analyzer = SanctionsContentAnalyzer(
        "Zimbabwe",
        variations_override=["Zimbabwean", "Harare", ".zw"],
    )
    # Built-in Zimbabwe variations don't exist in the dict, so the override
    # is the only way the regex catches these.
    assert analyzer.country_pattern.search("Based in Harare, Zimbabwe.") is not None
    assert analyzer.country_pattern.search("The client is Zimbabwean.") is not None
    # Literal country name is always included automatically.
    assert analyzer.country_pattern.search("Zimbabwe remains sanctioned.") is not None
    # Something that's NOT in the override list and shares a letter prefix
    # must not match (no substring leakage via the regex).
    assert analyzer.country_pattern.search("Zimmer frame for sale.") is None


def test_custom_country_without_variations_still_matches_literal_name() -> None:
    analyzer = SanctionsContentAnalyzer("Zimbabwe", variations_override=[])
    # Empty overrides still yield the literal name match via the built-in dict
    # fallback — `__init__` only swaps to overrides when the list is truthy.
    assert analyzer.country_pattern.search("Zimbabwe remains sanctioned.") is not None


# ---------------------------------------------------------------------------
# End-to-end: analyze_content produces the new excluded_references field
# ---------------------------------------------------------------------------


def test_analyze_content_returns_excluded_references() -> None:
    analyzer = get_analyzer("Syria")
    extraction = {
        "content": (
            "The blacksmith forged blades from genuine damascus steel for centuries. "
            "Syria is unrelated to this product page."
        ),
        "type": "HTML",
    }
    out = analyzer.analyze_content(extraction, url="https://example.com/")
    assert "excluded_references" in out
    # The damascus-steel sentence should land in excluded_references rather
    # than relevant_excerpts.
    assert any(
        e.get("matched_phrase") == "damascus steel" for e in out["excluded_references"]
    )
