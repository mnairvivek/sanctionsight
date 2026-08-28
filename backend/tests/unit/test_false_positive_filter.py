"""Unit tests for ``SanctionsContentAnalyzer._analyze_context``.

The context analyzer is the gate between "sentence mentions a sanctioned
term" and "we record a finding." Changes here move the tool's precision/
recall directly, so this is a Tier A surface per ``compliance/governance.md``.

These tests pin down:
 - The false-positive phrase allow-list (damascus steel, cuban sandwich, …)
 - PERSON-entity NER downgrade (e.g. "Ms Cuba" on a staff page)
 - Keyword-scoring tier boundaries (DIRECT_BUSINESS / INDIRECT_BUSINESS / etc.)
 - Sanctions-term confidence boost + risk-score reduction when negated

spaCy is required for the NER pass, so the whole module is skipped if the
model can't be loaded.
"""
from __future__ import annotations

import pytest

pytest.importorskip("spacy")

from sanctions_engine import SanctionsContentAnalyzer  # noqa: E402


@pytest.fixture(scope="module")
def analyzer() -> SanctionsContentAnalyzer:
    # Cuba is convenient: short variations list + it appears in two of the
    # canonical false-positive phrases ("cuban sandwich", "cuban link").
    return SanctionsContentAnalyzer("Cuba")


# ---------------------------------------------------------------------------
# False-positive phrase exclusion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase,matched", [
    ("Our chef serves the best cuban sandwich in town.", "cuban sandwich"),
    ("The bracelet features a classic cuban link chain.", "cuban link"),
    ("Damascus steel knives are forged using traditional methods.", "damascus steel"),
])
def test_confirmed_fp_returns_excluded_reference_envelope(
    analyzer: SanctionsContentAnalyzer, phrase: str, matched: str,
) -> None:
    # A8b: confirmed false positives no longer silently drop — they return a
    # categorised finding so the UI can show "N excluded references" in the
    # audit trail. The top-level ``relevant`` stays False so the pass-1 loop
    # in analyze_content skips the risk aggregation.
    result = analyzer._analyze_context(phrase, phrase)
    assert result["relevant"] is False
    assert result["excluded_reference"] is True
    assert result["matched_phrase"] == matched
    assert result["risk_type"] == "NON_SANCTIONS_REFERENCE"


@pytest.mark.parametrize("phrase", [
    "See our shipping policy for details on supported regions.",
    "We do not ship to embargoed territories.",
    "Countries we ship to include the EU, UK, and Canada.",
])
def test_deferred_review_phrases_are_silently_skipped(
    analyzer: SanctionsContentAnalyzer, phrase: str
) -> None:
    # Shipping-policy family is parked in DEFERRED_FOR_REVIEW and keeps the
    # pre-A8b "silent drop" behaviour until a future scope revisits it.
    result = analyzer._analyze_context(phrase, phrase)
    assert result == {"relevant": False}


def test_false_positive_match_is_case_insensitive(
    analyzer: SanctionsContentAnalyzer,
) -> None:
    # Phrase list is lowercased; the comparator lowercases the context.
    upper = "DAMASCUS STEEL is renowned for its layered pattern."
    result = analyzer._analyze_context(upper, upper)
    assert result["relevant"] is False
    assert result["excluded_reference"] is True
    assert result["matched_phrase"] == "damascus steel"


def test_false_positive_in_context_excludes_even_if_sentence_is_clean(
    analyzer: SanctionsContentAnalyzer,
) -> None:
    # The exclusion operates on the context window, not just the focal
    # sentence — a mention of "cuban sandwich" anywhere in the ±3 window
    # suppresses the whole finding and returns the excluded-reference envelope.
    sentence = "The kitchen also sources ingredients from Havana."
    context = (
        "Today's lunch specials are posted on the board. "
        "Our chef serves the best cuban sandwich in town. "
        + sentence
    )
    result = analyzer._analyze_context(sentence, context)
    assert result["relevant"] is False
    assert result["excluded_reference"] is True
    assert result["matched_phrase"] == "cuban sandwich"


# ---------------------------------------------------------------------------
# PERSON-entity NER downgrade
# ---------------------------------------------------------------------------


