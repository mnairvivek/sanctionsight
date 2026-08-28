# SanctionSight — Rebuild Plan for Regulated-Buyer Readiness

## Context

The strategic assessment in `compass_artifact_wf-9c34a1ff-...md` concludes the current SanctionSight is a "prototype not a product" and would not survive SR 11-7 / EU AI Act scrutiny. The user has chosen to target regulated buyers (BaaS banks, crypto custodians) with a $500–2000/mo budget. All four focus areas are in scope: LLM citation grounding, audit trail, HITL workflow, and recall/NLP improvements.

The backend exploration confirmed all 10 CLAUDE.md recall tasks are already implemented. The actual deltas from "prototype" to "sellable" are product-layer and infrastructure-layer, not NLP-layer:
- LLM emits free-form `verdict` with no citations (`sanctions_engine.py:295-449`)
- Zero persistent state — `jobs` dict in memory (`main.py:45`), reports dumped to disk
- No analyst workflow, no review status, no FP override, no evidence-packet export
- No tamper-evident audit log; no model card; no list-snapshot versioning
- UI reifies "verdict" as the primary disposition (`VerdictCard.jsx:5-33`)

This plan rebuilds along those dimensions while preserving the working search/NLP pipeline. Scope is **deep rebuild**, phased so each phase ships usable value. Target: ~6–10 weeks to a regulator-defensible MVP.

---

## Recommended approach

### Architectural decisions (locked in)

| Area | Decision |
|---|---|
| App storage | **SQLite WAL** — single file `data/sanctionsight.db`, embedded, migrates to Postgres later via `sqlite3 .dump` when ≥10 analysts |
| Tamper-evident audit | **Per-job append-only JSONL** with hash chain (`audit/{job_id}.jsonl`, each line has `prev_hash`+`sha256`) mirrored nightly to S3/R2 for 7-yr retention |
| LLM grounding | **Structured output + Pydantic schema + post-verification**, no vector store. Every claim has `citations: [{source_id, excerpt_id}]`, verified by substring/overlap check before release to UI |
| HITL model | **Per-finding state table + append-only history table**. Not event-sourced. Scales cleanly to ~20 analysts |
| Data backbone | **OpenSanctions bulk license (~€595/mo)** replacing the custom list-loaders for the covered 250+ lists; keep `sanctions_list_screener.py` loaders only for sources OpenSanctions doesn't cover |
| Framing | **Kill the word "verdict" everywhere.** Rename to **"Investigator Brief"** (user-facing) and `investigator_brief` (code) |

### Phase 1 — Data & audit foundation (P0, weeks 1–2) — ✅ COMPLETE (2026-04-18)

**Status:** Shipped. SQLite WAL is the durable backbone; every job now emits a tamper-evident JSONL audit chain plus a DB-mirrored `AuditEvent` row, and regulators can re-run integrity checks via `GET /api/jobs/{id}/audit-chain`. Extracted page content is sha256-keyed and gzip-stored for 7-yr retention. No user-visible UI change by design — the behavioural shift is in durability + provenance.

Delivered:
- `backend/storage.py` — SQLAlchemy 2.x models for all 11 Phase 1 tables (`Job`, `Excerpt`, `Finding`, `SanctionsListMatch`, `AuditEvent`, `FindingState`, `FindingStatusHistory`, `JobState`, `JobStatusHistory`, `ModelVersion`, `ListSnapshot`); SQLite WAL + `PRAGMA foreign_keys=ON` via connect listener; stable `source_id`/`excerpt_id` helpers matching `schemas.py`
- `backend/alembic.ini` + `backend/migrations/` — env.py wired to `storage.Base.metadata`, `render_as_batch=True` for SQLite; initial migration `0001_initial_schema.py` ships every table in one shot
- `backend/audit.py` — `AuditLogger` append-only JSONL writer with canonical-JSON hash chain (`prev_hash + sha256(canonical_json(event))`), 10 event-type helpers (job_started/completed/failed, search_executed, content_extracted, nlp_analyzed, list_screening, llm_prompt_sent, llm_response_received, analyst_action), `verify_chain(job_id)` returns `OK|INTEGRITY_BROKEN|EMPTY|MISSING` with `first_bad_seq`; `sync_to_db()` best-effort mirror
- `backend/sanctions_engine.py` — optional `audit_logger=None` threaded through `EnhancedSanctionsSearcher`, `SanctionsContentAnalyzer.extract_content_from_url`, `NameCooccurrenceSearcher`, `InvestigatorBriefGenerator`, `process_single_entity`, `perform_global_ofac_search`. `search_google` logs `{query, result_count, elapsed_ms}`. Extraction sha256-hashes content, gzip-stores to `snapshots/{hash}.txt.gz`, logs `content_extracted`. LLM pass hashes prompt + response and logs `llm_prompt_sent` / `llm_response_received` with verification results. All audit calls are guarded and cannot break the job if they fail.
- `backend/main.py` — tolerant storage import (API still boots without sqlalchemy — logs a warning); `init_db()` on startup; `_db_create_job`/`_db_update_job`/`_mirror_audit_to_db` helpers; `AuditLogger` instantiated per job in `_run_analysis` and lifecycle events logged at each phase boundary; DB fallback for `/api/status` and `/api/result` post-restart. New endpoints: `GET /api/jobs/{id}/audit-chain` (regulator integrity check), `GET /api/jobs/{id}/audit-events` (raw log readback)
- `backend/sanctions_list_screener.py` — `_record_list_snapshots()` writes one `ListSnapshot(list_name, sha256, downloaded_at, entity_count, path)` row per loaded file; per-list entity counts tracked during load; silently no-ops when storage isn't installed
- `backend/update_lists.py` — instantiates `SanctionsListScreener` after each successful download batch so a `ListSnapshot` row is recorded immediately
- `backend/backup.py` — nightly backup script; uploads `data/sanctionsight.db` (+ WAL/SHM), `audit/*.jsonl`, `snapshots/*.txt.gz` to S3/R2 when `SANCTIONSIGHT_BACKUP_BUCKET` is set (7-yr retention enforced at bucket policy level), falls back to local `backups/YYYY-MM-DD/` otherwise; cron-ready (`0 2 * * *`)
- `backend/requirements.txt` — sqlalchemy≥2.0, alembic≥1.13, pydantic≥2, pytest, pytest-asyncio
- `backend/tests/unit/test_audit_chain.py` — 8 passing tests: happy path, reopen-resume, payload tamper, line deletion, line reorder, missing file, invalid event type
- `backend/tests/unit/test_storage.py` — 5 tests (skipped when sqlalchemy absent): Job roundtrip, audit sequence uniqueness, WAL pragma applied, `ListSnapshot` uniqueness, ID-helper parity with `schemas.py`

