"""
Enhanced Sanctions Site Search Tool v2.3
-----------------------------------------
Fixes applied vs v2.2:
  1.  API credentials loaded from environment variables (never hardcoded).
  2.  HTML-injection fix: html.escape() applied BEFORE regex highlighting in reports.
  3.  Financial risk scoring now checks for negation in context (no more blanket HIGH on any
      "fund"/"support" mention that is negated/prohibited).
  4.  False-positive context now correctly returns relevant=False to exclude results.
  5.  Hardcoded "Iran" in NameCooccurrenceSearcher._analyze_url replaced with a shared,
      country-agnostic URL fetcher.
  6.  Exponential-backoff retry decorator replaces fixed time.sleep() on all HTTP calls.
  7.  Python logging module replaces raw print() throughout.
  8.  SanctionsContentAnalyzer instances are cached per-country (no repeated construction).
  9.  Report trigger-regexes compiled once at report-generation time (not rebuilt per-excerpt).
 10.  result titles/snippets always html.escape()'d before HTML insertion.
"""

import concurrent.futures
import html
import json
import logging
import os
import re
import sys
import functools
import tempfile
import time
import threading
import webbrowser
import warnings
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import spacy
import trafilatura
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

# Optional Streamlit integration
try:
    import streamlit as st
    import streamlit.components.v1 as components
except Exception:
    st = None
    components = None

# Optional Gemma 4 verdict (pip install google-genai)
try:
    from google import genai as _google_genai
except Exception as _genai_import_err:
    _google_genai = None
    print(f"[WARNING] google-genai import failed: {_genai_import_err}")

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sanctions_tool")

# ---------------------------------------------------------------------------
# API Credentials – set the corresponding environment variables.
#
#   API_KEY              : Google Custom Search JSON API key
#                          https://console.cloud.google.com/apis/credentials
#   SEARCH_ENGINE_ID     : Programmable Search Engine ID
#                          https://programmablesearchengine.google.com/
#
# LLM credentials — the engine accepts either Vertex AI (preferred, billed
# against your GCP project) or a Google AI Studio API key (free tier):
#
#   GOOGLE_CLOUD_PROJECT  : enables Vertex AI mode when set. Uses ADC from
#                           `gcloud auth application-default login` — no key.
#   GOOGLE_CLOUD_LOCATION : Vertex region (default us-central1).
#   GOOGLE_GENAI_API_KEY  : fallback AI Studio key when Vertex is not configured
#                           https://aistudio.google.com/apikey
# ---------------------------------------------------------------------------
API_KEY              = os.environ.get("GOOGLE_API_KEY", "")
SEARCH_ENGINE_ID     = os.environ.get("GOOGLE_CSE_ID", "")
GOOGLE_GENAI_API_KEY = os.environ.get("GOOGLE_GENAI_API_KEY", "")

VERTEX_PROJECT  = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
VERTEX_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
USE_VERTEX      = bool(VERTEX_PROJECT)

LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-2.5-flash")

warnings.filterwarnings("ignore")

# ---------------------------------------------------------------------------
# spaCy model (loaded once at module level)
#
# Default: en_core_web_lg (better NER + sentence boundaries than sm).
# Override via env var SANCTIONSIGHT_SPACY_MODEL — set to `en_core_web_trf`
# for transformer-grade accuracy (slower, ~460MB) when compute allows, or
# fall back to `en_core_web_sm` for constrained dev environments.
# ---------------------------------------------------------------------------
SPACY_MODEL = os.environ.get("SANCTIONSIGHT_SPACY_MODEL", "en_core_web_lg")


def _load_spacy_model(preferred: str):
    """Load the preferred model, auto-downloading on first use.

    Falls back through trf → lg → sm if the preferred model is unavailable so
    the engine never dies on a fresh checkout — the worst case is reduced NER
    quality, which is logged loudly so operators notice.

    GPU: ``spacy.prefer_gpu()`` is called *before* the first load. It's a
    no-op on CPU boxes and enables cuDNN pipelines on the GCP deploy where
    CUDA + torch are available. Transformer models (``en_core_web_trf``) get
    the biggest win here — 5–10× batched throughput.
    """
    # Best-effort GPU hand-off. Safe no-op when no CUDA device is present.
    try:
        if spacy.prefer_gpu():  # type: ignore[attr-defined]
            logger.info("spaCy using GPU acceleration")
    except Exception as exc:
        logger.debug("spaCy GPU not available (%s) — continuing on CPU", exc)

    fallback_chain = [preferred]
    if preferred != "en_core_web_lg":
        fallback_chain.append("en_core_web_lg")
    if "en_core_web_sm" not in fallback_chain:
        fallback_chain.append("en_core_web_sm")

    last_exc: Optional[Exception] = None
    for model_name in fallback_chain:
        try:
            return spacy.load(model_name), model_name
        except OSError as exc:
            last_exc = exc
            logger.info("spaCy model %s not present — attempting download", model_name)
            rc = os.system(f"python -m spacy download {model_name}")
            if rc == 0:
                try:
                    return spacy.load(model_name), model_name
                except OSError as exc2:
                    last_exc = exc2
            logger.warning("spaCy model %s unavailable after download attempt", model_name)
    raise RuntimeError(
        f"Could not load any spaCy model from {fallback_chain}. Last error: {last_exc}"
    )


nlp, SPACY_MODEL_LOADED = _load_spacy_model(SPACY_MODEL)
if SPACY_MODEL_LOADED != SPACY_MODEL:
    logger.warning(
        "spaCy fell back from requested model %s to %s — NER quality may be reduced",
        SPACY_MODEL, SPACY_MODEL_LOADED,
    )

# Warm the pipeline at import time so the first in-flight job doesn't pay the
# cold-start cost (model compilation + CUDA kernel allocation for trf).
try:
    nlp("SanctionSight warmup.")
except Exception as _warmup_exc:  # pragma: no cover — defensive
    logger.debug("spaCy warmup failed (non-fatal): %s", _warmup_exc)

# ---------------------------------------------------------------------------
# Shared HTTP session (Phase 1 speed: connection pooling).
#
# trafilatura.fetch_url opens a fresh TCP+TLS connection per call, paying the
# full handshake (~100–300ms on cold hosts). A pooled requests.Session mounted
# with an HTTPAdapter amortises that across every fetch in a job — especially
# effective on runs that pull multiple articles from the same outlet, and on
# the cross-country case where the same host is hit repeatedly.
#
# Quality impact: none. Trafilatura's extract() accepts HTML bytes, so it sees
# the same content either way — only the fetcher changes.
# ---------------------------------------------------------------------------
_HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_http_session: Optional[requests.Session] = None
_http_session_lock = threading.Lock()


def _get_http_session() -> requests.Session:
    global _http_session
    if _http_session is None:
        with _http_session_lock:
            if _http_session is None:
                s = requests.Session()
                retry = Retry(
                    total=2,
                    backoff_factor=0.3,
                    status_forcelist=[500, 502, 503, 504],
                    allowed_methods=frozenset(["GET", "HEAD"]),
                )
                adapter = HTTPAdapter(
                    pool_connections=50,
                    pool_maxsize=50,
                    max_retries=retry,
                )
                s.mount("http://", adapter)
                s.mount("https://", adapter)
                s.headers.update({"User-Agent": _HTTP_USER_AGENT})
                _http_session = s
    return _http_session


# ---------------------------------------------------------------------------
# Cross-country URL extraction cache (Phase 1 speed: deduplicate fetches).
#
# A single URL is often surfaced by Google searches for multiple sanctioned
# jurisdictions (e.g. a news article mentioning both Iran and Syria). Without
# this cache, each country's searcher re-downloads + re-parses the same page.
# With the cache, the raw extracted text is fetched once and reused.
#
# Scope: per-job. main.py calls reset_extraction_cache() at job start so
# long-running dev servers don't leak memory or serve stale content across
# jobs (the same URL may legitimately change between runs).
#
# We cache the *raw inner-extraction* dict — NLP still re-runs per country
# against that country's variations. audit logging, content hashing, and
# language detection (all in the outer extract_content_from_url wrapper)
# still fire per-country so audit trails stay per-country-complete.
# ---------------------------------------------------------------------------
_extraction_cache: Dict[str, dict] = {}
_extraction_cache_lock = threading.Lock()


def _normalize_cache_key(url: str) -> str:
    """Normalise a URL for cache lookup.

    Lowercases scheme + host, preserves path + query (different query strings
    are different pages). Strips trailing slash on bare roots so ``a.com`` and
    ``a.com/`` hit the same cache entry.
    """
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        scheme = parsed.scheme.lower() or "https"
        path = parsed.path or ""
        key = f"{scheme}://{host}{path}"
        if parsed.query:
            key = f"{key}?{parsed.query}"
        return key
    except Exception:
        return url


def reset_extraction_cache() -> None:
    """Clear the per-job extraction cache. Call at job start and job end."""
    with _extraction_cache_lock:
        _extraction_cache.clear()


def _cache_get(url: str) -> Optional[dict]:
    key = _normalize_cache_key(url)
    with _extraction_cache_lock:
        entry = _extraction_cache.get(key)
    if entry is None:
        return None
    # Return a shallow copy so callers that mutate (e.g. adding content_hash)
    # don't pollute the cached entry.
    return dict(entry)


def _cache_put(url: str, result: dict) -> None:
    key = _normalize_cache_key(url)
    with _extraction_cache_lock:
        _extraction_cache[key] = dict(result)


# ---------------------------------------------------------------------------
# FIX 8: Analyzer cache – one instance per country, reused across all callers.
# Locked so the upcoming parallel country loop (B2) can share analyzers safely.
# Cache key includes the variations override so custom-country analyzers don't
# collide with built-in ones that happen to share a name.
# ---------------------------------------------------------------------------
_analyzer_cache: Dict[tuple, "SanctionsContentAnalyzer"] = {}
_analyzer_cache_lock = threading.Lock()


def get_analyzer(
    country: str,
    variations_override: Optional[List[str]] = None,
) -> "SanctionsContentAnalyzer":
    key = (country, tuple(variations_override) if variations_override else None)
    with _analyzer_cache_lock:
        analyzer = _analyzer_cache.get(key)
        if analyzer is None:
            analyzer = SanctionsContentAnalyzer(country, variations_override=variations_override)
            _analyzer_cache[key] = analyzer
    return analyzer


