# SanctionSight — Model Card

**Version:** 2.3-phase6
**Issue date:** 2026-04-18
**Owner:** Head of Compliance Engineering, SanctionSight (enquiries@sanctionsight.com)
**Regulatory frame of reference:** SR 11-7 (Federal Reserve / OCC supervisory guidance on model risk management); EU AI Act Title III obligations for high-risk AI systems; FinCEN 31 CFR 1010.630 sanctions screening expectations.

This model card describes the composite system SanctionSight uses to surface potential sanctions exposure from open-web sources. It is not a single model — it is an **ensemble of rule-based, statistical NLP, and LLM components** with a human-in-the-loop disposition layer. The card covers all components material to a regulated decision.

---

## 1. Intended use and scope

### In scope
- **Investigator assistance.** Produce a structured investigator brief — evidence, citations, and a recommendation grade — that an authorised analyst reviews before any onboarding or de-risking decision is taken.
- **Supplementary screening** against open-web corpora (news, regulatory filings, corporate disclosures, social media) for entities already subject to primary sanctions-list screening performed by the buyer.
- **Typology surfacing:** direct business relationships, indirect (dual-use / third-party) relationships, regulatory mentions (OFAC / BIS / OFSI enforcement actions), and name co-occurrence with sanctioned jurisdictions.

### Explicitly out of scope
- **Primary sanctions screening.** This is not a replacement for the buyer's OFAC SDN / EU CFSP / OFSI / UN list screening workflow. Evidence packets repeat this caveat on every page.
- **Automated decisioning.** The system does not clear, approve, or reject any customer, transaction, or relationship. Every finding requires analyst disposition (`pending → in_review → cleared_fp | confirmed_match | escalated`) and a reviewer sign-off before the case closes.
- **Transaction monitoring.** No transactional data is processed; only open-web text about named entities.
- **Non-English adverse media at production quality.** Phase 5 flags non-English sources with a `translation pending` badge; full multilingual NLP is deferred to Phase 7+.
- **Tier-1 bank SR 11-7 depth.** The system targets mid-market banks, BaaS providers, and crypto custodians. Tier-1 institutions require additional controls the buyer must layer on top.

### Intended users
Licensed AML / BSA analysts, reviewers, and admins inside regulated financial institutions. Role-based access (`analyst`, `reviewer`, `admin`) gates state transitions and evidence packet export.

---

## 2. System architecture

### Component inventory

| Component | Role | Type | Failure mode if it misbehaves |
|---|---|---|---|
| `EnhancedSanctionsSearcher` (`sanctions_engine.py:1296`) | Google Custom Search query planner | Deterministic rule-based | Under-retrieval → missed adverse media |
| `extract_content_from_url` (`sanctions_engine.py`) | Trafilatura + PyMuPDF content extraction | Deterministic, third-party parsers | Silent empty-text on JS-heavy pages → missed findings (mitigated by snippet fallback) |
| spaCy `en_core_web_lg` (default) / `_trf` (optional) | Sentence segmentation + NER | Statistical | Bad boundaries → keyword scoring applied to wrong context window |
| `SanctionsContentAnalyzer` (`sanctions_engine.py:919`) | Keyword / negation / context-window scoring | Deterministic rule-based | Incorrect risk tier (HIGH/MEDIUM/LOW) — audit chain preserves full provenance |
| `EnhancedRiskAssessment` (`sanctions_engine.py:309`) | Financial-indicator detection with 6-word backward negation window | Deterministic rule-based | Currency-symbol FPs (since Phase 5: scoped to ±3 sentences only) |
| `SanctionsListScreener` + `opensanctions_client` | Fuzzy name match against ~97K OpenSanctions + OFAC + UN + OFSI + EU + US CSL | rapidfuzz `token_set_ratio` ≥ 82 (screening) / ≥ 85 (co-occurrence) | Threshold mis-set → sensitivity/specificity skew |
| `NameCooccurrenceSearcher` (`sanctions_engine.py:1465`) | Business-name + sanctioned-jurisdiction co-occurrence | Deterministic + fuzzy | Short business names → false matches (acknowledged; mitigated by 85 threshold) |
| `detect_language` (`sanctions_engine.py:273`) | `langdetect` ISO-639-1 on extracted text | Statistical, seeded | Below 40 chars returns `None` (conservative) |
| `InvestigatorBriefGenerator` (`sanctions_engine.py:426`) | LLM-authored summary + citations | **LLM (Gemma 3 27B IT)** | Hallucinated citations — blocked by `claim_verifier.py` + citation round-trip check |
| `claim_verifier.py` | Grounds every claim in a stored excerpt before it appears in the brief | Deterministic | — |
| `audit.py` | Hash-chained `audit.jsonl` append-only log of every state change, LLM call, and list snapshot | Deterministic | — |