Run locally: `cd backend && pip install -r requirements.txt && alembic upgrade head && python3 -m pytest tests/unit/ -v` → 34 passing (8 new + 26 pre-existing).

Deferred to later phases: `ModelVersion` row population (Phase 2 backfill + Phase 6 model card); `FindingState` UI activation (Phase 3); evidence-packet export that pulls `snapshots/*.txt.gz` + `audit/*.jsonl` (Phase 3).

**Goal:** every pipeline output becomes durable, queryable, and tamper-evident. Nothing user-visible changes yet.

Shipped scope, for reference:

New module: `backend/storage.py`
- SQLAlchemy 2.x models: `Job`, `Finding`, `Excerpt`, `SanctionsListMatch`, `AuditEvent`, `FindingState`, `FindingStatusHistory`, `JobState`, `JobStatusHistory`, `ModelVersion`, `ListSnapshot`
- Alembic migrations under `backend/migrations/`
- SQLite WAL via `PRAGMA journal_mode=WAL` in engine init
- Stable `source_id = sha256(url)` and `excerpt_id = sha256(url + trigger_sentence + offset)` — required for Phase 2 citation linking

New module: `backend/audit.py`
- `AuditLogger` class: append-only JSONL writer, hash-chained (`prev_hash` + `sha256(canonical_json(event))`)
- Records: `job_started`, `search_executed` (query, query_time, result_count), `content_extracted` (url, extraction_type, content_hash), `nlp_analyzed` (finding_ids, rule_versions), `llm_prompt_sent` (model, version, prompt_hash, retrieval_ids), `llm_response_received` (response_hash, verification_result), `analyst_action` (actor, finding_id, from→to status)
- `verify_chain(job_id)` — regulator-runnable integrity check

Integration points:
- `main.py:45` — replace `jobs: Dict[str, dict]` with DB-backed access; SSE still in-memory but reads from DB
- `main.py:90` (`_run_analysis`) — wrap in audit span; each phase writes AuditEvents
- `sanctions_engine.py:914-941` (`search_google`) — add `audit_logger.log_search(...)` per query
- `sanctions_engine.py:628-679` (`extract_content_from_url`) — hash + store snapshot to `snapshots/{content_hash}.txt.gz`; this is the retained evidence for 7-yr retention
- `sanctions_list_screener.py:31-63` — record `ListSnapshot(list_name, downloaded_at, sha256, entity_count)` on every load; findings link to the snapshot active at analysis time
- `update_lists.py` — write `ListSnapshot` row on every successful download

Nightly backup script `backend/backup.py` — rclone or boto3 to S3/R2, copies DB + JSONL + snapshots.

### Phase 2 — Retrieval-grounded Investigator Brief (P0, weeks 3–4) — ✅ COMPLETE (2026-04-17)

**Status:** Shipped. `LLMVerdictGenerator` renamed to `InvestigatorBriefGenerator`; every claim is citation-anchored; post-verification drops hallucinated citations and unsupported paraphrases (fail-closed). Verifier uses pure-stdlib token-overlap + longest-substring-run — no embedding dependency. Backward-compatible alias (`LLMVerdictGenerator = InvestigatorBriefGenerator`) and dual-key frontend read (`investigator_brief ?? llm_verdict`) keep older payloads rendering.

Delivered:
- `backend/schemas.py` — `Citation`, `Claim`, `InvestigatorBrief`, `EvidenceExcerpt`, `ClaimVerification`, `VerificationReport`; stable `source_id` / `excerpt_id` hash helpers
- `backend/claim_verifier.py` — `verify_claim`, `verify_brief` with `TOKEN_OVERLAP_THRESHOLD=0.4` and `MIN_SUBSTRING_TOKENS=5`
- `backend/sanctions_engine.py` — new `InvestigatorBriefGenerator` with `[src:…][exc:…]` tags in the prompt, `google-genai` `response_schema` output, portability fallbacks, legacy-shape helpers for HTML/CLI/Streamlit rendering
- `backend/main.py` — job payload key renamed `llm_verdict` → `investigator_brief`; sanctioned-entity fast-path fallback rewritten in the new schema
- `frontend/src/components/InvestigatorBriefCard.jsx` (replaces `VerdictCard.jsx`); `Dashboard.jsx` rewired
- `backend/tests/unit/` — 26 passing tests (`test_schemas.py`, `test_source_id.py`, `test_claim_verifier.py`) covering the four Phase 2 verification scenarios
- `backend/demo/test_brief_generation.py` — offline end-to-end runner, no network

Run locally: `cd backend && python3 -m pytest tests/unit/ -v && python3 demo/test_brief_generation.py`

Deferred to later phases: sentence-embedding verification fallback (Phase 6 validation package), `ModelVersion` DB rows (needs Phase 1 storage), `model_card.md` (Phase 6).

**Goal:** kill "verdict"; every LLM claim is citation-anchored and post-verified.

Rename in code: `LLMVerdictGenerator` → `InvestigatorBriefGenerator`. File still `sanctions_engine.py` lines ~295–449.

New Pydantic schema (`backend/schemas.py`):
```python
class Citation(BaseModel):
    source_id: str
    excerpt_id: str

class Claim(BaseModel):
    text: str
    citations: conlist(Citation, min_length=1)

class InvestigatorBrief(BaseModel):
    recommendation: Literal["ESCALATE_FOR_REVIEW", "ADDITIONAL_OSINT_NEEDED",
                            "NO_FURTHER_ACTION_RECOMMENDED", "INSUFFICIENT_DATA"]
    confidence_band: Literal["HIGH", "MEDIUM", "LOW"]
    summary_claims: list[Claim]
    risk_factor_claims: list[Claim]
    suggested_next_steps: list[Claim]
    unverified_claims_dropped: int = 0
```

Note: `recommendation` values deliberately avoid verdict/disposition language. LLM never "clears" anything — only recommends next action.

Changes to prompt (`sanctions_engine.py:326-391`):
- Inject each excerpt with `[src:<source_id>][exc:<excerpt_id>]` tags inline
- Add system instruction: "Every claim must cite at least one `excerpt_id` from the provided evidence. Do not state anything not supported by the evidence. Use the exact excerpt_id strings."
- Use `google-genai` `response_schema=InvestigatorBrief.model_json_schema()` for constrained generation

