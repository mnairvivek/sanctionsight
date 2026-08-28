"""
FastAPI backend wrapper for the Sanctions Search Engine.
Exposes the existing Python logic as a REST API with SSE progress streaming.
"""

import asyncio
import json
import os
import re
import time
import uuid
import logging
from datetime import datetime
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor

from pathlib import Path

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Import the existing sanctions engine (unchanged)
import sanctions_engine as engine

# Import the sanctions list screener
from sanctions_list_screener import SanctionsListScreener

from countries import BUILTINS as BUILTIN_COUNTRIES

# Phase 1: durable storage + tamper-evident audit. We import lazily-tolerantly
# so the API still boots if SQLAlchemy hasn't been pip-installed yet; the
# audit JSONL log is stdlib-only and always works.
import audit as audit_mod

try:
    import storage
    _STORAGE_AVAILABLE = True
except Exception as _storage_exc:  # pragma: no cover — install-time branch
    storage = None
    _STORAGE_AVAILABLE = False
    logging.getLogger("sanctions_api").warning(
        "Phase 1 storage unavailable (%s). Run: pip install -r requirements.txt && alembic upgrade head",
        _storage_exc,
    )

# Phase 3: auth. Same tolerant pattern — API boots without pyjwt/passlib,
# just the /api/auth/* + HITL routes throw 503 until the deps are installed.
import auth as auth_mod

logger = logging.getLogger("sanctions_api")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Per-job log files
#
# Each analysis run attaches a dedicated FileHandler to the root logger, so
# every log line from every module (engine, auth, storage, etc.) during the
# job is captured verbatim in backend/logs/. The handler is detached in the
# finally block to avoid leaking across jobs. Existing console logging is
# unchanged.
# ---------------------------------------------------------------------------
_JOB_LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def _attach_job_log_handler(job_id: str, website: str) -> Optional[logging.FileHandler]:
    try:
        os.makedirs(_JOB_LOGS_DIR, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe_site = re.sub(r"[^\w.-]+", "_", website or "nosite")[:40] or "nosite"
        log_path = os.path.join(_JOB_LOGS_DIR, f"job_{ts}_{safe_site}_{job_id[:8]}.log")
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root = logging.getLogger()
        if root.level > logging.INFO or root.level == logging.NOTSET:
            root.setLevel(logging.INFO)
        root.addHandler(handler)
        logger.info("Job %s log file: %s", job_id, log_path)
        return handler
    except Exception as exc:
        logger.warning("Could not attach per-job log handler: %s", exc)
        return None


def _detach_job_log_handler(handler: Optional[logging.Handler]) -> None:
    if handler is None:
        return
    try:
        logging.getLogger().removeHandler(handler)
        handler.flush()
        handler.close()
    except Exception:
        pass


app = FastAPI(title="Sanctions Site Search API", version="2.3")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory job store
#
# The dict still backs the SSE progress stream (fast, ephemeral updates).
# Job lifecycle and the final result are persisted to SQLite so history
# survives process restarts and is queryable by SQL.
# ---------------------------------------------------------------------------
jobs: Dict[str, dict] = {}
executor = ThreadPoolExecutor(max_workers=3)


# ---------------------------------------------------------------------------
# Phase 1 helpers — persist Job lifecycle into the DB
# ---------------------------------------------------------------------------

def _db_create_job(job_id: str, req: "AnalyzeRequest") -> None:
    if not _STORAGE_AVAILABLE:
        return
    try:
        with storage.get_session() as session:
            row = storage.Job(
                id=job_id,
                website=req.website or "",
                business_name=req.business_name or "",
                legal_name=req.legal_name or "",
                skip_content=bool(req.skip_content),
                run_name_cooccurrence=bool(req.run_name_cooccurrence),
                status="pending",
                progress=0.0,
                current_step="Queued",
            )
            session.add(row)
            session.commit()
    except Exception as exc:
        logger.warning("Job DB insert failed for %s: %s", job_id, exc)


def _db_update_job(job_id: str, **fields) -> None:
    if not _STORAGE_AVAILABLE:
        return
    try:
        with storage.get_session() as session:
            row = session.get(storage.Job, job_id)
            if row is None:
                return
            for k, v in fields.items():
                setattr(row, k, v)
            session.commit()
    except Exception as exc:
        logger.warning("Job DB update failed for %s: %s", job_id, exc)


def _mirror_audit_to_db(job_id: str) -> None:
    if not _STORAGE_AVAILABLE:
        return
    try:
        events = list(audit_mod.read_events(job_id))
        audit_mod.sync_to_db(job_id, events)
    except Exception as exc:
        logger.warning("Audit DB mirror failed for %s: %s", job_id, exc)


def _ensure_model_version() -> Optional[int]:
    """Seed a ModelVersion row describing the current analyser stack so each
    Job can cite the exact model/prompt/rules versions used. Idempotent by
    `model_version_hash`. Returns the row id or None if storage is off."""
    if not _STORAGE_AVAILABLE:
        return None
    try:
        import hashlib as _hashlib
        model_id = getattr(engine, "LLM_MODEL", None) or os.environ.get("LLM_MODEL", "gemini-2.5-flash")
        spacy_model = getattr(engine, "SPACY_MODEL_LOADED", None) or "en_core_web_sm"
        rules_version = "v2.3"
        prompt_version = "investigator_brief_v1"
        schema_version = "phase2_schemas_v1"
        vertex_project = getattr(engine, "VERTEX_PROJECT", "") or ""
        vertex_location = getattr(engine, "VERTEX_LOCATION", "") or ""
        llm_backend = "vertex" if getattr(engine, "USE_VERTEX", False) else "aistudio"
        fingerprint = (
            f"{model_id}|{spacy_model}|{rules_version}|{prompt_version}|{schema_version}"
            f"|{llm_backend}|{vertex_project}|{vertex_location}"
        )
        mv_hash = _hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]

        notes_payload = (
            f"Auto-registered from FastAPI startup / first analysis. "
            f"backend={llm_backend} project={vertex_project or 'n/a'} location={vertex_location or 'n/a'}"
        )

        with storage.get_session() as session:
            from sqlalchemy import select
            row = session.execute(
                select(storage.ModelVersion).where(storage.ModelVersion.model_version_hash == mv_hash)
            ).scalar_one_or_none()
            if row is None:
                row = storage.ModelVersion(
                    model_id=model_id,
                    model_version_hash=mv_hash,
                    prompt_template_version=prompt_version,
                    schema_version=schema_version,
                    spacy_model=spacy_model,
                    rules_version=rules_version,
                    deployed_at=datetime.utcnow(),
                    notes=notes_payload,
                )
                session.add(row)
                session.commit()
            return row.id
    except Exception as exc:
        logger.warning("ModelVersion seed failed: %s", exc)
        return None