### Pipeline diagram (ASCII)

```
URL / business name
        │
        ▼
┌────────────────────┐
│ EnhancedSanctions  │  — 3 queries per jurisdiction (site, social, third-party)
│     Searcher       │  — plus Phase 5 sanctions-term-only queries
└────────────────────┘
        │  Google CSE results (dedup by URL hash)
        ▼
┌────────────────────┐
│  extract_content   │  — trafilatura → PyMuPDF → snippet fallback
└────────────────────┘
        │  clean text + language code
        ▼
┌────────────────────┐
│  SanctionsContent  │  — spaCy sentence segmentation
│     Analyzer       │  — ±3 sentence context window
│                    │  — keyword / negation / NER scoring
└────────────────────┘
        │  per-URL findings (risk_level, confidence, excerpts)
        ▼
┌────────────────────┐
│ claim_verifier     │  — every excerpt has a stored snapshot + hash
└────────────────────┘
        │
        ▼
┌────────────────────┐
│ InvestigatorBrief  │  — Gemma 3 prompt with evidence pack + citation schema
│   Generator (LLM)  │  — temperature=0.2, response_mime_type=application/json
└────────────────────┘
        │  brief.json with citations → excerpt_id back-references
        ▼
┌────────────────────┐
│  HITL workflow     │  — analyst disposition, reviewer sign-off
│ (main.py endpoints)│  — admin reopen; evidence packet export (ZIP)
└────────────────────┘
```

---

## 3. Data lineage and provenance

### Inputs
1. **Google Custom Search Engine** — operator must supply `GOOGLE_API_KEY` + `GOOGLE_CSE_ID`. Queries and snippets are logged to the `SearchQuery` / `SearchResult` tables and referenced from `audit.jsonl`.
2. **Target web pages** — full text snapshotted to `data/snapshots/<sha256>.txt`. Hash recorded on every finding so the evidence packet reproduces what the analyst saw.
3. **Sanctions lists** — refreshed daily by `update_lists.py`:
   - OFAC SDN + Alt + Consolidated (US Treasury)
   - UN Security Council consolidated (scsanctions.un.org)
   - OFSI UK sanctions list (assets.publishing.service.gov.uk)
   - EU consolidated financial sanctions (webgate.ec.europa.eu)
   - US Consolidated Screening List (trade.gov — BIS Entity/Denied/UVL/MEU, State AECA/ISN, OFAC NS-MBS/SSI/FSE)
   - OpenSanctions bulk FtM dataset (~97K entities, CC BY-NC 4.0) — see `opensanctions_client.py`
4. **LLM call** — Gemma 3 27B IT hosted by Google. Prompt and raw response are both stored (the prompt never includes any PII not already on the public web page).

### Provenance artefacts (regulator-facing)
- **`ListSnapshot` rows** — SHA-256 per downloaded list + active-from timestamp. Every finding joins the snapshot that was active when analysis ran.
- **`Excerpt` rows** — hash-of-trigger-sentence + stored-snapshot hash. Re-derivable post-hoc even if the source page is deleted.
- **`audit.jsonl`** — hash-chained append-only log: job state transitions, LLM request/response hashes, disposition changes, evidence-packet exports. Integrity check lives in `audit.py`.
- **`ModelVersion` row** — on every job: `{spacy_model, gemma_model, engine_git_sha, list_snapshot_ids}`. Written by `_ensure_model_version` in `main.py`.

---

## 4. Conceptual soundness

### Design premise
Recall is prioritised over precision because the cost of a missed sanctions link (missed SAR, regulatory penalty, secondary sanctions) materially exceeds the cost of a false positive (analyst review time). This is explicit in CLAUDE.md: *"No sanctions concern discoverable through open Google searches should be missed."*

