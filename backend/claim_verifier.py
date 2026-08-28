"""
Post-verification of LLM-produced claims against the evidence set.

Rules (fail-closed drop-and-count):
  1. Every citation.excerpt_id must exist in the evidence set. Citations
     that reference unknown IDs are discarded. A claim with no valid
     citations is dropped.
  2. The claim text must have token-overlap >= TOKEN_OVERLAP_THRESHOLD with
     at least one of its cited excerpts, OR contain a substring (>= 5 tokens)
     of one of its cited excerpts. Otherwise the claim is dropped.
  3. A dropped claim is recorded in the VerificationReport; it is not
     shown to the user. The brief is returned with unverified_claims_dropped
     incremented.

No embeddings, no network. Pure stdlib so the verifier can run in CI.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Tuple

from schemas import (
    Claim,
    ClaimVerification,
    EvidenceExcerpt,
    InvestigatorBrief,
    VerificationReport,
)


TOKEN_OVERLAP_THRESHOLD = 0.4  # Jaccard-like overlap on content tokens
MIN_SUBSTRING_TOKENS = 5       # continuous run of shared tokens considered a match


_WORD_RE = re.compile(r"[A-Za-z0-9'\-]+")

# Very common English words that dilute overlap scoring without adding signal.
# Kept small on purpose — this is verification, not retrieval.
_STOPWORDS = frozenset(
    """a an and are as at be been being but by could did do does doing done for
    from had has have having he her hers him his how i if in into is it its
    itself me my myself of on or our ours ourselves out over own should so such
    than that the their theirs them themselves then there these they this those
    to too under until up was we were what when where which while who whom why
    will with would you your yours yourself""".split()
)


def _tokenize(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _content_tokens(text: str) -> List[str]:
    return [t for t in _tokenize(text) if t not in _STOPWORDS and len(t) > 1]


def _token_overlap(a_tokens: Iterable[str], b_tokens: Iterable[str]) -> float:
    """Jaccard-like overlap on content tokens."""
    a_set = set(a_tokens)
    b_set = set(b_tokens)
    if not a_set or not b_set:
        return 0.0
    intersection = a_set & b_set
    # Asymmetric: how much of the claim is supported by the excerpt?
    # We care that the claim's words appear in the excerpt, not the reverse.
    return len(intersection) / len(a_set)


def _longest_substring_tokens(claim_tokens: List[str], excerpt_tokens: List[str]) -> int:
    """
    Longest continuous run of claim tokens that also appears as a contiguous
    subsequence in the excerpt. O(n*m) — fine for sentence-length claims.
    """
    if not claim_tokens or not excerpt_tokens:
        return 0
    best = 0
    m, n = len(claim_tokens), len(excerpt_tokens)
    # Sliding compare
    for i in range(m):
        for j in range(n):
            k = 0
            while (
                i + k < m
                and j + k < n
                and claim_tokens[i + k] == excerpt_tokens[j + k]
            ):
                k += 1
            if k > best:
                best = k
    return best


def _verify_claim_against_excerpt(
    claim_text: str, excerpt_text: str
) -> Tuple[bool, str]:
    """Return (matched, reason) for a single (claim, excerpt) pair."""
    c_tokens = _content_tokens(claim_text)
    e_tokens = _content_tokens(excerpt_text)
    overlap = _token_overlap(c_tokens, e_tokens)
    if overlap >= TOKEN_OVERLAP_THRESHOLD:
        return True, f"token_overlap={overlap:.2f}"
    run = _longest_substring_tokens(c_tokens, e_tokens)
    if run >= MIN_SUBSTRING_TOKENS:
        return True, f"shared_run={run}_tokens"
    return False, f"overlap={overlap:.2f}, max_run={run}"


def verify_claim(
    claim: Claim, evidence: Dict[str, EvidenceExcerpt]
) -> ClaimVerification:
    """
    Check a single claim against the evidence map keyed by excerpt_id.
    """
    matched_ids: List[str] = []
    reasons: List[str] = []

    known_citations = [c for c in claim.citations if c.excerpt_id in evidence]
    if not known_citations:
        return ClaimVerification(
            claim_text=claim.text,
            verified=False,
            reason="no_known_citations",
        )

    for citation in known_citations:
        excerpt = evidence[citation.excerpt_id]
        ok, reason = _verify_claim_against_excerpt(claim.text, excerpt.text)
        if ok:
            matched_ids.append(citation.excerpt_id)
            reasons.append(f"{citation.excerpt_id}:{reason}")

    if matched_ids:
        return ClaimVerification(
            claim_text=claim.text,
            verified=True,
            reason="; ".join(reasons),
            matched_excerpt_ids=matched_ids,
        )

    return ClaimVerification(
        claim_text=claim.text,
        verified=False,
        reason="no_citation_supported_text",
    )


def verify_brief(
    brief: InvestigatorBrief, evidence: Dict[str, EvidenceExcerpt]
) -> Tuple[InvestigatorBrief, VerificationReport]:
    """
    Run the verifier over every claim in every section of the brief.
    Unverified claims are dropped. Returns the filtered brief and a report.
    """
    per_claim: List[ClaimVerification] = []

    def _filter(claims: List[Claim]) -> List[Claim]:
        kept: List[Claim] = []
        for c in claims:
            result = verify_claim(c, evidence)
            per_claim.append(result)
            if result.verified:
                kept.append(c)
        return kept

    kept_summary = _filter(brief.summary_claims)
    kept_factors = _filter(brief.risk_factor_claims)
    kept_steps = _filter(brief.suggested_next_steps)

    total = len(brief.summary_claims) + len(brief.risk_factor_claims) + len(brief.suggested_next_steps)
    verified = len(kept_summary) + len(kept_factors) + len(kept_steps)
    dropped = total - verified

    new_brief = brief.model_copy(
        update={
            "summary_claims": kept_summary,
            "risk_factor_claims": kept_factors,
            "suggested_next_steps": kept_steps,
            "unverified_claims_dropped": brief.unverified_claims_dropped + dropped,
        }
    )

    report = VerificationReport(
        total_claims=total,
        verified_claims=verified,
        dropped_claims=dropped,
        per_claim=per_claim,
    )
    return new_brief, report