# ---------------------------------------------------------------------------
# FIX 6: Exponential-backoff retry decorator for HTTP-bound functions.
# ---------------------------------------------------------------------------
def with_retry(max_retries: int = 3, backoff_factor: float = 2.0, reraise: bool = True):
    """Decorator that retries a function on requests.RequestException with exp. backoff."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.RequestException as exc:
                    last_exc = exc
                    wait = backoff_factor ** attempt
                    logger.warning(
                        "Request failed (attempt %d/%d): %s. Retrying in %.1fs…",
                        attempt + 1, max_retries, exc, wait,
                    )
                    import time
                    import time as _time
                    _time.sleep(wait)
            if reraise and last_exc:
                raise last_exc
            return None
        return wrapper
    return decorator


# ==========================================================================
# Helper functions
# ==========================================================================

def normalize_for_match(s: str) -> str:
    s = s.lower().strip()
    s = s.replace("&", " and ")
    s = re.sub(r"[\u2010-\u2015\-_/.,:;!?\"'(){}\[\]]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


def fuzzy_name_in_text(text: str, name: str, threshold: int = 85) -> bool:
    if not text or not name:
        return False
    nt = normalize_for_match(text)
    nn = normalize_for_match(name)
    score = max(fuzz.partial_ratio(nn, nt), fuzz.token_set_ratio(nn, nt))
    return score >= threshold


def build_name_regex(name: str) -> str:
    if not name:
        return ""
    name = name.strip()
    name = name.replace("&", "(?:&|and)")
    esc = re.escape(name)
    esc = esc.replace("\\&", "(?:&|and)")
    pattern = re.sub(r"[^A-Za-z0-9]+", r"\\W+", esc)
    return r"(?i)\b" + pattern + r"\b"


def get_all_country_variations_map(
    countries: List[str],
    overrides: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, List[str]]:
    """Map country name → lowercased variations.

    ``overrides`` carries per-country user-supplied variation lists (from the
    country selector's custom-country input). When present for a country, the
    analyzer is fetched with that override so both the cmap and subsequent
    `get_analyzer(country)` calls return the same variation set.
    """
    overrides = overrides or {}
    cmap: Dict[str, List[str]] = {}
    for c in countries:
        analyzer = get_analyzer(c, variations_override=overrides.get(c))
        cmap[c] = list(set([c.lower()] + analyzer.country_variations))
    return cmap


def build_country_or_chunks(cmap: Dict[str, List[str]], chunk_size: int = 20) -> List[List[str]]:
    all_terms: List[str] = []
    for vars_ in cmap.values():
        all_terms.extend(vars_)
    all_terms = sorted(list(set(all_terms)))
    return [all_terms[i: i + chunk_size] for i in range(0, len(all_terms), chunk_size)]


def build_name_country_queries(
    name: str, cmap: Dict[str, List[str]], chunk_size: int = 20
) -> List[str]:
    if not name:
        return []
    name_phrase = f'"{name}"'
    queries: List[str] = []
    for terms in build_country_or_chunks(cmap, chunk_size=chunk_size):
        ors = " OR ".join([f'"{t}"' if " " in t else t for t in terms])
        queries.append(f"({name_phrase}) ({ors})")
    return queries


# ==========================================================================
# Phase 1: Content snapshot storage (retained evidence for 7-yr audit trail)
# ==========================================================================

import gzip  # noqa: E402 — grouped with Phase 1 snapshot helpers
import hashlib  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_SNAPSHOTS_DIR_ENV = "SANCTIONSIGHT_SNAPSHOTS_DIR"
_DEFAULT_SNAPSHOTS_DIR = _Path(__file__).resolve().parent / "snapshots"


def _snapshots_dir() -> _Path:
    override = os.environ.get(_SNAPSHOTS_DIR_ENV)
    d = _Path(override) if override else _DEFAULT_SNAPSHOTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


# Phase 5: lightweight language detection so non-English sources get an
# honest badge in the UI instead of silently getting run through an
# English-only NER pipeline. langdetect is non-deterministic by default —
# seeding it gives reproducible results within a job.
_LANGDETECT_AVAILABLE = True
try:
    from langdetect import detect as _langdetect_detect
    from langdetect import DetectorFactory as _LangDetectorFactory

    _LangDetectorFactory.seed = 0
except ImportError:
    _LANGDETECT_AVAILABLE = False


def detect_language(text: Optional[str], *, min_chars: int = 40) -> Optional[str]:
    """Best-effort ISO-639-1 code for ``text``.

    Returns ``None`` when langdetect is missing, the sample is too short
    to be reliable, or detection fails — callers treat ``None`` as
    "unknown" rather than "English" so short English snippets don't get
    silently downgraded.
    """
    if not text or not _LANGDETECT_AVAILABLE:
        return None
    sample = text.strip()
    if len(sample) < min_chars:
        return None
    try:
        return _langdetect_detect(sample)
    except Exception:
        return None


def _store_snapshot(text: str) -> str:
    """Gzip-store extracted content keyed by sha256(text). Returns the hash."""
    digest = _hash_text(text)
    path = _snapshots_dir() / f"{digest}.txt.gz"
    if not path.exists():
        try:
            with gzip.open(path, "wt", encoding="utf-8") as fh:
                fh.write(text)
        except Exception as exc:
            logger.warning("Snapshot write failed for %s: %s", digest[:8], exc)
    return digest


# ==========================================================================
# FIX 3 & 4: Enhanced risk assessment with negation awareness
# ==========================================================================

class EnhancedRiskAssessment:
    """Risk assessment with negation-aware financial indicator detection."""

    # Negation words that cancel a financial indicator
    _NEGATION_WORDS = (
        "no", "not", "never", "prohibited", "forbidden", "illegal",
        "avoid", "exclude", "except", "cannot", "can't", "won't",
        "wouldn't", "don't", "doesn't", "do not", "does not",
        "will not", "shall not", "must not",
    )

    # Max words between a negation and an indicator to count as negated
    _NEGATION_WINDOW = 6

    @classmethod
    def _is_negated(cls, text: str, indicator: str) -> bool:
        """
        Return True if *indicator* appears to be negated in *text*.

        Bidirectional (A5): scans a window of ±_NEGATION_WINDOW words on both
        sides of every indicator occurrence. Also considers indicator-as-
        substring hits (currency symbols like ``$`` don't word-tokenize).
        Backward catches "We do not accept payments"; forward catches
        "Funding to Iran is prohibited".
        """
        text_lower = text.lower()
        ind_lower = indicator.lower()
        words = re.findall(r"\b\w[\w']*\b", text_lower)

        # Word-indexed pass — handles alphabetic indicators.
        hit_idxs = [i for i, w in enumerate(words) if w == ind_lower]
        for idx in hit_idxs:
            start = max(0, idx - cls._NEGATION_WINDOW)
            end = min(len(words), idx + cls._NEGATION_WINDOW + 1)
            window = words[start:idx] + words[idx + 1:end]
            if any(neg in window for neg in cls._NEGATION_WORDS):
                return True

        # Regex pass — handles substring indicators (``$``, ``€``, ``£``) and
        # multi-word negations ("do not", "shall not") on either side.
        esc = re.escape(ind_lower)
        neg_alt = "|".join(re.escape(n) for n in cls._NEGATION_WORDS)
        gap = rf"(?:\w+\W+){{0,{cls._NEGATION_WINDOW}}}"
        back_pat = rf"\b(?:{neg_alt})\b\W+{gap}{esc}"
        fwd_pat = rf"{esc}\W+{gap}\b(?:{neg_alt})\b"
        if re.search(back_pat, text_lower) or re.search(fwd_pat, text_lower):
            return True
        return False

    @staticmethod
    def is_negated_span(sent, indicator: str) -> bool:
        """Dep-parse-based negation check for callers that already have a Span.

        Walks each token of ``sent`` matching ``indicator``; if the token's
        head has a child with ``dep_ == 'neg'`` (or the token itself does),
        treat it as negated. Falls through cleanly when the caller only has
        a string — they should use :meth:`_is_negated` instead.
        """
        try:
            ind_lower = indicator.lower()
            for token in sent:
                if token.lower_ != ind_lower:
                    continue
                # A 'neg' dep child directly under this token, or under its head,
                # is the structural signature of "not funded" / "is prohibited".
                for tok in (token, token.head):
                    if any(child.dep_ == "neg" for child in tok.children):
                        return True
        except Exception:
            # Span may not be parsed (non-spaCy caller). Give up silently.
            return False
        return False

    @classmethod
    def has_active_financial_indicator(cls, content: str, indicators: List[str]) -> bool:
        """
        Return True only if a financial indicator is present AND not negated.

        Currency symbols ($, €, £) go through the same negation-aware path as
        text indicators. The ±3-sentence context window passed by the caller
        (`analyze_content`) already scopes symbols to country-proximate text,
        so a product-catalogue price tag many paragraphs from any country
        mention no longer triggers HIGH risk.
        """
        content_lower = content.lower()
        for indicator in indicators:
            if indicator in content_lower:
                if not cls._is_negated(content_lower, indicator):
                    return True
        return False

    @staticmethod
    def calculate_risk_score(context_analysis: dict, content: str, financial_indicators: List[str]) -> dict:
        """
        Calculate risk score.
        Financial transaction near sanctioned country → HIGH only if not negated.
        """
        risk_type = context_analysis.get("risk_type", "GENERAL_MENTION")
        base_score = context_analysis.get("risk_score", 10)
        confidence = context_analysis.get("confidence", 60)

        has_financial = EnhancedRiskAssessment.has_active_financial_indicator(content, financial_indicators)

        if has_financial:
            return {
                "risk_level": "HIGH",
                "risk_score": max(85, base_score),
                "confidence": max(90, confidence),
                "risk_type": "FINANCIAL_TRANSACTION",
                "note": "Active financial transaction or funding detected near sanctioned country",
            }

        if base_score >= 70:
            risk_level = "HIGH"
        elif base_score >= 40:
            risk_level = "MEDIUM"
        elif base_score >= 15:
            risk_level = "LOW"
        else:
            risk_level = "MINIMAL"

        return {
            "risk_level": risk_level,
            "risk_score": base_score,
            "confidence": confidence,
            "risk_type": risk_type,
            "note": None,
        }


# ==========================================================================
# Zero-shot NLI classifier (A2) — reads meaning, not keyword presence
#
# Classifies the ±3-sentence context window against four hypotheses per
# sanctioned country. Labels map onto the existing risk_type enum so the
# downstream LLM prompt and React dashboard don't change.
#
# Enabled by default when the `transformers` + `torch` deps + model cache are
# present. Set SANCTIONSIGHT_USE_NLI=false to force the keyword-score path for
# local dev without the ~180MB model download.
# ==========================================================================

_NLI_ENABLED = os.environ.get("SANCTIONSIGHT_USE_NLI", "true").lower() not in ("false", "0", "no")
_NLI_MODEL = os.environ.get(
    "SANCTIONSIGHT_NLI_MODEL", "MoritzLaurer/deberta-v3-base-zeroshot-v2.0"
)
_nli_singleton = None
_nli_lock = threading.Lock()
_nli_disabled_reason: Optional[str] = None

# Natural-language hypotheses. Order matters only for stable label→type mapping.
_NLI_HYPOTHESES = [
    "active business activity with {country}",
    "compliance or sanctions discussion about {country}",
    "historical or cultural reference to {country}",
    "unrelated — product or generic mention not about {country}",
]
_NLI_LABEL_TO_RISK = {
    "active business activity with {country}": ("DIRECT_BUSINESS", 80, 85),
    "compliance or sanctions discussion about {country}": ("COMPLIANCE_MENTION", 25, 80),
    "historical or cultural reference to {country}": ("GENERAL_MENTION", 10, 70),
    "unrelated — product or generic mention not about {country}": (None, 0, 0),
}


def _get_nli_classifier():
    """Lazy-init a zero-shot pipeline. Returns None if unavailable (falls back
    to the keyword-score path). Thread-safe.
    """
    global _nli_singleton, _nli_disabled_reason
    if not _NLI_ENABLED:
        return None
    if _nli_singleton is not None:
        return _nli_singleton
    if _nli_disabled_reason is not None:
        # Already tried and failed; don't retry every call.
        return None
    with _nli_lock:
        if _nli_singleton is not None:
            return _nli_singleton
        if _nli_disabled_reason is not None:
            return None
        try:
            from transformers import pipeline  # type: ignore
            import torch  # type: ignore
            device = 0 if torch.cuda.is_available() else -1
            _nli_singleton = pipeline(
                "zero-shot-classification",
                model=_NLI_MODEL,
                device=device,
            )
            logger.info(
                "Zero-shot NLI classifier loaded (%s, device=%s)",
                _NLI_MODEL, "cuda" if device == 0 else "cpu",
            )
            return _nli_singleton
        except Exception as exc:
            _nli_disabled_reason = str(exc)
            logger.warning(
                "NLI classifier unavailable (%s) — falling back to keyword scoring",
                exc,
            )
            return None


def classify_context_nli(context: str, country: str) -> Optional[dict]:
    """Run zero-shot classification on *context* with hypotheses templated for
    *country*. Returns ``None`` when the classifier is disabled / unavailable
    so the caller can fall through to keyword scoring.
    """
    clf = _get_nli_classifier()
    if clf is None or not context or not country:
        return None
    try:
        hypotheses = [h.format(country=country) for h in _NLI_HYPOTHESES]
        # ``multi_label=False`` forces softmax over labels so scores sum to 1,
        # which matches how we gate on the top label's probability.
        result = clf(context, hypotheses, multi_label=False)
        top_label = result["labels"][0]
        top_score = float(result["scores"][0])

        # Reverse-template so we can look up the label→risk mapping.
        matched_key = next(
            (k for k in _NLI_LABEL_TO_RISK if k.format(country=country) == top_label),
            None,
        )
        if matched_key is None:
            return None
        risk_type, risk_score, base_conf = _NLI_LABEL_TO_RISK[matched_key]
        if risk_type is None:
            # "Unrelated" — tell the caller to drop the finding.
            return {"relevant": False, "_nli_top_label": top_label, "_nli_score": top_score}

        # Scale confidence by the classifier's own probability in [0, 1].
        confidence = int(round(base_conf * max(0.5, min(1.0, top_score))))
        return {
            "relevant": True,
            "risk_type": risk_type,
            "risk_score": risk_score,
            "confidence": confidence,
            "_nli_top_label": top_label,
            "_nli_score": top_score,
        }
    except Exception as exc:
        logger.debug("NLI classification failed (%s) — falling back", exc)
        return None


# ==========================================================================
# Investigator Brief Generator (Gemma 4 via Google AI Studio)
#
# Phase 2 rework: replaces the prior free-form "verdict" with a
# citation-grounded InvestigatorBrief. Every claim the LLM emits must point
# at a specific excerpt in the evidence set; claims whose citations fail the
# post-verifier are silently dropped (fail-closed).
#
# The class-level alias LLMVerdictGenerator = InvestigatorBriefGenerator is
# preserved so older imports continue to work during the transition.
# ==========================================================================

from schemas import (
    CONFIDENCE_BAND_VALUES,
    RECOMMENDATION_VALUES,
    Citation,
    Claim,
    EvidenceExcerpt,
    InvestigatorBrief,
    stable_excerpt_id,
    stable_source_id,
)
from claim_verifier import verify_brief


# Per-URL cap applied when assigning stable IDs. Must match the cap used by
# the API serializer in main.py (_serialize_reports / _serialize_name_co) and
# the DB persistence loop (_persist_findings_and_excerpts) so every layer of
# the pipeline hashes the same (url, trigger, index) triple for the same
# excerpt. Excerpts past this index are dropped from evidence entirely.
EXCERPTS_PER_URL = 5


def assign_stable_ids(
    reports: Optional[List[dict]],
    name_co_results: Optional[List[dict]] = None,
    regulatory_report: Optional[dict] = None,
) -> None:
    """Mutate each excerpt dict to carry a stable excerpt_id + source_id.

    Single point of truth for ID assignment. Call once, before anything
    downstream (LLM prompt, JSON serializer, DB persistence) reads excerpt
    IDs — that keeps citations in the investigator brief resolvable in the
    UI payload and the audit log.

    Indexing is per-URL, capped at EXCERPTS_PER_URL, matching the serializer
    and persistence caps. Idempotent: re-running on the same input yields
    the same IDs.
    """
    def _stamp(bucket_url: str, excerpts: List[dict]) -> None:
        src_id = stable_source_id(bucket_url)
        for idx, ex in enumerate((excerpts or [])[:EXCERPTS_PER_URL]):
            trigger = (ex.get("trigger_sentence") or "").strip()
            ex["source_id"] = src_id
            ex["excerpt_id"] = stable_excerpt_id(bucket_url, trigger, idx)

    for rep in reports or []:
        for ar in rep.get("analyzed_results", []) or []:
            _stamp(ar.get("url", "") or "", ar.get("relevant_excerpts", []))

    for r in name_co_results or []:
        _stamp(r.get("url", "") or "", r.get("relevant_excerpts", []))

    if regulatory_report:
        for ar in regulatory_report.get("analyzed_results", []) or []:
            _stamp(ar.get("url", "") or "", ar.get("relevant_excerpts", []))


class InvestigatorBriefGenerator:
    """
    Builds the evidence set, prompts Gemma 4 with citation tags inline,
    parses the structured response into an InvestigatorBrief, and runs the
    claim verifier. Fails gracefully if the API key is absent or the call
    errors — the rest of the tool is unaffected.
    """

    MAX_EXCERPTS_PER_COUNTRY = 8
    MAX_NAME_CO_LINES = 5
    MAX_PER_LINK_VERDICT_LINES = 20

    def __init__(
        self,
        all_reports_data: List[dict],
        name_co_results: List[dict],
        website: str,
        business_name: str = "",
        legal_name: str = "",
        audit_logger=None,
        per_link_verdicts: Optional[Dict[str, dict]] = None,
        coverage: Optional[Dict[str, int]] = None,
    ):
        self.all_reports_data = all_reports_data
        self.name_co_results = name_co_results
        self.website = website
        self.business_name = business_name
        self.legal_name = legal_name
        self.audit_logger = audit_logger
        # Per-URL LLM verdicts produced earlier in the pipeline. Feeding these
        # into the case brief lets the model build on analysis that already
        # identified concrete transactions, instead of re-synthesising from
        # raw excerpts and losing the signal under policy/advocacy noise.
        self.per_link_verdicts: Dict[str, dict] = per_link_verdicts or {}
        # Extraction coverage counts (from main.py's aggregation). Used to
        # tell the LLM that N URLs only got a partial/snippet-only read so its
        # confidence_band reflects real coverage, not the evidence it did see.
        # Keys: urls_analyzed_fully, urls_need_review, urls_not_attempted.
        self.coverage: Dict[str, int] = coverage or {}
        # Evidence map populated by _build_evidence_set(), consumed by
        # the prompt builder and later by the verifier.
        self._evidence: Dict[str, EvidenceExcerpt] = {}

    # ------------------------------------------------------------------
    # Evidence set
    # ------------------------------------------------------------------
    def _build_evidence_set(self) -> List[EvidenceExcerpt]:
        """
        Walk per-country results + name-co-occurrence results and collect the
        evidence the LLM is allowed to cite. IDs are read directly off each
        excerpt dict (assigned earlier by assign_stable_ids) so the prompt,
        the JSON payload the UI consumes, and the DB rows all share the same
        (source_id, excerpt_id) space. Excerpts missing an ID were dropped by
        the per-URL cap and are excluded here so the LLM cannot cite evidence
        the analyst will never see.
        """
        evidence: List[EvidenceExcerpt] = []

        for report in self.all_reports_data:
            country = report.get("country", "")
            for r in report.get("analyzed_results", []):
                url = r.get("url", "") or ""
                for ex in r.get("relevant_excerpts", []) or []:
                    exc_id = ex.get("excerpt_id")
                    src_id = ex.get("source_id")
                    if not exc_id or not src_id:
                        continue
                    text = (ex.get("text") or "").replace("\n", " ").strip()
                    if not text:
                        continue
                    item = EvidenceExcerpt(
                        source_id=src_id,
                        excerpt_id=exc_id,
                        url=url,
                        text=text[:500],
                        country=country,
                        risk_type=ex.get("risk_type"),
                        confidence=ex.get("confidence"),
                    )
                    evidence.append(item)
                    self._evidence[exc_id] = item

        for r in self.name_co_results or []:
            url = r.get("url", "") or ""
            # Name-co URLs may expose relevant_excerpts (preferred, carries
            # trigger sentences and stamped IDs) or just a snippet. Prefer
            # the structured excerpts so IDs line up with what the UI shows.
            structured = r.get("relevant_excerpts") or []
            if structured:
                for ex in structured:
                    exc_id = ex.get("excerpt_id")
                    src_id = ex.get("source_id")
                    if not exc_id or not src_id:
                        continue
                    text = (ex.get("text") or "").replace("\n", " ").strip()
                    if not text:
                        continue
                    item = EvidenceExcerpt(
                        source_id=src_id,
                        excerpt_id=exc_id,
                        url=url,
                        text=text[:500],
                        country=r.get("country"),
                        risk_type=ex.get("risk_type") or "NAME_COOCCURRENCE",
                        confidence=ex.get("confidence") or r.get("confidence"),
                    )
                    evidence.append(item)
                    self._evidence[exc_id] = item
                continue

            # Fallback: no structured excerpts; cite the snippet itself. This
            # path still needs an ID; we synthesize one with index 0 so it
            # matches what the serializer would produce for the same URL.
            snippet = (r.get("snippet") or r.get("title") or "").replace("\n", " ").strip()
            if not snippet:
                continue
            src_id = stable_source_id(url)
            exc_id = stable_excerpt_id(url, snippet[:120], 0)
            item = EvidenceExcerpt(
                source_id=src_id,
                excerpt_id=exc_id,
                url=url,
                text=snippet[:500],
                country=r.get("country"),
                risk_type="NAME_COOCCURRENCE",
                confidence=r.get("confidence"),
            )
            evidence.append(item)
            self._evidence[exc_id] = item

        return evidence

    # ------------------------------------------------------------------
    # Per-link verdict highlights (feed pre-analyzed LLM notes into prompt)
    # ------------------------------------------------------------------
    def _collect_per_link_highlights(self) -> List[dict]:
        """Rank concern=True per-link verdicts by the URL's NLP risk level
        and return the top N so the case brief's prompt contains them."""
        if not self.per_link_verdicts:
            return []

        level_rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "MINIMAL": 0}
        url_level_map: Dict[str, int] = {}
        for rep in self.all_reports_data or []:
            for ar in rep.get("analyzed_results", []) or []:
                url = ar.get("url") or ""
                if not url:
                    continue
                r = level_rank.get((ar.get("risk_level") or "").upper(), 0)
                if r > url_level_map.get(url, 0):
                    url_level_map[url] = r
        for nc in self.name_co_results or []:
            url = nc.get("url") or ""
            if not url:
                continue
            r = level_rank.get((nc.get("risk_level") or "").upper(), 0)
            if r > url_level_map.get(url, 0):
                url_level_map[url] = r

        rows: List[dict] = []
        for _uh, v in (self.per_link_verdicts or {}).items():
            if not v.get("concern"):
                continue
            reasoning = (v.get("reasoning") or "").strip()
            if not reasoning:
                continue
            url = v.get("url") or ""
            rank = url_level_map.get(url, 0)
            level_name = next(
                (k for k, val in level_rank.items() if val == rank), "UNKNOWN"
            ) if rank > 0 else "UNKNOWN"
            rows.append({
                "url": url,
                "country": v.get("country") or "—",
                "reasoning": reasoning[:400],
                "rank": rank,
                "risk_level": level_name,
                "source_id": stable_source_id(url) if url else "-",
            })
        rows.sort(key=lambda x: x["rank"], reverse=True)
        return rows[: self.MAX_PER_LINK_VERDICT_LINES]

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------
    def _build_prompt(self, evidence: List[EvidenceExcerpt]) -> str:
        lines = [
            "You are a sanctions compliance investigator assistant.",
            "You do NOT decide, clear, or close any matter. You produce an "
            "investigator brief consisting of structured claims that point at "
            "specific retrieved excerpts by ID.",
            "",
            "RULES:",
            "- Every claim you emit MUST cite at least one excerpt_id drawn "
            "from the EVIDENCE section below.",
            "- Do not state anything that is not supported by the cited text.",
            "- Do not invent new excerpt_ids. Use the exact IDs shown.",
            "- Prefer ESCALATE_FOR_REVIEW when evidence is ambiguous; the human "
            "analyst makes the final determination.",
            "- CRITICAL PRIORITY — SPECIFIC TRANSACTIONS OUTRANK POLICY TALK: "
            "When the evidence contains a specific transaction — a dollar "
            "amount or valued shipment paired with a sanctioned jurisdiction, "
            "a named delivery of goods, a direct aid presentation, a named "
            "financial transfer, or any identifiable act of sending value to "
            "a sanctioned country — that fact MUST appear as the lead item "
            "in summary_claims AND in risk_factor_claims. One documented "
            "transaction outranks any number of policy or advocacy "
            "statements. Never let 'advocates for', 'urges', 'calls for', "
            "or 'supports' language displace a concrete transfer.",
            "- USE PER-LINK VERDICTS AS SYNTHESIS HINTS: The PER-LINK LLM "
            "VERDICTS section below is a pre-analysis of individual URLs by "
            "another pass of this model. When a verdict note identifies a "
            "concrete transaction, echo the underlying facts (amount, "
            "parties, action, recipient) in your claims, and cite the "
            "excerpt(s) from that URL in the EVIDENCE section. Do not cite "
            "the verdict note itself — cite the source excerpt.",
            "",
            f"TARGET ENTITY:",
            f"  Website:       {self.website or '(not provided)'}",
            f"  Business name: {self.business_name or '(not provided)'}",
            f"  Legal name:    {self.legal_name or '(not provided)'}",
            "",
            "PER-COUNTRY FINDING SUMMARY (for context, not cite-able):",
        ]

        any_findings = False
        for report in self.all_reports_data:
            country = report.get("country", "")
            results = report.get("analyzed_results", [])
            if not results:
                continue
            any_findings = True
            n_high = sum(1 for r in results if r.get("risk_level") == "HIGH")
            n_medium = sum(1 for r in results if r.get("risk_level") == "MEDIUM")
            n_low = sum(1 for r in results if r.get("risk_level") == "LOW")
            lines.append(
                f"  {country}: {n_high} HIGH, {n_medium} MEDIUM, {n_low} LOW risk URLs"
            )
        if not any_findings:
            lines.append("  No country-specific findings collected.")

        lines += ["", "NAME CO-OCCURRENCE:"]
        if self.name_co_results:
            lines.append(
                f"  {len(self.name_co_results)} URLs where the business/legal name "
                "appears alongside a sanctioned country."
            )
        else:
            lines.append("  None.")

        highlights = self._collect_per_link_highlights()
        lines += [
            "",
            "PER-LINK LLM VERDICTS (pre-analyzed per-URL notes — synthesis hints, "
            "NOT directly cite-able; when you lift a fact from one of these, "
            "cite the matching excerpt in the EVIDENCE section using its "
            "source_id):",
        ]
        if not highlights:
            lines.append("  None flagged by the per-link pass.")
        else:
            for h in highlights:
                lines.append(
                    f"  [src:{h['source_id']}] country={h['country']} "
                    f"url_risk={h['risk_level']} url={h['url']}"
                )
                lines.append(f'    note: "{h["reasoning"]}"')

        # Coverage gap note — tells the LLM how many URLs only got partial or
        # no content extraction so it can calibrate confidence_band rather
        # than assume the evidence set is exhaustive.
        if self.coverage:
            analysed = int(self.coverage.get("urls_analyzed_fully", 0) or 0)
            review = int(self.coverage.get("urls_need_review", 0) or 0)
            not_attempted = int(self.coverage.get("urls_not_attempted", 0) or 0)
            if analysed or review or not_attempted:
                lines += [
                    "",
                    (
                        f"COVERAGE: {analysed} URLs analysed in full, "
                        f"{review} needed analyst review (extraction failed or only "
                        f"Google snippet available), {not_attempted} not attempted "
                        f"(excluded domains). Factor this into confidence_band — "
                        f"the evidence below is not exhaustive if review/not_attempted "
                        f"counts are non-zero."
                    ),
                ]

        lines += ["", "EVIDENCE (cite these excerpt_ids only):"]
        if not evidence:
            lines.append("  (empty — no excerpts retrieved)")
        else:
            # URLs whose per-link LLM verdict flagged a concern must retain at
            # least one excerpt in the prompt even if the per-country cap
            # would otherwise drop them — these are exactly the URLs whose
            # concrete findings we can't afford to silence.
            concern_urls = {
                (v.get("url") or "")
                for v in (self.per_link_verdicts or {}).values()
                if v.get("concern") and v.get("url")
            }
            urls_included: set = set()

            def _emit(item: EvidenceExcerpt) -> None:
                country = item.country or "—"
                conf = f"{item.confidence:.0f}%" if item.confidence is not None else "-"
                lines.append(
                    f'  [src:{item.source_id}][exc:{item.excerpt_id}] '
                    f'country={country} risk_type={item.risk_type or "-"} conf={conf}'
                )
                lines.append(f'    text: "{item.text}"')
                lines.append(f'    url:  {item.url}')

            per_country_count: Dict[str, int] = {}
            for item in evidence:
                country = item.country or "—"
                count = per_country_count.get(country, 0)
                if count >= self.MAX_EXCERPTS_PER_COUNTRY and item.risk_type != "NAME_COOCCURRENCE":
                    continue
                per_country_count[country] = count + 1
                if item.url:
                    urls_included.add(item.url)
                _emit(item)

            # Rescue pass: re-insert the first excerpt from any concern URL
            # that the cap above dropped entirely.
            missing = concern_urls - urls_included
            if missing:
                rescued: set = set()
                for item in evidence:
                    if item.url in missing and item.url not in rescued:
                        _emit(item)
                        rescued.add(item.url)
                        if rescued == missing:
                            break

        lines += [
            "",
            "OUTPUT:",
            "Return JSON exactly matching this shape (no markdown fences):",
            "{",
            '  "recommendation": "ESCALATE_FOR_REVIEW | ADDITIONAL_OSINT_NEEDED | NO_FURTHER_ACTION_RECOMMENDED | INSUFFICIENT_DATA",',
            '  "confidence_band": "HIGH | MEDIUM | LOW",',
            '  "summary_claims":      [ { "text": "...", "citations": [ { "source_id": "src_...", "excerpt_id": "exc_..." } ] } ],',
            '  "risk_factor_claims":  [ { "text": "...", "citations": [ ... ] } ],',
            '  "suggested_next_steps":[ { "text": "...", "citations": [ ... ] } ]',
            "}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def generate(self) -> Optional[dict]:
        """
        Returns a dict with the InvestigatorBrief shape plus a
        `verification_report` key for audit, or None on total failure.
        When the API key / SDK is missing, returns None so upstream code
        can fall back cleanly.
        """
        if not USE_VERTEX and not GOOGLE_GENAI_API_KEY:
            logger.info("No LLM credentials configured – skipping investigator brief.")
            return None
        if _google_genai is None:
            logger.warning(
                "google-genai not installed – skipping investigator brief. "
                "Run: pip install google-genai"
            )
            return None

        evidence = self._build_evidence_set()
        if not evidence:
            logger.info("No excerpt evidence available – returning empty brief.")
            return self._empty_brief_dict("No retrieved excerpts to cite.")

        try:
            if USE_VERTEX:
                client = _google_genai.Client(
                    vertexai=True,
                    project=VERTEX_PROJECT,
                    location=VERTEX_LOCATION,
                )
            else:
                client = _google_genai.Client(api_key=GOOGLE_GENAI_API_KEY)
            prompt = self._build_prompt(evidence)
            logger.info(
                "Sending %d evidence excerpts to %s (backend=%s)…",
                len(evidence), LLM_MODEL,
                "vertex" if USE_VERTEX else "aistudio",
            )

            if self.audit_logger is not None:
                try:
                    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
                    self.audit_logger.log_llm_prompt(
                        model=LLM_MODEL,
                        prompt_hash=prompt_hash,
                        evidence_ids=[e.excerpt_id for e in evidence],
                    )
                except Exception as exc:
                    logger.warning("Audit log_llm_prompt failed: %s", exc)

            response = self._call_model(client, prompt)
            raw = (getattr(response, "text", None) or "").strip()

            # Strip markdown fences if the model adds them.
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw)

            parsed = json.loads(raw)
            brief = InvestigatorBrief.model_validate(parsed).normalize()

            verified_brief, report = verify_brief(brief, self._evidence)

            logger.info(
                "Brief: recommendation=%s confidence=%s verified=%d dropped=%d",
                verified_brief.recommendation,
                verified_brief.confidence_band,
                report.verified_claims,
                report.dropped_claims,
            )

            out = verified_brief.model_dump()
            out["verification_report"] = report.model_dump()
            out["evidence_count"] = len(evidence)

            if self.audit_logger is not None:
                try:
                    response_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else ""
                    self.audit_logger.log_llm_response(
                        response_hash=response_hash,
                        verification_result={
                            "total_claims": report.total_claims,
                            "verified_claims": report.verified_claims,
                            "dropped_claims": report.dropped_claims,
                            "recommendation": verified_brief.recommendation,
                            "confidence_band": verified_brief.confidence_band,
                        },
                    )
                except Exception as exc:
                    logger.warning("Audit log_llm_response failed: %s", exc)

            return out

        except json.JSONDecodeError:
            logger.warning(
                "Investigator brief: model response was not valid JSON. "
                "Raw (first 500 chars): %s", raw[:500] if raw else "(empty)"
            )
            return self._empty_brief_dict(
                f"Model response was not valid JSON. Raw (first 500 chars): {raw[:500] if raw else '(empty)'}"
            )
        except Exception as exc:
            import traceback
            logger.error(
                "Investigator brief generation failed: %s: %s",
                type(exc).__name__, exc,
            )
            logger.error("Brief generation traceback:\n%s", traceback.format_exc())
            return self._empty_brief_dict(
                f"Brief generation failed: {type(exc).__name__}: {exc}"
            )

    # ------------------------------------------------------------------
    def _call_model(self, client, prompt: str):
        """
        Try structured output via response_schema first; if the SDK / model
        rejects it, fall back to plain generation and rely on JSON parsing.
        """
        schema = InvestigatorBrief.model_json_schema_for_llm()
        try:
            # Newer google-genai supports this via types.GenerateContentConfig,
            # but older releases take a plain dict. Try both.
            try:
                from google.genai import types as _gen_types  # type: ignore
                config = _gen_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                )
                return client.models.generate_content(
                    model=LLM_MODEL, contents=prompt, config=config,
                )
            except Exception:
                return client.models.generate_content(
                    model=LLM_MODEL,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": schema,
                    },
                )
        except Exception as exc:
            logger.info(
                "Structured response_schema not accepted by model (%s); "
                "falling back to unconstrained generation.", exc,
            )
            return client.models.generate_content(
                model=LLM_MODEL, contents=prompt,
            )

    def _empty_brief_dict(self, note: str) -> dict:
        brief = InvestigatorBrief(
            recommendation="INSUFFICIENT_DATA",
            confidence_band="LOW",
            summary_claims=[],
            risk_factor_claims=[],
            suggested_next_steps=[],
        )
        out = brief.model_dump()
        out["verification_report"] = {
            "total_claims": 0, "verified_claims": 0, "dropped_claims": 0, "per_claim": [],
        }
        out["evidence_count"] = len(self._evidence)
        out["note"] = note
        return out