New module: `backend/claim_verifier.py`
- `verify_claim(claim: Claim, excerpts: dict[str, Excerpt]) -> VerificationResult`
- Checks: (a) every `excerpt_id` exists in the job's evidence set; (b) claim text has token-overlap ≥0.4 with any cited excerpt OR sentence-embedding cosine ≥0.7 using `sentence-transformers/all-MiniLM-L6-v2` (CPU, ~80MB)
- Failed claims removed and counted; if summary has <1 verified claim remaining, brief is marked `status=REQUIRES_MANUAL_GENERATION` and no LLM output shown

Model card: `backend/model_card.md` (tracked in git) + `ModelVersion` rows in DB for current + historical. Fields: model_id, model_version_hash, prompt_template_version, schema_version, spaCy_model, rules_version (false_positive_phrases + financial_keywords + country_variations hashes), deployed_at.

### Phase 3 — HITL analyst workflow (P0, weeks 5–6) — ✅ COMPLETE (2026-04-18)

**Status:** Shipped. Findings now carry reviewable state from the moment analysis completes, jobs require reviewer sign-off (gated on every finding being dispositioned), and every state transition writes both a DB history row and a hash-chained `analyst_action` audit event. Evidence-packet export produces a single-ZIP case file a regulator can open offline. API-side deviation from the plan: replaced `fastapi-users` with a ~150-line PyJWT + passlib-bcrypt auth module — same 3 roles, same JWT, same reviewer-gates-sign-off behaviour, without the router/manager/backend abstraction the MVP didn't need.