def test_person_entity_match_is_downgraded(
    analyzer: SanctionsContentAnalyzer,
) -> None:
    # If a country variation appears inside a PERSON entity, the finding
    # is downgraded to PERSON_NAME_MATCH with a flat risk_score of 10.
    # "Havana" is a first name / place — spaCy often tags it PERSON in
    # capitalised prose.
    sentence = "Havana Cuba led the seminar on international logistics."
    result = analyzer._analyze_context(sentence, sentence)
    if result.get("risk_type") == "PERSON_NAME_MATCH":
        assert result["risk_score"] == 10
        assert result["confidence"] == 90
        assert result["relevant"] is True
    else:
        # spaCy's small models can miss the PERSON tag — this is a soft
        # assertion documenting the intended branch without making the
        # test flaky across model versions.
        pytest.skip("spaCy did not tag the span as PERSON in this environment")


# ---------------------------------------------------------------------------
# Keyword scoring tier boundaries
# ---------------------------------------------------------------------------


def test_direct_business_classification(
    analyzer: SanctionsContentAnalyzer,
) -> None:
    # Three business keywords (30 pts) with no negation → DIRECT_BUSINESS.
    sentence = (
        "We operate a subsidiary in Havana and maintain a local branch "
        "with ongoing trade activity."
    )
    result = analyzer._analyze_context(sentence, sentence)
    assert result["relevant"] is True
    assert result["risk_type"] == "DIRECT_BUSINESS"
    assert result["risk_score"] == 80
    assert result["confidence"] == 85


def test_indirect_business_classification(
    analyzer: SanctionsContentAnalyzer,
) -> None:
    # Two business keywords (20 pts) with no negation → INDIRECT_BUSINESS.
    sentence = "Our supplier sources products from Havana."
    result = analyzer._analyze_context(sentence, sentence)
    assert result["relevant"] is True
    assert result["risk_type"] == "INDIRECT_BUSINESS"
    assert result["risk_score"] == 50
    assert result["confidence"] == 70


def test_compliance_mention_classification(
    analyzer: SanctionsContentAnalyzer,
) -> None:
    # Compliance keywords > 10 and no business keywords → COMPLIANCE_MENTION,
    # then boosted by the sanctions-term bonus.
    sentence = (
        "Our compliance team enforces the OFAC embargo policy for Cuba."
    )
    result = analyzer._analyze_context(sentence, sentence)
    assert result["relevant"] is True
    assert result["risk_type"] == "COMPLIANCE_MENTION"


def test_general_mention_fallback(
    analyzer: SanctionsContentAnalyzer,
) -> None:
    # No business, compliance, or negative keywords → GENERAL_MENTION.
    sentence = "The photograph was taken in Havana during the 1970s."
    result = analyzer._analyze_context(sentence, sentence)
    assert result["relevant"] is True
    assert result["risk_type"] == "GENERAL_MENTION"
    assert result["risk_score"] == 10
    assert result["confidence"] == 60


# ---------------------------------------------------------------------------
# Sanctions-term boost + negation penalty
# ---------------------------------------------------------------------------


def test_sanctions_term_boosts_confidence(
    analyzer: SanctionsContentAnalyzer,
) -> None:
    # "sanction" / "ofac" / "sdn" / "embargo" etc. add +10 confidence.
    # Both sentences hit the same base tier (INDIRECT_BUSINESS) so only the
    # sanctions-term boost differs.
    plain = "Our supplier sources products from Havana."
    boosted = "Our supplier sources products from Havana under OFAC review."
    plain_conf = analyzer._analyze_context(plain, plain)["confidence"]
    boosted_conf = analyzer._analyze_context(boosted, boosted)["confidence"]
    assert boosted_conf == plain_conf + 10


def test_sanctions_term_with_negation_reduces_risk_score(
    analyzer: SanctionsContentAnalyzer,
) -> None:
    # Sanctions term + negation → risk_score reduced by 30 (floor 10).
    sentence = (
        "We do not operate in Havana because OFAC sanctions prohibit such trade."
    )
    result = analyzer._analyze_context(sentence, sentence)
    assert result["relevant"] is True
    # The INDIRECT / DIRECT base would be >= 50; with sanctions+neg it drops.
    assert result["risk_score"] < 50


def test_risk_score_never_exceeds_100(
    analyzer: SanctionsContentAnalyzer,
) -> None:
    # Stuff the sentence with business + sanctions keywords — scorer clips
    # at 100 via min(100, risk_score).
    sentence = (
        "We operate our business, trade, export, import, subsidiary, branch, "
        "office, partner, supplier in Havana under OFAC sanctions."
    )
    result = analyzer._analyze_context(sentence, sentence)
    assert result["risk_score"] <= 100


def test_confidence_never_exceeds_95(
    analyzer: SanctionsContentAnalyzer,
) -> None:
    sentence = (
        "We operate a subsidiary in Havana under OFAC sanctions and SDN rules."
    )
    result = analyzer._analyze_context(sentence, sentence)
    assert result["confidence"] <= 95