# Backward-compatible alias. Older code paths (CLI, streamlit) still import
# LLMVerdictGenerator by name. The output dict now uses the new schema keys,
# so those paths must read via the helpers below, not via verdict/summary.
LLMVerdictGenerator = InvestigatorBriefGenerator


# ==========================================================================
# Per-link verdict generator
#
# Runs alongside the aggregate InvestigatorBrief. For every analyzed URL the
# caller gets a binary concern flag + 1–2 sentence rationale, scoped *only*
# to that URL's trigger sentences and context windows — not the whole case.
# The aggregate brief is still the case-level narrative; per-link verdicts
# give the analyst a quick read per row before they expand it.
# ==========================================================================

class PerLinkVerdictGenerator:
    """Small-prompt LLM call per URL. Returns ``{concern: bool, reasoning: str}``.

    Failure handling is intentionally silent on the hot path — the engine
    skips the per-link verdict and records the error in the returned dict so
    the UI can show "LLM unavailable" per row rather than failing the job.
    """

    MAX_EVIDENCE_LINES_PER_LINK = 6

    def __init__(self, website: str = "", business_name: str = "", legal_name: str = ""):
        self.website = website
        self.business_name = business_name
        self.legal_name = legal_name
        self._client = None

    # -----------------------------------------------------------------
    def _get_client(self):
        if self._client is not None:
            return self._client
        if _google_genai is None:
            return None
        if not USE_VERTEX and not GOOGLE_GENAI_API_KEY:
            return None
        try:
            if USE_VERTEX:
                self._client = _google_genai.Client(
                    vertexai=True,
                    project=VERTEX_PROJECT,
                    location=VERTEX_LOCATION,
                )
            else:
                self._client = _google_genai.Client(api_key=GOOGLE_GENAI_API_KEY)
        except Exception as exc:
            logger.warning("Per-link LLM client init failed: %s", exc)
            return None
        return self._client

    # -----------------------------------------------------------------
    @staticmethod
    def _extract_evidence_lines(analyzed_result: dict, max_lines: int) -> List[str]:
        """Pull trigger sentence + context from each relevant excerpt, capped."""
        lines: List[str] = []
        for ex in (analyzed_result.get("relevant_excerpts") or [])[:max_lines]:
            trigger = (ex.get("trigger_sentence") or "").strip()
            ctx = (ex.get("text") or "").strip()
            if not ctx and not trigger:
                continue
            label = ex.get("risk_type") or "GENERAL"
            lines.append(f"- [{label}] trigger: {trigger[:300]}")
            if ctx and ctx != trigger:
                lines.append(f"    context: {ctx[:600]}")
        return lines

    def _build_prompt(
        self, url: str, country: Optional[str], analyzed_result: dict
    ) -> str:
        evidence_lines = self._extract_evidence_lines(
            analyzed_result, self.MAX_EVIDENCE_LINES_PER_LINK
        )
        lines = [
            "You are a sanctions compliance triage assistant.",
            "Given one URL's extracted evidence, decide whether THIS single "
            "source shows a plausible sanctions concern worth an analyst's "
            "closer look.",
            "",
            "OUTPUT RULES:",
            "- Return JSON with exactly two keys: concern (boolean) and "
            "reasoning (one or two short sentences).",
            "- \"concern\": true only if the evidence plausibly links the "
            "target entity to sanctioned activity, jurisdictions, or parties.",
            "- Do not cite excerpts you don't see below.",
            "- Do not infer beyond the evidence.",
            "",
            f"TARGET:",
            f"  Website:       {self.website or '(not provided)'}",
            f"  Business name: {self.business_name or '(not provided)'}",
            f"  Legal name:    {self.legal_name or '(not provided)'}",
            f"  URL:           {url}",
            f"  Country bucket:{country or '(—)'}",
            "",
            "EVIDENCE:",
        ]
        if evidence_lines:
            lines.extend(evidence_lines)
        else:
            lines.append("  (no trigger excerpts — only metadata available)")
            title = (analyzed_result.get("original_title") or "").strip()
            snippet = (analyzed_result.get("original_snippet") or "").strip()
            if title:
                lines.append(f"  title:   {title[:200]}")
            if snippet:
                lines.append(f"  snippet: {snippet[:400]}")

        lines += [
            "",
            "Return exactly:",
            '{"concern": true|false, "reasoning": "..."}',
        ]
        return "\n".join(lines)

    # -----------------------------------------------------------------
    def _call_with_retry(self, client, prompt: str, max_attempts: int = 4):
        """Send the prompt with exponential backoff on 429 RESOURCE_EXHAUSTED.

        Backoff: 2s, 4s, 8s jittered. Non-429 errors re-raise immediately so
        the caller's error classifier still works.
        """
        import random
        last_exc: Optional[Exception] = None
        for attempt in range(max_attempts):
            try:
                try:
                    from google.genai import types as _gen_types  # type: ignore
                    config = _gen_types.GenerateContentConfig(
                        response_mime_type="application/json",
                    )
                    return client.models.generate_content(
                        model=LLM_MODEL, contents=prompt, config=config,
                    )
                except Exception:
                    return client.models.generate_content(
                        model=LLM_MODEL, contents=prompt,
                    )
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                is_rate_limit = "429" in msg or "RESOURCE_EXHAUSTED" in msg
                if not is_rate_limit or attempt == max_attempts - 1:
                    raise
                delay = (2 ** attempt) + random.uniform(0, 1)
                logger.info(
                    "Per-link 429 (attempt %d/%d) — retrying in %.1fs",
                    attempt + 1, max_attempts, delay,
                )
                time.sleep(delay)
        if last_exc:
            raise last_exc

    # -----------------------------------------------------------------
    def generate_for_link(
        self, url: str, country: Optional[str], analyzed_result: dict
    ) -> dict:
        """Returns a dict with either {concern, reasoning, model} or {error}."""
        client = self._get_client()
        if client is None:
            return {"error": "llm_unavailable"}

        prompt = self._build_prompt(url, country, analyzed_result)
        raw = ""
        try:
            response = self._call_with_retry(client, prompt)
            raw = (getattr(response, "text", None) or "").strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
            raw = re.sub(r"\s*```$", "", raw)
            parsed = json.loads(raw)
            concern = bool(parsed.get("concern", False))
            reasoning = (parsed.get("reasoning") or "").strip()
            if not reasoning:
                reasoning = "(model returned no reasoning)"
            # Hard cap — a stray multi-paragraph response should not flood the UI.
            if len(reasoning) > 800:
                reasoning = reasoning[:800].rstrip() + "…"
            return {"concern": concern, "reasoning": reasoning, "model": LLM_MODEL}
        except json.JSONDecodeError:
            return {"error": "non_json_response", "raw": raw[:400]}
        except Exception as exc:
            return {"error": f"llm_call_failed: {exc}"}


# --------------------------------------------------------------------------
# Legacy-shape helpers for HTML/CLI rendering
# --------------------------------------------------------------------------

_REC_DISPLAY_LABEL = {
    "ESCALATE_FOR_REVIEW": "Possible concern — review needed",
    "ADDITIONAL_OSINT_NEEDED": "More research needed",
    "NO_FURTHER_ACTION_RECOMMENDED": "No sanctions concerns found",
    "INSUFFICIENT_DATA": "Not enough information to assess",
}

_REC_CSS_CLASS = {
    "ESCALATE_FOR_REVIEW": "high",
    "ADDITIONAL_OSINT_NEEDED": "possible",
    "NO_FURTHER_ACTION_RECOMMENDED": "clear",
    "INSUFFICIENT_DATA": "unknown",
}

_REC_COLOR = {
    "ESCALATE_FOR_REVIEW": "#FF5630",
    "ADDITIONAL_OSINT_NEEDED": "#FFB800",
    "NO_FURTHER_ACTION_RECOMMENDED": "#00D924",
    "INSUFFICIENT_DATA": "#aaa",
}


def brief_summary_text(brief: Optional[dict]) -> str:
    """Flatten summary_claims into a single paragraph for HTML/CLI render."""
    if not brief:
        return ""
    claims = brief.get("summary_claims") or []
    return " ".join((c.get("text") or "").strip() for c in claims if c.get("text"))


def brief_factors_list(brief: Optional[dict]) -> List[str]:
    if not brief:
        return []
    return [(c.get("text") or "").strip() for c in (brief.get("risk_factor_claims") or []) if c.get("text")]


def brief_next_steps_list(brief: Optional[dict]) -> List[str]:
    if not brief:
        return []
    return [(c.get("text") or "").strip() for c in (brief.get("suggested_next_steps") or []) if c.get("text")]


def brief_recommendation_label(brief: Optional[dict]) -> str:
    fallback = _REC_DISPLAY_LABEL["INSUFFICIENT_DATA"]
    if not brief:
        return fallback
    key = (brief.get("recommendation") or "INSUFFICIENT_DATA").upper()
    return _REC_DISPLAY_LABEL.get(key, fallback)


def brief_confidence_label(brief: Optional[dict]) -> str:
    if not brief:
        return ""
    return (brief.get("confidence_band") or "").upper()


def _print_investigator_brief(brief: dict) -> None:
    """Print the investigator brief to the terminal."""
    rec_key = (brief.get("recommendation") or "INSUFFICIENT_DATA").upper()
    label = _REC_DISPLAY_LABEL.get(rec_key, _REC_DISPLAY_LABEL["INSUFFICIENT_DATA"])
    conf = brief.get("confidence_band", "?")
    marker = {
        "ESCALATE_FOR_REVIEW": "🔴",
        "ADDITIONAL_OSINT_NEEDED": "🟡",
        "NO_FURTHER_ACTION_RECOMMENDED": "🟢",
        "INSUFFICIENT_DATA": "⚪",
    }.get(rec_key, "❓")
    print("\n" + "=" * 60)
    print(f"  {marker}  GEMMA 4 INVESTIGATOR BRIEF")
    print("=" * 60)
    print(f"  Recommendation : {label}")
    print(f"  Confidence     : {conf}")
    summary = brief_summary_text(brief)
    if summary:
        print(f"  Summary        : {summary}")
    factors = brief_factors_list(brief)
    if factors:
        print("  Key risk factors:")
        for f in factors:
            print(f"    • {f}")
    steps = brief_next_steps_list(brief)
    if steps:
        print("  Suggested next steps:")
        for s in steps:
            print(f"    • {s}")
    dropped = brief.get("unverified_claims_dropped") or 0
    if dropped:
        print(f"  Unverified claims dropped: {dropped}")
    print("=" * 60)


# Backward-compatible alias (CLI path).
_print_verdict = _print_investigator_brief


# ==========================================================================
# Social media detector
# ==========================================================================

class SocialMediaDetector:
    def __init__(self, api_key: str, search_engine_id: str):
        self.api_key = api_key
        self.search_engine_id = search_engine_id
        self.social_platforms = {
            "facebook": {"domain": "facebook.com", "patterns": ["facebook.com/", "fb.com/"]},
            "linkedin": {"domain": "linkedin.com", "patterns": ["linkedin.com/company/", "linkedin.com/in/"]},
        }

    def extract_social_links_from_html(self, html_content: str, base_url: str) -> Dict[str, str]:
        found: Dict[str, str] = {}
        soup = BeautifulSoup(html_content, "html.parser")
        for platform, config in self.social_platforms.items():
            for pattern in config["patterns"]:
                for link in soup.find_all("a", href=lambda x, p=pattern: x and p in x):
                    href = link.get("href", "")
                    if href:
                        full_url = urljoin(base_url, href)
                        if pattern in full_url and platform not in found:
                            found[platform] = full_url
                            break
        return found

    @with_retry(max_retries=3, backoff_factor=2)
    def _search_platform(self, platform: str, website_domain: str) -> Optional[str]:
        import time
        config = self.social_platforms[platform]
        query = f'"{website_domain}" site:{config["domain"]}'
        params = {
            "q": query,
            "key": self.api_key,
            "cx": self.search_engine_id,
            "num": 3,
        }
        response = requests.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        time.sleep(1)
        for item in data.get("items", []):
            link = item.get("link", "")
            if config["domain"] in link:
                return link
        return None

    def search_social_profiles(
        self, website_domain: str, platforms_to_search: Optional[List[str]] = None
    ) -> Dict[str, str]:
        found: Dict[str, str] = {}
        platforms = platforms_to_search or list(self.social_platforms.keys())
        for platform in platforms:
            if platform not in self.social_platforms:
                continue
            try:
                url = self._search_platform(platform, website_domain)
                if url:
                    found[platform] = url
            except Exception as exc:
                logger.error("Error searching for %s profile: %s", platform, exc)
        return found


# ==========================================================================
# Content analyzer
# ==========================================================================