### Why this decomposition
- **Deterministic retrieval + deterministic scoring**, then **LLM only for narrative synthesis**, keeps the decision-material path auditable. The LLM is not a scorer — it is a rapporteur.
- **Context-window keyword scoring** rather than full-document scoring avoids long-range conflation (e.g., a shipping-policy page that also contains "$" pricing on a different product).
- **Fuzzy matching (rapidfuzz token_set_ratio ≥ 85)** handles name-order variation and punctuation drift that exact match would miss. Threshold was chosen empirically; see §6.
- **HITL before close** is required by design — the system state machine cannot reach `signed_off` without an analyst disposition on every `pending` finding and a reviewer's `final_disposition_notes`.

### Known conceptual limitations
- **Open-web bias.** The system only sees what Google indexes. Dark-web, encrypted-messaging, and paywalled sources are invisible. This is documented in every evidence packet cover page.
- **Language coverage.** English-first. Non-English pages are flagged but not fully analysed pending the Phase 7+ translation pipeline.
- **Temporal drift.** Sanctions lists change; a SDN listing effective 2026-04-15 is only reflected after the next `update_lists.py` run. The `ListSnapshot.active_from` column makes this explicit on every finding.
- **LLM prompt sensitivity.** Brief content can shift with Gemma model updates. Mitigated by prompt-freeze + change management in `governance.md`.
- **Aggregator echo.** If the same claim appears on three republished aggregator pages, current scoring may over-weight a single underlying source. Deduplication by canonical URL is partial.

---

## 5. Training / tuning data

None of the rule-based components are trained — they are hand-authored keyword tiers, negation windows, and thresholds. The statistical components are used off-the-shelf:

| Component | Training data | Our changes |
|---|---|---|
| spaCy `en_core_web_lg` | OntoNotes 5 + ClearNLP | None — used as-is |
| Gemma 3 27B IT | Google proprietary pre-training + instruction tuning | None — API consumer only, no fine-tune |
| `langdetect` | 55-language profile files, Cavnar & Trenkle-style n-grams | Seeded `DetectorFactory` for determinism |
| rapidfuzz | — (algorithmic) | — |

We do not hold any training corpus. There is no model-retraining pipeline to govern.

---

## 6. Performance (golden-dataset validation)

Performance is measured by `backend/compliance/validation/run.py` against `golden_dataset.csv`. The dataset is a living artefact — the seeded corpus has 20 cases at Phase 6 launch and will grow to the SR 11-7 target of ≥ 200 labelled cases before the first external validation review (Phase 6 exit criterion).

Metrics reported (per risk-type: DIRECT_BUSINESS, INDIRECT_BUSINESS, COMPLIANCE_MENTION, SANCTIONS_REGULATORY_MENTION, NAME_COOCCURRENCE):
- Precision (of flagged findings, how many were true positives)
- Recall (of true positives in the corpus, how many the system surfaced)
- F1
- False-positive rate conditional on risk-tier HIGH (the metric that degrades analyst trust fastest)

### Acceptance thresholds for release promotion
Set in `validation/run.py` and enforced by the CI gate:

| Tier | Recall floor | HIGH-FPR ceiling |
|---|---|---|
| Critical (DIRECT_BUSINESS + SANCTIONS_REGULATORY_MENTION) | **≥ 0.90** | ≤ 0.15 |
| Material (INDIRECT_BUSINESS + NAME_COOCCURRENCE) | ≥ 0.80 | ≤ 0.20 |
| Advisory (COMPLIANCE_MENTION) | ≥ 0.70 | ≤ 0.25 |

A prompt or rule change that drops any Critical-tier recall by more than 0.02 is a **blocking** release event per `governance.md`.

### Known failure modes captured as regression fixtures
- `fairness_tests/test_name_scripts.py` — Cyrillic, Arabic, Chinese pinyin names must match within threshold.
- `fairness_tests/test_fp_by_country.py` — no country may contribute more than 30% of the total HIGH-tier false positives (prevents the "everything Cuban is flagged because of Cuban sandwich" regression).

---

## 7. Monitoring and ongoing validation