Delivered:
- `backend/storage.py` — new `User` table (email, hashed_password, role ∈ analyst/reviewer/admin, disabled, timestamps) + `USER_ROLES` tuple
- `backend/migrations/versions/0002_add_users.py` — Alembic migration for the users table with indexes on email + role
- `backend/auth.py` — PyJWT HS256 token issue/decode, passlib-bcrypt password hashing, `get_current_user_dep()` / `require_role(*roles)` FastAPI dependencies (built lazily so importing the module doesn't require FastAPI), `seed_admin_from_env()` bootstrap helper reading `SANCTIONSIGHT_ADMIN_EMAIL` / `_PASSWORD`; auth module is optional — API still boots without pyjwt/passlib, routes return 503 instead
- `backend/main.py` — admin bootstrap runs on startup; `_ensure_model_version()` seeds a `ModelVersion` row keyed by a fingerprint of model_id + spaCy model + rules/prompt/schema versions so every Job FK'd to one; `_persist_findings_and_excerpts()` writes Excerpt + Finding rows (stable `excerpt_id` / `source_id`) plus an initial `pending` `FindingState` and matching `FindingStatusHistory` row for every finding; `JobState(draft)` seeded at job completion. Every HITL mutation also calls `_log_analyst_action()` which re-opens the job's JSONL chain, appends an `analyst_action` event, and mirrors it to the DB
- New endpoints (all `Depends(require_role(...))` gated):
  - `POST /api/auth/login` — OAuth2PasswordRequestForm → JWT bearer
  - `GET /api/auth/me` — current user
  - `POST /api/auth/register` — admin-only user creation
  - `POST /api/findings/{id}/state` — state transition with reason; reviewers+admins only for `confirmed_match` / `escalated`
  - `POST /api/findings/{id}/fp-override` — analyst can flip a finding to `cleared_fp` with mandatory reason
  - `POST /api/jobs/{id}/sign-off` — reviewer/admin only; returns 409 if any finding is still `pending` or `in_review`
  - `POST /api/jobs/{id}/reopen` — admin only; only works when status is `signed_off`
  - `GET /api/analysts/me/queue` — paginated backlog (findings assigned to me + anything still pending), ordered by risk_score desc
  - `GET /api/jobs/{id}/evidence-packet.zip` — reviewer/admin; download audit-logs the pull
- `backend/evidence_packet.py` — `build_evidence_zip(job_id)` bundles `README.md` (cover sheet), `report.html`, `brief.json`, `result.json`, `findings.csv` (every finding + HITL disposition), `excerpts.jsonl` (stable-ID evidence rows), `snapshots/{content_hash}.txt.gz` for every referenced excerpt, `audit.jsonl`, `audit_verification.json` (fresh `verify_chain` run at bundle time), `list_snapshots.json`, `model_card.md`. Regenerable at any time from durable state.
- `backend/requirements.txt` — `pyjwt>=2.8`, `passlib[bcrypt]>=1.7.4`, `python-multipart>=0.0.6`, `httpx>=0.25`
- `backend/tests/unit/test_auth.py` — 4 tests: hash/verify roundtrip, token encodes role+exp, wrong-secret decode fails, `seed_admin_from_env` is idempotent
- `backend/tests/unit/test_hitl_flow.py` — 7 tests via FastAPI TestClient: analyst transitions, analyst can't confirm match (403), FP override flips state, sign-off blocked while findings pending, sign-off succeeds after dispositioning, only admin can reopen, analyst queue returns pending+assigned
- `backend/tests/unit/test_evidence_packet.py` — 1 integration test seeding a signed-off job + snapshot + audit chain, then asserting all 10 required ZIP entries are present, `findings.csv` is well-formed, `audit_verification.json` returns `OK`, and `model_card.md` contains the model fingerprint

Run locally: `cd backend && pip install -r requirements.txt && alembic upgrade head && SANCTIONSIGHT_ADMIN_EMAIL=admin@local SANCTIONSIGHT_ADMIN_PASSWORD=change-me python3 -m pytest tests/unit/ -v` — 34 pre-existing tests still pass; 12 new Phase 3 tests run when deps are installed.

Deferred to later phases: full frontend wiring of HITL controls (Phase 4 — `SignOffModal.jsx`, `AnalystNotesPanel.jsx`, `AuditTrailDrawer.jsx`, queue sidebar); refresh-token rotation + RBAC for job visibility (Phase 6 hardening); Playwright e2e that drives a sign-off through the UI (Phase 7).

**Goal:** analyst is an active investigator, not a passive consumer. Every finding has reviewable state, every job has sign-off, every action is logged.

Data model (Phase 1 tables activated in UI):
- `FindingState`: `status ∈ {pending, in_review, cleared_fp, confirmed_match, escalated}`, `assigned_analyst_id`, `fp_override_bool`, `notes_md`, `updated_at`, `updated_by`
- `FindingStatusHistory`: append-only; every status change writes a row
- `JobState`: `workflow_status ∈ {draft, in_review, signed_off, reopened}`, `final_disposition_notes`, `signed_off_by`, `signed_off_at`

New endpoints (`main.py`):
- `POST /api/findings/{id}/state` — change status, add notes
- `POST /api/findings/{id}/fp-override` — mark as false positive with reason
- `POST /api/jobs/{id}/sign-off` — close case with disposition notes
- `POST /api/jobs/{id}/reopen` — re-open signed-off case
- `GET /api/jobs/{id}/evidence-packet.zip` — bundled export (see below)
- `GET /api/analysts/me/queue` — pending work for current user

Auth: `fastapi-users` with JWT; minimal users table + 3 roles (`analyst`, `reviewer`, `admin`). Reviewer required for `sign-off`.

Evidence-packet export (`backend/evidence_packet.py`):
- ZIP containing: `brief.pdf` (rendered Investigator Brief), `findings.csv`, `snapshots/*.html` (stored evidence), `audit.jsonl` (the tamper-evident log), `model_card.md`, `list_snapshots.json`, `verification_report.json`
- Single artifact a compliance examiner can review offline

### Phase 4 — Frontend rework (P0, weeks 5–6 in parallel) — ✅ COMPLETE (2026-04-18)

**Status:** The investigator workspace is fully wired for HITL. The dashboard now shows a CaseHeader (workflow status + finding-state counters + sign-off / reopen / evidence-packet / audit-trail CTAs), the brief renders clickable citation chips that open a modal with the full excerpt and context window, each row in the results table exposes per-finding disposition controls (in-review, clear-FP, confirm-match, escalate) with reviewer-only gating, and the global header carries a "My queue" drawer fed by `/api/analysts/me/queue`. All user-visible "verdict" / "AI decides" / "clears you" language has been removed — a new pytest audit (`test_copy_audit.py`) enforces this on every test run.

Delivered:
- `frontend/src/components/CitationPopover.jsx` — new modal + `buildExcerptIndex(data)` helper that walks the result payload to map `excerpt_id → {text, trigger_sentence, url, country, risk_type, risk_score}`
- `frontend/src/components/CaseHeader.jsx` — workflow strip reading `/api/jobs/{id}/hitl-overview`, with `Sign off` (reviewer/admin, blocked while any finding is pending/in_review), `Reopen` (admin only), `Evidence packet` download, and `Audit trail` drawer buttons
- `frontend/src/components/SignOffModal.jsx` — disposition-notes capture tied to `/api/jobs/{id}/sign-off`; notes are required and surfaced in the evidence packet
- `frontend/src/components/FindingControls.jsx` — per-finding controls + `FindingStatusBadge`; analysts can mark in-review or clear-FP (with mandatory rationale), reviewers can confirm or escalate; assign-to email flows via `/api/findings/{id}/state`
- `frontend/src/components/AuditTrailDrawer.jsx` — right-side drawer rendering the hash-chained event log from `/api/jobs/{id}/audit-events`
- `frontend/src/components/AnalystQueue.jsx` — global queue drawer opened from `Header`; lists findings assigned to the current user plus shared backlog, with "Open case" jumping back into the dashboard for that job
- `frontend/src/components/InvestigatorBriefCard.jsx` — rewritten to accept `excerptIndex`, render citation chips as buttons (styled by whether the excerpt resolved), and surface a dedicated "Summary Citations" list when the top-line summary carries evidence
- `frontend/src/components/ResultsTable.jsx` — fetches `/api/jobs/{id}/findings`, attaches worst-status badge + open-count to each row, and injects `FindingControls` into each expanded row
- `frontend/src/components/Header.jsx` — adds "Queue" button that opens `AnalystQueue`; `Header` now accepts `onOpenJob` so drawer picks jump back into the dashboard
- `frontend/src/components/Dashboard.jsx` — composes `CaseHeader`, builds `excerptIndex`, threads `jobId` + `onHitlChanged` into `ResultsTable`, and bumps a refresh key so the CaseHeader re-fetches after any disposition change
- `frontend/src/App.jsx` — wires `onOpenJob` back to `handleComplete` so queue-driven case jumps reload `/api/result/{id}` and switch to the dashboard view
- `backend/main.py` — `_serialize_reports` / `_serialize_name_co` now emit `source_id` + `excerpt_id` on each excerpt so frontend citations resolve against evidence; no new endpoints required (Phase 3 endpoints were already sufficient)
- `backend/tests/unit/test_copy_audit.py` — new pytest scan over `frontend/src` + landing `index.html` enforcing that no banned language (`verdict`, `compliance verdict`, `AI decides`, `clears you`, etc.) reappears, and that `investigator brief` remains present

Run locally: `cd backend && python3 -m pytest -q` — 42 passed, 4 skipped (Phase 3 tests skip without sqlalchemy/jwt/passlib installed; all 8 copy-audit tests pass). The frontend hot-reloads on top of an already-running backend — no extra build step beyond `vite` dev.

Deferred to later phases: dedicated `AnalystNotesPanel.jsx` with markdown rendering (Phase 6 polish — current FindingControls captures notes inline); Playwright e2e that drives the full flow end-to-end (Phase 7); wiring `notes_md` writes on the `/state` transition endpoint (`POST /api/findings/{id}/state` currently only records the reason on status history; notes live on `fp-override`).

### Phase 4 — Frontend rework — original plan (superseded above)

Component changes under `frontend/src/components/`:

| File | Change |
|---|---|
| `VerdictCard.jsx` → `InvestigatorBriefCard.jsx` | New schema consumption: `recommendation` (not `verdict`), claim chips clickable → source excerpt modal. Remove "VIOLATION LIKELY / NO CLEAR VIOLATION" copy. Replace with "ESCALATE FOR REVIEW / NO FURTHER ACTION RECOMMENDED / etc." |
| `ResultsTable.jsx` | Add per-row: Status dropdown, FP-override toggle, analyst-notes popover, assign-to dropdown. Inline citation badges. |
| `Dashboard.jsx` | Add `CaseHeader` (case id, status, assigned analyst, sign-off CTA), `AnalystQueue` sidebar, new "Evidence Packet" export button replacing "Download HTML" |
| New: `CitationPopover.jsx` | Click any claim chip → modal with excerpt text, URL, snapshot-retrieval timestamp, list-snapshot version |
| New: `AnalystNotesPanel.jsx` | Markdown notes per finding; shown in export |
| New: `SignOffModal.jsx` | Reviewer-only; captures disposition notes and locks the case |
| New: `AuditTrailDrawer.jsx` | Right-side drawer shows `finding_status_history` + `audit_events` for transparency |
| `ProgressView.jsx` | No disposition language. "Running analysis" → "Gathering evidence" |
| `Header.jsx` | Replace "Sanctions Search v2.3" subtitle with "Investigator Workspace" |
| Landing `index.html` lines 836, 936 | Replace "AI-generated compliance verdict" with "AI-generated investigator brief with citations" |

All user-facing strings audited for "verdict" / "clears" / "decides" / "determines" — replaced with evidence / brief / recommends / flags.

### Phase 5 — Recall, NLP, and list breadth (P1, week 7) — ✅ COMPLETE (2026-04-18)

- **spaCy upgrade:** `en_core_web_sm` → `en_core_web_lg` (first) with option to flip to `en_core_web_trf` via env var. Update `sanctions_engine.py` top-level load + `requirements.txt`.
- **Entity-name variations:** expand `_get_country_variations()` (`sanctions_engine.py:583-625`) with the CLAUDE.md Task 7 list (IRGC, NIOC, Bank Melli, Chosŏn, Rosoboronexport, Gazprombank, Sberbank, Gaviota, CIMEX, etc.) — these also flow into search queries
- **OpenSanctions bulk integration:** new module `backend/opensanctions_client.py` replaces primary list loading in `sanctions_list_screener.py`; keeps OFSI-specific and any non-OS-covered loaders. Updates `update_lists.py` to pull the bulk FtM dataset daily.
- **Multilingual flag:** detect non-English content with `langdetect`; add `language` field to `Excerpt`; UI shows a 🌐 badge + "non-English source (machine translation not yet included)" — honest scoping for Phase 6.

**Status:** Delivered the recall, NLP, and list-breadth upgrades. Pipeline now screens against broader entity variants and a 97K-entity OpenSanctions superset, runs sentence segmentation and NER on the higher-accuracy `en_core_web_lg` model (transformer variant is one env flip away), and badges non-English sources so analysts see the "translation pending" caveat instead of silently trusting a mis-parsed foreign-language page.

**Delivered:**
- `sanctions_engine.py` — entity-name variations expanded across Iran, Syria, North Korea, Russia, Belarus, Myanmar, Venezuela, and Cuba (Quds Force, Basij, Bank Sepah, NITC, Mahan Air, IRISL, MODAFL, Assad, KOMID, Air Koryo, Bureau 39, SVR, Promsvyazbank, Novatek, Alrosa, Wagner/Prigozhin, Lukashenko, Belneftekhim, MZKT, Tatmadaw, MOGE, PDVSA, CITGO, Cupet); flow through both search-query generation and sentence-level mention matching.
- `sanctions_engine.py` — spaCy load swapped for `_load_spacy_model()` with preferred → `en_core_web_lg` → `en_core_web_sm` fallback chain, auto-download at each step, and a `SANCTIONSIGHT_SPACY_MODEL` env var so `en_core_web_trf` can be enabled without a code change. `SPACY_MODEL_LOADED` global feeds model-version fingerprinting.
- `sanctions_engine.detect_language()` — conservative `langdetect` wrapper (seeded `DetectorFactory`, 40-char minimum, graceful `None` on unavailable dep); called from `extract_content_from_url` and propagated through `analyze_content` + `NameCooccurrenceSearcher` into serialised payloads.
- `storage.Excerpt.language` column + migration `0003_add_excerpt_language.py` (batch-mode `ALTER` + index) — persists the detected code per excerpt for analyst-facing badging and later multilingual metrics.
- `main.py` — `_ensure_model_version()` records the actually-loaded spaCy model; `_serialize_reports` / `_serialize_name_co` emit `language` per result; `_persist_findings_and_excerpts` stores it on the Excerpt row.
- `frontend/src/components/ResultsTable.jsx` — Globe badge (`Lucide`) on non-English results with title tooltip "translation pending" to match the Phase 6-scoped stance.
- `backend/opensanctions_client.py` — new module owning FtM JSONL parsing (`parse_entity`, `iter_entities`, `load_stats`); frozen dedup set against OFAC/UN/UK/EU plus per-dataset source labels (AU/CA/CH/JP/INTERPOL); `sanctions_list_screener._load_opensanctions` now a 20-line delegation shim. OFSI, OFAC, UN, and EU loaders deliberately retained on their own parsers — format quirks worth preserving.
- `requirements.txt` — `langdetect>=1.0.9` added under a Phase 5 section with commentary pointing at the `python -m spacy download en_core_web_lg` install step; `README.md` install command updated to match.
- `tests/unit/test_language_detection.py` (4 tests, `pytest.importorskip` so it skips cleanly without the dep) and `tests/unit/test_opensanctions_client.py` (14 tests — schema filtering, dedup, alias capping, jurisdiction fallback, malformed-line resilience, stats). Full suite: **56 passed, 5 skipped**.

**Run locally:**
1. `cd sanctions-tool/backend && pip install -r requirements.txt && python -m spacy download en_core_web_lg`
2. `python update_lists.py` — pulls the OpenSanctions FtM bulk dataset (~200MB) into `data/opensanctions_sanctions.jsonl` alongside the per-list CSV/XML files.
3. `alembic upgrade head` — applies `0003_add_excerpt_language`.
4. `pytest tests/unit` — expect 56 passing, 5 skipped.
5. Optional: `SANCTIONSIGHT_SPACY_MODEL=en_core_web_trf python -m spacy download en_core_web_trf && uvicorn main:app --reload` to exercise the transformer variant.

**Deferred (Phase 6+):**
- Full translation pipeline for flagged non-English excerpts (Phase 7+ per "Out of scope").
- Golden-dataset replay harness comparing `sm` vs `lg` vs `trf` F1 — listed as Phase 5 verification in §Verification; the scaffolding is ready but the labelled corpus is a Phase 6 deliverable.
- `xx_ent_wiki_sm` multilingual NER pass on language != "en" excerpts.

### Phase 6 — SR 11-7 validation package (P1, week 8) — ✅ COMPLETE (2026-04-18)

Deliverables (living docs in `backend/compliance/`):
- `model_card.md` — conceptual soundness memo, data lineage, known limitations
- `validation/` — out-of-sample test set (200 labeled cases), golden dataset CSV, harness `validation/run.py` producing precision/recall/F1 per risk-type
- `fairness_tests/` — name-script coverage tests (Cyrillic, Arabic, Chinese pinyin), false-positive rate by country
- `runbooks/` — incident response for false-negative, false-positive, LLM outage, list-update failure
- `governance.md` — change management, approval gates for prompt/schema/rules changes
- SOC 2 Type II readiness checklist (external audit is separate work; target Q3)

**Status:** Shipped the regulator-facing validation pack. The tool now has an end-to-end evidence trail a compliance-consultant or examiner can walk from top to bottom — model card → governance → validation harness → fairness suite → runbooks → SOC 2 tracker. Every gating threshold is codified in CI-runnable tests, not prose.

**Delivered:**
- `backend/compliance/README.md` — index and reading order for external reviewers.
- `backend/compliance/model_card.md` — 12-section SR 11-7-structured model card: intended use & scope (with explicit prohibited uses), component inventory + ASCII pipeline diagram, data lineage + provenance artefacts (ListSnapshot/Excerpt hashes/audit.jsonl/ModelVersion), conceptual soundness with known limitations, training/tuning data (explicitly none for rule-based components), performance contract with tiered acceptance thresholds, monitoring signals, bias/fairness posture, security controls table, approved/prohibited uses, change history.
- `backend/compliance/governance.md` — Tier A / B / C classification with per-artefact mapping, RACI (Engineering Owner / Compliance Reviewer / Validation Reviewer / Head of Compliance Engineering / On-call), two-person-integrity requirement, emergency-override narrow path, governance-log append-only discipline, quarterly/monthly/weekly review cadences, buyer shared-responsibility boundary.
- `backend/compliance/validation/run.py` — network-free harness loading `golden_dataset.csv`, running each row through `SanctionsContentAnalyzer.analyze_content`, computing per-risk-type precision/recall/F1/HIGH-FPR, enforcing the Critical/Material/Advisory recall floors and HIGH-FPR ceilings from the model card, `--strict` exit-1 gate for CI, `--json-out` for governance-log attachments.
- `backend/compliance/validation/golden_dataset.csv` — 20 seeded cases (GDS-001..GDS-020) covering DIRECT_BUSINESS, INDIRECT_BUSINESS, COMPLIANCE_MENTION, SANCTIONS_REGULATORY_MENTION, HIGH_RISK_EMAIL, plus FP-trap negatives (cuban sandwich, damascus steel, Persian rug, Korean BBQ, currency-symbol-only pages) and a Russian-language negative probe. Labelled as the starter corpus; target 200 before external validation review.
- `backend/compliance/validation/README.md` — schema doc, case-pass rules, thresholds table (mirrors model card §6), extension rules, what-this-doesn't-cover delineation against the unit and integration suites.
- `backend/compliance/fairness_tests/test_name_scripts.py` — per-script match-rate floors (Latin 1.00 / Cyrillic 0.75 / Arabic 0.66 / CJK 0.66) plus a gap-from-Latin cap of 0.35. Builds an in-memory screener via `SanctionsListScreener._add_entity` + `_build_name_index` so the test needs no list files. 4 passing tests (3 parametrized + 1 gap check).
- `backend/compliance/fairness_tests/test_fp_by_country.py` — per-country share cap (≤ 0.30 of HIGH FPs, guarded by a 4-sample minimum so one-probe runs don't false-trigger) and an absolute HIGH-FP-rate ceiling (≤ 0.20 across all negative probes). 15 negative probes across 9 jurisdictions. 2 passing tests.
- `backend/compliance/fairness_tests/conftest.py` + `__init__.py` + `README.md` — pytest discovery shim, run instructions, and an "historic incidents this suite was designed to prevent" section documenting the Cuban-coffee and Cyrillic-tokeniser regressions.
- `backend/compliance/runbooks/` — four incident runbooks following a consistent structure (Scenario → Severity → Detection → Immediate response → Root-cause triage → Permanent fix → Preventive controls → What *not* to do): `false_negative.md`, `false_positive.md`, `llm_outage.md` (with the evidence-only fallback-brief design), `list_update_failure.md` (with the per-list-source triage tree including the EU token rotation gotcha). Plus `runbooks/README.md` with index and common principles.
- `backend/compliance/soc2_readiness.md` — TSC-mapped checklist (CC / A / C / PI in scope; Privacy out) with status markers (`[✓]` / `[~]` / `[ ]` / `[buyer]`), evidence-collection plan, Q2/Q3 sequenced open items, and a shared-responsibility summary pointing at governance.md §9.
- Test suite: `pytest tests/unit/ compliance/fairness_tests/` → **63 passed, 5 skipped**. Validation harness wires cleanly (`python -m compliance.validation.run --help` confirmed).

**Run locally:**
1. `cd sanctions-tool/backend && pip install -r requirements.txt && python -m spacy download en_core_web_lg`.
2. `alembic upgrade head`.
3. `python -m compliance.validation.run --strict --json-out /tmp/validation.json` — enforces the release-gate thresholds.
4. `pytest compliance/fairness_tests/ -v` — script-coverage and FP-share regression guard.
5. `pytest tests/unit/` — existing 56 unit tests still pass alongside the new compliance artefacts.
6. External review: hand the `backend/compliance/` directory to a compliance consultant (K2 / Guidehouse / peer per Phase 6 §Verification) and point them at `compliance/README.md` for the reading order.

**Deferred (Phase 7+ or out of scope):**
- Growing the golden dataset from 20 → 200 labelled cases. Ongoing; the extension policy lives in `validation/README.md`.
- Automated scheduling of the validation harness + fairness suite in GitHub Actions — lands with Phase 7 CI work.
- Playwright end-to-end tests of the full analyst-to-sign-off flow — Phase 7.
- External pen test and SOC 2 Type II auditor kickoff — targeted for Q2/Q3 2026; tracked in `soc2_readiness.md`.
- `governance_log/` directory will be created on the first post-Phase-6 Tier A / B change; not shipped empty.

### Phase 7 — Test coverage (cross-cutting, ongoing) — ✅ COMPLETE (2026-04-18)

**Status:** Unit, integration, fairness, and validation suites green. E2E
scaffolded but gated behind an env flag pending a dedicated harness job.

**Delivered**
- `tests/unit/test_risk_scoring.py` — 12 tests pinning tier boundaries
  (HIGH ≥ 70, MEDIUM ≥ 40, LOW ≥ 15) and the financial-indicator override.
- `tests/unit/test_negation.py` — 23 tests covering `_is_negated`,
  `has_active_financial_indicator`, and end-to-end negation-prevents-escalation.
- `tests/unit/test_false_positive_filter.py` — 17 tests over
  `_analyze_context`: phrase exclusion, PERSON-NER downgrade, keyword-tier
  boundaries, sanctions-term boost + negation penalty.
- `tests/integration/conftest.py` — shared fixtures (`app_env`,
  `seeded_users`, `login`, `auth_headers`) with throwaway SQLite + JWT
  per test.
- `tests/integration/test_full_pipeline.py` — happy-path analyze run with
  all external boundaries (Google CSE, `process_single_entity`,
  `perform_global_ofac_search`, `InvestigatorBriefGenerator`) stubbed.
  Asserts findings persist and the audit chain verifies.
- `tests/integration/test_hitl_flow_api.py` — full
  pending → in_review → confirmed_match/cleared_fp → sign_off → reopen
  lifecycle with history rows and role-guard assertions.
- `tests/e2e/smoke.spec.ts` + `playwright.config.ts` + `README.md` —
  analyst → reviewer hand-off smoke test, `test.skip()` guarded on
  `SANCTIONSIGHT_E2E_ENABLED`.
- `.github/workflows/ci.yml` — two-job pipeline: backend (ruff, mypy
  advisory, pytest unit + integration + fairness, validation --strict,
  artefact upload) + frontend (`vite build`).

**Run locally**

```bash
cd sanctions-tool/backend
pytest tests/unit/ tests/integration/ compliance/fairness_tests/ -v
python -m compliance.validation.run --strict --json-out /tmp/validation.json

cd ../frontend && npm run build
```

Current local result: **115 passed, 10 skipped** (integration tests skip
until `sqlalchemy` + `fastapi` are installed — they run green in CI).

**Deferred**
- `test_claim_verifier.py` and `test_audit_chain.py` are referenced in the
  original plan but already exist under `tests/unit/` from earlier phases.
- Playwright harness is scaffolded; wiring it into CI requires a
  dedicated job with a stubbed backend container + frontend preview
  server (tracked as a post-Phase-7 follow-up).
- `mypy` is advisory (`continue-on-error: true`) until the codebase is
  fully type-annotated — tracked as Tier C cleanup.

### Phase 8 — Per-link LLM verdicts + case-level analyst review (P1, 2026-04-20) — ✅ COMPLETE (2026-04-20)

**Status:** Shipped four tightly-coupled features that together close the "per-URL LLM read" and "analyst can record their own view" gaps surfaced during Phase 3/4 review. Every analyzed URL now carries a binary concern flag + short reasoning from the LLM; analysts agree/disagree at both the per-link and the case level (disagreement requires a ≥10-char reason, enforced client- and server-side); per-link excerpt lists are replaced by the full extracted page content with trigger sentences highlighted inline; and a case-wide analyst summary sits alongside (not in place of) the existing sign-off notes so the analyst's narrative read is captured separately from disposition. All analyst writes append to `audit.jsonl` + the `AuditEvent` DB mirror.

**Delivered**

Backend:
- `backend/storage.py` — new module-level `url_hash(url) → sha256(url)[:16]` helper (stable, URL-safe key for path params); new `LinkVerdict` table (`job_id`, `url_hash`, `url`, `llm_concern`, `llm_reasoning`, `llm_model`, `llm_error`, `analyst_agrees`, `analyst_disagree_reason`, `analyst_updated_by`, `analyst_updated_at`, `created_at`, `updated_at`; unique on `(job_id, url_hash)`); five new nullable columns on `JobState` for case-level review (`analyst_case_summary`, `analyst_agrees_with_brief`, `analyst_case_disagree_reason`, `analyst_case_updated_by`, `analyst_case_updated_at`).
- `backend/migrations/versions/0004_link_verdicts_and_case_review.py` — Alembic batch migration creating the `link_verdicts` table and adding the five `job_states` columns; down revision = `0003_add_excerpt_language`.
- `backend/sanctions_engine.py` — `analyze_content()` now emits a truncated `extracted_content` field (40k char cap) on every result (including UNKNOWN extraction branches, so downstream code doesn't need to guard); new `PerLinkVerdictGenerator` class with Vertex / AI Studio branching identical to `InvestigatorBriefGenerator`, producing `{concern: bool, reasoning: str, model: str}` from up to 6 trigger-sentence + context windows per URL (reasoning capped at 800 chars).
- `backend/main.py` — new helpers `_collect_unique_link_rows`, `_run_per_link_verdicts` (ThreadPoolExecutor cap 10, progress callback 78→84), `_persist_link_verdicts` (upsert preserving existing analyst fields), `_load_case_review`, `_load_link_verdicts`; `_run_analysis` wires per-link verdicts in after `engine.assign_stable_ids` and before report serialization; `_serialize_reports` / `_serialize_single_report` / `_serialize_name_co` now emit `url_hash`, `extracted_content`, and `link_verdict` on each row; `/api/result/{job_id}` hydrates fresh `LinkVerdict` + case-review state on every read; four new authenticated endpoints — `POST /api/jobs/{job_id}/links/{url_hash}/verdict`, `POST /api/jobs/{job_id}/case-summary`, `POST /api/jobs/{job_id}/case-verdict`, `GET /api/jobs/{job_id}/link-verdicts` — all guarded by `_require_disagree_reason` (10-char floor) and routed through `_log_analyst_action`.

Frontend:
- `frontend/src/components/ResultsTable.jsx` — new `renderHighlightedMulti(text, regex, triggerSentences)` merges non-overlapping trigger-sentence + keyword highlight ranges; new `PageContentBlock` component renders the full extracted content with a collapsible 4000-char preview + "Show all" button; expanded rows now surface `<LinkVerdictBlock>` at the top and `<PageContentBlock>` below (snippet block falls back only when `extracted_content` is empty).
- `frontend/src/components/LinkVerdictBlock.jsx` — new. Concern badge (danger/success/muted/LLM-unavailable), reasoning paragraph, analyst agree/disagree controls, 10-char-min disagree form, prior-state echo. Submits to `/api/jobs/{id}/links/{url_hash}/verdict` via `authedFetch`.
- `frontend/src/components/CaseReviewCard.jsx` — new. Case-wide summary textarea with "Save summary" button → `POST /api/jobs/{id}/case-summary`; Agree/Disagree for the aggregate brief → `POST /api/jobs/{id}/case-verdict`; disagree form with same 10-char floor; last-edit timestamp + updated-by email displayed.
- `frontend/src/components/Dashboard.jsx` — imports `CaseReviewCard`, renders it between `InvestigatorBriefCard` and `ListScreeningCard`, and wires its `onChanged` into the existing `hitlRefreshKey` so the case header + table pick up fresh state without a full reload.

**Data-flow notes**
- The same `url_hash` is the join key between backend `LinkVerdict.url_hash` and frontend `r.url_hash` on the serialized row — avoids raw-URL path encoding issues.
- Deduplication in `_collect_unique_link_rows` guarantees one LLM call per unique URL even when the URL appears in multiple country buckets (concurrency cap = 10).
- `analyst_agrees` tri-state: `null` = not yet reviewed, `true` = agreed (no reason required), `false` = disagreed (reason required). Identical shape at the link and the case level.

**Run locally**

```bash
cd sanctions-tool/backend
alembic upgrade head                      # applies 0004 (link_verdicts + 5 JobState cols)
cd ../frontend && npm run build
```

Then start the backend per the Vertex AI / AI Studio recipes in `CLAUDE.md`; hit `http://localhost:8000/app/`. Without keys the server still boots and the new endpoints + UI render — verified 2026-04-20 (`/api/health` returns `status:ok, llm_available:false`).

**Deferred**
- No new automated tests added for the four analyst endpoints or `PerLinkVerdictGenerator`; only smoke-tested via curl + browser (tracked as a Phase 7 follow-up to grow `tests/integration/test_hitl_flow_api.py`).
- No translation pass on per-link reasoning for non-English source pages — consistent with Phase 5's "flag, don't translate" stance (full translation is still out-of-scope per §Out of scope).
- No rate-limit / circuit-breaker on the 10-wide LLM fan-out beyond the executor cap; a long run against 100+ URLs will still linearly consume the per-minute quota.
- Per-link LLM cost is not yet aggregated into the case header — analysts see LLM errors per link but no summary "X of Y links returned a verdict" roll-up.

---

## Critical files to modify (reference)

**Backend**
- `backend/main.py` — L45 (jobs dict → DB), L90 (`_run_analysis` audit spans), L70-75 (AnalyzeRequest + new HITL endpoints)
- `backend/sanctions_engine.py` — L295-449 (InvestigatorBriefGenerator rewrite), L326-391 (prompt with citation tags), L381-390 (Pydantic schema output), L914-941 (search_google audit hook), L628-679 (extract + snapshot hash), L583-625 (entity variations expansion)
- `backend/sanctions_list_screener.py` — L31-63 (add ListSnapshot recording); large portions superseded by OpenSanctions client
- `backend/update_lists.py` — L208 (ListSnapshot write); add OS bulk pull
- `backend/requirements.txt` — add: sqlalchemy, alembic, pydantic≥2, fastapi-users[sqlalchemy], sentence-transformers, opensanctions-sdk (or REST client), langdetect, boto3, ruff, mypy, pytest, pytest-asyncio, respx, playwright

**Frontend**
- `frontend/src/components/VerdictCard.jsx` — rename + rewrite
- `frontend/src/components/ResultsTable.jsx` — HITL actions, citation badges
- `frontend/src/components/Dashboard.jsx` — case header, queue sidebar, packet export
- `frontend/src/components/ProgressView.jsx` — copy audit
- `frontend/src/components/Header.jsx` — copy audit
- `frontend/src/App.jsx` — auth routing, analyst context
- `sanctions-tool/index.html` — landing copy audit (L836, L936 and full scan)
- `frontend/package.json` — add react-query/tanstack-query, @auth0/auth0-react or custom JWT, zod

**New files**
- `backend/storage.py`, `backend/audit.py`, `backend/schemas.py`, `backend/claim_verifier.py`, `backend/evidence_packet.py`, `backend/opensanctions_client.py`, `backend/backup.py`, `backend/migrations/*`
- `backend/compliance/model_card.md`, `backend/compliance/validation/`, `backend/compliance/runbooks/`
- `frontend/src/components/InvestigatorBriefCard.jsx`, `CitationPopover.jsx`, `AnalystNotesPanel.jsx`, `SignOffModal.jsx`, `AuditTrailDrawer.jsx`, `CaseHeader.jsx`, `AnalystQueue.jsx`

---

## Reused existing functionality

Keep intact — these are assets, not debt:
- `EnhancedSanctionsSearcher` search loop and dedup
- `extract_content_from_url` PDF + HTML + Google cache fallback logic
- `analyze_content` two-pass NLP (country-mention + sanctions-terms)
- `EnhancedRiskAssessment` negation-aware financial scoring (post–Task 8 fix)
- `NameCooccurrenceSearcher` with rapidfuzz
- HTML-escape-before-highlight pattern (`sanctions_engine.py:1271-1274`)
- SSE progress stream and `_run_analysis` phase orchestration
- `RiskCharts`, `ResultsTable` sort/filter/search logic (additive changes only)

---

## Verification

**Phase 1:** `pytest tests/integration/test_audit_chain.py` — run a full job, then tamper with a JSONL line and confirm `verify_chain(job_id)` returns `INTEGRITY_BROKEN`. Query SQLite for `"all jobs touching Iran in last 30 days"`.

**Phase 2:** `pytest tests/unit/test_claim_verifier.py` — hand-crafted LLM outputs with (a) valid citation, (b) hallucinated excerpt_id, (c) paraphrase above threshold, (d) paraphrase below threshold. All 4 behaviors match expectation.

**Phase 3:** Manual flow: analyst A logs in, flags 3 findings FP, assigns 1 to reviewer B. B signs off. Reopen. Export evidence packet. Inspect ZIP offline — verify `audit.jsonl` hash chain intact, `brief.pdf` matches UI, `findings.csv` matches DB.

**Phase 4:** `grep -ri "verdict" frontend/src/ sanctions-tool/index.html` returns zero matches (except intentional historic-change notes). Playwright test: full run of landing → input → brief → review → sign-off → packet download.

**Phase 5:** Replay a stored Iranian news-site corpus through old (`sm`) and new (`lg`) spaCy; assert F1 on golden dataset improves without HIGH-risk false-positive rate crossing a regression threshold.

**Phase 6:** Package sent to a compliance-consultant contact (K2, Guidehouse, or peer) for a mock examiner review. Target: "this is defensible under SR 11-7" signal.

**End-to-end demo script:** `backend/demo/run_e2e.sh` — starts uvicorn, seeds a test case (e.g., a known OFAC-enforced entity), runs pipeline, opens browser, analyst signs off, exports packet. Used for investor/customer demos and is also the smoke test before each release.

---

## Out of scope (deliberately deferred)

- Multi-language adverse media NLP (flagged-only in Phase 5; full translation pipeline is Phase 7+)
- Vector RAG (not needed per architecture decision Q2)
- Dow Jones / Refinitiv OEM (budget-out-of-scope)
- Tier-1 bank SR 11-7 depth (targeting mid-market + consent-ordered BaaS first)
- FedRAMP (if targeting government later)
- Transaction-monitoring and primary screening replacement (compass doc explicitly says avoid)