class SanctionsContentAnalyzer:
    """NLP-based content analyzer for sanctions risk."""

    FINANCIAL_INDICATORS = [
        "fund", "funding", "grant", "donation", "payment", "transfer",
        "contribute", "contribution", "support", "financial", "monetary",
        "dollar", "$", "€", "£", "euro", "pound", "invest", "investment",
    ]

    # Lawful only for OFAC-licensed entities — any co-occurrence with a
    # sanctioned jurisdiction must be surfaced HIGH for analyst verification.
    LICENSED_ACTIVITY_PHRASES = [
        "humanitarian aid", "humanitarian assistance", "humanitarian mission",
        "humanitarian operations", "relief effort", "relief mission",
        "disaster relief", "medical aid", "food aid", "medical supplies",
        "ngo operations", "non-governmental organization",
        "ofac license", "ofac-authorized", "general license", "specific license",
        "authorized under license", "sanctions license", "treasury license",
        "license from ofac", "under general license",
    ]

    # Product / cuisine / idiom matches — legitimately irrelevant to sanctions.
    # Surfaced on the per-URL result as an audit trail so the analyst can see
    # the tool saw and excluded them.
    CONFIRMED_NON_SANCTIONS_REFERENCES = [
        "damascus steel", "cuban sandwich", "cuban link",
    ]

    # Arguably sanctions-adjacent; kept silent (dropped entirely) for now per
    # this scope. CLAUDE.md flags these for future downgrade-not-exclude.
    DEFERRED_FOR_REVIEW = [
        "shipping policy", "we do not ship", "countries we ship to",
    ]

    def __init__(self, sanctioned_country: str, variations_override: Optional[List[str]] = None):
        self.sanctioned_country = sanctioned_country
        self.nlp = nlp
        self.risk_assessor = EnhancedRiskAssessment()
        self._variations_override = variations_override

        self.business_keywords = [
            "operate", "operations", "business", "trade", "trading", "export", "import",
            "subsidiary", "branch", "office", "partner", "supplier", "customer", "client",
            "ship", "shipping", "delivery", "service", "presence", "activity", "activities",
            "transaction", "payment", "investment", "joint venture", "fundraising for",
            "travel to", "mission trip to", "products from", "originating from",
            "fund", "funding", "grant", "donation", "contribute", "support",
        ]
        self.compliance_keywords = [
            "comply", "compliance", "restriction", "prohibited", "forbidden", "banned",
            "embargo", "sanction", "OFAC", "SDN", "blocked", "avoid", "exclude",
            "policy", "regulation", "legal", "lawful", "unauthorized", "do not ship",
        ]
        self.negative_indicators = [
            "not", "no", "don't", "doesn't", "cannot", "can't", "won't", "wouldn't",
            "prohibited", "forbidden", "illegal", "avoid", "exclude", "except",
        ]
        # Merged list is used by the pass-2 sanctions-term loop to conservatively
        # skip either-kind of FP context window (both confirmed and deferred).
        self.false_positive_phrases = (
            self.CONFIRMED_NON_SANCTIONS_REFERENCES + self.DEFERRED_FOR_REVIEW
        )
        self.country_variations = self._get_country_variations(sanctioned_country)
        if self._variations_override:
            # Custom country: user supplied variations verbatim. Always include
            # the country name itself as a match target, lowercase everything,
            # and dedupe.
            extra = [sanctioned_country.lower()] + [
                v.strip().lower() for v in self._variations_override if v.strip()
            ]
            self.country_variations = list(dict.fromkeys(extra))
        # Word-boundary regex over every variation. Substring matching used to
        # fire on things like "incubate" (contains "cuba") or "habanero"
        # (contains "habana"). `re.escape` keeps TLDs like ".cu" literal; \b
        # handles word/non-word transitions correctly at either end.
        escaped = [re.escape(v) for v in self.country_variations if v]
        self.country_pattern: Optional[re.Pattern] = (
            re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)
            if escaped else None
        )

    def _get_country_variations(self, country: str) -> List[str]:
        variations = [country.lower()]
        country_map = {
            "Cuba": [
                "cuba", "havana", "habana", ".cu", "cuban",
                "gaviota", "cimex", "habaguanex", "cupet",
            ],
            "Iran": [
                "iran", "persia", "tehran", ".ir", "iranian",
                "irgc", "islamic revolutionary guard", "quds force", "basij",
                "bank melli", "bank saderat", "bank markazi", "bank sepah",
                "nioc", "national iranian oil", "nitc", "kish island",
                "mahan air", "irisl", "modafl",
            ],
            "Syria": [
                "syria", "syrie", "siria", "damascus", "aleppo", ".sy", "syrian",
                "latakia", "central bank of syria", "assad",
                "general intelligence directorate",
            ],
            "North Korea": [
                "north korea", "dprk", "pyongyang", ".kp", "north korean",
                "choson", "joseon", "kpa", "korea mining development", "komid",
                "reconnaissance general bureau", "mansudae",
                "air koryo", "bureau 39", "office 39",
            ],
            "Crimea": ["crimea", "krim", "sevastopol", "simferopol", "crimean"],
            "Luhansk": ["luhansk", "lugansk"],
            "Donetsk": ["donetsk", "donbas"],
            "Ukraine": ["ukraine", "kyiv", "kiev", ".ua", "ukrainian"],
            "Russia": [
                "russia", "russian", "moscow", "russian federation", ".ru",
                "fsb", "gru", "svr", "rosoboronexport", "rostec", "gazprombank",
                "vtb bank", "vtb", "sberbank", "promsvyazbank", "novatek",
                "alrosa", "wagner group", "prigozhin", "kremlin",
            ],
            "Belarus": [
                "belarus", "belarusian", "minsk", ".by",
                "lukashenko", "belneftekhim", "mzkt",
            ],
            "Myanmar": [
                "myanmar", "burma", "burmese", "naypyidaw", "rangoon", "yangon", ".mm",
                "tatmadaw", "moge", "myanmar oil and gas",
            ],
            "Venezuela": [
                "venezuela", "venezuelan", "caracas", "maduro", ".ve",
                "pdvsa", "citgo",
            ],
        }
        if country in country_map:
            variations.extend(country_map[country])
        return list(set(variations))

    @with_retry(max_retries=2, backoff_factor=2, reraise=False)
    def extract_content_from_url(self, url: str, timeout: int = 10, audit_logger=None) -> dict:
        """Extract main content from URL using trafilatura. PDFs extracted via PyMuPDF.

        When ``audit_logger`` is provided, the extracted text is sha256-hashed,
        gzip-stored to the snapshots directory (content-addressed), and a
        `content_extracted` audit event is written. The returned dict carries
        the `content_hash` so downstream code can link findings to the stored
        evidence.
        """
        result = self._extract_content_from_url_inner(url, timeout=timeout)

        content = result.get("content")
        content_hash = None
        if content:
            content_hash = _store_snapshot(content)
            result["content_hash"] = content_hash
            result["language"] = detect_language(content)

        if audit_logger is not None:
            try:
                audit_logger.log_extraction(
                    url=url,
                    extraction_type=result.get("type", "UNKNOWN"),
                    content_hash=content_hash,
                    content_length=len(content) if content else 0,
                )
            except Exception as exc:
                logger.warning("Audit log_extraction failed for %s: %s", url[:60], exc)

        return result

    def _extract_content_from_url_inner(self, url: str, timeout: int = 10) -> dict:
        # --- Per-job extraction cache ---
        # If we've already fetched + parsed this URL on this job (including
        # failed fetches — those are cached too so we don't waste budget
        # retrying them), return the cached result immediately.
        cached = _cache_get(url)
        if cached is not None:
            logger.info("[cache_hit] %s", url[:80])
            return cached

        result = self._perform_extraction(url, timeout=timeout)
        _cache_put(url, result)
        return result

    def _perform_extraction(self, url: str, timeout: int = 10) -> dict:
        ext = url.lower().split("?")[0].split(".")[-1]
        session = _get_http_session()

        # --- PDF extraction (Task 2) ---
        if ext == "pdf":
            try:
                import fitz  # PyMuPDF
                resp = session.get(url, timeout=timeout)
                resp.raise_for_status()
                doc = fitz.open(stream=resp.content, filetype="pdf")
                pages_text = [page.get_text() for page in doc]
                doc.close()
                text = "\n".join(pages_text).strip()
                if text:
                    logger.info("PDF extracted: %d chars from %s", len(text), url[:60])
                    return {"content": text, "type": "PDF", "message": None}
            except ImportError:
                logger.warning("PyMuPDF not installed — skipping PDF. Run: pip install PyMuPDF")
            except Exception as exc:
                logger.debug("PDF extraction failed for %s: %s — trying Google cache", url[:60], exc)
                # Fallback: try Google cache via the shared session.
                cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
                try:
                    resp = session.get(cache_url, timeout=timeout)
                    resp.raise_for_status()
                    config = trafilatura.settings.use_config()
                    content = trafilatura.extract(
                        resp.content, include_comments=False,
                        include_tables=True, deduplicate=True, config=config,
                    )
                    if content:
                        return {"content": content, "type": "PDF_CACHE", "message": None}
                except Exception:
                    pass
            return {"content": None, "type": "PDF", "message": "PDF extraction failed"}

        # --- Skip other binary formats ---
        if ext in ("doc", "docx", "xls", "xlsx", "ppt", "pptx"):
            return {"content": None, "type": "DOCUMENT", "message": f"Unable to extract content from {ext.upper()} file"}

        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            config = trafilatura.settings.use_config()
            content = trafilatura.extract(
                resp.content, include_comments=False,
                include_tables=True, deduplicate=True, config=config,
            )
            return {"content": content, "type": "HTML", "message": None}
        except Exception as exc:
            logger.debug("Error extracting from %s: %s", url[:60], exc)
        return {"content": None, "type": "ERROR", "message": "Failed to extract content"}

    # High-signal sanctions-only terms (Task 6 — second NLP pass)
    SANCTIONS_ONLY_TERMS = [
        "ofac", "sdn", "specially designated", "entity list", "denied persons",
        "sanctions violation", "sanctions evasion", "enforcement action",
        "civil penalty", "consent agreement", "debarment", "blocked person",
        "blocked property", "embargo violation", "export control violation",
        "money laundering", "terrorist financing", "proliferation financing",
    ]

    def analyze_content(self, extraction_result: dict, url: str, doc=None) -> dict:
        # Accept any extraction type that produced content (HTML, PDF, PDF_CACHE, SNIPPET_FALLBACK)
        if not extraction_result.get("content"):
            return {
                "url": url,
                "risk_level": "UNKNOWN",
                "confidence": 0,
                "findings": [],
                "relevant_excerpts": [],
                "excluded_references": [],
                "extracted_content": "",
                "extraction_type": extraction_result.get("type", "UNKNOWN"),
                "extraction_message": extraction_result.get("message"),
            }

        text = extraction_result["content"]
        extraction_type = extraction_result.get("type", "HTML")

        # Expanded high-risk email TLDs to cover all sanctioned country codes
        high_risk_emails = re.findall(
            r"[\w\.-]+@[\w\.-]+\.(cu|ir|kp|sy|ru|by|mm|ve)\b", text, re.IGNORECASE
        )
        # Reuse a pre-computed doc from batched nlp.pipe (B1) when the caller
        # provides one. Falls back to a per-URL parse so old call sites still work.
        if doc is None:
            doc = self.nlp(text[:1_000_000])

        findings, relevant_excerpts, risk_scores = [], [], []
        excluded_references: list = []
        seen_excluded: set = set()

        for email in high_risk_emails:
            email_sentence = f"Found high-risk email domain: {email}"
            findings.append({
                "relevant": True,
                "risk_type": "HIGH_RISK_EMAIL",
                "risk_score": 95,
                "confidence": 98,
                "sentence": email_sentence,
            })
            relevant_excerpts.append({
                "text": email_sentence,
                "trigger_sentence": email_sentence,
                "risk_type": "HIGH_RISK_EMAIL",
                "confidence": 98,
                "risk_score": 95,
                "note": None,
            })
            risk_scores.append(95)

        # Keep Spans (not strings) so _analyze_context can reuse doc-level
        # entities and dependencies instead of re-parsing each sentence.
        sentences = list(doc.sents)
        seen_contexts: set = set()  # Deduplicate overlapping context windows

        # --- Pass 1: country-mention sentences ---
        for i, sentence in enumerate(sentences):
            sent_text = sentence.text
            if self.country_pattern and self.country_pattern.search(sent_text):
                ctx_start = max(0, i - 3)
                ctx_end = min(len(sentences), i + 4)
                context_window = " ".join(s.text for s in sentences[ctx_start:ctx_end])

                context_analysis = self._analyze_context(sentence, context_window)

                if context_analysis.get("excluded_reference"):
                    ex_key = context_window[:120]
                    if ex_key not in seen_excluded:
                        seen_excluded.add(ex_key)
                        excluded_references.append({
                            "text": context_window,
                            "trigger_sentence": sent_text,
                            "matched_phrase": context_analysis["matched_phrase"],
                            "risk_type": context_analysis["risk_type"],
                            "note": context_analysis["note"],
                        })
                    continue

                if not context_analysis.get("relevant", False):
                    continue

                # Task 2 fix: pass context_window (not full text) so currency symbols
                # only trigger HIGH risk when they appear near a country mention
                risk_assessment = self.risk_assessor.calculate_risk_score(
                    context_analysis, context_window, self.FINANCIAL_INDICATORS
                )
                context_analysis.update(risk_assessment)
                findings.append(context_analysis)
                ctx_key = context_window[:120]
                if ctx_key not in seen_contexts:
                    seen_contexts.add(ctx_key)
                    relevant_excerpts.append({
                        "text": context_window,
                        "trigger_sentence": sent_text,
                        "risk_type": risk_assessment["risk_type"],
                        "confidence": risk_assessment["confidence"],
                        "risk_score": risk_assessment["risk_score"],
                        "note": risk_assessment.get("note"),
                    })
                risk_scores.append(risk_assessment["risk_score"])

        # --- Pass 2: sanctions-term sentences (Task 6) ---
        # Catches SDN/OFAC/enforcement mentions that don't name a country
        for i, sentence in enumerate(sentences):
            sent_text = sentence.text
            sent_lower = sent_text.lower()
            if any(term in sent_lower for term in self.SANCTIONS_ONLY_TERMS):
                # Skip if already captured in pass 1 via country mention
                ctx_start = max(0, i - 3)
                ctx_end = min(len(sentences), i + 4)
                context_window = " ".join(s.text for s in sentences[ctx_start:ctx_end])
                ctx_key = context_window[:120]
                if ctx_key in seen_contexts:
                    continue
                seen_contexts.add(ctx_key)

                # Check false-positive phrases before adding
                if any(phrase in context_window.lower() for phrase in self.false_positive_phrases):
                    continue

                risk_score = 60
                confidence = 80
                # Boost if multiple sanctions terms appear
                matches = sum(1 for t in self.SANCTIONS_ONLY_TERMS if t in context_window.lower())
                if matches >= 3:
                    risk_score = 75
                    confidence = 88

                finding = {
                    "relevant": True,
                    "risk_type": "SANCTIONS_REGULATORY_MENTION",
                    "risk_score": risk_score,
                    "confidence": confidence,
                    "sentence": sent_text,
                }
                findings.append(finding)
                relevant_excerpts.append({
                    "text": context_window,
                    "trigger_sentence": sent_text,
                    "risk_type": "SANCTIONS_REGULATORY_MENTION",
                    "confidence": confidence,
                    "risk_score": risk_score,
                    "note": None,
                })
                risk_scores.append(risk_score)

        if not risk_scores:
            risk_level, confidence = "NONE", 90
        else:
            avg_risk = sum(risk_scores) / len(risk_scores)
            max_risk = max(risk_scores)
            risk_level = (
                "HIGH" if max_risk >= 80
                else "MEDIUM" if avg_risk >= 40
                else "LOW" if avg_risk >= 15
                else "MINIMAL"
            )
            confidence = sum(f["confidence"] for f in findings) / len(findings)

        # Truncate the preserved page content so a 100-URL job doesn't blow
        # past the SQLite row size / client payload budget. 40k chars ≈ 8–10k
        # tokens, which is still plenty for an analyst to skim and well under
        # the per-link LLM context cap.
        preserved_text = text if extraction_type != "SNIPPET_FALLBACK" else text
        if preserved_text and len(preserved_text) > 40_000:
            preserved_text = preserved_text[:40_000] + "\n\n[...truncated]"

        # Tie-breaker: excerpts whose text pairs a currency/amount indicator
        # with an action-of-transfer verb get a ranking boost so concrete
        # documented transactions beat abstract policy/advocacy statements
        # when risk_score + confidence are otherwise equal. The boost affects
        # ordering only — the stored risk_score is unchanged.
        _TX_AMOUNT_MARKERS = ("$", "€", "£", "usd", "eur", "valued at", "worth")
        _TX_ACTION_MARKERS = (
            "present", "presented", "deliver", "delivered", "donat", "gift",
            "transfer", "ship", "shipped", "sent", "provided", "funded",
            "financing", "paid", "payment",
        )

        def _tx_boost(ex: dict) -> int:
            blob = (ex.get("text") or "").lower()
            has_amount = any(m in blob for m in _TX_AMOUNT_MARKERS)
            has_action = any(m in blob for m in _TX_ACTION_MARKERS)
            return 5 if (has_amount and has_action) else 0

        return {
            "url": url,
            "risk_level": risk_level,
            "confidence": round(confidence, 2),
            "findings": findings,
            "relevant_excerpts": sorted(
                relevant_excerpts,
                key=lambda e: (
                    e.get("risk_score", 0) + _tx_boost(e),
                    e.get("confidence", 0),
                ),
                reverse=True,
            )[:5],
            "excluded_references": excluded_references,
            "extracted_content": preserved_text,
            "extraction_type": extraction_type,
            "extraction_message": None,
            "language": extraction_result.get("language"),
        }

    def _analyze_context(self, sentence, context: str) -> dict:
        # `sentence` may be either a spaCy Span (preferred, reuses doc-level
        # NER) or a bare string (snippet-fallback path). Normalise up front.
        if isinstance(sentence, str):
            sent_text = sentence
            sent_ents = None
        else:
            sent_text = sentence.text
            sent_ents = sentence.ents

        context_lower = context.lower()

        # Licensed-activity pre-check — humanitarian / OFAC-license family near
        # a sanctioned jurisdiction is HIGH by policy. Lawful only for OFAC-
        # licensed entities, so always surface for analyst verification.
        matched_license_phrase = next(
            (p for p in self.LICENSED_ACTIVITY_PHRASES if p in context_lower),
            None,
        )
        if matched_license_phrase:
            return {
                "relevant": True,
                "risk_type": "LICENSED_ACTIVITY_MENTION",
                "risk_score": 85,
                "confidence": 90,
                "sentence": sent_text,
                "note": (
                    f"Matched '{matched_license_phrase}' near a sanctioned jurisdiction. "
                    "Such activity is lawful only for OFAC-licensed entities — verify authorization."
                ),
            }

        # Confirmed false positives → not a risk finding, but preserved on the
        # per-URL result as an audit trail so the analyst can see the tool saw
        # and excluded them.
        matched_confirmed_fp = next(
            (p for p in self.CONFIRMED_NON_SANCTIONS_REFERENCES if p in context_lower),
            None,
        )
        if matched_confirmed_fp:
            return {
                "relevant": False,
                "excluded_reference": True,
                "matched_phrase": matched_confirmed_fp,
                "risk_type": "NON_SANCTIONS_REFERENCE",
                "sentence": sent_text,
                "note": (
                    f"Matched '{matched_confirmed_fp}' — known non-sanctions reference "
                    "(product / cuisine / idiom)."
                ),
            }

        # Deferred-for-review phrases: silently skip (existing behavior).
        if any(phrase in context_lower for phrase in self.DEFERRED_FOR_REVIEW):
            return {"relevant": False}

        # Reuse doc-level NER when we have a Span; only re-parse for bare strings.
        ents = sent_ents if sent_ents is not None else self.nlp(sent_text).ents
        for ent in ents:
            if ent.label_ == "PERSON" and any(v in ent.text.lower() for v in self.country_variations):
                return {
                    "relevant": True, "risk_type": "PERSON_NAME_MATCH",
                    "risk_score": 10, "confidence": 90, "sentence": sent_text,
                }

        # --- Zero-shot NLI classification (A2) ---
        # Runs before the keyword heuristic. When the classifier is disabled
        # or fails, we fall through to the keyword path so behaviour is
        # unchanged in local dev without transformers installed.
        nli = classify_context_nli(context, self.sanctioned_country)
        if nli is not None:
            if not nli.get("relevant", False):
                # "Unrelated" verdict — drop the finding entirely.
                return {"relevant": False}
            return {
                "relevant": True,
                "risk_type": nli["risk_type"],
                "risk_score": min(100, nli["risk_score"]),
                "confidence": min(95, nli["confidence"]),
                "sentence": sent_text,
                "nli_label": nli.get("_nli_top_label"),
                "nli_score": nli.get("_nli_score"),
            }

        # --- Keyword fallback (original path) ---
        business_score = sum(10 for kw in self.business_keywords if kw in context_lower)
        compliance_score = sum(5 for kw in self.compliance_keywords if kw in context_lower)
        negative_score = sum(15 for kw in self.negative_indicators if kw in sent_text.lower())

        if business_score > 20 and negative_score < 15:
            risk_type, risk_score, confidence = "DIRECT_BUSINESS", 80, 85
        elif business_score > 10 and negative_score < 15:
            risk_type, risk_score, confidence = "INDIRECT_BUSINESS", 50, 70
        elif compliance_score > 10 or negative_score > 15:
            risk_type, risk_score, confidence = "COMPLIANCE_MENTION", 20, 80
        else:
            risk_type, risk_score, confidence = "GENERAL_MENTION", 10, 60

        if any(kw in context_lower for kw in ["sanction", "ofac", "sdn", "embargo", "prohibited", "restricted"]):
            confidence += 10
            if negative_score > 0:
                risk_score = max(10, risk_score - 30)

        return {
            "relevant": True,
            "risk_type": risk_type,
            "risk_score": min(100, risk_score),
            "confidence": min(95, confidence),
            "sentence": sent_text,
        }


# ==========================================================================
# Google search wrapper
# ==========================================================================

class EnhancedSanctionsSearcher:
    def __init__(self, country: str, audit_logger=None):
        self.sanctioned_country = country
        base_keywords = ["OFAC", "SDN", "sanctions", "sanctioned", "blocked", "prohibited", "embargo", "restricted"]
        country_keywords = [country]
        extras = {
            "Cuba": ["Cuban"], "Iran": ["Iranian"], "Syria": ["Syrian"],
            "North Korea": ["North Korean", "DPRK"], "Ukraine": ["Ukrainian"], "Crimea": ["Crimean"],
            "Russia": ["Russian"], "Belarus": ["Belarusian"],
            "Myanmar": ["Burmese", "Burma"], "Venezuela": ["Venezuelan"],
        }
        country_keywords.extend(extras.get(country, []))
        self.sanctions_keywords = base_keywords + country_keywords
        self.content_analyzer = get_analyzer(country)
        self.analyzed_urls: set = set()
        # URLs that were returned by Google but intentionally skipped because
        # their domain is on the excluded list (Wikipedia, LinkedIn, etc.).
        # Surfaced in the final report so analysts can see what the tool did
        # NOT attempt.
        self.excluded_urls: List[dict] = []
        self.audit_logger = audit_logger

    @with_retry(max_retries=3, backoff_factor=2, reraise=False)
    def _fetch_search_page(self, query: str, start_index: int) -> Optional[dict]:
        import time
        params = {
            "q": query,
            "key": API_KEY,
            "cx": SEARCH_ENGINE_ID,
            "start": start_index,
        }
        response = requests.get("https://www.googleapis.com/customsearch/v1", params=params, timeout=15)
        response.raise_for_status()
        time.sleep(1)
        return response.json()

    def search_google(self, query: str, num_pages: int = 2) -> List[dict]:
        if not API_KEY or not SEARCH_ENGINE_ID:
            logger.error("GOOGLE_API_KEY or GOOGLE_CSE_ID environment variable not set.")
            return []

        import time as _time
        started_at = _time.perf_counter()
        all_results: List[dict] = []
        for page_num in range(num_pages):
            try:
                data = self._fetch_search_page(query, page_num * 10 + 1)
                if not data or "items" not in data:
                    break
                for item in data.get("items", []):
                    title = item.get("title")
                    link = item.get("link")
                    snippet = item.get("snippet")
                    if title and link and link.startswith("http"):
                        all_results.append({
                            "title": title, "link": link, "snippet": snippet,
                            "query": query, "page": page_num + 1,
                            "timestamp": datetime.now().isoformat(),
                            "sanctions_indicators": self._check_sanctions_indicators(title, snippet),
                        })
            except Exception as exc:
                logger.error("Search error for query '%s': %s", query, exc)
                break

        elapsed_ms = (_time.perf_counter() - started_at) * 1000.0
        logger.info("Found %d results for query: '%s'", len(all_results), query)

        if self.audit_logger is not None:
            try:
                self.audit_logger.log_search(
                    query=query,
                    result_count=len(all_results),
                    elapsed_ms=elapsed_ms,
                )
            except Exception as exc:
                logger.warning("Audit log_search failed: %s", exc)

        return all_results

    def _check_sanctions_indicators(self, title: Optional[str], snippet: Optional[str]) -> List[str]:
        combined = f"{title or ''} {snippet or ''}".lower()
        return list({kw for kw in self.sanctions_keywords if kw.lower() in combined})

    def analyze_search_results(self, search_results: List[dict], max_urls: int = 50) -> List[dict]:
        """Two-stage pipeline (B1+B3):

        1. I/O pool (8 workers) runs content extraction in parallel. Network-
           bound; releases the GIL so workers don't starve each other.
        2. NLP stage batches the extracted texts through ``nlp.pipe`` so the
           transformer pipeline can amortise per-batch overhead. On CPU ``lg``
           this is a ~1.5–2× win; on GPU ``trf`` it's 5–10×.
        """
        logger.info("Analyzing content from up to %d URLs…", max_urls)
        urls_to_analyze: List[tuple] = []
        for r in search_results[:max_urls]:
            link = r["link"]
            if link in self.analyzed_urls:
                continue
            if self._is_excluded_url(link):
                # Capture with metadata so the UI can tell analysts which
                # pages we deliberately didn't fetch (and why).
                domain = urlparse(link).netloc.lower()
                self.excluded_urls.append({
                    "url": link,
                    "title": r.get("title") or "",
                    "snippet": r.get("snippet") or "",
                    "domain": domain,
                    "country": self.sanctioned_country,
                })
                continue
            urls_to_analyze.append((link, r))
        for url, _ in urls_to_analyze:
            self.analyzed_urls.add(url)

        if not urls_to_analyze:
            logger.info("No new URLs to analyze.")
            return []

        # --- Stage 1: parallel extraction (I/O-bound) ---
        # 16 workers across ~50 different hosts; per-host concurrency is
        # bounded by the shared HTTP session's pool_maxsize (20), so no
        # single host gets hammered.
        extracted: List[tuple] = []  # (url, original_result, extraction)
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            fut_map = {
                pool.submit(self._extract_only, url, result): (url, result)
                for url, result in urls_to_analyze
            }
            for fut in concurrent.futures.as_completed(fut_map):
                url, original = fut_map[fut]
                try:
                    extraction = fut.result()
                    extracted.append((url, original, extraction))
                except Exception as exc:
                    logger.error("ERROR extracting %s: %s", url[:60], exc)

        if not extracted:
            return []

        # --- Stage 2: batched NLP ---
        # Collect (text, index) for items that have content; snippet-fallbacks
        # go through nlp.pipe too so the tokens are sentencized.
        texts_with_idx: List[tuple] = []
        for i, (_, _, extraction) in enumerate(extracted):
            content = extraction.get("content") or ""
            if content:
                texts_with_idx.append((content[:1_000_000], i))

        docs_by_idx: Dict[int, object] = {}
        if texts_with_idx:
            texts = [t for t, _ in texts_with_idx]
            indices = [i for _, i in texts_with_idx]
            try:
                piped = self.content_analyzer.nlp.pipe(texts, batch_size=16)
                for idx, piped_doc in zip(indices, piped):
                    docs_by_idx[idx] = piped_doc
            except Exception as exc:
                logger.warning("nlp.pipe batched parse failed (%s) — falling back to per-URL parse", exc)

        analyzed: List[dict] = []
        symbols = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢", "MINIMAL": "⚪", "NONE": "✅"}
        for i, (url, original, extraction) in enumerate(extracted):
            try:
                analysis = self.content_analyzer.analyze_content(
                    extraction, url, doc=docs_by_idx.get(i)
                )
                analysis["original_title"] = original["title"]
                analysis["original_snippet"] = original["snippet"]
                analyzed.append({**original, **analysis})
                sym = symbols.get(analysis.get("risk_level", ""), "❓")
                logger.info(
                    "%s %s – %s… (%d/%d)",
                    sym, analysis.get("risk_level", "?"), url[:60], i + 1, len(extracted),
                )
            except Exception as exc:
                logger.error("ERROR analyzing %s: %s", url[:60], exc)

        logger.info("Analysis complete. Processed %d URLs.", len(analyzed))
        return analyzed

    def _extract_only(self, url: str, original_result: dict) -> dict:
        """I/O-only stage of the pipeline — runs in the 8-worker extraction pool.

        Produces the same ``extraction`` dict shape ``analyze_content`` expects,
        including the SNIPPET_FALLBACK synth when primary extraction yields nothing.
        """
        extraction = self.content_analyzer.extract_content_from_url(
            url, audit_logger=self.audit_logger
        )
        if not extraction.get("content"):
            title = original_result.get("title") or ""
            snippet = original_result.get("snippet") or ""
            combined = f"{title}. {snippet}".strip()
            if combined and combined != ".":
                extraction = {
                    "content": combined,
                    "type": "SNIPPET_FALLBACK",
                    "message": None,
                }
                logger.debug("Using snippet fallback for %s", url[:60])
        return extraction

    def _analyze_single_url(self, url: str, original_result: dict) -> dict:
        """Legacy single-URL path — kept for any callers that still invoke it
        directly. The batched pipeline is ``analyze_search_results``.
        """
        extraction = self._extract_only(url, original_result)
        analysis = self.content_analyzer.analyze_content(extraction, url)
        analysis["original_title"] = original_result["title"]
        analysis["original_snippet"] = original_result["snippet"]
        return analysis

    def _is_excluded_url(self, url: str) -> bool:
        excluded = ["wikipedia.org", "youtube.com", "twitter.com", "facebook.com", "linkedin.com", "instagram.com", "reddit.com"]
        return any(d in urlparse(url).netloc for d in excluded)

    def perform_enhanced_site_search(
        self, website: str, additional_searches: bool = True,
        business_name: str = "", legal_name: str = "",
    ) -> dict:
        logger.info("[Enhanced Search for %s on %s]", self.sanctioned_country, website)
        all_results: List[dict] = []

        # --- Standard country × site queries (2 pages each for deeper coverage) ---
        if website:
            all_results.extend(self.search_google(f'"{self.sanctioned_country}" site:{website}', num_pages=2))
            if additional_searches:
                all_results.extend(self.search_google(
                    f'"{website}" "{self.sanctioned_country}" (facebook OR linkedin OR twitter OR instagram)',
                    num_pages=1,
                ))
                all_results.extend(self.search_google(
                    f'"{website}" "{self.sanctioned_country}" -site:{website}', num_pages=2
                ))

        unique = list({r["link"]: r for r in all_results}.values())
        if unique:
            analyzed = self.analyze_search_results(unique, max_urls=50)
            return {
                "search_results": unique,
                "analyzed_results": analyzed,
                "total_urls_analyzed": len(analyzed),
                "excluded_urls": list(self.excluded_urls),
            }
        return {
            "search_results": [],
            "analyzed_results": [],
            "total_urls_analyzed": 0,
            "excluded_urls": list(self.excluded_urls),
        }