def _collect_unique_link_rows(
    all_reports: List[dict],
    name_co_results: List[dict],
    regulatory_report: Optional[dict],
) -> List[dict]:
    """One row per unique URL with a stable country label + the analyzed dict.

    Prefer the first-seen analyzed_result so excerpts/trigger sentences from
    the richer bucket are used. Same URL appearing under multiple countries
    only gets one LLM call and one LinkVerdict row.
    """
    seen: Dict[str, dict] = {}

    def _feed(bucket_reports):
        for report in bucket_reports or []:
            country = report.get("country")
            for ar in report.get("analyzed_results", []) or []:
                url = (ar.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen[url] = {"url": url, "country": country, "result": ar}

    _feed(all_reports)
    if regulatory_report:
        _feed([regulatory_report])
    for r in name_co_results or []:
        url = (r.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen[url] = {"url": url, "country": r.get("country"), "result": r}

    return list(seen.values())


def _run_per_link_verdicts(
    job_id: str,
    link_rows: List[dict],
    website: str,
    business_name: str,
    legal_name: str,
    progress_cb=None,
) -> Dict[str, dict]:
    """Call the per-link LLM on every URL, capped at 4 concurrent requests.

    Returns a dict keyed by url_hash → {concern, reasoning, model, error}.
    Swallows per-row errors so a single flaky URL can't fail the whole job;
    errors are recorded on the row itself so the UI and the DB persistence
    step can surface them.

    Concurrency raised from 4 to 8 — assumes the Vertex project has either a
    raised gemini-2.5-flash RPM quota or enough credit headroom to absorb
    occasional 429s. The generator retries 429s with exponential backoff
    internally, so brief spikes above quota don't drop verdicts.
    """
    if not link_rows:
        return {}

    gen = engine.PerLinkVerdictGenerator(
        website=website, business_name=business_name, legal_name=legal_name,
    )
    out: Dict[str, dict] = {}
    total = len(link_rows)
    logger.info("[job=%s] Running per-link LLM verdicts on %d URLs (concurrency=16)", job_id, total)
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {
            pool.submit(
                gen.generate_for_link,
                row["url"], row.get("country"), row["result"],
            ): row
            for row in link_rows
        }
        done = 0
        import concurrent.futures as _cf
        for fut in _cf.as_completed(futures):
            row = futures[fut]
            url = row["url"]
            uh = storage.url_hash(url)
            try:
                verdict = fut.result()
            except Exception as exc:
                verdict = {"error": f"unexpected: {exc}"}
            out[uh] = {**verdict, "url": url, "country": row.get("country")}
            done += 1
            if progress_cb is not None and (done % 5 == 0 or done == total):
                progress_cb(done, total)
    return out


def _persist_link_verdicts(job_id: str, verdicts: Dict[str, dict]) -> int:
    """Upsert LinkVerdict rows for the job. Preserves prior analyst agreement
    if a row already exists (shouldn't happen in fresh jobs, but cheap to be
    safe for reopened/rerun cases)."""
    if not _STORAGE_AVAILABLE or not verdicts:
        return 0
    try:
        from sqlalchemy import select
        written = 0
        now = datetime.utcnow()
        with storage.get_session() as session:
            for uh, v in verdicts.items():
                existing = session.execute(
                    select(storage.LinkVerdict).where(
                        storage.LinkVerdict.job_id == job_id,
                        storage.LinkVerdict.url_hash == uh,
                    )
                ).scalar_one_or_none()

                concern = v.get("concern")
                reasoning = v.get("reasoning")
                model = v.get("model")
                error = v.get("error")

                if existing is None:
                    row = storage.LinkVerdict(
                        job_id=job_id,
                        url_hash=uh,
                        url=v.get("url", "")[:2048],
                        llm_concern=concern,
                        llm_reasoning=reasoning,
                        llm_model=model,
                        llm_error=error,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(row)
                else:
                    existing.llm_concern = concern
                    existing.llm_reasoning = reasoning
                    existing.llm_model = model
                    existing.llm_error = error
                    existing.updated_at = now
                written += 1
            session.commit()
        logger.info("Persisted %d link verdicts for job %s", written, job_id)
        return written
    except Exception as exc:
        logger.warning("LinkVerdict persistence failed for %s: %s", job_id, exc)
        return 0


def _load_case_review(job_id: str) -> Optional[dict]:
    """Return the case-level analyst review fields (summary + agree/disagree)
    off JobState, or None if storage is off / no row exists. The core
    workflow_status/sign-off fields are already surfaced via /hitl-overview;
    this helper carries just the case-review payload that the new UI binds to."""
    if not _STORAGE_AVAILABLE:
        return None
    try:
        from sqlalchemy import select
        with storage.get_session() as session:
            row = session.execute(
                select(storage.JobState).where(storage.JobState.job_id == job_id)
            ).scalar_one_or_none()
            if row is None:
                return None
            return {
                "analyst_case_summary": row.analyst_case_summary,
                "analyst_agrees_with_brief": row.analyst_agrees_with_brief,
                "analyst_case_disagree_reason": row.analyst_case_disagree_reason,
                "analyst_case_updated_by": row.analyst_case_updated_by,
                "analyst_case_updated_at": row.analyst_case_updated_at.isoformat() if row.analyst_case_updated_at else None,
            }
    except Exception as exc:
        logger.warning("case_review load failed for %s: %s", job_id, exc)
        return None


def _load_link_verdicts(job_id: str) -> Dict[str, dict]:
    """Read the job's LinkVerdict rows as a dict keyed by url_hash."""
    if not _STORAGE_AVAILABLE:
        return {}
    try:
        from sqlalchemy import select
        out: Dict[str, dict] = {}
        with storage.get_session() as session:
            rows = session.execute(
                select(storage.LinkVerdict).where(storage.LinkVerdict.job_id == job_id)
            ).scalars().all()
            for row in rows:
                out[row.url_hash] = {
                    "url_hash": row.url_hash,
                    "url": row.url,
                    "llm_concern": row.llm_concern,
                    "llm_reasoning": row.llm_reasoning,
                    "llm_model": row.llm_model,
                    "llm_error": row.llm_error,
                    "analyst_agrees": row.analyst_agrees,
                    "analyst_disagree_reason": row.analyst_disagree_reason,
                    "analyst_updated_by": row.analyst_updated_by,
                    "analyst_updated_at": row.analyst_updated_at.isoformat() if row.analyst_updated_at else None,
                }
        return out
    except Exception as exc:
        logger.warning("LinkVerdict load failed for %s: %s", job_id, exc)
        return {}


def _persist_findings_and_excerpts(
    job_id: str,
    all_reports: List[dict],
    name_co_results: List[dict],
    regulatory_report: Optional[dict],
) -> int:
    """Write Excerpt + Finding rows so HITL has per-row state to attach to.

    Returns the number of Finding rows written (0 on error). This is a
    best-effort mirror — if it fails, analysis still completes and the
    frontend keeps working. HITL endpoints simply have nothing to mutate.
    """
    if not _STORAGE_AVAILABLE:
        return 0
    try:
        findings_written = 0
        with storage.get_session() as session:
            # Dedup state shared across every _sink call: the same URL — and
            # therefore the same (job_id, excerpt_id) — can appear under
            # multiple sanctioned-country reports (e.g. a page mentioning
            # both Iran and Syria) and across both the per-country bucket and
            # the regulatory bucket. The excerpts table enforces
            # UNIQUE(job_id, excerpt_id), so we insert each excerpt once and
            # route subsequent Findings to the existing row's PK.
            excerpt_pk_by_id: Dict[str, int] = {}
            trigger_pk_by_url: Dict[str, Dict[str, int]] = {}

            def _sink(bucket_reports, source_label: str):
                nonlocal findings_written
                for report in bucket_reports:
                    country = report.get("country")
                    for ar in report.get("analyzed_results", []):
                        url = ar.get("url", "")
                        if not url:
                            continue
                        trigger_to_pk = trigger_pk_by_url.setdefault(url, {})

                        for idx, ex in enumerate(ar.get("relevant_excerpts", [])[:5]):
                            trigger = ex.get("trigger_sentence") or ""
                            # IDs stamped earlier by engine.assign_stable_ids.
                            # Fallback recomputation mirrors the helper's
                            # (url, trigger, per-URL index) scheme for code
                            # paths that persist without running the brief.
                            excerpt_id = ex.get("excerpt_id") or storage.stable_excerpt_id(url, trigger, idx)
                            source_id = ex.get("source_id") or storage.stable_source_id(url)

                            existing_pk = excerpt_pk_by_id.get(excerpt_id)
                            if existing_pk is not None:
                                # Already inserted under a prior country /
                                # bucket. The excerpt's own country field
                                # records where it was first seen; each
                                # Finding below carries its own country so
                                # per-country attribution is preserved.
                                if trigger:
                                    trigger_to_pk[trigger] = existing_pk
                                continue

                            excerpt_row = storage.Excerpt(
                                job_id=job_id,
                                source_id=source_id,
                                excerpt_id=excerpt_id,
                                url=url,
                                country=country,
                                risk_type=ex.get("risk_type"),
                                risk_score=float(ex.get("risk_score") or 0.0),
                                confidence=float(ex.get("confidence") or 0.0),
                                trigger_sentence=trigger,
                                text=ex.get("text") or "",
                                content_hash=ar.get("content_hash"),
                                extraction_type=ar.get("extraction_type"),
                                language=ar.get("language"),
                            )
                            session.add(excerpt_row)
                            session.flush()
                            excerpt_pk_by_id[excerpt_id] = excerpt_row.id
                            if trigger:
                                trigger_to_pk[trigger] = excerpt_row.id

                        for f in ar.get("findings", []):
                            if not f.get("relevant", True):
                                continue
                            sentence = f.get("sentence") or ""
                            excerpt_pk = trigger_to_pk.get(sentence)
                            finding_row = storage.Finding(
                                job_id=job_id,
                                excerpt_pk=excerpt_pk,
                                country=country,
                                url=url,
                                risk_type=f.get("risk_type", "GENERAL"),
                                risk_score=float(f.get("risk_score") or 0.0),
                                confidence=float(f.get("confidence") or 0.0),
                                sentence=sentence,
                                note=f.get("note"),
                            )
                            session.add(finding_row)
                            session.flush()

                            session.add(storage.FindingState(
                                finding_id=finding_row.id,
                                status="pending",
                                fp_override=False,
                                updated_at=datetime.utcnow(),
                            ))
                            session.add(storage.FindingStatusHistory(
                                finding_id=finding_row.id,
                                from_status=None,
                                to_status="pending",
                                changed_at=datetime.utcnow(),
                                changed_by="system",
                                reason=f"Initial HITL queue entry ({source_label})",
                            ))
                            findings_written += 1

            _sink(all_reports, "country_search")
            if regulatory_report:
                _sink([regulatory_report], "regulatory_search")
            if name_co_results:
                _sink([{"country": r.get("country"), "analyzed_results": [r]} for r in name_co_results],
                      "name_cooccurrence")

            # Case-level workflow row (draft). Single source of truth for sign-off state.
            session.merge(storage.JobState(
                job_id=job_id,
                workflow_status="draft",
                updated_at=datetime.utcnow(),
            ))
            session.commit()
        logger.info("Persisted %d findings for job %s", findings_written, job_id)
        return findings_written
    except Exception as exc:
        logger.warning("Persistence of findings/excerpts failed for %s: %s", job_id, exc)
        return 0


@app.on_event("startup")
async def _startup_initialize_storage() -> None:  # pragma: no cover — startup
    if not _STORAGE_AVAILABLE:
        return
    try:
        storage.init_db()
        logger.info("Phase 1 storage ready.")
    except Exception as exc:
        logger.warning("Could not initialise storage on startup: %s", exc)
    try:
        seeded = auth_mod.seed_admin_from_env()
        if seeded:
            logger.info("Admin bootstrap ready for %s", seeded)
    except Exception as exc:
        logger.warning("Admin bootstrap failed: %s", exc)

# ---------------------------------------------------------------------------
# Sanctions list screener (loaded once at startup)
# ---------------------------------------------------------------------------
_screener_data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_screener: Optional[SanctionsListScreener] = None

def _get_screener() -> Optional[SanctionsListScreener]:
    global _screener
    if _screener is None and os.path.isdir(_screener_data_dir):
        _screener = SanctionsListScreener(_screener_data_dir)
    return _screener

SANCTIONED_ENTITIES = BUILTIN_COUNTRIES


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class CustomCountry(BaseModel):
    name: str
    variations: List[str] = []


class AnalyzeRequest(BaseModel):
    website: str = ""
    skip_content: bool = False
    business_name: str = ""
    legal_name: str = ""
    run_name_cooccurrence: bool = False
    # None => use all builtins (back-compat with older clients).
    selected_builtins: Optional[List[str]] = None
    custom_countries: List[CustomCountry] = []

    def effective_entities(self) -> List[str]:
        builtins = (
            self.selected_builtins
            if self.selected_builtins is not None
            else list(BUILTIN_COUNTRIES)
        )
        custom_names = [c.name for c in self.custom_countries if c.name.strip()]
        return [e for e in builtins if e in BUILTIN_COUNTRIES] + custom_names

    def custom_variations(self) -> Dict[str, List[str]]:
        return {
            c.name: [v.strip() for v in c.variations if v.strip()]
            for c in self.custom_countries
            if c.name.strip()
        }


class JobStatus(BaseModel):
    job_id: str
    status: str  # pending | running | completed | failed
    progress: float  # 0-100
    current_step: str
    result: Optional[dict] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Analysis runner (runs in background thread)
# ---------------------------------------------------------------------------
def _run_analysis(job_id: str, req: AnalyzeRequest):
    job = jobs[job_id]
    audit_logger = audit_mod.AuditLogger(job_id)
    log_handler = _attach_job_log_handler(job_id, req.website or req.business_name or req.legal_name or "")
    job_started_at = time.time()
    try:
        logger.info(
            "=== Job %s started — website=%r business=%r legal=%r skip_content=%s name_co=%s countries=%s ===",
            job_id, req.website, req.business_name, req.legal_name,
            req.skip_content, req.run_name_cooccurrence,
            (req.selected_builtins or []) + [c.name for c in (req.custom_countries or []) if c and c.name],
        )
        audit_logger.log_job_started({
            "website": req.website,
            "business_name": req.business_name,
            "legal_name": req.legal_name,
            "skip_content": req.skip_content,
            "run_name_cooccurrence": req.run_name_cooccurrence,
        })
        _db_update_job(job_id, status="running", current_step="Starting")

        website = req.website.replace("http://", "").replace("https://", "").replace("www.", "").rstrip("/")

        job["status"] = "running"
        job["partial"] = {"social_links": {}}

        # Per-job HTTP extraction cache — cleared at start so dev-server hot
        # reloads can't serve stale content from a prior job.
        engine.reset_extraction_cache()

        # ==================================================================
        # PHASE 1: Sanctions list screening (fast, runs first)
        # ==================================================================
        list_screening_results = []
        is_sanctioned = False
        screener = _get_screener()

        if screener and screener.entities:
            job["current_step"] = "Screening against sanctions lists"
            job["progress"] = 5

            seen_ids = set()
            for name in [req.business_name, req.legal_name]:
                if name and name.strip():
                    matches = screener.screen(name.strip(), threshold=82)
                    for m in matches:
                        key = (m["list_source"], m["source_id"], m["listed_name"])
                        if key not in seen_ids:
                            seen_ids.add(key)
                            m["query_name"] = name.strip()
                            list_screening_results.append(m)

            if website:
                site_matches = screener.screen_website(website, threshold=80)
                for m in site_matches:
                    key = (m["list_source"], m["source_id"], m["listed_name"])
                    if key not in seen_ids:
                        seen_ids.add(key)
                        m["query_name"] = website
                        list_screening_results.append(m)

            list_screening_results.sort(key=lambda r: r["score"], reverse=True)

            # Check if any high-confidence match exists (score >= 90)
            high_conf_matches = [m for m in list_screening_results if m["score"] >= 90]
            is_sanctioned = len(high_conf_matches) > 0

            logger.info(
                "Sanctions list screening: %d matches (%d high-confidence). Sanctioned: %s",
                len(list_screening_results), len(high_conf_matches), is_sanctioned,
            )
            audit_logger.log_list_screening(
                query_name=" | ".join(
                    s for s in [req.business_name, req.legal_name, website] if s
                ),
                match_count=len(list_screening_results),
                high_conf_count=len(high_conf_matches),
            )
            _db_update_job(job_id, is_sanctioned=bool(is_sanctioned))

            # Resolve official sanctions page URLs for high-confidence matches
            if high_conf_matches:
                job["current_step"] = "Resolving sanctions page URLs"
                job["progress"] = 12
                screener.resolve_urls_for_matches(
                    list_screening_results,
                    api_key=engine.API_KEY,
                    cse_id=engine.SEARCH_ENGINE_ID,
                    min_score=90,
                )

            job["progress"] = 15
        else:
            logger.info("Sanctions list screening skipped — no lists loaded. Run update_lists.py first.")
            job["progress"] = 15

        # ==================================================================
        # PHASE 2: Open-web search (skipped if entity is directly sanctioned)
        # ==================================================================
        all_reports: List[dict] = []
        social_links = {}
        name_co_results = []
        regulatory_report: Optional[dict] = None
        # Tracking buckets for the final report's "Needs analyst review" and
        # "Not attempted" sections. Populated as each search stage runs.
        ncs_excluded_urls: List[dict] = []
        ncs_failed_urls: List[dict] = []

        if is_sanctioned:
            # Entity found on sanctions lists — skip open-web search
            job["current_step"] = "Entity found on sanctions list — skipping open-web search"
            job["progress"] = 80
            logger.info("Skipping open-web search — entity is directly sanctioned.")
        else:
            # No direct list hit — proceed with full open-web investigation
            if website:
                # Step 2a: Social media
                job["current_step"] = "Detecting social media profiles"
                job["progress"] = 20
                social_links = engine.search_website_for_social_media(website)
                job["partial"] = {"social_links": social_links}
                job["progress"] = 25

                # Step 2b: Country analysis — parallel (B2).
                # Each worker runs a full Google-search + content-extraction +
                # NLP pipeline for one country. Workers share the module-level
                # analyzer cache (locked in sanctions_engine.get_analyzer) and
                # the per-process spaCy pipeline; the GIL is released during
                # HTTP fetches, which is where most of the wall-clock lives.
                import threading as _threading
                from concurrent.futures import ThreadPoolExecutor, as_completed

                effective_entities = req.effective_entities()
                custom_vars = req.custom_variations()
                entity_order = {e: i for i, e in enumerate(effective_entities)}
                total_entities = max(1, len(effective_entities))
                completed_count = 0
                job_lock = _threading.Lock()

                def _run_entity(entity: str):
                    return entity, engine.process_single_entity(
                        entity, website, req.skip_content,
                        business_name=req.business_name, legal_name=req.legal_name,
                        audit_logger=audit_logger,
                        variations=custom_vars.get(entity),
                    )

                with ThreadPoolExecutor(max_workers=min(12, total_entities)) as pool:
                    futures = {pool.submit(_run_entity, e): e for e in effective_entities}
                    for fut in as_completed(futures):
                        entity_name = futures[fut]
                        try:
                            entity, data = fut.result()
                        except Exception as exc:
                            logger.error("Country worker failed for %s: %s", entity_name, exc)
                            data = None
                        with job_lock:
                            completed_count += 1
                            job["current_step"] = (
                                f"Analyzing {entity_name} "
                                f"({completed_count}/{total_entities})"
                            )
                            job["progress"] = 25 + completed_count / total_entities * 40
                            if data:
                                all_reports.append(data)
                                job["partial"]["reports"] = _serialize_reports(all_reports)

                all_reports.sort(key=lambda r: entity_order.get(r["country"], 999))

                # Global OFAC/sanctions-term search — runs once, not once per country.
                # Kept separate from all_reports so it doesn't pollute country stats/charts.
                if not req.skip_content:
                    job["current_step"] = "Running global OFAC/sanctions term search"
                    job["progress"] = 66
                    regulatory_report = engine.perform_global_ofac_search(
                        website,
                        business_name=req.business_name,
                        legal_name=req.legal_name,
                        audit_logger=audit_logger,
                    )
                    job["partial"]["reports"] = _serialize_reports(all_reports)
            else:
                job["current_step"] = "No website provided — skipping site search"
                job["progress"] = 65

            # Step 2c: Name co-occurrence (optional)
            if req.run_name_cooccurrence and (req.business_name or req.legal_name):
                job["current_step"] = "Running name co-occurrence search"
                job["progress"] = 70
                ncs = engine.NameCooccurrenceSearcher(
                    req.business_name, req.legal_name, req.effective_entities(),
                    audit_logger=audit_logger,
                    custom_variations=req.custom_variations(),
                )
                name_co_results = ncs.perform(num_pages=2, threshold=85, max_workers=10)
                ncs_excluded_urls = list(ncs.excluded_urls)
                ncs_failed_urls = list(ncs.failed_urls)

        # ==================================================================
        # PHASE 3: Verdict & report
        # ==================================================================

        # Stamp every excerpt with its stable (source_id, excerpt_id) pair
        # before any downstream consumer reads them. The LLM prompt, the JSON
        # payload returned to the UI, and the DB persistence step must all
        # see the same IDs — otherwise investigator-brief citations cannot
        # be resolved in the popover.
        engine.assign_stable_ids(all_reports, name_co_results, regulatory_report)

        # Step 3a: Per-link LLM verdicts (concurrency cap 10). These feed the
        # ResultsTable row-level concern badges and are independent of the
        # aggregate brief.
        per_link_verdicts: Dict[str, dict] = {}
        link_rows = _collect_unique_link_rows(all_reports, name_co_results, regulatory_report)
        if link_rows:
            job["current_step"] = "Running per-link LLM verdicts"
            job["progress"] = 78

            def _link_progress(done, total):
                job["current_step"] = f"Running per-link LLM verdicts ({done}/{total})"
                # Smooth 78 → 84 across the per-link sweep.
                job["progress"] = 78 + int(6 * done / max(total, 1))

            per_link_verdicts = _run_per_link_verdicts(
                job_id, link_rows, website, req.business_name, req.legal_name,
                progress_cb=_link_progress,
            )

        # Step 3b: Investigator brief
        job["current_step"] = "Generating investigator brief"
        job["progress"] = 85

        # Compute extraction buckets before the brief so the LLM gets a
        # coverage line (N analysed, M need review, K not attempted).
        extraction_buckets = _build_extraction_buckets(
            all_reports, name_co_results, ncs_excluded_urls, ncs_failed_urls,
        )
        urls_need_review = len(extraction_buckets["needs_analyst_review"])
        urls_not_attempted = len(extraction_buckets["not_attempted"])
        # urls_analyzed_fully = all analysed URLs (across countries + name-co)
        # minus the ones flagged for review.
        total_analysed = sum(
            len(r.get("analyzed_results", []) or []) for r in (all_reports or [])
        ) + len(name_co_results or [])
        urls_analyzed_fully = max(0, total_analysed - urls_need_review)
        coverage = {
            "urls_analyzed_fully": urls_analyzed_fully,
            "urls_need_review": urls_need_review,
            "urls_not_attempted": urls_not_attempted,
        }

        llm_verdict = engine.InvestigatorBriefGenerator(
            all_reports, name_co_results, website,
            req.business_name, req.legal_name,
            audit_logger=audit_logger,
            per_link_verdicts=per_link_verdicts,
            coverage=coverage,
        ).generate()

        # If entity is sanctioned but LLM didn't produce a brief, synthesize
        # one from the direct list match. Claims cite no excerpts because the
        # sanctioned-match path bypasses open-web evidence; the UI shows the
        # list-screening card separately as the source of truth.
        if is_sanctioned and not llm_verdict:
            matched_lists = list(set(m["list_source"] for m in list_screening_results if m["score"] >= 90))
            matched_names = list(set(m["listed_name"] for m in list_screening_results if m["score"] >= 90))
            llm_verdict = {
                "recommendation": "ESCALATE_FOR_REVIEW",
                "confidence_band": "HIGH",
                "summary_claims": [],
                "risk_factor_claims": [],
                "suggested_next_steps": [],
                "unverified_claims_dropped": 0,
                "verification_report": {
                    "total_claims": 0, "verified_claims": 0, "dropped_claims": 0, "per_claim": [],
                },
                "evidence_count": 0,
                "note": (
                    f"Direct sanctions list match on {len(matched_lists)} list(s): "
                    f"{', '.join(matched_lists)}. Matched name(s): {', '.join(matched_names[:3])}. "
                    "Open-web investigation was skipped — see the sanctions list screening card for source details."
                ),
                "direct_list_match": {
                    "matched_lists": matched_lists,
                    "matched_names": matched_names,
                },
            }

        # Step 3b: Generate HTML report
        job["current_step"] = "Generating HTML report"
        job["progress"] = 90
        html_report_path = None

        reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
        os.makedirs(reports_dir, exist_ok=True)

        try:
            import tempfile
            _orig_tmpdir = tempfile.gettempdir
            _orig_streamlit = engine._is_running_in_streamlit
            tempfile.gettempdir = lambda: reports_dir
            engine._is_running_in_streamlit = lambda: True

            try:
                if req.skip_content and not name_co_results:
                    html_report_path = engine.generate_basic_enhanced_report(
                        all_reports, website, social_links,
                        llm_verdict=llm_verdict,
                        list_screening=list_screening_results,
                    )
                else:
                    html_report_path = engine.generate_enhanced_html_report(
                        all_reports, website, social_links,
                        name_co_results=name_co_results,
                        business_name=req.business_name,
                        legal_name=req.legal_name,
                        llm_verdict=llm_verdict,
                        list_screening=list_screening_results,
                        regulatory_report=regulatory_report,
                        needs_analyst_review=extraction_buckets["needs_analyst_review"],
                        not_attempted=extraction_buckets["not_attempted"],
                    )
            finally:
                tempfile.gettempdir = _orig_tmpdir
                engine._is_running_in_streamlit = _orig_streamlit

            if html_report_path and os.path.exists(html_report_path):
                logger.info("HTML report saved to: %s (size: %d bytes)",
                            html_report_path, os.path.getsize(html_report_path))
            else:
                logger.warning("HTML report path returned but file not found: %s", html_report_path)
                html_report_path = None
        except Exception as exc:
            logger.warning("HTML report generation failed: %s", exc)
            import traceback
            traceback.print_exc()
            html_report_path = None

        # Step 3c: Assemble final result
        job["current_step"] = "Assembling report"
        job["progress"] = 95

        highlight_keywords = _build_highlight_keywords(all_reports, name_co_results)

        result = {
            "job_id": job_id,
            "website": website,
            "timestamp": datetime.now().isoformat(),
            "social_links": social_links,
            "reports": _serialize_reports(all_reports, link_verdicts=per_link_verdicts),
            "name_co_results": _serialize_name_co(name_co_results, link_verdicts=per_link_verdicts),
            "list_screening": list_screening_results,
            "is_sanctioned": is_sanctioned,
            "regulatory_findings": _serialize_single_report(regulatory_report, link_verdicts=per_link_verdicts),
            "investigator_brief": llm_verdict,
            "summary": _build_summary(all_reports, name_co_results),
            "highlight_keywords": highlight_keywords,
            "link_verdicts": per_link_verdicts,
            "needs_analyst_review": extraction_buckets["needs_analyst_review"],
            "not_attempted": extraction_buckets["not_attempted"],
        }

        job["status"] = "completed"
        job["progress"] = 100
        job["current_step"] = "Done"
        job["result"] = result
        job["html_report_path"] = html_report_path

        model_version_id = _ensure_model_version()
        findings_persisted = _persist_findings_and_excerpts(
            job_id, all_reports, name_co_results, regulatory_report,
        )
        link_verdicts_persisted = _persist_link_verdicts(job_id, per_link_verdicts)

        audit_logger.log_job_completed({
            "is_sanctioned": bool(is_sanctioned),
            "report_count": len(all_reports),
            "name_co_count": len(name_co_results or []),
            "list_match_count": len(list_screening_results or []),
            "recommendation": (llm_verdict or {}).get("recommendation"),
            "has_html_report": bool(html_report_path),
            "findings_persisted": findings_persisted,
            "link_verdicts_persisted": link_verdicts_persisted,
        })
        _db_update_job(
            job_id,
            status="completed",
            progress=100.0,
            current_step="Done",
            completed_at=datetime.utcnow(),
            html_report_path=html_report_path,
            result_json=json.dumps(result),
            model_version_id=model_version_id,
        )
        _mirror_audit_to_db(job_id)

    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        job["status"] = "failed"
        job["error"] = str(exc)
        job["current_step"] = f"Error: {exc}"
        try:
            audit_logger.log_job_failed(str(exc))
        except Exception:
            logger.exception("Failed to write job_failed audit event for %s", job_id)
        _db_update_job(
            job_id,
            status="failed",
            current_step=f"Error: {exc}",
            error=str(exc),
            completed_at=datetime.utcnow(),
        )
        _mirror_audit_to_db(job_id)
    finally:
        # Release the per-job extraction cache regardless of outcome so long-
        # running dev servers don't accumulate HTML bytes across jobs.
        try:
            engine.reset_extraction_cache()
        except Exception:
            pass
        try:
            duration = time.time() - job_started_at
            logger.info(
                "=== Job %s finished — status=%s duration=%.1fs ===",
                job_id, job.get("status", "unknown"), duration,
            )
        except Exception:
            pass
        _detach_job_log_handler(log_handler)


def _build_extraction_buckets(
    all_reports: List[dict],
    name_co_results: List[dict],
    ncs_excluded_urls: List[dict],
    ncs_failed_urls: List[dict],
) -> Dict[str, List[dict]]:
    """Aggregate the per-job "needs analyst review" and "not attempted" lists.

    ``needs_analyst_review`` — URLs where analysts should manually verify the
    page content because the tool either (a) couldn't extract the page at all
    (ERROR / DOCUMENT / PDF-failed) or (b) only got the 160-char Google
    snippet to analyse (SNIPPET_FALLBACK). Deduped by URL; if the same URL
    surfaced in multiple country searches, ``countries`` is a sorted list.

    ``not_attempted`` — URLs from excluded domains (Wikipedia, LinkedIn, etc.)
    that the tool deliberately didn't fetch. Kept as a separate smaller
    bucket so the primary review list stays focused.
    """
    review_map: Dict[str, dict] = {}

    def _add_review(url: str, country: Optional[str], source: str,
                    extraction_type: str, extraction_message: str,
                    title: str, snippet: str) -> None:
        if not url:
            return
        entry = review_map.get(url)
        if entry is None:
            review_map[url] = {
                "url": url,
                "countries": [country] if country else [],
                "sources": [source],
                "extraction_type": extraction_type,
                "extraction_message": extraction_message or "",
                "title": title or "",
                "snippet": snippet or "",
            }
            return
        if country and country not in entry["countries"]:
            entry["countries"].append(country)
        if source and source not in entry["sources"]:
            entry["sources"].append(source)
        # Preserve the most informative title/snippet if missing.
        if not entry["title"] and title:
            entry["title"] = title
        if not entry["snippet"] and snippet:
            entry["snippet"] = snippet

    # Country-search failures / degraded extractions live in the analysed
    # results. risk_level=="UNKNOWN" means no content reached the analyser;
    # extraction_type=="SNIPPET_FALLBACK" means we only had the Google snippet.
    for report in all_reports or []:
        country = report.get("country")
        for ar in report.get("analyzed_results", []) or []:
            etype = (ar.get("extraction_type") or "").upper()
            rlevel = (ar.get("risk_level") or "").upper()
            needs_review = rlevel == "UNKNOWN" or etype == "SNIPPET_FALLBACK"
            if not needs_review:
                continue
            _add_review(
                url=ar.get("url", ""),
                country=country,
                source="COUNTRY",
                extraction_type=etype or "UNKNOWN",
                extraction_message=ar.get("extraction_message") or "",
                title=ar.get("original_title") or "",
                snippet=ar.get("original_snippet") or "",
            )

    # Name-co results that ran through analysis but came from SNIPPET_FALLBACK
    # or ended UNKNOWN.
    for r in name_co_results or []:
        etype = (r.get("extraction_type") or "").upper()
        rlevel = (r.get("risk_level") or "").upper()
        needs_review = rlevel == "UNKNOWN" or etype == "SNIPPET_FALLBACK"
        if not needs_review:
            continue
        _add_review(
            url=r.get("url", ""),
            country=r.get("country"),
            source="NAME",
            extraction_type=etype or "UNKNOWN",
            extraction_message=r.get("extraction_message") or "",
            title=r.get("original_title") or "",
            snippet=r.get("original_snippet") or "",
        )

    # Name-co URLs where extraction failed AND no snippet fallback was
    # possible (so they never reached the analyser). Tracked explicitly on
    # the NameCooccurrenceSearcher instance.
    for f in ncs_failed_urls or []:
        _add_review(
            url=f.get("url", ""),
            country=f.get("country"),
            source=f.get("source") or "NAME",
            extraction_type=(f.get("extraction_type") or "ERROR").upper(),
            extraction_message=f.get("extraction_message") or "",
            title=f.get("title") or "",
            snippet=f.get("snippet") or "",
        )

    # Sort country lists deterministically for stable UI ordering.
    review_list: List[dict] = []
    for entry in review_map.values():
        entry["countries"] = sorted(set(c for c in entry["countries"] if c))
        entry["sources"] = sorted(set(entry["sources"]))
        review_list.append(entry)
    review_list.sort(key=lambda e: (-len(e["countries"]), e["url"]))

    # Not-attempted: excluded domains from country searches + name-co.
    excluded_map: Dict[str, dict] = {}
    for report in all_reports or []:
        country = report.get("country")
        for ex in report.get("excluded_urls", []) or []:
            url = ex.get("url", "")
            if not url:
                continue
            entry = excluded_map.get(url)
            if entry is None:
                excluded_map[url] = {
                    "url": url,
                    "domain": ex.get("domain") or "",
                    "title": ex.get("title") or "",
                    "snippet": ex.get("snippet") or "",
                    "countries": [country] if country else [],
                    "sources": ["COUNTRY"],
                }
            else:
                if country and country not in entry["countries"]:
                    entry["countries"].append(country)

    for ex in ncs_excluded_urls or []:
        url = ex.get("url", "")
        if not url:
            continue
        entry = excluded_map.get(url)
        if entry is None:
            excluded_map[url] = {
                "url": url,
                "domain": ex.get("domain") or "",
                "title": ex.get("title") or "",
                "snippet": ex.get("snippet") or "",
                "countries": [],
                "sources": ["NAME"],
            }
        else:
            if "NAME" not in entry["sources"]:
                entry["sources"].append("NAME")

    excluded_list = list(excluded_map.values())
    for e in excluded_list:
        e["countries"] = sorted(set(c for c in e["countries"] if c))
        e["sources"] = sorted(set(e["sources"]))
    excluded_list.sort(key=lambda e: (e["domain"], e["url"]))

    return {
        "needs_analyst_review": review_list,
        "not_attempted": excluded_list,
    }


def _serialize_reports(reports: List[dict], link_verdicts: Optional[Dict[str, dict]] = None) -> List[dict]:
    out = []
    for r in reports:
        analyzed = []
        for ar in r.get("analyzed_results", []):
            # Include findings with sentence-level data
            findings = []
            for f in ar.get("findings", []):
                if f.get("relevant", True):
                    findings.append({
                        "risk_type": f.get("risk_type", "GENERAL"),
                        "risk_score": f.get("risk_score", 0),
                        "confidence": f.get("confidence", 0),
                        "sentence": f.get("sentence", ""),
                        "note": f.get("note"),
                    })

            url = ar.get("url", "")
            raw_excerpts = []
            for ex in ar.get("relevant_excerpts", [])[:5]:
                row = {
                    "text": ex.get("text", ""),
                    "trigger_sentence": ex.get("trigger_sentence", "") or "",
                    "risk_type": ex.get("risk_type", "GENERAL"),
                    "confidence": ex.get("confidence", 0),
                    "risk_score": ex.get("risk_score", 0),
                    "note": ex.get("note"),
                }
                # IDs were stamped by engine.assign_stable_ids before the brief
                # ran. Pass them through verbatim so the UI's citation popover
                # resolves against the exact IDs the LLM cited.
                if ex.get("excerpt_id"):
                    row["excerpt_id"] = ex["excerpt_id"]
                if ex.get("source_id"):
                    row["source_id"] = ex["source_id"]
                raw_excerpts.append(row)

            # Debug logging
            if raw_excerpts:
                logger.info(
                    "URL %s: %d excerpts, %d findings",
                    url[:60], len(raw_excerpts), len(findings),
                )
                for i, ex in enumerate(raw_excerpts):
                    text_preview = (ex.get("text") or "")[:100]
                    logger.info("  excerpt[%d]: text=%r...", i, text_preview)
            elif findings:
                logger.info(
                    "URL %s: 0 excerpts but %d findings (sentences only)",
                    url[:60], len(findings),
                )

            url_h = storage.url_hash(url) if _STORAGE_AVAILABLE else ""
            link_verdict = (link_verdicts or {}).get(url_h)

            analyzed.append({
                "url": url,
                "url_hash": url_h,
                "risk_level": ar.get("risk_level", "UNKNOWN"),
                "confidence": ar.get("confidence", 0),
                "original_title": ar.get("original_title", ""),
                "original_snippet": ar.get("original_snippet", ""),
                "extracted_content": ar.get("extracted_content", ""),
                "relevant_excerpts": raw_excerpts,
                "findings": findings,
                "extraction_type": ar.get("extraction_type", "HTML"),
                "extraction_message": ar.get("extraction_message", ""),
                "language": ar.get("language"),
                "source": ar.get("source", "SITE"),
                "matched_name_type": ar.get("matched_name_type", "NONE"),
                "matched_names": ar.get("matched_names", []),
                "link_verdict": link_verdict,
            })
        out.append({
            "country": r["country"],
            "total_search_results": len(r.get("search_results", [])),
            "total_urls_analyzed": r.get("total_urls_analyzed", 0),
            "analyzed_results": analyzed,
        })
    return out


def _serialize_single_report(report: Optional[dict], link_verdicts: Optional[Dict[str, dict]] = None) -> Optional[dict]:
    """Serialize one report dict (e.g. regulatory_report) the same way _serialize_reports does."""
    if not report:
        return None
    serialized = _serialize_reports([report], link_verdicts=link_verdicts)
    return serialized[0] if serialized else None


def _serialize_name_co(results: List[dict], link_verdicts: Optional[Dict[str, dict]] = None) -> List[dict]:
    out = []
    for r in results:
        url = r.get("url", "")
        excerpts = []
        for ex in r.get("relevant_excerpts", [])[:5]:
            row = {
                "text": ex.get("text", ""),
                "trigger_sentence": ex.get("trigger_sentence", "") or "",
                "risk_type": ex.get("risk_type", "GENERAL"),
                "confidence": ex.get("confidence", 0),
                "risk_score": ex.get("risk_score", 0),
                "note": ex.get("note"),
            }
            # IDs are stamped once by engine.assign_stable_ids; see _serialize_reports.
            if ex.get("excerpt_id"):
                row["excerpt_id"] = ex["excerpt_id"]
            if ex.get("source_id"):
                row["source_id"] = ex["source_id"]
            excerpts.append(row)
        url_h = storage.url_hash(url) if _STORAGE_AVAILABLE else ""
        link_verdict = (link_verdicts or {}).get(url_h)
        out.append({
            "url": url,
            "url_hash": url_h,
            "country": r.get("country", ""),
            "risk_level": r.get("risk_level", "UNKNOWN"),
            "confidence": r.get("confidence", 0),
            "original_title": r.get("original_title", ""),
            "original_snippet": r.get("original_snippet", ""),
            "extracted_content": r.get("extracted_content", ""),
            "relevant_excerpts": excerpts,
            "language": r.get("language"),
            "matched_name_type": r.get("matched_name_type", "NONE"),
            "matched_names": r.get("matched_names", []),
            "source": "NAME",
            "link_verdict": link_verdict,
        })
    return out


def _build_summary(reports: List[dict], name_co: List[dict]) -> dict:
    high = medium = low = minimal = total = 0
    country_breakdown = {}
    for r in reports:
        country = r["country"]
        ch = cm = cl = cmin = 0
        for ar in r.get("analyzed_results", []):
            level = ar.get("risk_level", "")
            if level == "HIGH":
                high += 1; ch += 1
            elif level == "MEDIUM":
                medium += 1; cm += 1
            elif level == "LOW":
                low += 1; cl += 1
            elif level == "MINIMAL":
                minimal += 1; cmin += 1
        total += r.get("total_urls_analyzed", 0)
        country_breakdown[country] = {
            "search_results": len(r.get("search_results", [])),
            "analyzed": r.get("total_urls_analyzed", 0),
            "high": ch, "medium": cm, "low": cl, "minimal": cmin,
        }

    return {
        "total_high": high,
        "total_medium": medium,
        "total_low": low,
        "total_minimal": minimal,
        "total_analyzed": total,
        "name_co_count": len(name_co),
        "country_breakdown": country_breakdown,
    }


def _build_highlight_keywords(reports: List[dict], name_co: List[dict]) -> dict:
    """Build keyword lists for frontend highlighting."""
    countries = set()
    country_variations = []

    for r in reports:
        c = r.get("country", "")
        if c:
            countries.add(c)
            try:
                analyzer = engine.get_analyzer(c)
                country_variations.extend(analyzer.country_variations)
            except Exception:
                country_variations.append(c.lower())

    for r in name_co:
        c = r.get("country", "")
        if c:
            countries.add(c)

    # Sanctions/compliance keywords
    sanctions_keywords = [
        "OFAC", "SDN", "sanctions", "sanctioned", "blocked", "prohibited",
        "embargo", "restricted", "comply", "compliance", "restriction",
        "forbidden", "banned",
    ]

    # Financial indicators
    financial_keywords = [
        "fund", "funding", "grant", "donation", "payment", "transfer",
        "contribute", "contribution", "support", "financial", "monetary",
        "invest", "investment",
    ]

    return {
        "countries": sorted(list(countries)),
        "country_variations": sorted(list(set(country_variations))),
        "sanctions_keywords": sanctions_keywords,
        "financial_keywords": financial_keywords,
    }


# ---------------------------------------------------------------------------
# Auth endpoints (Phase 3)
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: str
    password: str
    role: str = "analyst"
    display_name: str = ""


class UserOut(BaseModel):
    id: int
    email: str
    role: str
    display_name: Optional[str] = None


def _require_auth_stack():
    if not _STORAGE_AVAILABLE or not auth_mod.AUTH_AVAILABLE:
        raise HTTPException(
            503,
            "Auth is not configured — install pyjwt + passlib[bcrypt] and ensure SQLAlchemy is available.",
        )


if not auth_mod.AUTH_AVAILABLE:
    @app.post("/api/auth/login")
    async def login():
        """Stub registered only when auth deps are unavailable."""
        raise HTTPException(
            501,
            "Auth deps not installed — install pyjwt + passlib[bcrypt] + python-multipart",
        )


if auth_mod.AUTH_AVAILABLE:
    from fastapi import Depends
    from fastapi.security import OAuth2PasswordRequestForm

    @app.post("/api/auth/login")
    async def login(form: OAuth2PasswordRequestForm = Depends()):
        _require_auth_stack()
        email = form.username.strip().lower()
        try:
            from sqlalchemy import select
            with storage.get_session() as session:
                user = session.execute(
                    select(storage.User).where(storage.User.email == email)
                ).scalar_one_or_none()
                if user is None or user.disabled or not auth_mod.verify_password(form.password, user.hashed_password):
                    raise HTTPException(401, "Invalid email or password")
                user.last_login_at = datetime.utcnow()
                session.commit()
                token = auth_mod.create_access_token(subject=user.email, role=user.role)
                return {"access_token": token, "token_type": "bearer", "role": user.role, "email": user.email}
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Login failed: %s", exc)
            raise HTTPException(500, "Login error")

    @app.get("/api/auth/me", response_model=UserOut)
    async def get_me(user: dict = Depends(auth_mod.get_current_user_dep())):
        return UserOut(
            id=user["id"], email=user["email"], role=user["role"], display_name=user.get("display_name"),
        )

    @app.post("/api/auth/register", response_model=UserOut)
    async def register_user(
        req: RegisterRequest,
        actor: dict = Depends(auth_mod.require_role("admin")),
    ):
        _require_auth_stack()
        if req.role not in storage.USER_ROLES:
            raise HTTPException(400, f"role must be one of {storage.USER_ROLES}")
        email = req.email.strip().lower()
        if not email or not req.password:
            raise HTTPException(400, "email and password required")

        from sqlalchemy import select
        with storage.get_session() as session:
            existing = session.execute(
                select(storage.User).where(storage.User.email == email)
            ).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(409, "User already exists")
            user = storage.User(
                email=email,
                hashed_password=auth_mod.hash_password(req.password),
                role=req.role,
                display_name=req.display_name or None,
                disabled=False,
                created_at=datetime.utcnow(),
            )
            session.add(user)
            session.commit()
            return UserOut(id=user.id, email=user.email, role=user.role, display_name=user.display_name)


# ---------------------------------------------------------------------------
# HITL workflow endpoints (Phase 3)
# ---------------------------------------------------------------------------

class FindingStateTransitionRequest(BaseModel):
    to_status: str
    reason: Optional[str] = None
    assign_to_email: Optional[str] = None


class FpOverrideRequest(BaseModel):
    reason: str
    notes_md: Optional[str] = None


class SignOffRequest(BaseModel):
    final_disposition_notes: str


class ReopenRequest(BaseModel):
    reason: str


def _log_analyst_action(job_id: str, actor_email: str, target: str, action: str,
                        from_state: Optional[str] = None, to_state: Optional[str] = None,
                        reason: Optional[str] = None) -> None:
    """Best-effort: append an analyst_action audit event + DB mirror row."""
    try:
        logger_obj = audit_mod.AuditLogger(job_id)
        logger_obj.log_analyst_action(
            actor=actor_email, target=target, action=action,
            from_state=from_state, to_state=to_state, reason=reason,
        )
        _mirror_audit_to_db(job_id)
    except Exception as exc:
        logger.warning("analyst_action audit write failed (job=%s, action=%s): %s", job_id, action, exc)


if auth_mod.AUTH_AVAILABLE:

    @app.post("/api/findings/{finding_id}/state")
    async def transition_finding_state(
        finding_id: int,
        req: FindingStateTransitionRequest,
        actor: dict = Depends(auth_mod.require_role("analyst", "reviewer", "admin")),
    ):
        _require_auth_stack()
        if req.to_status not in storage.FINDING_STATUS_VALUES:
            raise HTTPException(400, f"to_status must be one of {storage.FINDING_STATUS_VALUES}")

        from sqlalchemy import select
        with storage.get_session() as session:
            finding = session.get(storage.Finding, finding_id)
            if finding is None:
                raise HTTPException(404, "Finding not found")
            state = session.execute(
                select(storage.FindingState).where(storage.FindingState.finding_id == finding_id)
            ).scalar_one_or_none()
            if state is None:
                state = storage.FindingState(finding_id=finding_id, status="pending", updated_at=datetime.utcnow())
                session.add(state)
                session.flush()

            prev = state.status
            # Only reviewers+admins can mark confirmed_match / escalated.
            if req.to_status in ("confirmed_match", "escalated") and actor["role"] not in ("reviewer", "admin"):
                raise HTTPException(403, f"Only reviewers/admins can transition to {req.to_status}")

            state.status = req.to_status
            state.updated_at = datetime.utcnow()
            state.updated_by = actor["email"]
            if req.assign_to_email:
                state.assigned_analyst_id = req.assign_to_email.strip().lower()

            session.add(storage.FindingStatusHistory(
                finding_id=finding_id,
                from_status=prev,
                to_status=req.to_status,
                changed_at=datetime.utcnow(),
                changed_by=actor["email"],
                reason=req.reason,
            ))
            job_id = finding.job_id
            session.commit()

        _log_analyst_action(
            job_id, actor["email"], f"finding:{finding_id}", "state_transition",
            from_state=prev, to_state=req.to_status, reason=req.reason,
        )
        return {"finding_id": finding_id, "from_status": prev, "to_status": req.to_status}

    @app.post("/api/findings/{finding_id}/fp-override")
    async def mark_false_positive(
        finding_id: int,
        req: FpOverrideRequest,
        actor: dict = Depends(auth_mod.require_role("analyst", "reviewer", "admin")),
    ):
        _require_auth_stack()
        from sqlalchemy import select
        with storage.get_session() as session:
            finding = session.get(storage.Finding, finding_id)
            if finding is None:
                raise HTTPException(404, "Finding not found")
            state = session.execute(
                select(storage.FindingState).where(storage.FindingState.finding_id == finding_id)
            ).scalar_one_or_none()
            if state is None:
                state = storage.FindingState(finding_id=finding_id, updated_at=datetime.utcnow())
                session.add(state)
                session.flush()

            prev = state.status
            state.fp_override = True
            state.status = "cleared_fp"
            state.updated_at = datetime.utcnow()
            state.updated_by = actor["email"]
            if req.notes_md:
                state.notes_md = req.notes_md

            session.add(storage.FindingStatusHistory(
                finding_id=finding_id,
                from_status=prev,
                to_status="cleared_fp",
                changed_at=datetime.utcnow(),
                changed_by=actor["email"],
                reason=req.reason,
            ))
            job_id = finding.job_id
            session.commit()

        _log_analyst_action(
            job_id, actor["email"], f"finding:{finding_id}", "fp_override",
            from_state=prev, to_state="cleared_fp", reason=req.reason,
        )
        return {"finding_id": finding_id, "status": "cleared_fp"}

    @app.post("/api/jobs/{job_id}/sign-off")
    async def sign_off_job(
        job_id: str,
        req: SignOffRequest,
        actor: dict = Depends(auth_mod.require_role("reviewer", "admin")),
    ):
        _require_auth_stack()
        from sqlalchemy import select
        with storage.get_session() as session:
            job = session.get(storage.Job, job_id)
            if job is None:
                raise HTTPException(404, "Job not found")
            state = session.execute(
                select(storage.JobState).where(storage.JobState.job_id == job_id)
            ).scalar_one_or_none()
            if state is None:
                state = storage.JobState(job_id=job_id, workflow_status="draft", updated_at=datetime.utcnow())
                session.add(state)
                session.flush()

            if state.workflow_status == "signed_off":
                raise HTTPException(409, "Job is already signed off")

            # Block sign-off when there are still pending or in_review findings —
            # the whole point of the reviewer gate is that every signal has
            # been dispositioned.
            still_open = session.execute(
                select(storage.FindingState).where(
                    storage.FindingState.finding_id.in_(
                        select(storage.Finding.id).where(storage.Finding.job_id == job_id)
                    ),
                    storage.FindingState.status.in_(("pending", "in_review")),
                )
            ).scalars().all()
            if still_open:
                raise HTTPException(
                    409,
                    f"{len(still_open)} findings still in pending/in_review. Disposition them before sign-off.",
                )

            prev = state.workflow_status
            state.workflow_status = "signed_off"
            state.signed_off_by = actor["email"]
            state.signed_off_at = datetime.utcnow()
            state.final_disposition_notes = req.final_disposition_notes
            state.updated_at = datetime.utcnow()

            session.add(storage.JobStatusHistory(
                job_id=job_id, from_status=prev, to_status="signed_off",
                changed_at=datetime.utcnow(), changed_by=actor["email"],
                reason=req.final_disposition_notes,
            ))
            session.commit()

        _log_analyst_action(
            job_id, actor["email"], f"job:{job_id}", "sign_off",
            from_state=prev, to_state="signed_off", reason=req.final_disposition_notes,
        )
        return {"job_id": job_id, "workflow_status": "signed_off"}

    @app.post("/api/jobs/{job_id}/reopen")
    async def reopen_job(
        job_id: str,
        req: ReopenRequest,
        actor: dict = Depends(auth_mod.require_role("admin")),
    ):
        _require_auth_stack()
        from sqlalchemy import select
        with storage.get_session() as session:
            state = session.execute(
                select(storage.JobState).where(storage.JobState.job_id == job_id)
            ).scalar_one_or_none()
            if state is None:
                raise HTTPException(404, "Job state not found")
            if state.workflow_status != "signed_off":
                raise HTTPException(409, "Only signed-off jobs can be reopened")

            prev = state.workflow_status
            state.workflow_status = "reopened"
            state.updated_at = datetime.utcnow()
            session.add(storage.JobStatusHistory(
                job_id=job_id, from_status=prev, to_status="reopened",
                changed_at=datetime.utcnow(), changed_by=actor["email"],
                reason=req.reason,
            ))
            session.commit()

        _log_analyst_action(
            job_id, actor["email"], f"job:{job_id}", "reopen",
            from_state=prev, to_state="reopened", reason=req.reason,
        )
        return {"job_id": job_id, "workflow_status": "reopened"}

    @app.get("/api/jobs/{job_id}/findings")
    async def list_job_findings(
        job_id: str,
        actor: dict = Depends(auth_mod.require_role("analyst", "reviewer", "admin")),
    ):
        """Return every Finding row for a job with its HITL state — drives
        per-row disposition controls in the results table."""
        _require_auth_stack()
        from sqlalchemy import select
        with storage.get_session() as session:
            rows = session.execute(
                select(storage.Finding, storage.FindingState)
                .join(storage.FindingState, storage.FindingState.finding_id == storage.Finding.id, isouter=True)
                .where(storage.Finding.job_id == job_id)
                .order_by(storage.Finding.id)
            ).all()
            return {
                "job_id": job_id,
                "findings": [
                    {
                        "finding_id": f.id,
                        "country": f.country,
                        "url": f.url,
                        "risk_type": f.risk_type,
                        "risk_score": f.risk_score,
                        "confidence": f.confidence,
                        "sentence": f.sentence,
                        "state": {
                            "status": s.status if s else "pending",
                            "fp_override": bool(s.fp_override) if s else False,
                            "assigned_analyst_id": s.assigned_analyst_id if s else None,
                            "notes_md": s.notes_md if s else None,
                            "updated_by": s.updated_by if s else None,
                        } if s else {"status": "pending", "fp_override": False},
                    }
                    for (f, s) in rows
                ],
            }

    @app.get("/api/jobs/{job_id}/hitl-overview")
    async def job_hitl_overview(
        job_id: str,
        actor: dict = Depends(auth_mod.require_role("analyst", "reviewer", "admin")),
    ):
        """Aggregated workflow state for the CaseHeader strip."""
        _require_auth_stack()
        from sqlalchemy import select, func
        with storage.get_session() as session:
            state = session.execute(
                select(storage.JobState).where(storage.JobState.job_id == job_id)
            ).scalar_one_or_none()

            counts = {v: 0 for v in storage.FINDING_STATUS_VALUES}
            rows = session.execute(
                select(storage.FindingState.status, func.count(storage.FindingState.finding_id))
                .join(storage.Finding, storage.Finding.id == storage.FindingState.finding_id)
                .where(storage.Finding.job_id == job_id)
                .group_by(storage.FindingState.status)
            ).all()
            for status_val, count in rows:
                counts[status_val] = int(count)

            return {
                "job_id": job_id,
                "workflow_status": state.workflow_status if state else "draft",
                "signed_off_by": state.signed_off_by if state else None,
                "signed_off_at": state.signed_off_at.isoformat() if state and state.signed_off_at else None,
                "final_disposition_notes": state.final_disposition_notes if state else None,
                "counts": counts,
                "total_findings": sum(counts.values()),
            }

    @app.get("/api/jobs/{job_id}/evidence-packet.zip")
    async def download_evidence_packet(
        job_id: str,
        actor: dict = Depends(auth_mod.require_role("reviewer", "admin")),
    ):
        """Returns the full evidence ZIP. Logs the download as an analyst
        action so the audit trail records who pulled the bundle."""
        _require_auth_stack()
        import evidence_packet as ep_mod
        data = ep_mod.build_evidence_zip(job_id)
        if data is None:
            raise HTTPException(503, "Evidence packet unavailable — storage not configured")

        _log_analyst_action(
            job_id, actor["email"], f"job:{job_id}", "evidence_packet_downloaded",
            reason=f"size_bytes={len(data)}",
        )

        from fastapi.responses import Response
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="evidence-{job_id}.zip"'},
        )

    class LinkVerdictAnalystRequest(BaseModel):
        agrees: bool
        reason: Optional[str] = None

    class CaseSummaryRequest(BaseModel):
        summary: str

    class CaseVerdictRequest(BaseModel):
        agrees: bool
        reason: Optional[str] = None

    def _require_disagree_reason(reason: Optional[str]) -> str:
        """Shared validation: disagree must carry a ≥10-char reason."""
        trimmed = (reason or "").strip()
        if len(trimmed) < 10:
            raise HTTPException(
                400,
                "A reason of at least 10 characters is required when disagreeing.",
            )
        return trimmed

    @app.post("/api/jobs/{job_id}/links/{url_hash}/verdict")
    async def analyst_link_verdict(
        job_id: str,
        url_hash: str,
        req: LinkVerdictAnalystRequest,
        actor: dict = Depends(auth_mod.require_role("analyst", "reviewer", "admin")),
    ):
        """Record the analyst's agree/disagree vote on a per-link LLM verdict.
        Disagree requires a reason; agree clears any prior reason."""
        _require_auth_stack()
        reason_to_store: Optional[str] = None
        if not req.agrees:
            reason_to_store = _require_disagree_reason(req.reason)

        from sqlalchemy import select
        with storage.get_session() as session:
            row = session.execute(
                select(storage.LinkVerdict).where(
                    storage.LinkVerdict.job_id == job_id,
                    storage.LinkVerdict.url_hash == url_hash,
                )
            ).scalar_one_or_none()
            if row is None:
                raise HTTPException(404, "LinkVerdict not found for this job/url")

            prev_state = "agree" if row.analyst_agrees else ("disagree" if row.analyst_agrees is False else "unset")
            new_state = "agree" if req.agrees else "disagree"

            row.analyst_agrees = bool(req.agrees)
            row.analyst_disagree_reason = reason_to_store
            row.analyst_updated_by = actor["email"]
            row.analyst_updated_at = datetime.utcnow()
            row.updated_at = datetime.utcnow()
            session.commit()

        _log_analyst_action(
            job_id, actor["email"], f"link:{url_hash}", "link_verdict",
            from_state=prev_state, to_state=new_state, reason=reason_to_store,
        )
        return {
            "job_id": job_id,
            "url_hash": url_hash,
            "analyst_agrees": req.agrees,
            "analyst_disagree_reason": reason_to_store,
        }

    @app.post("/api/jobs/{job_id}/case-summary")
    async def set_case_summary(
        job_id: str,
        req: CaseSummaryRequest,
        actor: dict = Depends(auth_mod.require_role("analyst", "reviewer", "admin")),
    ):
        """Set the analyst-authored case-wide summary (separate from the
        reviewer's final_disposition_notes used at sign-off)."""
        _require_auth_stack()
        summary = (req.summary or "").strip()
        if not summary:
            raise HTTPException(400, "summary cannot be empty")

        from sqlalchemy import select
        with storage.get_session() as session:
            state = session.execute(
                select(storage.JobState).where(storage.JobState.job_id == job_id)
            ).scalar_one_or_none()
            if state is None:
                state = storage.JobState(job_id=job_id, workflow_status="draft")
                session.add(state)
                session.flush()

            state.analyst_case_summary = summary
            state.analyst_case_updated_by = actor["email"]
            state.analyst_case_updated_at = datetime.utcnow()
            state.updated_at = datetime.utcnow()
            session.commit()

        _log_analyst_action(
            job_id, actor["email"], f"job:{job_id}", "case_summary_updated",
            reason=f"len={len(summary)}",
        )
        return {"job_id": job_id, "analyst_case_summary": summary}

    @app.post("/api/jobs/{job_id}/case-verdict")
    async def analyst_case_verdict(
        job_id: str,
        req: CaseVerdictRequest,
        actor: dict = Depends(auth_mod.require_role("analyst", "reviewer", "admin")),
    ):
        """Agree/disagree with the aggregate investigator brief.
        Disagree requires a reason ≥10 chars."""
        _require_auth_stack()
        reason_to_store: Optional[str] = None
        if not req.agrees:
            reason_to_store = _require_disagree_reason(req.reason)

        from sqlalchemy import select
        with storage.get_session() as session:
            state = session.execute(
                select(storage.JobState).where(storage.JobState.job_id == job_id)
            ).scalar_one_or_none()
            if state is None:
                state = storage.JobState(job_id=job_id, workflow_status="draft")
                session.add(state)
                session.flush()

            prev_state = (
                "agree" if state.analyst_agrees_with_brief
                else ("disagree" if state.analyst_agrees_with_brief is False else "unset")
            )
            new_state = "agree" if req.agrees else "disagree"

            state.analyst_agrees_with_brief = bool(req.agrees)
            state.analyst_case_disagree_reason = reason_to_store
            state.analyst_case_updated_by = actor["email"]
            state.analyst_case_updated_at = datetime.utcnow()
            state.updated_at = datetime.utcnow()
            session.commit()

        _log_analyst_action(
            job_id, actor["email"], f"job:{job_id}", "case_verdict",
            from_state=prev_state, to_state=new_state, reason=reason_to_store,
        )
        return {
            "job_id": job_id,
            "analyst_agrees_with_brief": req.agrees,
            "analyst_case_disagree_reason": reason_to_store,
        }

    @app.get("/api/jobs/{job_id}/link-verdicts")
    async def list_link_verdicts(
        job_id: str,
        actor: dict = Depends(auth_mod.require_role("analyst", "reviewer", "admin")),
    ):
        """Return every LinkVerdict row for a job. Used by the UI to refresh
        per-row concern badges without re-fetching the whole result payload."""
        _require_auth_stack()
        return {"job_id": job_id, "link_verdicts": _load_link_verdicts(job_id)}

    @app.get("/api/analysts/me/queue")
    async def my_analyst_queue(
        limit: int = 50,
        offset: int = 0,
        actor: dict = Depends(auth_mod.require_role("analyst", "reviewer", "admin")),
    ):
        """Paginated queue of findings assigned to me plus anything still
        pending (so analysts can pull from a shared backlog)."""
        _require_auth_stack()
        from sqlalchemy import select, or_
        limit = max(1, min(limit, 200))
        with storage.get_session() as session:
            stmt = (
                select(storage.Finding, storage.FindingState)
                .join(storage.FindingState, storage.FindingState.finding_id == storage.Finding.id)
                .where(
                    or_(
                        storage.FindingState.assigned_analyst_id == actor["email"],
                        storage.FindingState.status == "pending",
                    )
                )
                .order_by(storage.Finding.risk_score.desc(), storage.Finding.id.asc())
                .limit(limit).offset(offset)
            )
            rows = session.execute(stmt).all()
            return {
                "count": len(rows),
                "offset": offset,
                "items": [
                    {
                        "finding_id": f.id,
                        "job_id": f.job_id,
                        "country": f.country,
                        "url": f.url,
                        "risk_type": f.risk_type,
                        "risk_score": f.risk_score,
                        "confidence": f.confidence,
                        "sentence": f.sentence,
                        "status": s.status,
                        "assigned_to": s.assigned_analyst_id,
                    }
                    for (f, s) in rows
                ],
            }


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
@app.get("/api/countries")
async def list_countries():
    """Expose the built-in country list so the frontend doesn't duplicate it."""
    return {
        "builtins": list(BUILTIN_COUNTRIES),
        "default_selected": list(BUILTIN_COUNTRIES),
    }


@app.post("/api/analyze")
async def start_analysis(req: AnalyzeRequest, background_tasks: BackgroundTasks):
    if not req.website.strip() and not req.business_name.strip() and not req.legal_name.strip():
        raise HTTPException(400, "At least one of website, business name, or legal name is required")

    if not req.effective_entities():
        raise HTTPException(400, "At least one country (built-in or custom) is required")

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "pending",
        "progress": 0,
        "current_step": "Queued",
        "result": None,
        "error": None,
        "partial": {},
    }
    _db_create_job(job_id, req)

    background_tasks.add_task(_run_analysis, job_id, req)
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}/audit-chain")
async def verify_audit_chain(job_id: str):
    """Re-hash the JSONL audit log for this job and return chain status.
    Regulator-runnable integrity check.
    """
    return audit_mod.verify_chain(job_id)


