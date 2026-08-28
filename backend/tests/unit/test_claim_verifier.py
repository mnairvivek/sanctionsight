"""
Post-verification of LLM claims. These tests are the safety net that keeps
hallucinated citations from reaching the analyst. The four scenarios from
the Phase 2 verification plan each have their own test.
"""

from claim_verifier import verify_brief, verify_claim
from schemas import (
    Citation,
    Claim,
    EvidenceExcerpt,
    InvestigatorBrief,
)


def _excerpt(excerpt_id="exc_1", source_id="src_1", text="placeholder"):
    return EvidenceExcerpt(
        source_id=source_id,
        excerpt_id=excerpt_id,
        url="https://example.com",
        text=text,
    )


def _brief(summary=None, factors=None, steps=None):
    return InvestigatorBrief(
        recommendation="ESCALATE_FOR_REVIEW",
        confidence_band="MEDIUM",
        summary_claims=summary or [],
        risk_factor_claims=factors or [],
        suggested_next_steps=steps or [],
    )


# ---------------------------------------------------------------------------
# Scenario (a): valid citation — high token overlap between claim and excerpt.
# ---------------------------------------------------------------------------

def test_verified_claim_with_valid_citation():
    excerpt = _excerpt(
        text="The company exports industrial machinery to Tehran and Isfahan.",
    )
    evidence = {excerpt.excerpt_id: excerpt}

    claim = Claim(
        text="Company exports industrial machinery to Tehran.",
        citations=[Citation(source_id=excerpt.source_id, excerpt_id=excerpt.excerpt_id)],
    )

    result = verify_claim(claim, evidence)
    assert result.verified is True
    assert excerpt.excerpt_id in result.matched_excerpt_ids


# ---------------------------------------------------------------------------
# Scenario (b): hallucinated excerpt_id — LLM cited an ID not in the evidence.
# ---------------------------------------------------------------------------

def test_claim_with_hallucinated_excerpt_id_is_dropped():
    excerpt = _excerpt(excerpt_id="exc_real", text="The company exports to Tehran.")
    evidence = {excerpt.excerpt_id: excerpt}

    claim = Claim(
        text="Company exports to Tehran.",
        citations=[Citation(source_id="src_1", excerpt_id="exc_FAKE")],
    )

    result = verify_claim(claim, evidence)
    assert result.verified is False
    assert "no_known_citations" in result.reason


# ---------------------------------------------------------------------------
# Scenario (c): paraphrase above threshold — different words, same substance.
# The overlap threshold (0.4) plus the content-token filter should accept this.
# ---------------------------------------------------------------------------

def test_paraphrased_claim_above_threshold_is_verified():
    excerpt = _excerpt(
        text=(
            "Acme Industries announced a joint venture with an Iranian oil "
            "refinery based in Tehran, including machinery exports."
        ),
    )
    evidence = {excerpt.excerpt_id: excerpt}

    # Paraphrase: reuses the high-signal content tokens (acme, joint, venture,
    # iranian, tehran, machinery, exports) in a different order.
    claim = Claim(
        text="Acme pursued an Iranian joint venture involving machinery exports near Tehran.",
        citations=[Citation(source_id=excerpt.source_id, excerpt_id=excerpt.excerpt_id)],
    )

    result = verify_claim(claim, evidence)
    assert result.verified is True


# ---------------------------------------------------------------------------
# Scenario (d): paraphrase below threshold — semantically related but no shared
# content tokens. Verifier MUST drop it. This is the hallucination case.
# ---------------------------------------------------------------------------

def test_paraphrased_claim_below_threshold_is_dropped():
    excerpt = _excerpt(text="The firm shipped crude petroleum via Bandar Abbas port.")
    evidence = {excerpt.excerpt_id: excerpt}

    # No shared content tokens with the excerpt. Sounds plausible, unsupported.
    claim = Claim(
        text="Executives attended a diplomatic summit in Geneva last winter.",
        citations=[Citation(source_id=excerpt.source_id, excerpt_id=excerpt.excerpt_id)],
    )

    result = verify_claim(claim, evidence)
    assert result.verified is False
    assert "no_citation_supported_text" in result.reason


# ---------------------------------------------------------------------------
# Brief-level behavior: counts, drops, and that Pydantic construction of a
# brief with mixed claims survives the pipeline cleanly.
# ---------------------------------------------------------------------------

def test_verify_brief_drops_unverified_and_increments_counter():
    good = _excerpt(
        excerpt_id="exc_good",
        text="The company exports machinery to Tehran and Isfahan.",
    )
    evidence = {good.excerpt_id: good}

    kept_claim = Claim(
        text="Company exports machinery to Tehran.",
        citations=[Citation(source_id=good.source_id, excerpt_id=good.excerpt_id)],
    )
    hallucinated_claim = Claim(
        text="Company attended a Geneva summit last winter.",
        citations=[Citation(source_id=good.source_id, excerpt_id=good.excerpt_id)],
    )

    brief = _brief(
        summary=[kept_claim, hallucinated_claim],
        factors=[hallucinated_claim],
    )

    filtered, report = verify_brief(brief, evidence)

    assert len(filtered.summary_claims) == 1
    assert filtered.summary_claims[0].text == kept_claim.text
    assert filtered.risk_factor_claims == []
    assert filtered.unverified_claims_dropped == 2
    assert report.total_claims == 3
    assert report.verified_claims == 1
    assert report.dropped_claims == 2


def test_verify_brief_with_empty_evidence_drops_everything():
    claim = Claim(
        text="Company exports to Tehran.",
        citations=[Citation(source_id="src_1", excerpt_id="exc_1")],
    )
    brief = _brief(summary=[claim])

    filtered, report = verify_brief(brief, evidence={})

    assert filtered.summary_claims == []
    assert filtered.unverified_claims_dropped == 1
    assert report.dropped_claims == 1


def test_substring_match_catches_quoted_phrase():
    """
    A claim that quotes a contiguous phrase from the excerpt should verify
    via the longest-run path even if overall token overlap is below threshold.
    """
    excerpt = _excerpt(
        text=(
            "In 2023 the Treasury added the firm to the Specially Designated "
            "Nationals list following an investigation into procurement networks."
        ),
    )
    evidence = {excerpt.excerpt_id: excerpt}

    claim = Claim(
        text='Quoted phrase: "added to the Specially Designated Nationals list" appears verbatim.',
        citations=[Citation(source_id=excerpt.source_id, excerpt_id=excerpt.excerpt_id)],
    )

    result = verify_claim(claim, evidence)
    assert result.verified is True
