"""
Schema shape + normalization. Guards the contract between the prompt
builder, the LLM, and the frontend.
"""

import pytest
from pydantic import ValidationError

from schemas import (
    CONFIDENCE_BAND_VALUES,
    RECOMMENDATION_VALUES,
    Citation,
    Claim,
    InvestigatorBrief,
)


def _c(src="src_abc", exc="exc_123"):
    return Citation(source_id=src, excerpt_id=exc)


def test_claim_requires_at_least_one_citation():
    with pytest.raises(ValidationError):
        Claim(text="hello", citations=[])


def test_claim_accepts_citation():
    c = Claim(text="Company exports to Tehran.", citations=[_c()])
    assert c.citations[0].source_id == "src_abc"


def test_claim_text_must_not_be_empty():
    with pytest.raises(ValidationError):
        Claim(text="", citations=[_c()])


def test_brief_minimal_construction():
    brief = InvestigatorBrief(
        recommendation="ESCALATE_FOR_REVIEW",
        confidence_band="MEDIUM",
    )
    assert brief.summary_claims == []
    assert brief.unverified_claims_dropped == 0


def test_brief_normalize_unknown_recommendation_falls_back():
    brief = InvestigatorBrief(recommendation="VIOLATION LIKELY", confidence_band="HIGH")
    normalized = brief.normalize()
    assert normalized.recommendation == "INSUFFICIENT_DATA"
    assert normalized.confidence_band == "HIGH"


def test_brief_normalize_lowercases_and_spaces():
    brief = InvestigatorBrief(
        recommendation="escalate for review", confidence_band="high"
    )
    normalized = brief.normalize()
    assert normalized.recommendation == "ESCALATE_FOR_REVIEW"
    assert normalized.confidence_band == "HIGH"


def test_brief_normalize_unknown_confidence_falls_back():
    brief = InvestigatorBrief(recommendation="ESCALATE_FOR_REVIEW", confidence_band="MEDIUMISH")
    normalized = brief.normalize()
    assert normalized.confidence_band == "LOW"


def test_recommendation_vocabulary():
    assert "ESCALATE_FOR_REVIEW" in RECOMMENDATION_VALUES
    assert "NO_FURTHER_ACTION_RECOMMENDED" in RECOMMENDATION_VALUES
    # 'VIOLATION LIKELY' (old verdict label) must NOT be in the new vocab.
    assert "VIOLATION LIKELY" not in RECOMMENDATION_VALUES


def test_confidence_band_vocabulary():
    assert set(CONFIDENCE_BAND_VALUES) == {"HIGH", "MEDIUM", "LOW"}


def test_llm_schema_is_a_valid_dict():
    schema = InvestigatorBrief.model_json_schema_for_llm()
    assert schema["type"] == "object"
    # Required fields for the LLM must include all three claim lists.
    for key in ("recommendation", "confidence_band", "summary_claims",
                "risk_factor_claims", "suggested_next_steps"):
        assert key in schema["required"]
    # Nested claim schema enforces >=1 citation.
    claim_items = schema["properties"]["summary_claims"]["items"]
    assert claim_items["properties"]["citations"]["minItems"] == 1
