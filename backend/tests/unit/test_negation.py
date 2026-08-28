"""Unit tests for the 6-word backward negation window.

Negation handling is a known failure mode for rule-based compliance
NLP. "We do not ship to Iran" must not produce the same risk tier as
"We ship to Iran." These tests lock in the current window size and
trigger set — changes are Tier A per ``compliance/governance.md``.
"""
from __future__ import annotations

import pytest

from sanctions_engine import EnhancedRiskAssessment


# ---------------------------------------------------------------------------
# _is_negated — the core detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,indicator", [
    ("we do not accept payments from sanctioned regions", "payment"),
    ("the firm does not process payments for Iran", "payment"),
    ("no wire transfers are permitted under this programme", "wire"),
    ("shall not remit funds to restricted counterparties", "remit"),
    ("prohibited to accept funds from embargoed countries", "fund"),
    ("cannot process payments to the Tehran office", "payment"),
    ("we never remit to sanctioned jurisdictions", "remit"),
])
def test_negated_patterns_are_detected(text, indicator) -> None:
    assert EnhancedRiskAssessment._is_negated(text, indicator) is True


@pytest.mark.parametrize("text,indicator", [
    ("the contract includes wire transfer provisions", "wire"),
    ("payments are processed quarterly", "payment"),
    ("we remit funds to counterparties globally", "remit"),
    ("funds are available on demand", "fund"),
])
def test_unnegated_patterns_are_not_flagged(text, indicator) -> None:
    assert EnhancedRiskAssessment._is_negated(text, indicator) is False


def test_negation_window_detects_close_negation() -> None:
    # Five words between "not" and "payment" — comfortably in window.
    in_window = "we do not really ever generally process payment requests from them"
    assert EnhancedRiskAssessment._is_negated(in_window, "payment") is True


def test_bidirectional_negation_window() -> None:
    # A5: the window is now symmetric — negation that appears AFTER the
    # indicator (within _NEGATION_WINDOW words) also counts. This intentionally
    # accepts a small false-positive band so the bigger false-negative class
    # ("Funding to Iran is prohibited") is caught.
    assert EnhancedRiskAssessment._is_negated(
        "Funding to Iran is prohibited by internal policy.", "funding"
    ) is True
    # And of course a sentence with no nearby negation is still unnegated.
    assert EnhancedRiskAssessment._is_negated(
        "Payment requests are processed on a quarterly cycle.", "payment"
    ) is False


def test_negation_is_case_insensitive() -> None:
    assert EnhancedRiskAssessment._is_negated(
        "We Do NOT Process Payments", "payment"
    ) is True


def test_negation_without_indicator_returns_false() -> None:
    # No indicator match → nothing to negate → False.
    assert EnhancedRiskAssessment._is_negated(
        "we do not accept cookies", "payment"
    ) is False


# ---------------------------------------------------------------------------
# has_active_financial_indicator — the composite check used by the scorer
# ---------------------------------------------------------------------------


def test_active_indicator_when_present_and_unnegated() -> None:
    assert EnhancedRiskAssessment.has_active_financial_indicator(
        "Quarterly wire transfers processed on schedule.",
        ["wire", "transfer"],
    ) is True


def test_indicator_suppressed_when_negated_within_window() -> None:
    assert EnhancedRiskAssessment.has_active_financial_indicator(
        "We do not initiate wire transfers for sanctioned parties.",
        ["wire"],
    ) is False


def test_any_indicator_suffices_for_true() -> None:
    # One unnegated indicator is enough to return True. We space the two
    # indicators far enough apart that A5's bidirectional window can't reach
    # "wire" from the later "not remit" clause.
    text = (
        "Wire transfers run daily across the treasury desk, feeding the "
        "downstream settlement system. In a completely separate policy note, "
        "the firm does not remit funds to sanctioned regions under any license."
    )
    assert EnhancedRiskAssessment.has_active_financial_indicator(
        text, ["wire", "remit"]
    ) is True


def test_currency_symbol_respects_negation() -> None:
    # A6 removed the currency short-circuit — ``$``, ``€``, ``£`` now go
    # through the same negation-aware path as text indicators. A8 (A5) made
    # the negation scan bidirectional, so a preceding ``not`` suppresses a
    # following currency symbol just like any other term.
    assert EnhancedRiskAssessment.has_active_financial_indicator(
        "We do not accept $ payments from sanctioned regions.", ["$"]
    ) is False


def test_currency_symbol_fires_when_unnegated() -> None:
    # Counterfactual: no negation in the window → currency symbol still counts
    # as an active financial indicator.
    assert EnhancedRiskAssessment.has_active_financial_indicator(
        "We invoice clients in $ and ship globally.", ["$"]
    ) is True


def test_empty_content_returns_false() -> None:
    assert EnhancedRiskAssessment.has_active_financial_indicator(
        "", ["wire", "payment"]
    ) is False


def test_indicator_not_in_content_returns_false() -> None:
    assert EnhancedRiskAssessment.has_active_financial_indicator(
        "A purely narrative paragraph with no financial terms at all.",
        ["wire", "remit", "payment"],
    ) is False


# ---------------------------------------------------------------------------
# Interaction with calculate_risk_score — the end-to-end negation path
# ---------------------------------------------------------------------------


def test_negation_prevents_financial_high_escalation() -> None:
    """End-to-end: a medium-risk context with a negated indicator must
    stay at the base tier rather than jumping to HIGH via the financial
    override."""
    context_analysis = {
        "risk_type": "INDIRECT_BUSINESS",
        "risk_score": 50,
        "confidence": 70,
        "sentence": "The firm does not process payments for Iran.",
    }
    result = EnhancedRiskAssessment.calculate_risk_score(
        context_analysis,
        "The firm does not process payments for Iran.",
        ["payment"],  # no currency symbols
    )
    assert result["risk_level"] == "MEDIUM"
    assert result["risk_type"] == "INDIRECT_BUSINESS"  # preserved, not FINANCIAL_TRANSACTION


def test_unnegated_indicator_escalates_to_high() -> None:
    """Counterfactual of the previous test: without negation, the same
    base score should escalate to HIGH via the financial override."""
    context_analysis = {
        "risk_type": "INDIRECT_BUSINESS",
        "risk_score": 50,
        "confidence": 70,
        "sentence": "The firm processes payments for Iran.",
    }
    result = EnhancedRiskAssessment.calculate_risk_score(
        context_analysis,
        "The firm processes payments for Iran quarterly.",
        ["payment"],
    )
    assert result["risk_level"] == "HIGH"
    assert result["risk_type"] == "FINANCIAL_TRANSACTION"