# ==========================================================================
# FIX 5: Name co-occurrence searcher – no hardcoded "Iran"
# ==========================================================================

class NameCooccurrenceSearcher:
    def __init__(
        self,
        business_name: str,
        legal_name: str,
        sanctioned_entities: List[str],
        audit_logger=None,
        custom_variations: Optional[Dict[str, List[str]]] = None,
    ):
        self.business_name = (business_name or "").strip()
        self.legal_name = (legal_name or "").strip()
        self.entities = sanctioned_entities
        self.custom_variations = custom_variations or {}
        # Warm the analyzer cache for any custom countries so `get_analyzer`
        # lookups later (inside _analyze_url / build_name_country_queries)
        # see the user-supplied variation list.
        for name, vars_ in self.custom_variations.items():
            if vars_:
                get_analyzer(name, variations_override=vars_)
        self.cmap = get_all_country_variations_map(self.entities, overrides=self.custom_variations)
        self.google_searcher = EnhancedSanctionsSearcher(
            self.entities[0] if self.entities else "Iran", audit_logger=audit_logger,
        )
        self.audit_logger = audit_logger
        self.excluded_domains = [
            "wikipedia.org", "youtube.com", "twitter.com", "facebook.com",
            "linkedin.com", "instagram.com", "reddit.com",
        ]
        # Same surfacing contract as EnhancedSanctionsSearcher: track URLs
        # that needed analyst attention (extraction failed + no snippet
        # fallback useful) and URLs we refused to fetch (excluded domains).
        self.excluded_urls: List[dict] = []
        self.failed_urls: List[dict] = []

    def _is_excluded(self, url: str) -> bool:
        return any(d in urlparse(url).netloc.lower() for d in self.excluded_domains)

    def _collect_results_for_name(self, name: str, label: str, num_pages: int = 1) -> Dict[str, dict]:
        url_map: Dict[str, dict] = {}
        if not name:
            return url_map
        for q in build_name_country_queries(name, self.cmap):
            for item in self.google_searcher.search_google(q, num_pages=num_pages):
                link = item.get("link", "")
                if not link or not link.startswith("http"):
                    continue
                if self._is_excluded(link):
                    domain = urlparse(link).netloc.lower()
                    self.excluded_urls.append({
                        "url": link,
                        "title": item.get("title", "") or "",
                        "snippet": item.get("snippet", "") or "",
                        "domain": domain,
                        "country": None,  # name-co search is country-agnostic
                    })
                    continue
                if link in url_map:
                    url_map[link]["matched_types"].add(label)
                else:
                    url_map[link] = {
                        "original_title": item.get("title", ""),
                        "original_snippet": item.get("snippet", ""),
                        "matched_types": {label},
                    }
        return url_map

    def _analyze_url(self, url: str, meta: dict, threshold: int = 85) -> List[dict]:
        # FIX 5: Use a country-agnostic fetcher for URL extraction.
        # We pick the first entity's analyzer solely for its fetch capability;
        # country-specific analysis happens per matched country below.
        fetcher = get_analyzer(self.entities[0] if self.entities else "Iran")
        extraction = fetcher.extract_content_from_url(url, audit_logger=self.audit_logger)
        if not extraction.get("content"):
            # Snippet fallback for parity with the main country pipeline — if
            # the page couldn't be extracted but Google returned a title/
            # snippet, synthesize a SNIPPET_FALLBACK so the analyst still gets
            # something to reason about. These entries are flagged for analyst
            # review in the final report.
            title = (meta.get("original_title") or "").strip()
            snippet = (meta.get("original_snippet") or "").strip()
            combined = f"{title}. {snippet}".strip()
            if combined and combined != ".":
                extraction = {
                    "content": combined,
                    "type": "SNIPPET_FALLBACK",
                    "message": None,
                }
            else:
                # Nothing to analyse — record the failure so the UI can flag
                # it. The original Google metadata is preserved so analysts
                # can manually open the URL.
                self.failed_urls.append({
                    "url": url,
                    "title": title,
                    "snippet": snippet,
                    "extraction_type": extraction.get("type", "ERROR"),
                    "extraction_message": extraction.get("message") or "No content extracted",
                    "country": None,
                    "source": "NAME",
                })
                return []

        text = extraction.get("content") or ""
        has_business = fuzzy_name_in_text(text, self.business_name, threshold) if self.business_name else False
        has_legal = fuzzy_name_in_text(text, self.legal_name, threshold) if self.legal_name else False
        if not (has_business or has_legal):
            return []

        text_lower = text.lower()
        matched_countries = [
            c for c, vars_ in self.cmap.items() if any(v in text_lower for v in vars_)
        ]
        if not matched_countries:
            return []

        matched_types = set(meta.get("matched_types", set()))
        if has_business:
            matched_types.add("BUSINESS")
        if has_legal:
            matched_types.add("LEGAL")

        if "BUSINESS" in matched_types and "LEGAL" in matched_types:
            matched_label = "BOTH"
        elif "BUSINESS" in matched_types:
            matched_label = "BUSINESS"
        elif "LEGAL" in matched_types:
            matched_label = "LEGAL"
        else:
            matched_label = "NONE"

        matched_names = []
        if has_business and self.business_name:
            matched_names.append(self.business_name)
        if has_legal and self.legal_name and self.legal_name != self.business_name:
            matched_names.append(self.legal_name)

        # Emit one result per matched country — previously we collapsed to the
        # single highest-risk country and silently dropped the rest.
        results: List[dict] = []
        for country in matched_countries:
            analyzer = get_analyzer(country)
            analysis = analyzer.analyze_content(extraction, url)
            findings = analysis.get("findings", [])
            for f in findings:
                f["risk_score"] = min(100, f.get("risk_score", 0) + 10)
                f["confidence"] = min(100, f.get("confidence", 0) + 10)

            if findings:
                max_score = max(f.get("risk_score", 0) for f in findings)
                avg_score = sum(f.get("risk_score", 0) for f in findings) / len(findings)
                new_level = (
                    "HIGH" if max_score >= 80
                    else "MEDIUM" if avg_score >= 40
                    else "LOW" if avg_score >= 15
                    else "MINIMAL"
                )
            else:
                new_level = analysis.get("risk_level", "MINIMAL")

            new_conf = min(100, (analysis.get("confidence") or 0) + 10)
            results.append({
                "country": country,
                "risk_level": new_level,
                "url": url,
                "original_title": meta.get("original_title", ""),
                "original_snippet": meta.get("original_snippet", ""),
                "confidence": round(new_conf, 2),
                "relevant_excerpts": analysis.get("relevant_excerpts", [])[:5],
                "excluded_references": analysis.get("excluded_references", []),
                "extraction_type": analysis.get("extraction_type", "HTML"),
                "extraction_message": analysis.get("extraction_message", ""),
                "language": analysis.get("language"),
                "source": "NAME",
                "matched_name_type": matched_label,
                "matched_names": matched_names,
            })
        return results

    def perform(self, num_pages: int = 1, threshold: int = 85, max_workers: int = 10) -> List[dict]:
        url_meta: Dict[str, dict] = {}
        for url, meta in self._collect_results_for_name(self.business_name, "BUSINESS", num_pages).items():
            url_meta[url] = meta
        for url, meta in self._collect_results_for_name(self.legal_name, "LEGAL", num_pages).items():
            if url in url_meta:
                url_meta[url]["matched_types"] |= meta["matched_types"]
            else:
                url_meta[url] = meta

        if not url_meta:
            return []

        results: List[dict] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._analyze_url, url, meta, threshold): (url, meta)
                for url, meta in url_meta.items()
            }
            for fut in concurrent.futures.as_completed(futures):
                try:
                    res = fut.result()
                    if res:
                        # _analyze_url returns a list (one entry per matched country).
                        results.extend(res)
                except Exception as exc:
                    logger.error("Error analysing %s: %s", futures[fut][0][:60], exc)

        # Dedup on (url, country). If the same (url, country) arrives twice
        # (e.g. via both business- and legal-name paths), keep the higher-risk
        # record and merge the matched-name metadata.
        rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "MINIMAL": 0, "NONE": -1, "UNKNOWN": -2}
        out: Dict[tuple, dict] = {}
        for r in results:
            key = (r["url"], r["country"])
            if key not in out:
                out[key] = r
            else:
                existing = out[key]
                if rank.get(r["risk_level"], 0) > rank.get(existing["risk_level"], 0):
                    out[key] = r
                elif rank.get(r["risk_level"], 0) == rank.get(existing["risk_level"], 0):
                    if r.get("confidence", 0) > existing.get("confidence", 0):
                        out[key] = r
                mt = {existing["matched_name_type"], r["matched_name_type"]}
                if "BUSINESS" in mt and "LEGAL" in mt:
                    out[key]["matched_name_type"] = "BOTH"
                out[key]["matched_names"] = list(
                    set(out[key].get("matched_names", []) + r.get("matched_names", []))
                )

        return list(out.values())


# ==========================================================================
# Report generation
# FIX 2 & 9 & 10: html.escape() before highlighting; regexes compiled once.
# ==========================================================================

def _is_running_in_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx  # type: ignore
        return get_script_run_ctx() is not None
    except Exception:
        return False


def _build_phrase_pattern(phrase: str) -> str:
    if not phrase:
        return ""
    p = phrase.strip()
    esc = re.escape(p)
    esc = re.sub(r"\\\s+", r"\\s+", esc)
    start_b = r"\b" if re.match(r"^\w", p, flags=re.UNICODE) else ""
    end_b = r"\b" if re.match(r".*\w$", p, flags=re.UNICODE) else ""
    return f"{start_b}{esc}{end_b}"


def _highlight_with_class(text: str, pattern: re.Pattern) -> str:
    """Insert highlight spans. Input MUST already be HTML-escaped."""
    def repl(m: re.Match) -> str:
        return f'<span class="sanction-highlight">{m.group(0)}</span>'
    try:
        return pattern.sub(repl, text)
    except re.error:
        return text


def _center_around_first_match(text: str, pattern: re.Pattern, max_len: int = 800) -> str:
    if not text or len(text) <= max_len:
        return text
    try:
        m = pattern.search(text)
    except re.error:
        m = None
    if not m:
        return (text[:max_len] + "…") if len(text) > max_len else text
    mid = (m.start() + m.end()) // 2
    half = max_len // 2
    start = max(0, mid - half)
    end = min(len(text), start + max_len)
    if end - start < max_len and start > 0:
        start = max(0, end - max_len)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet += "…"
    return snippet


def _safe_highlight(raw_text: str, *patterns: re.Pattern) -> str:
    """
    FIX 2: Escape HTML FIRST, then apply highlighting spans so that
    scraped web content can never inject arbitrary HTML into the report.
    """
    escaped = html.escape(raw_text)
    for pattern in patterns:
        escaped = _highlight_with_class(escaped, pattern)
    return escaped


def _build_trigger_regex(unique_countries: set) -> re.Pattern:
    """FIX 9: Compile the trigger regex once per report generation call."""
    financial_indicators = SanctionsContentAnalyzer.FINANCIAL_INDICATORS
    all_business: set = set()
    all_compliance: set = set()
    for country in unique_countries or ["Iran"]:
        an = get_analyzer(country)
        all_business.update(an.business_keywords)
        all_compliance.update(an.compliance_keywords)
    all_compliance.add("restricted")

    trigger_parts: List[str] = []
    for term in (set(financial_indicators) | all_business | all_compliance) - {"$", "€", "£"}:
        trigger_parts.append(_build_phrase_pattern(term))
    trigger_parts += [re.escape("$"), re.escape("€"), re.escape("£")]
    trigger_parts.append(r"[\w\.-]+@[\w\.-]+\.(?:cu|ir|kp|sy|ru|by|mm|ve)\b")
    return re.compile("(?:" + "|".join(p for p in trigger_parts if p) + ")", flags=re.IGNORECASE)


LIST_SOURCE_LABELS = {
    "OFAC_SDN": "OFAC SDN", "OFAC_CONS": "OFAC Non-SDN",
    "EU": "EU", "UN_SC": "UN", "OFSI_UK": "OFSI (UK)",
    "BIS": "BIS", "BIS_ENTITY": "BIS Entity List", "BIS_DENIED": "BIS Denied Persons",
    "BIS_UVL": "BIS Unverified", "BIS_MEU": "BIS Mil. End User",
    "CSL": "US CSL", "STATE_DEBARRED": "State Debarred",
    "STATE_NONPRO": "State Nonprolif.",
    "OPENSANCTIONS": "OpenSanctions",
    "OPENSANCTIONS_AU": "Australia (DFAT)", "OPENSANCTIONS_CA": "Canada (SEMA)",
    "OPENSANCTIONS_CH": "Switzerland (SECO)", "OPENSANCTIONS_JP": "Japan (MOF)",
    "INTERPOL": "INTERPOL",
}

LIST_SOURCE_URLS = {
    "OFAC_SDN": "https://sanctionssearch.ofac.treas.gov/",
    "OFAC_CONS": "https://sanctionssearch.ofac.treas.gov/",
    "EU": "https://www.sanctionsmap.eu/",
    "UN_SC": "https://www.un.org/securitycouncil/sanctions/information",
    "OFSI_UK": "https://sanctionssearchapp.ofsi.hmtreasury.gov.uk/",
    "BIS_ENTITY": "https://www.trade.gov/consolidated-screening-list",
    "BIS_DENIED": "https://www.trade.gov/consolidated-screening-list",
    "BIS_UVL": "https://www.trade.gov/consolidated-screening-list",
    "BIS_MEU": "https://www.trade.gov/consolidated-screening-list",
    "CSL": "https://www.trade.gov/consolidated-screening-list",
    "STATE_DEBARRED": "https://www.trade.gov/consolidated-screening-list",
    "STATE_NONPRO": "https://www.trade.gov/consolidated-screening-list",
    "OPENSANCTIONS": "https://www.opensanctions.org/",
    "INTERPOL": "https://www.interpol.int/en/How-we-work/Notices/Red-Notices",
}


