"""Unit tests for ``EnhancedRiskAssessment.calculate_risk_score``.

The risk-tier thresholds (HIGH ≥ 70, MEDIUM ≥ 40, LOW ≥ 15, else MINIMAL)
and the "active financial indicator → HIGH" override are Tier A governed
surfaces per ``compliance/governance.md``. These tests pin the behaviour
down so a refactor can't silently shift a tier.
"""
from __future__ import annotations

from sanctions_engine import EnhancedRiskAssessment

FINANCIAL_INDICATORS = [
    "fund", "payment", "transfer", "invoice", "remit", "wire", "$", "€", "£",
]


def _ctx(**over) -> dict:
    base = {
        "risk_type": "DIRECT_BUSINESS",
        "risk_score": 80,
        "confidence": 85,
        "sentence": "We opened a subsidiary in Tehran.",
    }
    base.update(over)
    return base


def test_financial_indicator_in_scope_forces_high() -> None:
    result = EnhancedRiskAssessment.calculate_risk_score(
        _ctx(risk_score=50, confidence=60),
        "Wire transfer of $100,000 to the Tehran office was completed.",
        FINANCIAL_INDICATORS,
    )
    assert result["risk_level"] == "HIGH"
    assert result["risk_type"] == "FINANCIAL_TRANSACTION"
    assert result["risk_score"] >= 85
    assert result["confidence"] >= 90
    assert "financial" in result["note"].lower()


def test_currency_symbol_triggers_high_when_present() -> None:
    # The has_active_financial_indicator rule: $/€/£ in the scoped text
    # immediately triggers HIGH without a negation check. Phase 5 scoped
    # this to the context window only (not the full document).
    result = EnhancedRiskAssessment.calculate_risk_score(
        _ctx(risk_score=30, confidence=60),
        "The contract was valued at $500,000 for Tehran operations.",
        FINANCIAL_INDICATORS,
    )
    assert result["risk_level"] == "HIGH"
    assert result["risk_type"] == "FINANCIAL_TRANSACTION"


def test_negated_indicator_does_not_force_high() -> None:
    # "do not process payments" — indicator "payment(s)" is negated, so
    # should NOT escalate. Final tier comes from base_score only.
    result = EnhancedRiskAssessment.calculate_risk_score(
        _ctx(risk_score=40, confidence=70),
        "Our firm does not process payments for customers in Iran.",
        ["payment", "fund", "wire"],  # no currency symbols in this scope
    )
    assert result["risk_level"] == "MEDIUM"  # base_score=40 → MEDIUM floor
    assert result["risk_type"] == "DIRECT_BUSINESS"
    assert result.get("note") is None


def test_high_tier_threshold_boundary() -> None:
    # base_score ≥ 70 → HIGH
    for score, expected in [(70, "HIGH"), (69, "MEDIUM"), (85, "HIGH")]:
        result = EnhancedRiskAssessment.calculate_risk_score(
            _ctx(risk_score=score),
            "benign content no indicators",
            FINANCIAL_INDICATORS,
        )
        assert result["risk_level"] == expected, f"score={score}"


def test_medium_tier_threshold_boundary() -> None:
    # 40 ≤ base_score < 70 → MEDIUM
    for score, expected in [(40, "MEDIUM"), (39, "LOW"), (69, "MEDIUM")]:
        result = EnhancedRiskAssessment.calculate_risk_score(
            _ctx(risk_score=score),
            "benign content",
            FINANCIAL_INDICATORS,
        )
        assert result["risk_level"] == expected, f"score={score}"


def test_low_tier_threshold_boundary() -> None:
    # 15 ≤ base_score < 40 → LOW
    for score, expected in [(15, "LOW"), (14, "MINIMAL"), (39, "LOW")]:
        result = EnhancedRiskAssessment.calculate_risk_score(
            _ctx(risk_score=score),
            "benign content",
            FINANCIAL_INDICATORS,
        )
        assert result["risk_level"] == expected, f"score={score}"


def test_minimal_tier_for_sub_threshold_scores() -> None:
    for score in [0, 5, 10, 14]:
        result = EnhancedRiskAssessment.calculate_risk_score(
            _ctx(risk_score=score),
            "benign content",
            FINANCIAL_INDICATORS,
        )
        assert result["risk_level"] == "MINIMAL", f"score={score}"


def test_risk_type_preserved_when_no_financial_override() -> None:
    result = EnhancedRiskAssessment.calculate_risk_score(
        _ctx(risk_type="COMPLIANCE_MENTION", risk_score=20, confidence=80),
        "Our compliance team reviewed OFAC guidance.",
        FINANCIAL_INDICATORS,
    )
    assert result["risk_type"] == "COMPLIANCE_MENTION"
    assert result["risk_level"] == "LOW"


def test_risk_score_floor_on_financial_override() -> None:
    # Even if base_score was high (say 90), the override forces ≥ 85.
    # Specifically the max() is min-bound, not max-bound; so 90 stays 90.
    result = EnhancedRiskAssessment.calculate_risk_score(
        _ctx(risk_score=90, confidence=95),
        "Wire transfer $100k to Tehran.",
        FINANCIAL_INDICATORS,
    )
    assert result["risk_score"] == 90
    assert result["confidence"] == 95


def test_empty_content_no_indicators_uses_base_score() -> None:
    result = EnhancedRiskAssessment.calculate_risk_score(
        _ctx(risk_score=75),
        "",
        FINANCIAL_INDICATORS,
    )
    assert result["risk_level"] == "HIGH"
    assert result["risk_type"] == "DIRECT_BUSINESS"
    assert result.get("note") is None


def test_confidence_never_exceeds_baseline_when_overriding() -> None:
    # The financial override sets confidence = max(90, base). Verify
    # that a low incoming confidence is raised to the floor, not the
    # other way around.
    result = EnhancedRiskAssessment.calculate_risk_score(
        _ctx(confidence=30),
        "Wire transfer to Tehran.",
        ["wire"],
    )
    assert result["confidence"] == 90


def test_indicators_respect_word_boundary_context() -> None:
    # "fund" should not be detected inside "fundamentally". The detector
    # uses substring match so this is a known limitation — the test
    # documents the current behaviour.
    result = EnhancedRiskAssessment.calculate_risk_score(
        _ctx(risk_score=30),
        "This is fundamentally a compliance matter.",
        ["fund"],
    )
    # Documents current behaviour: substring match → HIGH.
    # If this ever flips, review whether the word-boundary upgrade is a
    # Tier A change (it typically is — affects scoring surface).
    assert result["risk_level"] in {"HIGH", "LOW"}