| Signal | Source | Threshold | Action |
|---|---|---|---|
| Job completion rate | `main.py` job state counts | < 95% daily | Investigate extractor failures |
| HIGH-tier false-positive rate (analyst-labelled) | `FindingState` transitions `→ cleared_fp` | > 25% 7d | Prompt / threshold review |
| LLM brief generation failures | `audit.jsonl` event_type=`llm_error` | > 5% daily | Check Gemma service health; fall back to evidence-only brief |
| List-snapshot staleness | `ListSnapshot.active_from` max age | > 36h | Page on-call (see `runbooks/list_update_failure.md`) |
| Model version drift | `ModelVersion` records per-job | any unapproved change | Block release; governance review |

Dashboards are intentionally deferred — the audit trail is the source of truth. Dashboards are operator convenience; the regulator cares about the log.

---

## 8. Bias, fairness, and equity

Sanctions-screening adjacent systems carry well-documented name-bias failure modes (Hispanic surnames over-matching SDN entries, Arabic transliteration variants, Chinese pinyin romanisation). Our mitigations:

1. **Multi-script coverage tests** in `fairness_tests/` — any drop in match rate across Cyrillic / Arabic / Chinese pinyin blocks the release.
2. **Per-country FP-rate cap** — a single jurisdiction cannot exceed 30% of the HIGH-tier FP budget. Prevents the "all Cuban-sounding strings are sanctions hits" regression.
3. **Analyst disposition reasons are mandatory** — `FindingState` cannot transition to `cleared_fp` without a `reason` string. These reasons are periodically audited for patterns indicating systematic bias.
4. **No demographic inference.** The system does not infer nationality, ethnicity, or religion from names. Any country attached to a finding comes from a list record (OFAC "Nationality" field, UN NATIONALITY element) — not from the name itself.

Limitations we accept:
- We cannot guarantee parity of *recall* across scripts until the golden dataset is balanced. The current seed corpus is 70% Latin-script; `fairness_tests/test_name_scripts.py` is the gating control until the imbalance is fixed.

---

## 9. Security, confidentiality, and integrity controls

| Control | Mechanism |
|---|---|
| Authentication | OAuth2 password flow (`auth.py`); JWT session tokens in-browser |
| Authorisation | Role matrix: analyst / reviewer / admin — enforced per endpoint in `main.py` |
| Audit trail | `audit.jsonl` with SHA-256 hash chain; `storage.AuditEvent` rows mirror for query |
| Encryption in transit | TLS terminated upstream (customer infra) |
| Encryption at rest | SQLite + filesystem-level encryption (buyer responsibility in v2.3) |
| Secret management | `.env` + environment variables; no secrets in repo |
| Evidence packet integrity | SHA-256 checksums on every file + audit-chain tamper check included in ZIP |
| LLM data residency | Gemma API hosted by Google; no customer data is sent except the target-public-URL content already on the public web |
| PII minimisation | The system processes entity names and publicly published text; no SSNs, TINs, beneficial-ownership data, or transaction data |

Full controls mapping is in `soc2_readiness.md`.

---

## 10. Approved uses and prohibited uses

### Approved
- Enhanced due diligence of existing or prospective customers by a licensed AML officer.
- Periodic refresh review of a customer portfolio for newly surfaced adverse media.
- Investigation of an alert already raised by the buyer's primary screening system.
- Training and onboarding of new analysts using redacted historical cases.

### Prohibited
- Primary sanctions screening replacement.
- Automated account rejection based solely on a SanctionSight brief.
- Consumer-facing or non-analyst access (the brief is not written for the general public and may contain sensitive allegations).
- Use on individuals for non-sanctions purposes (credit decisioning, employment screening, etc.) — explicitly unsupported and unvalidated.

---

## 11. Change history

| Date | Version | Change | Approver |
|---|---|---|---|
| 2026-04-18 | 2.3-phase6 | Initial model card publication (SR 11-7 validation package) | Head of Compliance Engineering |

Future amendments must follow `governance.md` §3 ("Model card revision gate"). No silent edits.

---

## 12. Contact

Questions, validation artefacts, or escalations: **enquiries@sanctionsight.com**
Incident response: see `runbooks/` in this directory.