def generate_enhanced_html_report(
    all_reports: List[dict],
    website: str,
    social_media_links: Dict[str, str],
    name_co_results: Optional[List[dict]] = None,
    business_name: Optional[str] = None,
    legal_name: Optional[str] = None,
    llm_verdict: Optional[dict] = None,
    list_screening: Optional[List[dict]] = None,
    regulatory_report: Optional[dict] = None,
    needs_analyst_review: Optional[List[dict]] = None,
    not_attempted: Optional[List[dict]] = None,
) -> str:
    name_co_results = name_co_results or []
    business_name = business_name or ""
    legal_name = legal_name or ""
    list_screening = list_screening or []
    needs_analyst_review = needs_analyst_review or []
    not_attempted = not_attempted or []

    # Defensive: drop any legacy Sanctions/OFAC pseudo-country so filter buttons,
    # stat totals, and country sections contain only real jurisdictions.
    all_reports = [r for r in all_reports if r.get("country") != "Sanctions/OFAC"]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_site = re.sub(r'[\\/*?:"<>|]', "", website)
    filename = f"enhanced_sanctions_report_{safe_site.replace('.', '_')}_{timestamp}.html"
    base_dir = tempfile.gettempdir() if _is_running_in_streamlit() else os.path.join(os.path.expanduser("~"), "Desktop")
    filepath = os.path.join(base_dir, filename)

    # Summary statistics
    total_high = sum(1 for r in all_reports for ar in r["analyzed_results"] if ar.get("risk_level") == "HIGH")
    total_medium = sum(1 for r in all_reports for ar in r["analyzed_results"] if ar.get("risk_level") == "MEDIUM")
    total_low = sum(1 for r in all_reports for ar in r["analyzed_results"] if ar.get("risk_level") == "LOW")
    total_minimal = sum(1 for r in all_reports for ar in r["analyzed_results"] if ar.get("risk_level") == "MINIMAL")
    total_analyzed = sum(r.get("total_urls_analyzed", 0) for r in all_reports)
    total_pdfs = sum(
        1 for r in all_reports for ar in r["analyzed_results"]
        if ar.get("extraction_type") in ("PDF", "PDF_CACHE")
    )
    regulatory_count = len((regulatory_report or {}).get("analyzed_results", []))
    is_sanctioned_direct = any((m.get("score") or 0) >= 90 for m in list_screening)

    # Unique countries
    unique_countries: set = set()
    for report in all_reports:
        unique_countries.add(report["country"])
    for r in name_co_results:
        if r.get("country"):
            unique_countries.add(r["country"])

    # FIX 9: Compile regexes once
    trigger_regex = _build_trigger_regex(unique_countries)

    country_regex_map: Dict[str, re.Pattern] = {}
    for country in unique_countries:
        vars_ = get_analyzer(country).country_variations
        if vars_:
            try:
                country_regex_map[country] = re.compile(
                    r"\b(" + "|".join(map(re.escape, vars_)) + r")\b", flags=re.IGNORECASE
                )
            except re.error:
                pass

    name_patterns: List[re.Pattern] = []
    for nm in [business_name, legal_name]:
        if nm:
            p = build_name_regex(nm)
            if p:
                try:
                    name_patterns.append(re.compile(p))
                except re.error:
                    pass

    # Merge site + name results by URL
    rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "MINIMAL": 0, "NONE": -1, "UNKNOWN": -2}

    def merge(existing: dict, new: dict) -> dict:
        best = existing if rank.get(existing["risk_level"], 0) >= rank.get(new["risk_level"], 0) else new
        src = {existing.get("source", "SITE"), new.get("source", "SITE")}
        best["source"] = "BOTH" if "NAME" in src and "SITE" in src else list(src)[0]
        mt = {existing.get("matched_name_type", "NONE"), new.get("matched_name_type", "NONE")}
        best["matched_name_type"] = (
            "BOTH" if "BUSINESS" in mt and "LEGAL" in mt
            else next((t for t in ["BUSINESS", "LEGAL"] if t in mt), "NONE")
        )
        best["matched_names"] = list(set(existing.get("matched_names", []) + new.get("matched_names", [])))
        best["country"] = best.get("country") or existing.get("country") or new.get("country")
        return best

    combined: Dict[str, dict] = {}
    for report in all_reports:
        for result in report["analyzed_results"]:
            entry = {
                "country": report["country"], "risk_level": result.get("risk_level", "UNKNOWN"),
                "url": result.get("url", ""), "title": result.get("original_title", ""),
                "snippet": result.get("original_snippet", ""), "confidence": result.get("confidence", 0),
                "excerpts": result.get("relevant_excerpts", []), "extraction_type": result.get("extraction_type", "HTML"),
                "extraction_message": result.get("extraction_message", ""),
                "source": "SITE", "matched_name_type": "NONE", "matched_names": [],
            }
            if entry["url"]:
                combined[entry["url"]] = merge(combined[entry["url"]], entry) if entry["url"] in combined else entry

    for r in name_co_results:
        entry = {
            "country": r.get("country", ""), "risk_level": r.get("risk_level", "UNKNOWN"),
            "url": r.get("url", ""), "title": r.get("original_title", ""),
            "snippet": r.get("original_snippet", ""), "confidence": r.get("confidence", 0),
            "excerpts": r.get("relevant_excerpts", []), "extraction_type": r.get("extraction_type", "HTML"),
            "extraction_message": r.get("extraction_message", ""),
            "source": r.get("source", "NAME"), "matched_name_type": r.get("matched_name_type", "NONE"),
            "matched_names": r.get("matched_names", []),
        }
        if entry["url"]:
            combined[entry["url"]] = merge(combined[entry["url"]], entry) if entry["url"] in combined else entry

    all_results_data = list(combined.values())
    unique_countries_sorted = sorted(unique_countries)

    # ---------- HTML generation ----------
    def result_item_html(result: dict, index: int) -> str:
        risk_level = result.get("risk_level", "UNKNOWN")
        risk_class = (risk_level or "unknown").lower()
        country_for_highlight = result.get("country", "")
        c_regex = country_regex_map.get(country_for_highlight)

        # FIX 10 & 2: escape all user/scraped data before insertion
        title_safe = html.escape(result.get("title", "") or "")
        url_safe = html.escape(result.get("url", "") or "")
        snippet_raw = (result.get("snippet", "") or "").replace("\n", " ").strip()
        highlighted_snippet = _safe_highlight(snippet_raw, trigger_regex,
                                               *([c_regex] if c_regex else []), *name_patterns)

        excerpts_html = ""
        for excerpt in result.get("excerpts", [])[:5]:
            raw_text = (excerpt.get("text", "") or "").replace("\\n", " ").strip()
            trigger_raw = (excerpt.get("trigger_sentence", "") or "").replace("\\n", " ").strip()
            centered = _center_around_first_match(raw_text, trigger_regex, max_len=800)
            if len(centered) > 800 and not centered.endswith("…"):
                centered = centered[:800] + "…"

            hl_args = (trigger_regex, *([c_regex] if c_regex else []), *name_patterns)
            # If the trigger sentence appears in the centered excerpt, split the raw text
            # around it BEFORE highlighting each piece — otherwise once _safe_highlight
            # has wrapped keywords in <span> tags the trigger substring no longer exists
            # as a contiguous block and a post-hoc string replace silently no-ops.
            if trigger_raw and trigger_raw in centered:
                idx = centered.find(trigger_raw)
                before = centered[:idx]
                after = centered[idx + len(trigger_raw):]
                highlighted = (
                    _safe_highlight(before, *hl_args)
                    + '<span class="trigger-sentence">'
                    + _safe_highlight(trigger_raw, *hl_args)
                    + '</span>'
                    + _safe_highlight(after, *hl_args)
                )
            else:
                highlighted = _safe_highlight(centered, *hl_args)

            risk_type_raw = str(excerpt.get("risk_type", "GENERAL"))
            risk_type_safe = html.escape(risk_type_raw)
            is_reg = risk_type_raw == "SANCTIONS_REGULATORY_MENTION"
            excerpt_type_class = "excerpt-type regulatory" if is_reg else "excerpt-type"
            badge_prefix = "🛡 " if is_reg else ""
            excerpts_html += f"""
                <div class="excerpt">
                    <div class="{excerpt_type_class}">{badge_prefix}{risk_type_safe}</div>
                    <div class="excerpt-text">{highlighted}</div>
                    <div class="excerpt-confidence">Confidence: {excerpt.get('confidence', 0)}%</div>
                </div>"""

        pdf_notice = ""
        et = result.get("extraction_type", "HTML")
        if et == "PDF":
            pdf_notice = """<div class="pdf-notice">
                    <span class="pdf-icon">📄</span>
                    PDF — full text extracted and analyzed.
                </div>"""
        elif et == "PDF_CACHE":
            pdf_notice = """<div class="pdf-notice">
                    <span class="pdf-icon">📄</span>
                    PDF — text extracted via Google cache.
                </div>"""
        elif et == "SNIPPET_FALLBACK":
            pdf_notice = """<div class="pdf-notice">
                    <span class="pdf-icon">⚠</span>
                    Full page extraction failed — analysis based on Google snippet only.
                </div>"""
        elif et == "DOCUMENT":
            pdf_notice = """<div class="pdf-notice">
                    <span class="pdf-icon">📄</span>
                    Office document — manual review needed.
                </div>"""

        matched_label = result.get("matched_name_type", "NONE")
        matched_names = result.get("matched_names", [])
        matched_meta = ""
        if matched_label and matched_label != "NONE":
            names_safe = html.escape(", ".join(matched_names))
            matched_meta = f"""<div class="meta-item">
                        <span class="meta-label">Name Match:</span> {html.escape(matched_label)}{(' — ' + names_safe) if names_safe else ''}
                    </div>"""

        return f"""
        <div class="result-item"
             data-risk="{html.escape(risk_level)}"
             data-country="{html.escape(country_for_highlight)}"
             data-type="{html.escape(result.get('extraction_type', 'HTML'))}"
             data-source="{html.escape(result.get('source', 'SITE'))}"
             data-match="{html.escape(matched_label)}"
             data-index="{index}">
            <div class="result-header">
                <div>
                    <h3 class="result-title">{title_safe}</h3>
                    <a href="{url_safe}" target="_blank" rel="noopener noreferrer" class="result-url">{url_safe}</a>
                </div>
                <span class="risk-badge {html.escape(risk_class)}">{html.escape(risk_level)} RISK</span>
            </div>
            <div class="result-meta">
                <div class="meta-item"><span class="meta-label">Country:</span> {html.escape(country_for_highlight)}</div>
                <div class="meta-item"><span class="meta-label">Confidence:</span> {result.get('confidence', 0)}%</div>
                <div class="meta-item"><span class="meta-label">Type:</span> {html.escape(result.get('extraction_type', 'HTML'))}</div>
                <div class="meta-item"><span class="meta-label">Source:</span> {html.escape(result.get('source', 'SITE'))}</div>
                {matched_meta}
            </div>
            <div class="result-content">
                <p><strong>Original Search Snippet:</strong> {highlighted_snippet}</p>
                {'<div class="excerpts-container">' + excerpts_html + '</div>' if excerpts_html else ''}
                {pdf_notice}
            </div>
        </div>"""

    results_html = (
        "\n".join(result_item_html(r, i) for i, r in enumerate(all_results_data))
        if all_results_data
        else """<div class="empty-state">
            <div class="empty-state-icon">🔍</div>
            <h3>No Results Found</h3>
            <p>No sanctions-related content was found for the specified website.</p>
        </div>"""
    )

    country_filter_buttons = "\n".join(
        f'<button class="filter-button" onclick="filterByCountry(\'{html.escape(c)}\')">{html.escape(c)}</button>'
        for c in unique_countries_sorted
    )

    # ------------------------------------------------------------------
    # Build Gemma 4 investigator brief HTML block (inserted above dashboard)
    # ------------------------------------------------------------------
    verdict_html = ""
    if llm_verdict:
        rec_key = (llm_verdict.get("recommendation") or "INSUFFICIENT_DATA").upper()
        v_text = html.escape(_REC_DISPLAY_LABEL.get(rec_key, "INSUFFICIENT DATA"))
        v_conf = html.escape(llm_verdict.get("confidence_band", "") or "")
        v_summ = html.escape(brief_summary_text(llm_verdict))
        v_css = _REC_CSS_CLASS.get(rec_key, "unknown")

        factors_li = "".join(
            f"<li>{html.escape(str(f))}</li>"
            for f in brief_factors_list(llm_verdict)
        )
        recs_li = "".join(
            f"<li>{html.escape(str(r))}</li>"
            for r in brief_next_steps_list(llm_verdict)
        )
        lists_block = ""
        if factors_li or recs_li:
            lists_block = f"""
            <div class="verdict-lists">
                <div>
                    <div class="verdict-list-title">Key Risk Factors</div>
                    <ul class="verdict-list">{factors_li or '<li>None identified.</li>'}</ul>
                </div>
                <div>
                    <div class="verdict-list-title">Suggested Next Steps</div>
                    <ul class="verdict-list">{recs_li or '<li>None.</li>'}</ul>
                </div>
            </div>"""

        dropped = llm_verdict.get("unverified_claims_dropped") or 0
        dropped_note = (
            f'<div class="verdict-note">{dropped} unverified claim(s) dropped by post-verification.</div>'
            if dropped else ""
        )

        verdict_html = f"""
    <div class="verdict-section verdict-{v_css}">
        <div class="verdict-header">
            <span class="verdict-badge">{v_text}</span>
            <span class="verdict-confidence">Confidence: {v_conf}</span>
            <span class="verdict-model-tag">Gemma 4 investigator brief · citation-grounded · recommendation only</span>
        </div>
        <p class="verdict-summary">{v_summ}</p>
        {lists_block}
        {dropped_note}
    </div>"""

    social_section_html = ""
    if social_media_links:
        links_html = "\n".join(
            f"""<a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer" class="social-link">
                <span class="social-icon">{'📘' if p == 'facebook' else '💼'}</span>
                <span>{html.escape(p.capitalize())} Profile</span>
            </a>"""
            for p, url in social_media_links.items()
        )
        social_section_html = f"""
        <div class="social-media-section" id="social-section">
            <h2>Social Media Profiles</h2>
            <div class="social-links">{links_html}</div>
        </div>"""

    # ------------------------------------------------------------------
    # Build sanctions list screening section (PART 4.1)
    # ------------------------------------------------------------------
    list_screening_html = ""
    if list_screening:
        high_conf_matches = [m for m in list_screening if (m.get("score") or 0) >= 90]
        card_class = "list-screening" if (is_sanctioned_direct or high_conf_matches) else "list-screening warning"

        banner_html = ""
        if is_sanctioned_direct:
            matched_labels = sorted({LIST_SOURCE_LABELS.get(m.get("list_source"), m.get("list_source", "?")) for m in high_conf_matches})
            banner_html = f"""
            <div class="ls-banner">
                <strong>🛡 Entity is Sanctioned</strong>
                This entity was found on {html.escape(', '.join(matched_labels)) or 'sanctions lists'}.
                Open-web investigation was skipped. Do not proceed with any transactions without compliance review.
            </div>"""

        match_rows = []
        for m in list_screening:
            src_key = m.get("list_source", "")
            src_label = LIST_SOURCE_LABELS.get(src_key, src_key or "UNKNOWN")
            src_url = m.get("sanctions_url") or LIST_SOURCE_URLS.get(src_key)
            score = int(round(m.get("score") or 0))
            score_class = "" if score >= 95 else "medium"
            src_class = "danger" if score >= 90 else "warning"
            listed_name = html.escape(m.get("listed_name", "") or "")
            matched_name = html.escape(m.get("matched_name", "") or "")
            matched_via = ""
            if matched_name and m.get("matched_name") != m.get("listed_name"):
                matched_via = f'<span class="ls-matched-via">Matched via: {matched_name}</span>'
            entity_type = m.get("entity_type", "ORGANIZATION") or "ORGANIZATION"
            entity_label = "Person" if entity_type == "INDIVIDUAL" else "Org"

            detail_items = []
            if m.get("country"):
                detail_items.append(f'<div><strong>Country:</strong> {html.escape(m["country"])}</div>')
            if m.get("programs"):
                detail_items.append(f'<div><strong>Programs:</strong> {html.escape(m["programs"])}</div>')
            if m.get("source_id"):
                detail_items.append(f'<div><strong>List ID:</strong> <code>{html.escape(str(m["source_id"]))}</code></div>')
            if m.get("query_name"):
                detail_items.append(f'<div><strong>Searched:</strong> {html.escape(m["query_name"])}</div>')
            aliases = m.get("aliases") or []
            if aliases:
                alias_text = ", ".join(html.escape(str(a)) for a in aliases[:8])
                detail_items.append(f'<div style="grid-column:1/-1"><strong>Also known as:</strong> {alias_text}</div>')
            if src_url:
                detail_items.append(f'<div style="grid-column:1/-1"><a href="{html.escape(src_url)}" target="_blank" rel="noopener noreferrer">→ View on official sanctions page</a></div>')

            details_html = f'<div class="ls-details">{"".join(detail_items)}</div>' if detail_items else ""
            match_rows.append(f"""
            <div class="ls-match">
                <div class="ls-match-name">{listed_name}{matched_via}</div>
                <span class="ls-badge source {src_class}">{html.escape(src_label)}</span>
                <span class="ls-badge score {score_class}">{score}% match</span>
                <span class="ls-badge type">{entity_label}</span>
                {details_html}
            </div>""")

        links_html_ls = ""
        if high_conf_matches:
            link_items = []
            seen = set()
            for m in high_conf_matches:
                src_key = m.get("list_source", "")
                url = m.get("sanctions_url") or LIST_SOURCE_URLS.get(src_key)
                if not url or url in seen:
                    continue
                seen.add(url)
                label = LIST_SOURCE_LABELS.get(src_key, src_key)
                link_items.append(
                    f'<a href="{html.escape(url)}" target="_blank" rel="noopener noreferrer" class="ls-link">→ Search on {html.escape(label)}</a>'
                )
            if link_items:
                links_html_ls = f'<div class="ls-links">{"".join(link_items)}</div>'

        list_screening_html = f"""
    <div class="{card_class}">
        <h2>🛡 Sanctions List Screening — {len(list_screening)} Match{'es' if len(list_screening) != 1 else ''}</h2>
        <div class="ls-subtitle">Fuzzy matching against downloaded sanctions lists · manual verification required</div>
        {banner_html}
        {''.join(match_rows)}
        {links_html_ls}
    </div>"""

    # ------------------------------------------------------------------
    # Build regulatory findings section (PART 4.2)
    # ------------------------------------------------------------------
    regulatory_findings_html = ""
    if regulatory_report and regulatory_report.get("analyzed_results"):
        reg_results = regulatory_report.get("analyzed_results", [])
        reg_rows_html = ""
        for i, ar in enumerate(reg_results):
            reg_entry = {
                "country": "Regulatory", "risk_level": ar.get("risk_level", "UNKNOWN"),
                "url": ar.get("url", ""), "title": ar.get("original_title", ""),
                "snippet": ar.get("original_snippet", ""), "confidence": ar.get("confidence", 0),
                "excerpts": ar.get("relevant_excerpts", []),
                "extraction_type": ar.get("extraction_type", "HTML"),
                "extraction_message": ar.get("extraction_message", ""),
                "source": ar.get("source", "REGULATORY"),
                "matched_name_type": ar.get("matched_name_type", "NONE"),
                "matched_names": ar.get("matched_names", []),
            }
            reg_rows_html += result_item_html(reg_entry, 10000 + i)

        regulatory_findings_html = f"""
    <div class="regulatory-section">
        <h2>⚠ Regulatory / OFAC Findings — {len(reg_results)} result{'s' if len(reg_results) != 1 else ''}</h2>
        <div class="rs-subtitle">These findings came from sanctions-term queries that don't reference a specific country.</div>
        {reg_rows_html}
    </div>"""

    # ------------------------------------------------------------------
    # Build Needs Analyst Review + Not Attempted sections
    # ------------------------------------------------------------------
    def _extraction_type_label(t: str) -> str:
        tu = (t or "").upper()
        return {
            "SNIPPET_FALLBACK": "Google snippet only",
            "DOCUMENT": "Binary document (not readable)",
            "PDF": "PDF extraction failed",
            "ERROR": "Page fetch failed",
        }.get(tu, t or "Unknown")

    def _extraction_type_class(t: str) -> str:
        tu = (t or "").upper()
        if tu == "SNIPPET_FALLBACK":
            return "warning"
        if tu in ("PDF", "ERROR"):
            return "danger"
        return "muted"

    review_section_html = ""
    if needs_analyst_review:
        review_rows_html = ""
        for item in needs_analyst_review:
            url = html.escape(item.get("url", "") or "")
            title = html.escape(item.get("title", "") or item.get("url", "") or "")
            snippet = html.escape(item.get("snippet", "") or "")
            ex_type = item.get("extraction_type", "") or ""
            ex_msg = html.escape(item.get("extraction_message", "") or "")
            type_class = _extraction_type_class(ex_type)
            type_label = html.escape(_extraction_type_label(ex_type))
            countries = item.get("countries") or []
            countries_html = (
                f'<span class="review-countries">{html.escape(" · ".join(countries))}</span>'
                if countries else ""
            )
            snippet_html = f'<div class="review-snippet">{snippet}</div>' if snippet else ""
            msg_html = f'<span class="review-msg">{ex_msg}</span>' if ex_msg else ""
            review_rows_html += f"""
            <div class="review-row">
                <div class="review-top">
                    <a href="{url}" target="_blank" rel="noopener noreferrer" class="review-link">{title}</a>
                    <span class="review-badge {type_class}">{type_label}</span>
                </div>
                {snippet_html}
                <div class="review-meta">
                    {countries_html}
                    {msg_html}
                </div>
            </div>"""
        review_section_html = f"""
    <div class="review-section">
        <h2>⚠ Needs Analyst Review — {len(needs_analyst_review)} URL{'s' if len(needs_analyst_review) != 1 else ''}</h2>
        <div class="review-subtitle">Extraction failed or only a Google snippet was available. Manual verification recommended.</div>
        {review_rows_html}
    </div>"""

    not_attempted_section_html = ""
    if not_attempted:
        na_rows_html = ""
        for item in not_attempted:
            url = html.escape(item.get("url", "") or "")
            domain = html.escape(item.get("domain", "") or "")
            na_rows_html += f"""
            <div class="na-row">
                <a href="{url}" target="_blank" rel="noopener noreferrer" class="na-link">{url}</a>
                <span class="na-domain">{domain}</span>
            </div>"""
        not_attempted_section_html = f"""
    <div class="review-section muted-section">
        <h2>👁 Not Attempted — {len(not_attempted)} URL{'s' if len(not_attempted) != 1 else ''}</h2>
        <div class="review-subtitle">Excluded domains (Wikipedia, LinkedIn, social media, etc.). Not fetched or analysed.</div>
        {na_rows_html}
    </div>"""

    # ------------------------------------------------------------------
    # PART 7: When directly sanctioned, apply a body class that hides the
    # dashboard/results/filters/social sections via CSS and emit a single
    # "search skipped" notice below the list-screening section.
    # ------------------------------------------------------------------
    body_mode_class = "sanctioned-mode" if is_sanctioned_direct else ""
    skipped_notice_html = ""
    if is_sanctioned_direct:
        skipped_notice_html = """
    <div class="skipped-notice">
        <strong>Open-web search skipped.</strong>
        This entity was found on one or more sanctions lists. No open-web
        investigation was performed — the compliance decision is based
        entirely on the direct list match above.
    </div>"""

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sanctions Analysis Report – {html.escape(website)}</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --primary:#0A2540;--accent:#635BFF;--success:#00D924;--warning:#FFB800;
            --danger:#FF5630;--bg:#F6F9FC;--surface:#FFFFFF;--text:#0A2540;
            --text2:#697386;--border:#E6E6E6;
            --sh-sm:0 1px 3px rgba(0,0,0,.12),0 1px 2px rgba(0,0,0,.24);
            --sh-md:0 4px 6px rgba(0,0,0,.1);--sh-lg:0 10px 20px rgba(0,0,0,.15);
            --r-sm:6px;--r-md:8px;--r-lg:12px;
        }}
        *{{margin:0;padding:0;box-sizing:border-box}}
        body{{font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
              background:var(--bg);color:var(--text);line-height:1.6;
              -webkit-font-smoothing:antialiased}}
        .container{{max-width:1200px;margin:0 auto;padding:2rem}}
        .header{{text-align:center;margin-bottom:3rem}}
        .header h1{{font-size:2.5rem;font-weight:700;letter-spacing:-.02em;margin-bottom:.5rem}}
        .header-meta{{color:var(--text2)}}
        .navigation{{position:sticky;top:0;background:var(--surface);border-radius:var(--r-lg);
                     box-shadow:var(--sh-md);padding:1rem;margin-bottom:2rem;z-index:100;display:none}}
        .nav-button{{background:var(--bg);border:1px solid var(--border);padding:.5rem 1rem;
                     border-radius:var(--r-sm);cursor:pointer;font-size:.875rem;
                     transition:all .2s;font-family:inherit}}
        .nav-button:hover{{transform:translateY(-1px);background:var(--accent);color:#fff;border-color:var(--accent)}}
        .dashboard{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:1.5rem;margin-bottom:3rem}}
        .dashboard-card{{background:var(--surface);border-radius:var(--r-lg);padding:1.5rem;
                         box-shadow:var(--sh-sm);transition:all .3s;cursor:pointer;overflow:hidden}}
        .dashboard-card:hover{{transform:translateY(-4px);box-shadow:var(--sh-lg)}}
        .dashboard-card.high-risk .card-number{{color:var(--danger)}}
        .dashboard-card.medium-risk .card-number{{color:var(--warning)}}
        .dashboard-card.low-risk .card-number{{color:var(--success)}}
        .dashboard-card.minimal-risk .card-number{{color:var(--text2)}}
        .card-label{{font-size:.875rem;color:var(--text2);margin-bottom:.5rem;font-weight:500}}
        .card-number{{font-size:2.5rem;font-weight:700;line-height:1}}
        .card-sublabel{{font-size:.75rem;color:var(--text2);margin-top:.5rem}}
        .filter-container{{background:var(--surface);border-radius:var(--r-lg);padding:1.5rem;margin-bottom:2rem;box-shadow:var(--sh-sm)}}
        .filter-buttons{{display:flex;flex-wrap:wrap;gap:.75rem}}
        .filter-button{{padding:.5rem 1rem;border:2px solid var(--border);background:var(--surface);
                        border-radius:var(--r-md);cursor:pointer;font-size:.875rem;font-weight:500;
                        transition:all .2s;font-family:inherit}}
        .filter-button:hover{{transform:translateY(-1px);box-shadow:var(--sh-sm)}}
        .filter-button.active{{background:var(--accent);color:#fff;border-color:var(--accent)}}
        .filter-section{{margin-bottom:1rem}}
        .filter-label{{font-size:.75rem;text-transform:uppercase;color:var(--text2);
                       margin-bottom:.5rem;font-weight:600;letter-spacing:.05em}}
        .results-container{{background:var(--surface);border-radius:var(--r-lg);padding:2rem;box-shadow:var(--sh-sm)}}
        .result-item{{border-bottom:1px solid var(--border);padding:2rem 0;transition:all .2s}}
        .result-item:last-child{{border-bottom:none}}
        .result-item.hidden{{display:none}}
        .result-header{{display:flex;justify-content:space-between;align-items:flex-start;
                        margin-bottom:1rem;flex-wrap:wrap;gap:1rem}}
        .result-title{{font-size:1.25rem;font-weight:600;margin-bottom:.5rem}}
        .result-url{{font-size:.875rem;color:var(--accent);text-decoration:none;word-break:break-all}}
        .result-url:hover{{text-decoration:underline}}
        .risk-badge{{padding:.25rem .75rem;border-radius:999px;font-size:.75rem;font-weight:600;
                     text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}}
        .risk-badge.high{{background:rgba(255,86,48,.1);color:var(--danger)}}
        .risk-badge.medium{{background:rgba(255,184,0,.1);color:var(--warning)}}
        .risk-badge.low{{background:rgba(0,217,36,.1);color:var(--success)}}
        .risk-badge.minimal{{background:var(--bg);color:var(--text2)}}
        .result-meta{{display:flex;gap:2rem;margin:1rem 0;flex-wrap:wrap}}
        .meta-item{{font-size:.875rem;color:var(--text2)}}
        .meta-label{{font-weight:500;color:var(--text)}}
        .excerpts-container{{margin-top:1.5rem}}
        .excerpt{{background:var(--bg);border-radius:var(--r-md);padding:1rem;
                  margin-bottom:1rem;border-left:3px solid var(--accent)}}
        .excerpt-type{{display:inline-block;background:var(--accent);color:#fff;
                       padding:.25rem .5rem;border-radius:var(--r-sm);font-size:.75rem;
                       font-weight:600;margin-bottom:.5rem}}
        .excerpt-text{{color:var(--text);line-height:1.7;margin:.5rem 0}}
        .excerpt-confidence{{font-size:.75rem;color:var(--text2);text-align:right}}
        .sanction-highlight{{color:var(--danger);font-weight:700;
                             background:rgba(255,86,48,.12);padding:1px 3px;border-radius:3px}}
        .trigger-sentence{{background:rgba(255,86,48,.15);padding:2px 4px;
                           border-radius:3px;font-weight:600;display:inline}}
        .excerpt-type.regulatory{{background:rgba(255,86,48,.15);color:var(--danger);
                                   border:1px solid rgba(255,86,48,.3)}}
        .pdf-notice{{background:rgba(255,184,0,.1);border:1px solid rgba(255,184,0,.3);
                     border-radius:var(--r-md);padding:1rem;margin-top:1rem;font-size:.875rem}}
        /* ---- Sanctions list screening section ---- */
        .list-screening{{background:var(--surface);border-left:5px solid var(--danger);
                          border-radius:var(--r-lg);padding:1.5rem 2rem;margin-bottom:2rem;
                          box-shadow:var(--sh-sm)}}
        .list-screening.warning{{border-left-color:var(--warning)}}
        .list-screening.clean{{border-left-color:var(--success)}}
        .list-screening h2{{font-size:1.25rem;margin-bottom:.25rem;display:flex;align-items:center;gap:.5rem}}
        .list-screening .ls-subtitle{{color:var(--text2);font-size:.875rem;margin-bottom:1rem}}
        .ls-banner{{background:rgba(255,86,48,.12);border-radius:var(--r-md);
                    padding:.75rem 1rem;margin-bottom:1rem}}
        .ls-banner strong{{color:var(--danger);display:block;font-size:.95rem;
                            text-transform:uppercase;letter-spacing:.04em;margin-bottom:.25rem}}
        .ls-match{{display:grid;grid-template-columns:1fr auto auto auto;gap:.75rem;
                   align-items:center;padding:.75rem 0;border-bottom:1px solid var(--border);font-size:.875rem}}
        .ls-match:last-child{{border-bottom:none}}
        .ls-match-name{{font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis}}
        .ls-match-name .ls-matched-via{{display:block;font-size:.7rem;color:var(--text2);font-weight:400}}
        .ls-badge{{padding:.2rem .6rem;border-radius:999px;font-size:.7rem;
                   font-weight:700;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}}
        .ls-badge.source{{background:rgba(99,91,255,.12);color:var(--accent)}}
        .ls-badge.source.danger{{background:rgba(255,86,48,.12);color:var(--danger)}}
        .ls-badge.source.warning{{background:rgba(255,184,0,.15);color:#c88200}}
        .ls-badge.score{{background:rgba(255,86,48,.12);color:var(--danger)}}
        .ls-badge.score.medium{{background:rgba(255,184,0,.15);color:#c88200}}
        .ls-badge.type{{background:rgba(99,91,255,.12);color:var(--accent);font-size:.65rem}}
        .ls-details{{padding:.5rem 0 .25rem;font-size:.8rem;color:var(--text2);
                     grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:.5rem}}
        .ls-details a{{color:var(--accent);text-decoration:none}}
        .ls-details a:hover{{text-decoration:underline}}
        .ls-links{{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:1rem}}
        .ls-link{{display:inline-flex;align-items:center;gap:.3rem;padding:.4rem .8rem;
                  background:rgba(255,86,48,.1);color:var(--danger);border-radius:var(--r-sm);
                  text-decoration:none;font-size:.75rem;font-weight:600;transition:all .2s}}
        .ls-link:hover{{background:rgba(255,86,48,.2)}}
        /* ---- Regulatory findings section ---- */
        .regulatory-section{{background:var(--surface);border-left:5px solid var(--danger);
                              border-radius:var(--r-lg);padding:1.5rem 2rem;margin-bottom:2rem;
                              box-shadow:var(--sh-sm)}}
        .regulatory-section h2{{font-size:1.25rem;margin-bottom:.25rem;display:flex;align-items:center;gap:.5rem}}
        .regulatory-section .rs-subtitle{{color:var(--text2);font-size:.875rem;margin-bottom:1rem}}
        /* ---- Sanctioned short-circuit notice ---- */
        .skipped-notice{{background:rgba(255,86,48,.08);border-left:4px solid var(--danger);
                         padding:1rem 1.5rem;border-radius:var(--r-md);margin:1.5rem 0;
                         color:var(--text2);font-size:.875rem;line-height:1.6}}
        .skipped-notice strong{{color:var(--danger);display:block;margin-bottom:.25rem;font-size:.95rem}}
        /* ---- Needs Analyst Review / Not Attempted sections ---- */
        .review-section{{background:var(--surface);border-left:5px solid var(--warning);
                          border-radius:var(--r-lg);padding:1.5rem 2rem;margin-bottom:2rem;
                          box-shadow:var(--sh-sm)}}
        .review-section.muted-section{{border-left-color:var(--border)}}
        .review-section h2{{font-size:1.15rem;margin-bottom:.25rem;display:flex;align-items:center;gap:.5rem}}
        .review-section .review-subtitle{{color:var(--text2);font-size:.8125rem;margin-bottom:1rem}}
        .review-row{{padding:.75rem 0;border-bottom:1px solid var(--border)}}
        .review-row:last-child{{border-bottom:none}}
        .review-top{{display:flex;justify-content:space-between;align-items:flex-start;gap:.75rem;margin-bottom:.25rem}}
        .review-link{{color:var(--accent);text-decoration:none;font-size:.875rem;word-break:break-all;flex:1;min-width:0}}
        .review-link:hover{{text-decoration:underline}}
        .review-badge{{font-size:.6875rem;font-weight:600;padding:.15rem .6rem;border-radius:999px;white-space:nowrap}}
        .review-badge.warning{{background:rgba(255,184,0,.12);color:var(--warning)}}
        .review-badge.danger{{background:rgba(255,86,48,.12);color:var(--danger)}}
        .review-badge.muted{{background:var(--bg);color:var(--text2)}}
        .review-snippet{{font-size:.8125rem;color:var(--text2);line-height:1.5;margin:.25rem 0}}
        .review-meta{{display:flex;gap:.75rem;flex-wrap:wrap;font-size:.6875rem;color:var(--text2);margin-top:.25rem}}
        .review-countries{{font-family:ui-monospace,Menlo,monospace}}
        .review-msg{{opacity:.75}}
        .na-row{{display:flex;justify-content:space-between;align-items:center;gap:.75rem;padding:.5rem 0;border-bottom:1px solid var(--border)}}
        .na-row:last-child{{border-bottom:none}}
        .na-link{{color:var(--text2);text-decoration:none;font-size:.8125rem;word-break:break-all;flex:1;min-width:0}}
        .na-link:hover{{color:var(--accent);text-decoration:underline}}
        .na-domain{{font-family:ui-monospace,Menlo,monospace;font-size:.6875rem;color:var(--text2);opacity:.7;white-space:nowrap}}
        /* When directly sanctioned, hide dashboard/results/filters/social sections */
        body.sanctioned-mode #dashboard-view,
        body.sanctioned-mode .results-container,
        body.sanctioned-mode .social-media-section,
        body.sanctioned-mode .regulatory-section,
        body.sanctioned-mode .review-section,
        body.sanctioned-mode .navigation {{ display: none !important; }}
        .social-media-section{{background:var(--surface);border-radius:var(--r-lg);
                                padding:2rem;margin-top:2rem;box-shadow:var(--sh-sm)}}
        .social-links{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1rem;margin-top:1rem}}
        .social-link{{display:flex;align-items:center;padding:1rem;background:var(--bg);
                      border-radius:var(--r-md);text-decoration:none;color:var(--text);transition:all .2s}}
        .social-link:hover{{transform:translateY(-2px);background:var(--accent);color:#fff}}
        .social-icon{{margin-right:.75rem}}
        .empty-state{{text-align:center;padding:3rem;color:var(--text2)}}
        .empty-state-icon{{font-size:3rem;margin-bottom:1rem;opacity:.5}}
        .footer{{text-align:center;padding:3rem 0;color:var(--text2);font-size:.875rem}}
        @media(max-width:768px){{
            .container{{padding:1rem}}.header h1{{font-size:2rem}}
            .dashboard{{grid-template-columns:1fr}}.result-header{{flex-direction:column}}
        }}
        /* ---- Gemma 4 Verdict Section ---- */
        .verdict-section{{
            border-radius:var(--radius-lg);padding:1.5rem 2rem;margin-bottom:2rem;
            box-shadow:var(--shadow-sm);border-left:6px solid #ccc;
        }}
        .verdict-high{{border-left-color:#FF5630;background:rgba(255,86,48,.06)}}
        .verdict-possible{{border-left-color:#FFB800;background:rgba(255,184,0,.06)}}
        .verdict-clear{{border-left-color:#00D924;background:rgba(0,217,36,.06)}}
        .verdict-unknown{{border-left-color:#aaa;background:rgba(0,0,0,.03)}}
        .verdict-header{{display:flex;align-items:center;gap:1rem;flex-wrap:wrap;margin-bottom:.75rem}}
        .verdict-model-tag{{font-size:.7rem;color:var(--text2);font-style:italic}}
        .verdict-badge{{
            font-size:.9rem;font-weight:700;text-transform:uppercase;
            padding:.3rem .85rem;border-radius:999px;letter-spacing:.04em;
        }}
        .verdict-high .verdict-badge{{background:rgba(255,86,48,.15);color:#FF5630}}
        .verdict-possible .verdict-badge{{background:rgba(255,184,0,.15);color:#c88200}}
        .verdict-clear .verdict-badge{{background:rgba(0,217,36,.15);color:#00a31c}}
        .verdict-unknown .verdict-badge{{background:rgba(0,0,0,.07);color:#555}}
        .verdict-confidence{{font-size:.8rem;color:var(--text2);font-weight:500}}
        .verdict-summary{{margin:.5rem 0 .75rem;line-height:1.6}}
        .verdict-lists{{display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:.75rem}}
        @media(max-width:600px){{.verdict-lists{{grid-template-columns:1fr}}}}
        .verdict-list-title{{font-weight:600;font-size:.85rem;margin-bottom:.4rem}}
        .verdict-list{{padding-left:1.2rem;margin:0;font-size:.875rem;line-height:1.6}}
    </style>
</head>
<body class="{body_mode_class}">
<div class="container">
    <header class="header">
        <h1>Sanctions Analysis Report</h1>
        <div class="header-meta">
            <div>Website: <strong>{html.escape(website)}</strong></div>
            <div>Analysis Date: {datetime.now().strftime('%B %d, %Y at %I:%M %p')}</div>
        </div>
    </header>
    <nav class="navigation" id="navigation">
        <button class="nav-button" onclick="showDashboard()">← Back to Dashboard</button>
    </nav>
    {verdict_html}
    {list_screening_html}
    {skipped_notice_html}
    {regulatory_findings_html}
    <div id="dashboard-view">
        <div class="dashboard">
            <div class="dashboard-card high-risk" onclick="filterByRisk('HIGH')">
                <div class="card-label">High Risk</div>
                <div class="card-number">{total_high}</div>
                <div class="card-sublabel">Direct business relationships</div>
            </div>
            <div class="dashboard-card medium-risk" onclick="filterByRisk('MEDIUM')">
                <div class="card-label">Medium Risk</div>
                <div class="card-number">{total_medium}</div>
                <div class="card-sublabel">Indirect relationships</div>
            </div>
            <div class="dashboard-card low-risk" onclick="filterByRisk('LOW')">
                <div class="card-label">Low Risk</div>
                <div class="card-number">{total_low}</div>
                <div class="card-sublabel">Compliance mentions</div>
            </div>
            <div class="dashboard-card minimal-risk" onclick="filterByRisk('MINIMAL')">
                <div class="card-label">Minimal Risk</div>
                <div class="card-number">{total_minimal}</div>
                <div class="card-sublabel">General references</div>
            </div>
        </div>
        <div class="dashboard">
            <div class="dashboard-card">
                <div class="card-label">Total URLs Analyzed</div>
                <div class="card-number" style="color:var(--accent)">{total_analyzed}</div>
                <div class="card-sublabel">Across all countries (site results)</div>
            </div>
            <div class="dashboard-card" onclick="filterByType('PDF')">
                <div class="card-label">PDFs Found</div>
                <div class="card-number" style="color:var(--warning)">{total_pdfs}</div>
                <div class="card-sublabel">PDFs extracted and analyzed</div>
            </div>
            <div class="dashboard-card" onclick="scrollToSocial()">
                <div class="card-label">Social Media Profiles</div>
                <div class="card-number" style="color:var(--accent)">{len(social_media_links)}</div>
                <div class="card-sublabel">Click to view profiles</div>
            </div>
            <div class="dashboard-card" onclick="filterBySource('NAME')">
                <div class="card-label">Name Co-occurrence</div>
                <div class="card-number" style="color:var(--danger)">{len(name_co_results)}</div>
                <div class="card-sublabel">Open web: names + countries</div>
            </div>
            <div class="dashboard-card">
                <div class="card-label">Regulatory Findings</div>
                <div class="card-number" style="color:var(--danger)">{regulatory_count}</div>
                <div class="card-sublabel">Global OFAC/SDN mentions</div>
            </div>
        </div>
        <div class="filter-container">
            <div class="filter-section">
                <div class="filter-label">Filter by Risk Level</div>
                <div class="filter-buttons">
                    <button class="filter-button active" onclick="filterByRisk('ALL')">All Results</button>
                    <button class="filter-button" onclick="filterByRisk('HIGH')">High Risk</button>
                    <button class="filter-button" onclick="filterByRisk('MEDIUM')">Medium Risk</button>
                    <button class="filter-button" onclick="filterByRisk('LOW')">Low Risk</button>
                    <button class="filter-button" onclick="filterByRisk('MINIMAL')">Minimal Risk</button>
                </div>
            </div>
            <div class="filter-section">
                <div class="filter-label">Filter by Country</div>
                <div class="filter-buttons">
                    <button class="filter-button active" onclick="filterByCountry('ALL')">All Countries</button>
                    {country_filter_buttons}
                </div>
            </div>
            <div class="filter-section">
                <div class="filter-label">Filter by Source</div>
                <div class="filter-buttons">
                    <button class="filter-button active" onclick="filterBySource('ALL')">All Sources</button>
                    <button class="filter-button" onclick="filterBySource('SITE')">Site Results</button>
                    <button class="filter-button" onclick="filterBySource('NAME')">Name Co-occurrence</button>
                </div>
            </div>
            <div class="filter-section">
                <div class="filter-label">Filter by Name Match</div>
                <div class="filter-buttons">
                    <button class="filter-button active" onclick="filterByMatch('ALL')">All</button>
                    <button class="filter-button" onclick="filterByMatch('BUSINESS')">Business</button>
                    <button class="filter-button" onclick="filterByMatch('LEGAL')">Legal</button>
                    <button class="filter-button" onclick="filterByMatch('BOTH')">Both</button>
                </div>
            </div>
        </div>
    </div>

    <div class="results-container" id="results-container">
        <h2 id="results-title">All Results</h2>
        <div id="results-list">
            {results_html}
        </div>
    </div>

    {review_section_html}
    {not_attempted_section_html}

    {social_section_html}

    <footer class="footer">
        <p>This report was generated using automated NLP analysis. Results should be reviewed by compliance professionals.</p>
        <p>Risk levels are determined based on content analysis and keyword patterns.</p>
    </footer>
</div>

<script>
const socialLinks = {json.dumps(social_media_links)};
let currentFilter = {{risk:'ALL',country:'ALL',type:'ALL',source:'ALL',match:'ALL'}};
let originalResultsHTML = '';
let showingSocial = false;

function showDashboard() {{
    document.getElementById('dashboard-view').style.display = 'block';
    document.getElementById('navigation').style.display = 'none';
    document.getElementById('results-title').textContent = 'All Results';
    currentFilter = {{risk:'ALL',country:'ALL',type:'ALL',source:'ALL',match:'ALL'}};
    restoreResults();
    applyFilters();
}}

function scrollToSocial() {{
    document.getElementById('dashboard-view').style.display = 'none';
    document.getElementById('navigation').style.display = 'block';
    document.getElementById('results-title').textContent = 'Social Media Profiles';
    const list = document.getElementById('results-list');
    if (!originalResultsHTML) originalResultsHTML = list.innerHTML;
    showingSocial = true;
    const keys = Object.keys(socialLinks || {{}});
    if (keys.length > 0) {{
        let h = '<div class="social-links">';
        keys.forEach(p => {{
            const url = socialLinks[p];
            const icon = p === 'facebook' ? '📘' : '💼';
            const label = p.charAt(0).toUpperCase() + p.slice(1);
            h += `<a href="${{url}}" target="_blank" rel="noopener noreferrer" class="social-link"><span class="social-icon">${{icon}}</span><span>${{label}} Profile</span></a>`;
        }});
        h += '</div>';
        list.innerHTML = h;
    }} else {{
        list.innerHTML = '<div class="empty-state"><p>No social media profiles found.</p></div>';
    }}
}}

function filterByRisk(risk) {{
    restoreResults();
    currentFilter = {{risk,country:'ALL',type:'ALL',source:'ALL',match:'ALL'}};
    updateView(risk === 'ALL' ? 'All Results' : risk + ' Risk Results');
    applyFilters();
}}

function filterByCountry(country) {{
    restoreResults();
    currentFilter = {{risk:'ALL',country,type:'ALL',source:'ALL',match:'ALL'}};
    updateView(country === 'ALL' ? 'All Results' : 'Results for ' + country);
    applyFilters();
}}

function filterByType(type) {{
    restoreResults();
    currentFilter = {{risk:'ALL',country:'ALL',type,source:'ALL',match:'ALL'}};
    updateView(type === 'ALL' ? 'All Results' : type + ' File Results');
    applyFilters();
}}

function filterBySource(source) {{
    restoreResults();
    currentFilter = {{risk:'ALL',country:'ALL',type:'ALL',source,match:'ALL'}};
    const label = source === 'ALL' ? 'All Results' : source === 'SITE' ? 'Site Results' : 'Name Co-occurrence Results';
    updateView(label);
    applyFilters();
}}

function filterByMatch(match) {{
    restoreResults();
    currentFilter.match = match;
    updateView(match === 'ALL' ? 'All Results' : 'Name Match: ' + match);
    applyFilters();
}}

function updateView(title) {{
    document.getElementById('results-title').textContent = title;
    const f = currentFilter;
    if (f.risk !== 'ALL' || f.country !== 'ALL' || f.type !== 'ALL' || f.source !== 'ALL' || f.match !== 'ALL') {{
        document.getElementById('dashboard-view').style.display = 'none';
        document.getElementById('navigation').style.display = 'block';
    }}
    document.querySelectorAll('.filter-button').forEach(b => b.classList.remove('active'));
    ['risk','country','type','source','match'].forEach(key => {{
        const val = currentFilter[key];
        const fns = {{risk:'filterByRisk',country:'filterByCountry',type:'filterByType',source:'filterBySource',match:'filterByMatch'}};
        const btn = document.querySelector(`.filter-button[onclick="${{fns[key]}}('${{val}}')"]`);
        if (btn) btn.classList.add('active');
    }});
}}

function applyFilters() {{
    let visible = 0;
    document.querySelectorAll('.result-item').forEach(item => {{
        const {{risk,country,type,source,match}} = item.dataset;
        const f = currentFilter;
        const showSource = f.source === 'ALL' || source === f.source || (source === 'BOTH' && (f.source === 'SITE' || f.source === 'NAME'));
        const show = (f.risk === 'ALL' || risk === f.risk) &&
                     (f.country === 'ALL' || country === f.country) &&
                     (f.type === 'ALL' || type === f.type) &&
                     showSource &&
                     (f.match === 'ALL' || match === f.match);
        item.classList.toggle('hidden', !show);
        if (show) visible++;
    }});
    if (visible === 0) {{
        document.getElementById('results-list').innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">🔍</div>
                <h3>No Results Match Your Filters</h3>
                <p>Try adjusting your filters or returning to the dashboard.</p>
            </div>`;
    }}
}}

function restoreResults() {{
    const list = document.getElementById('results-list');
    if (showingSocial || (list.querySelector('.empty-state') && !list.querySelector('.result-item'))) {{
        list.innerHTML = originalResultsHTML;
        document.getElementById('results-title').textContent = 'All Results';
        showingSocial = false;
    }}
}}

document.addEventListener('DOMContentLoaded', () => {{
    originalResultsHTML = document.getElementById('results-list').innerHTML;
}});
</script>
</body>
</html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html_doc)

    return filepath


def generate_basic_enhanced_report(
    all_reports: List[dict],
    website: str,
    social_media_links: Dict[str, str],
    llm_verdict: Optional[dict] = None,
    list_screening: Optional[List[dict]] = None,
) -> str:
    list_screening = list_screening or []
    is_sanctioned_direct = any((m.get("score") or 0) >= 90 for m in list_screening)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_site = re.sub(r'[\\/*?:"<>|]', "", website)
    filename = f"sanctions_search_report_{safe_site.replace('.', '_')}_{timestamp}.html"
    base_dir = tempfile.gettempdir() if _is_running_in_streamlit() else os.path.join(os.path.expanduser("~"), "Desktop")
    filepath = os.path.join(base_dir, filename)

    rows = ""
    for report in all_reports:
        n = len(report["search_results"])
        cls = "present" if n > 0 else "not-found"
        rows += f"<tr><td>{html.escape(report['country'])}</td><td class='{cls}'>{'YES' if n else 'NO'}</td><td>{n}</td></tr>\n"

    social_html = ""
    if social_media_links:
        items = "".join(
            f'<li><strong>{html.escape(p.title())}:</strong> <a href="{html.escape(u)}" target="_blank" rel="noopener noreferrer">{html.escape(u)}</a></li>'
            for p, u in social_media_links.items()
        )
        social_html = f"<h2>Social Media Profiles Found</h2><ul>{items}</ul>"
    else:
        social_html = "<p><em>No social media profiles found for this website.</em></p>"

    # Build investigator brief block for basic report
    basic_verdict_html = ""
    if llm_verdict:
        rec_key = (llm_verdict.get("recommendation") or "INSUFFICIENT_DATA").upper()
        v_text = html.escape(_REC_DISPLAY_LABEL.get(rec_key, "INSUFFICIENT DATA"))
        v_conf = html.escape(llm_verdict.get("confidence_band", "") or "")
        v_summ = html.escape(brief_summary_text(llm_verdict))
        v_color = _REC_COLOR.get(rec_key, "#aaa")
        factors = "".join(f"<li>{html.escape(str(f))}</li>" for f in brief_factors_list(llm_verdict))
        recs    = "".join(f"<li>{html.escape(str(r))}</li>" for r in brief_next_steps_list(llm_verdict))
        basic_verdict_html = f"""
    <div class="card" style="border-left:5px solid {v_color}">
        <h2>Gemma 4 Investigator Brief</h2>
        <p><strong style="color:{v_color}">{v_text}</strong> &nbsp;·&nbsp; Confidence: {v_conf}</p>
        <p>{v_summ}</p>
        {'<strong>Key risk factors:</strong><ul>' + factors + '</ul>' if factors else ''}
        {'<strong>Suggested next steps:</strong><ul>' + recs + '</ul>' if recs else ''}
        <p style="font-size:.8rem;color:#888">Citation-grounded investigator brief · no disposition decided</p>
    </div>"""

    # Build list screening block for basic report
    list_screening_block_html = ""
    if list_screening:
        ls_rows = ""
        for m in list_screening:
            src_key = m.get("list_source", "")
            src_label = LIST_SOURCE_LABELS.get(src_key, src_key or "UNKNOWN")
            score = int(round(m.get("score") or 0))
            entity_type = m.get("entity_type", "ORGANIZATION") or "ORGANIZATION"
            ls_rows += (
                f"<tr>"
                f"<td>{html.escape(src_label)}</td>"
                f"<td>{html.escape(m.get('listed_name', '') or '')}</td>"
                f"<td class='present'>{score}%</td>"
                f"<td>{html.escape(entity_type)}</td>"
                f"</tr>\n"
            )
        list_screening_block_html = f"""
    <div class="card" style="border-left:5px solid #FF5630">
        <h2>🛡 Sanctions List Screening — {len(list_screening)} Match{'es' if len(list_screening) != 1 else ''}</h2>
        <table>
            <tr><th>List Source</th><th>Listed Name</th><th>Score</th><th>Entity Type</th></tr>
            {ls_rows}
        </table>
    </div>"""

    # Short-circuit when sanctioned: verdict + list screening + skipped notice only
    skipped_notice_basic = ""
    if is_sanctioned_direct:
        skipped_notice_basic = """
    <div class="card" style="border-left:4px solid #FF5630;background:rgba(255,86,48,.05)">
        <p style="color:#FF5630;font-weight:700;margin-bottom:.25rem">Open-web search skipped.</p>
        <p style="font-size:.875rem;color:#697386">
            This entity was found on one or more sanctions lists. No open-web investigation was
            performed — the compliance decision is based entirely on the direct list match above.
        </p>
    </div>"""

    body_main = ""
    if is_sanctioned_direct:
        body_main = f"""
    {basic_verdict_html}
    {list_screening_block_html}
    {skipped_notice_basic}"""
    else:
        body_main = f"""
    {basic_verdict_html}
    {list_screening_block_html}
    <div class="card">
        <h2>Summary</h2>
        <table>
            <tr><th>Country/Region</th><th>Found on Website</th><th>Number of Results</th></tr>
            {rows}
        </table>
        <p><em>Note: Content analysis was skipped. This report shows only search result counts.</em></p>
    </div>
    <div class="card">{social_html}</div>"""

    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Sanctions Search Report – {html.escape(website)}</title>
    <style>
        body{{font-family:system-ui,sans-serif;background:#F6F9FC;color:#0A2540;line-height:1.6;margin:0}}
        .container{{max-width:900px;margin:0 auto;padding:2rem}}
        h1{{text-align:center;margin-bottom:.5rem}}
        .card{{background:#fff;border-radius:12px;padding:2rem;box-shadow:0 1px 3px rgba(0,0,0,.12);margin-bottom:2rem}}
        table{{width:100%;border-collapse:collapse;margin:1rem 0}}
        th,td{{padding:12px;text-align:left;border-bottom:1px solid #E6E6E6}}
        th{{background:#F6F9FC;font-weight:600}}
        .present{{color:#FF5630;font-weight:700}}
        .not-found{{color:#00D924;font-weight:700}}
        a{{color:#635BFF}}
    </style>
</head>
<body>
<div class="container">
    <div class="card">
        <h1>Sanctions Search Report</h1>
        <p style="text-align:center">Website: <strong>{html.escape(website)}</strong> &mdash; {datetime.now().strftime('%B %d, %Y at %I:%M %p')}</p>
    </div>
    {body_main}
</div>
</body>
</html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


# ==========================================================================
# Social media search helper
# ==========================================================================

def search_website_for_social_media(website_url: str) -> Dict[str, str]:
    logger.info("Searching %s for social media profiles…", website_url)
    detector = SocialMediaDetector(API_KEY, SEARCH_ENGINE_ID)
    found_on_site: Dict[str, str] = {}

    if not website_url.startswith("http"):
        website_url = f"https://{website_url}"

    try:
        response = requests.get(
            website_url, timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        if response.status_code == 200:
            found_on_site = detector.extract_social_links_from_html(response.text, website_url)
            logger.info("Found %d profiles on site: %s", len(found_on_site), ", ".join(found_on_site) or "none")
        else:
            logger.warning("Could not access website (HTTP %d). Falling back to external search.", response.status_code)
    except Exception as exc:
        logger.warning("Error accessing website: %s. Falling back to external search.", exc)

    missing = [p for p in detector.social_platforms if p not in found_on_site]
    final = dict(found_on_site)
    if missing:
        domain = urlparse(website_url).netloc or website_url
        found_externally = detector.search_social_profiles(domain, platforms_to_search=missing)
        for p, u in found_externally.items():
            if p not in final:
                final[p] = u

    return final


# ==========================================================================
# Global OFAC/sanctions-term search (runs ONCE per job, not once per country)
# ==========================================================================

def perform_global_ofac_search(
    website: str, business_name: str = "", legal_name: str = "",
    audit_logger=None,
) -> Optional[dict]:
    """Run sanctions-term-only and business-name OFAC queries a single time.

    These queries do not reference any specific sanctioned country, so running
    them once per country (12×) wastes 11 duplicate API calls per query.
    Results are returned as a pseudo-country entry with country='Sanctions/OFAC'.
    """
    searcher = EnhancedSanctionsSearcher("Iran", audit_logger=audit_logger)  # uses only the Google API machinery
    all_results: List[dict] = []

    if website:
        sanctions_queries = [
            f'"{website}" OFAC',
            f'"{website}" "SDN list"',
            f'"{website}" "sanctions violation"',
            f'"{website}" ("sanctions fine" OR "sanctions penalty" OR "sanctions enforcement")',
            f'"{website}" "entity list"',
            f'"{website}" "export control"',
            f'"{website}" (debarment OR "civil penalty" OR "consent agreement")',
        ]
        for q in sanctions_queries:
            all_results.extend(searcher.search_google(q, num_pages=1))

    if business_name:
        all_results.extend(searcher.search_google(
            f'"{business_name}" site:ofac.treasury.gov', num_pages=1
        ))
        all_results.extend(searcher.search_google(
            f'"{business_name}" (SDN OR sanctions OR "entity list")', num_pages=1
        ))
    if legal_name and legal_name.strip() and legal_name != business_name:
        all_results.extend(searcher.search_google(
            f'"{legal_name}" (SDN OR sanctions OR "entity list")', num_pages=1
        ))

    if not all_results:
        return None

    unique = list({r["link"]: r for r in all_results}.values())
    analyzed = searcher.analyze_search_results(unique, max_urls=50)
    if not analyzed:
        return None

    SANCTIONS_STRICT_TERMS = {
        "ofac", "sdn", "specially designated", "entity list", "denied persons",
        "sanctions violation", "sanctions evasion", "civil penalty",
        "consent agreement", "debarment", "blocked person", "embargo violation",
        "export control violation",
    }

    def has_strict_match(ar: dict) -> bool:
        blob = " ".join([
            (ar.get("original_title") or ""),
            (ar.get("original_snippet") or ""),
            *((ex.get("text") or "") for ex in ar.get("relevant_excerpts", [])),
        ]).lower()
        return any(t in blob for t in SANCTIONS_STRICT_TERMS)

    analyzed = [ar for ar in analyzed if has_strict_match(ar)]
    if not analyzed:
        return None

    return {
        "country": "Sanctions/OFAC",
        "search_results": unique,
        "analyzed_results": analyzed,
        "total_urls_analyzed": len(analyzed),
    }


# ==========================================================================
# Per-entity processing (thread-safe)
# ==========================================================================

def process_single_entity(
    entity: str, website: str, skip_content: bool,
    business_name: str = "", legal_name: str = "",
    audit_logger=None,
    variations: Optional[List[str]] = None,
) -> Optional[dict]:
    """Run a per-country sanctions investigation.

    ``variations`` is an optional user-supplied override: when present (custom
    country added via the country selector) it replaces the built-in variation
    map. Built-in countries always pass ``variations=None`` and use the
    hardcoded dict in ``SanctionsContentAnalyzer._get_country_variations``.
    """
    logger.info("Starting search for: %s", entity.upper())
    try:
        # Warm the analyzer cache with the override so every downstream
        # `get_analyzer(entity)` call sees the custom variations. Cache key
        # includes the override so custom countries don't collide with builtins.
        if variations:
            get_analyzer(entity, variations_override=variations)
        searcher = EnhancedSanctionsSearcher(entity, audit_logger=audit_logger)
        if skip_content:
            results = searcher.search_google(f'"{entity}" site:{website}', num_pages=1)
            data = {
                "country": entity,
                "search_results": results,
                "analyzed_results": [],
                "total_urls_analyzed": 0,
                "excluded_urls": [],
            }
        else:
            out = searcher.perform_enhanced_site_search(
                website, business_name=business_name, legal_name=legal_name
            )
            data = {
                "country": entity,
                "search_results": out["search_results"],
                "analyzed_results": out["analyzed_results"],
                "total_urls_analyzed": out["total_urls_analyzed"],
                "excluded_urls": out.get("excluded_urls", []),
            }
        logger.info("Finished search for: %s", entity.upper())
        return data
    except Exception as exc:
        logger.error("ERROR during search for %s: %s", entity, exc)
        return None


# ==========================================================================
# Main CLI workflow
# ==========================================================================

def run_enhanced_sanctions_site_search() -> None:
    print("=== Enhanced Sanctions Site Search Tool with NLP Analysis ===\n")

    sanctioned_entities = ["Iran", "Syria", "North Korea", "Cuba", "Luhansk", "Donetsk", "Crimea", "Ukraine", "Russia", "Belarus", "Myanmar", "Venezuela"]

    website = input("Enter the website to search (e.g., example.com): ").strip()
    if not website:
        print("Error: Website cannot be empty")
        return

    website = website.replace("http://", "").replace("https://", "").replace("www.", "").rstrip("/")
    skip_content = input("\nSkip content analysis for faster results? (y/n, default=n): ").strip().lower() == "y"

    print("\n" + "=" * 60 + "\nSTARTING ANALYSIS\n" + "=" * 60)

    print("\nStep 1: Social media profiles…")
    social_links = search_website_for_social_media(website)
    for p, u in social_links.items():
        print(f"    • {p.title()}: {u}")

    print(f"\nStep 2: Searching {len(sanctioned_entities)} sanctioned entities (up to 5 workers)…\n")
    all_reports: List[dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_map = {
            executor.submit(process_single_entity, entity, website, skip_content): entity
            for entity in sanctioned_entities
        }
        completed = 0
        for future in concurrent.futures.as_completed(future_map):
            entity = future_map[future]
            completed += 1
            try:
                data = future.result()
                if data:
                    all_reports.append(data)
                    n = len(data.get("search_results", []))
                    a = data.get("total_urls_analyzed", 0)
                    suffix = f"{n} results (analysis skipped)" if skip_content else f"{n} results, {a} analyzed"
                    print(f"    [{completed}/{len(sanctioned_entities)}] {entity}: {suffix}")
            except Exception as exc:
                print(f"    [{completed}/{len(sanctioned_entities)}] {entity}: ERROR – {exc}")

    all_reports.sort(key=lambda r: sanctioned_entities.index(r["country"]))

    name_co_results: List[dict] = []
    business_name = ""
    legal_name = ""
    print("\n" + "=" * 60)
    if input("Run business/legal name co-occurrence search? (y/n, default=n): ").strip().lower() == "y":
        business_name = input("Business (trading) name: ").strip()
        legal_name = input("Legal name: ").strip()
        logger.info("Starting Name Co-occurrence search…")
        ncs = NameCooccurrenceSearcher(business_name, legal_name, sanctioned_entities)
        name_co_results = ncs.perform(num_pages=1, threshold=85, max_workers=10)
        print(f"Name Co-occurrence search complete. {len(name_co_results)} URLs matched.")

    if all_reports or name_co_results:
        print("\n" + "=" * 60 + "\nGENERATING REPORT\n" + "=" * 60)

        print("\nStep 3: Generating investigator brief…")
        llm_verdict = InvestigatorBriefGenerator(
            all_reports, name_co_results, website, business_name, legal_name
        ).generate()
        if llm_verdict:
            _print_investigator_brief(llm_verdict)
        else:
            print("  (Skipped – set GOOGLE_CLOUD_PROJECT for Vertex AI, or GOOGLE_GENAI_API_KEY for AI Studio)")

        try:
            if skip_content and not name_co_results:
                path = generate_basic_enhanced_report(
                    all_reports, website, social_links, llm_verdict=llm_verdict
                )
            else:
                path = generate_enhanced_html_report(
                    all_reports, website, social_links,
                    name_co_results=name_co_results,
                    business_name=business_name,
                    legal_name=legal_name,
                    llm_verdict=llm_verdict,
                )
            print(f"\n📄 Report saved to: {path}")
            try:
                webbrowser.open(f"file://{os.path.abspath(path)}")
                print("🌐 Opening in browser…")
            except Exception:
                print("Could not open browser automatically. Please open the file manually.")
        except Exception as exc:
            import traceback
            logger.error("Error generating report: %s", exc)
            traceback.print_exc()
    else:
        print("\n⚠️  No data collected. Check errors above.")


# ==========================================================================
# Startup checks
# ==========================================================================

def check_dependencies() -> bool:
    missing = []
    for module, package in {"requests": "requests", "spacy": "spacy", "trafilatura": "trafilatura",
                            "bs4": "beautifulsoup4", "rapidfuzz": "rapidfuzz"}.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)

    # google-genai is optional – warn but don't block startup
    if _google_genai is None:
        logger.warning(
            "google-genai not installed. LLM verdict will be skipped. "
            "Install with: pip install google-genai"
        )

    if missing:
        print(f"\n❌ Missing packages: {', '.join(missing)}")
        print(f"Install with: pip install {' '.join(missing)}")
        return False
    try:
        spacy.load(SPACY_MODEL_LOADED)
    except OSError:
        print(f"\n❌ spaCy model not found. Run: python -m spacy download {SPACY_MODEL}")
        return False
    return True


def validate_api_credentials() -> bool:
    if not API_KEY or not SEARCH_ENGINE_ID:
        print("\n❌ API credentials not set.")
        print("  export GOOGLE_API_KEY='your_key'")
        print("  export GOOGLE_CSE_ID='your_cse_id'")
        return False
    logger.info("Testing API connection…")
    try:
        r = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"q": "test", "key": API_KEY, "cx": SEARCH_ENGINE_ID, "num": 1},
            timeout=10,
        )
        if r.status_code == 200:
            logger.info("API connection successful.")
            return True
        if r.status_code == 403:
            print("❌ API key invalid or Custom Search API not enabled.")
            return False
        if r.status_code == 400:
            print("❌ Search Engine ID invalid.")
            return False
        logger.warning("Unexpected status %d – continuing anyway.", r.status_code)
        return True
    except requests.exceptions.RequestException as exc:
        logger.warning("Could not verify API (%s) – continuing.", exc)
        return True


# ==========================================================================
# Streamlit UI
# ==========================================================================

def run_streamlit_app() -> None:
    if st is None or components is None:
        print("Streamlit not installed. Run: pip install streamlit")
        return

    st.set_page_config(page_title="Enhanced Sanctions Site Search", layout="wide")
    st.title("Enhanced Sanctions Site Search")

    website = st.text_input("Website to search (e.g., example.com)").strip()
    col_a, col_b = st.columns(2)
    with col_a:
        skip_content = st.checkbox("Skip content analysis for faster results", value=False)

    col1, col2 = st.columns(2)
    with col1:
        business_name = st.text_input("Business (trading) name (optional)").strip()
    with col2:
        legal_name = st.text_input("Legal name (optional)").strip()

    if st.button("Run Analysis", type="primary", use_container_width=True):
        if not website:
            st.warning("Please enter a website.")
            st.stop()

        sanctioned_entities = ["Iran", "Syria", "North Korea", "Cuba", "Luhansk", "Donetsk", "Crimea", "Ukraine", "Russia", "Belarus", "Myanmar", "Venezuela"]
        website_norm = website.replace("http://", "").replace("https://", "").replace("www.", "").rstrip("/")

        st.subheader("Step 1: Social Media Detection")
        with st.spinner("Searching for social media profiles…"):
            social_links = search_website_for_social_media(website_norm)
        if social_links:
            for p, u in social_links.items():
                st.markdown(f"- {p.title()}: {u}")
        else:
            st.info("No social media profiles found.")

        st.subheader("Step 2: Country Analysis")
        all_reports: List[dict] = []
        for entity in sanctioned_entities:
            with st.spinner(f"Analysing {entity}…"):
                data = process_single_entity(entity, website_norm, skip_content)
            if data:
                all_reports.append(data)
                n, a = len(data.get("search_results", [])), data.get("total_urls_analyzed", 0)
                st.success(f"{entity}: {n} results found{'' if skip_content else f', {a} analyzed'}.")
            else:
                st.error(f"{entity}: processing error.")

        all_reports.sort(key=lambda r: sanctioned_entities.index(r["country"]))

        name_co_results: List[dict] = []
        if business_name or legal_name:
            st.subheader("Step 3: Name Co-occurrence")
            with st.spinner("Searching open web for name + country co-occurrence…"):
                try:
                    ncs = NameCooccurrenceSearcher(business_name, legal_name, sanctioned_entities)
                    name_co_results = ncs.perform(num_pages=1, threshold=85, max_workers=10)
                    st.success(f"{len(name_co_results)} URLs matched.")
                except Exception as exc:
                    st.error(f"Name co-occurrence error: {exc}")

        # Step 4: Investigator brief
        st.subheader("Step 4: Investigator Brief")
        llm_verdict = None
        if _google_genai is None or (not USE_VERTEX and not GOOGLE_GENAI_API_KEY):
            if _google_genai is None:
                st.warning("Investigator brief skipped – `google-genai` package not importable. Run: `pip install google-genai`")
            else:
                st.warning("Investigator brief skipped – no LLM credentials. Set `GOOGLE_CLOUD_PROJECT` for Vertex AI (preferred) or `GOOGLE_GENAI_API_KEY` for AI Studio.")
        else:
            with st.spinner(f"Consulting {LLM_MODEL}…"):
                llm_verdict = InvestigatorBriefGenerator(
                    all_reports, name_co_results, website_norm, business_name, legal_name
                ).generate()
            if llm_verdict:
                rec_key = (llm_verdict.get("recommendation") or "INSUFFICIENT_DATA").upper()
                label = _REC_DISPLAY_LABEL.get(rec_key, "INSUFFICIENT DATA")
                _disp = {
                    "ESCALATE_FOR_REVIEW": st.error,
                    "ADDITIONAL_OSINT_NEEDED": st.warning,
                    "NO_FURTHER_ACTION_RECOMMENDED": st.success,
                }.get(rec_key, st.info)
                _disp(f"**{label}**  ·  Confidence: {llm_verdict.get('confidence_band', '?')}")
                st.write(brief_summary_text(llm_verdict))
                factors = brief_factors_list(llm_verdict)
                steps = brief_next_steps_list(llm_verdict)
                if factors:
                    st.markdown("**Key risk factors:**")
                    for f_item in factors:
                        st.markdown(f"- {f_item}")
                if steps:
                    st.markdown("**Suggested next steps:**")
                    for s_item in steps:
                        st.markdown(f"- {s_item}")
                dropped = llm_verdict.get("unverified_claims_dropped") or 0
                if dropped:
                    st.caption(f"{dropped} unverified claim(s) dropped by post-verification.")
                st.caption("Citation-grounded investigator brief · no disposition decided")
            else:
                st.warning("Investigator brief generation failed – check logs.")

        # Step 5: Report
        st.subheader("Step 5: Report")
        try:
            if skip_content and not name_co_results:
                fpath = generate_basic_enhanced_report(
                    all_reports, website_norm, social_links, llm_verdict=llm_verdict
                )
                st.success("Basic report generated.")
            else:
                fpath = generate_enhanced_html_report(
                    all_reports, website_norm, social_links,
                    name_co_results=name_co_results,
                    business_name=business_name,
                    legal_name=legal_name,
                    llm_verdict=llm_verdict,
                )
                st.success("Enhanced report generated.")

            with open(fpath, "r", encoding="utf-8") as f:
                html_content = f.read()
            components.html(html_content, height=1400, scrolling=True)
            st.download_button(
                label="Download HTML Report",
                data=html_content.encode("utf-8"),
                file_name=os.path.basename(fpath),
                mime="text/html",
                use_container_width=True,
            )
        except Exception as exc:
            st.error(f"Error generating report: {exc}")


# ==========================================================================
# Entry point
# ==========================================================================

def main() -> None:
    print("""
╔══════════════════════════════════════════════════════════════╗
║     ENHANCED SANCTIONS SITE SEARCH TOOL v2.3                 ║
║     NLP-Based Risk Analysis with Interactive Reporting       ║
╚══════════════════════════════════════════════════════════════╝
    """)
    if not check_dependencies():
        sys.exit(1)
    if not validate_api_credentials():
        sys.exit(1)

    while True:
        try:
            run_enhanced_sanctions_site_search()
            if input("\nSearch another website? (y/n): ").strip().lower() != "y":
                break
        except KeyboardInterrupt:
            print("\n\n⚠️  Interrupted.")
            break
        except Exception as exc:
            import traceback
            logger.error("Unexpected error: %s", exc)
            traceback.print_exc()
            if input("\nTry again? (y/n): ").strip().lower() != "y":
                break

    print("\n" + "=" * 60 + "\nThank you for using the Enhanced Sanctions Site Search Tool!\n" + "=" * 60)


if __name__ == "__main__":
    if _is_running_in_streamlit():
        run_streamlit_app()
    else:
        if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
            print("Usage: python sanctions_search_tool.py\nSet GOOGLE_API_KEY and GOOGLE_CSE_ID env vars first.")
            sys.exit(0)
        if sys.version_info < (3, 6):
            print("❌ Python 3.6+ required.")
            sys.exit(1)
        main()