@app.get("/api/jobs/{job_id}/audit-events")
async def list_audit_events(job_id: str):
    """Return the raw (append-only) audit events for a job."""
    return {"job_id": job_id, "events": list(audit_mod.read_events(job_id))}


def _load_job_from_db(job_id: str) -> Optional[dict]:
    """Fallback to DB when job isn't in the in-memory cache (e.g. post-restart)."""
    if not _STORAGE_AVAILABLE:
        return None
    try:
        with storage.get_session() as session:
            row = session.get(storage.Job, job_id)
            if row is None:
                return None
            out = {
                "status": row.status,
                "progress": row.progress,
                "current_step": row.current_step,
                "error": row.error,
                "result": json.loads(row.result_json) if row.result_json else None,
                "html_report_path": row.html_report_path,
                "partial": {},
            }
            return out
    except Exception as exc:
        logger.warning("DB job lookup failed for %s: %s", job_id, exc)
        return None


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    job = jobs.get(job_id) or _load_job_from_db(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job["progress"],
        "current_step": job["current_step"],
        "error": job.get("error"),
    }


@app.get("/api/result/{job_id}")
async def get_result(job_id: str):
    job = jobs.get(job_id) or _load_job_from_db(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job["status"] == "completed":
        result = job["result"]
        # Hydrate with the latest LinkVerdict rows so any analyst agreement
        # written after initial persistence shows up on refetch. Same for
        # the case-level analyst review fields on JobState.
        try:
            fresh_verdicts = _load_link_verdicts(job_id)
            if fresh_verdicts:
                result = dict(result)
                result["link_verdicts"] = fresh_verdicts
                # Patch nested link_verdict blocks so the UI can read
                # analyst state without stitching two dicts.
                for bucket in result.get("reports", []) or []:
                    for ar in bucket.get("analyzed_results", []) or []:
                        uh = ar.get("url_hash")
                        if uh and uh in fresh_verdicts:
                            ar["link_verdict"] = fresh_verdicts[uh]
                for r in result.get("name_co_results", []) or []:
                    uh = r.get("url_hash")
                    if uh and uh in fresh_verdicts:
                        r["link_verdict"] = fresh_verdicts[uh]
                reg = result.get("regulatory_findings")
                if isinstance(reg, dict):
                    for ar in reg.get("analyzed_results", []) or []:
                        uh = ar.get("url_hash")
                        if uh and uh in fresh_verdicts:
                            ar["link_verdict"] = fresh_verdicts[uh]

            case_review = _load_case_review(job_id)
            if case_review:
                result = dict(result)
                result["case_review"] = case_review
        except Exception as exc:
            logger.warning("Result hydration failed for %s: %s", job_id, exc)
        return result
    if job["status"] == "failed":
        raise HTTPException(500, job.get("error", "Unknown error"))
    # Return partial results if still running
    return {
        "status": job["status"],
        "progress": job["progress"],
        "partial": job.get("partial", {}),
    }


@app.get("/api/stream/{job_id}")
async def stream_progress(job_id: str):
    """SSE endpoint for real-time progress updates."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    async def event_generator():
        while True:
            job = jobs.get(job_id)
            if not job:
                break
            data = json.dumps({
                "status": job["status"],
                "progress": job["progress"],
                "current_step": job["current_step"],
                "error": job.get("error"),
            })
            yield f"data: {data}\n\n"
            if job["status"] in ("completed", "failed"):
                break
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/health")
async def health():
    has_api_key = bool(engine.API_KEY and engine.API_KEY != "placeholder")
    has_cse = bool(engine.SEARCH_ENGINE_ID and engine.SEARCH_ENGINE_ID != "placeholder")
    has_llm = bool(
        (getattr(engine, "USE_VERTEX", False) or engine.GOOGLE_GENAI_API_KEY)
        and engine._google_genai is not None
    )
    screener = _get_screener()
    return {
        "status": "ok",
        "google_api_key_set": has_api_key,
        "google_cse_id_set": has_cse,
        "llm_available": has_llm,
        "llm_backend": "vertex" if getattr(engine, "USE_VERTEX", False) else ("aistudio" if engine.GOOGLE_GENAI_API_KEY else "none"),
        "sanctions_lists": screener.get_stats() if screener else {"total_entities": 0, "lists_loaded": {}},
    }


@app.get("/api/report/{job_id}")
async def download_report(job_id: str):
    """Download the original HTML report generated by the sanctions engine."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if job["status"] != "completed":
        raise HTTPException(400, "Analysis not yet completed")

    report_path = job.get("html_report_path")
    logger.info("Report download requested. Path: %s, exists: %s",
                report_path, os.path.exists(report_path) if report_path else "N/A")

    if not report_path:
        raise HTTPException(404, detail="HTML report was not generated. Check backend logs for errors.")
    if not os.path.exists(report_path):
        raise HTTPException(404, detail=f"Report file not found at: {report_path}")

    filename = os.path.basename(report_path)
    return FileResponse(
        report_path,
        media_type="text/html",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Static files & page serving
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent  # e.g. E:\sanctions-tool when main.py is in backend/
CWD = Path(os.getcwd())

def _find_path(*candidates: Path) -> Optional[Path]:
    """Return the first path that exists, or None."""
    for p in candidates:
        if p.exists():
            return p
    return None

# Log resolved paths at startup for debugging
logger.info("BASE_DIR     = %s", BASE_DIR)
logger.info("PROJECT_ROOT = %s", PROJECT_ROOT)
logger.info("CWD          = %s", CWD)

# --- Static assets (Logo.png etc.) ---
_static = _find_path(
    BASE_DIR / "static",
    PROJECT_ROOT / "static",
    CWD / "static",
)
if _static and _static.is_dir():
    logger.info("Mounting /static from %s", _static)
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")
else:
    logger.warning("No static/ directory found. Checked: %s, %s, %s",
                    BASE_DIR / "static", PROJECT_ROOT / "static", CWD / "static")

# --- Built React app at /app ---
_react_dist = _find_path(
    BASE_DIR / "frontend" / "dist",
    PROJECT_ROOT / "frontend" / "dist",
    CWD / "frontend" / "dist",
    BASE_DIR / "dist",
    PROJECT_ROOT / "dist",
    CWD / "dist",
)
if _react_dist and _react_dist.is_dir():
    logger.info("React dist found at %s", _react_dist)
    _react_assets = _react_dist / "assets"
    if _react_assets.is_dir():
        app.mount("/app/assets", StaticFiles(directory=str(_react_assets)), name="react-assets")
else:
    logger.warning("No React dist/ found. Checked frontend/dist and dist/ under: %s, %s, %s",
                    BASE_DIR, PROJECT_ROOT, CWD)


@app.get("/app/{full_path:path}")
async def serve_react_app(full_path: str = ""):
    """Serve the React SPA for /app and any sub-routes (client-side routing)."""
    react_index = _find_path(
        BASE_DIR / "frontend" / "dist" / "index.html",
        PROJECT_ROOT / "frontend" / "dist" / "index.html",
        CWD / "frontend" / "dist" / "index.html",
        BASE_DIR / "dist" / "index.html",
        PROJECT_ROOT / "dist" / "index.html",
        CWD / "dist" / "index.html",
    )
    if react_index:
        return HTMLResponse(react_index.read_text(encoding="utf-8"))
    raise HTTPException(
        404,
        detail=(
            "React app not built yet. "
            f"Searched frontend/dist/index.html and dist/index.html under: "
            f"{BASE_DIR}, {PROJECT_ROOT}, {CWD}. "
            "Run 'cd frontend && npm run build' first."
        ),
    )


@app.get("/")
async def serve_landing_page():
    """Serve the marketing landing page."""
    landing = _find_path(
        BASE_DIR / "index.html",
        PROJECT_ROOT / "index.html",
        CWD / "index.html",
    )
    if landing:
        logger.info("Serving landing page from %s", landing)
        return HTMLResponse(landing.read_text(encoding="utf-8"))
    raise HTTPException(
        404,
        detail=(
            f"Landing page not found. "
            f"Searched: {BASE_DIR / 'index.html'}, {PROJECT_ROOT / 'index.html'}, {CWD / 'index.html'}. "
            f"Place index.html in {BASE_DIR} or {PROJECT_ROOT}."
        ),
    )


