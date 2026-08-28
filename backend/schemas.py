"""
Pydantic schemas for the Investigator Brief (Phase 2).

The brief replaces the previous free-form "verdict" with a citation-grounded
structure. Every claim the LLM makes must reference at least one excerpt from
the evidence set so that a regulator can click any assertion and land on the
specific retrieved text that supports it.

Stable source_id / excerpt_id hash helpers live here so the prompt builder,
the LLM response parser, and the verifier all compute them the same way.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional

from pydantic import BaseModel, Field, conlist


# ---------------------------------------------------------------------------
# Deterministic ID helpers
# ---------------------------------------------------------------------------

def stable_source_id(url: str) -> str:
    """sha256 of the URL, truncated to 16 chars. Deterministic across runs."""
    if not url:
        return "src_empty"
    digest = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()
    return f"src_{digest[:16]}"


def stable_excerpt_id(url: str, trigger_sentence: str, index: int) -> str:
    """
    sha256 of (url, trigger_sentence, index), truncated. The index
    disambiguates multiple excerpts that might share a trigger.
    """
    key = f"{url or ''}||{trigger_sentence or ''}||{index}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"exc_{digest[:16]}"


# ---------------------------------------------------------------------------
# Brief schema
# ---------------------------------------------------------------------------

RECOMMENDATION_VALUES = (
    "ESCALATE_FOR_REVIEW",
    "ADDITIONAL_OSINT_NEEDED",
    "NO_FURTHER_ACTION_RECOMMENDED",
    "INSUFFICIENT_DATA",
)

CONFIDENCE_BAND_VALUES = ("HIGH", "MEDIUM", "LOW")


class Citation(BaseModel):
    """A pointer from a claim back to a specific excerpt in a specific source."""

    source_id: str = Field(..., description="Stable ID of the source URL")
    excerpt_id: str = Field(..., description="Stable ID of the excerpt within that source")


class Claim(BaseModel):
    """A single assertion. Must cite at least one excerpt."""

    text: str = Field(..., min_length=1)
    citations: conlist(Citation, min_length=1) = Field(
        ...,
        description="At least one citation is required. The LLM must never make a bare claim.",
    )


class InvestigatorBrief(BaseModel):
    """
    Replaces the prior free-form verdict. The LLM never 'clears' anything;
    it produces a structured recommendation with every claim anchored to
    retrieved evidence.
    """

    recommendation: str = Field(
        ...,
        description=(
            "One of: ESCALATE_FOR_REVIEW, ADDITIONAL_OSINT_NEEDED, "
            "NO_FURTHER_ACTION_RECOMMENDED, INSUFFICIENT_DATA"
        ),
    )
    confidence_band: str = Field(..., description="HIGH, MEDIUM, or LOW")
    summary_claims: List[Claim] = Field(default_factory=list)
    risk_factor_claims: List[Claim] = Field(default_factory=list)
    suggested_next_steps: List[Claim] = Field(default_factory=list)
    unverified_claims_dropped: int = Field(
        default=0,
        description="Claims whose citations failed post-verification and were removed.",
    )

    @classmethod
    def model_json_schema_for_llm(cls) -> dict:
        """
        Produce a schema object suitable for passing to the google-genai
        response_schema parameter. google-genai prefers a simplified schema
        shape (no $ref), so we flatten here.
        """
        return {
            "type": "object",
            "properties": {
                "recommendation": {
                    "type": "string",
                    "enum": list(RECOMMENDATION_VALUES),
                },
                "confidence_band": {
                    "type": "string",
                    "enum": list(CONFIDENCE_BAND_VALUES),
                },
                "summary_claims": _claim_list_schema(),
                "risk_factor_claims": _claim_list_schema(),
                "suggested_next_steps": _claim_list_schema(),
            },
            "required": [
                "recommendation",
                "confidence_band",
                "summary_claims",
                "risk_factor_claims",
                "suggested_next_steps",
            ],
        }

    def normalize(self) -> "InvestigatorBrief":
        """
        Coerce recommendation/confidence_band into the allowed vocabulary.
        Unknown values fall through to INSUFFICIENT_DATA / LOW. Called
        defensively after parsing a potentially-loose model response.
        """
        rec = (self.recommendation or "").strip().upper().replace(" ", "_")
        if rec not in RECOMMENDATION_VALUES:
            rec = "INSUFFICIENT_DATA"
        band = (self.confidence_band or "").strip().upper()
        if band not in CONFIDENCE_BAND_VALUES:
            band = "LOW"
        return self.model_copy(update={"recommendation": rec, "confidence_band": band})


def _claim_list_schema() -> dict:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "citations": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {"type": "string"},
                            "excerpt_id": {"type": "string"},
                        },
                        "required": ["source_id", "excerpt_id"],
                    },
                },
            },
            "required": ["text", "citations"],
        },
    }


# ---------------------------------------------------------------------------
# Evidence set & verification result
# ---------------------------------------------------------------------------

class EvidenceExcerpt(BaseModel):
    """
    A single excerpt passed into the LLM and available for citation.
    The (source_id, excerpt_id) pair is the stable handle; text is the
    substance the verifier matches against.
    """

    source_id: str
    excerpt_id: str
    url: str
    text: str
    country: Optional[str] = None
    risk_type: Optional[str] = None
    confidence: Optional[float] = None


class ClaimVerification(BaseModel):
    """Outcome for a single claim."""

    claim_text: str
    verified: bool
    reason: str
    matched_excerpt_ids: List[str] = Field(default_factory=list)


class VerificationReport(BaseModel):
    """Aggregate outcome from running the verifier over a brief."""

    total_claims: int
    verified_claims: int
    dropped_claims: int
    per_claim: List[ClaimVerification] = Field(default_factory=list)
