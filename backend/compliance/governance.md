# SanctionSight — Model Governance

**Version:** 2.3-phase6
**Effective:** 2026-04-18
**Owner:** Head of Compliance Engineering
**Authority:** This document, together with `model_card.md`, constitutes the governance artefact SanctionSight is supplied with. Buyers are expected to integrate these controls with their own model risk management framework.

---

## 1. Scope

Every component listed in `model_card.md` §2 (rules, thresholds, keyword tiers, prompts, schemas, list-refresh pipeline, HITL workflow, audit chain) is a **governed artefact**. Changes to governed artefacts require the approvals below before they reach a customer's production tenant.

Out of scope for this document:
- Cosmetic UI copy that does not appear in an evidence packet (covered by the Phase 4 copy-audit test suite).
- Build tooling, CI lint config, non-production test fixtures.
- Dependency *patch* upgrades when no behavioural change is observed in the validation harness.

---

## 2. Roles

| Role | Responsibility |
|---|---|
| **Engineering Owner** | Author of the change. Runs the validation harness, files the PR, attaches harness output. |
| **Compliance Reviewer** | Must be independent from the engineer. Reviews the change against the model card. Signs governance log. |
| **Validation Reviewer** | Runs the golden-dataset harness independently. Compares to baseline. Signs governance log. |
| **Head of Compliance Engineering** | Final approval for any Tier-A change (see §4). Accountable to the buyer. |
| **On-call** | Executes runbook actions; not a governance role but signs the incident closure. |

No single human occupies more than one role on the same change — two-person integrity is a hard requirement.

---

## 3. Change tiers

Changes are classified by blast radius, not by how much code they touch.

### Tier A — Decision-material
Anything that can alter a risk tier, a recommendation label, or a flagged/not-flagged outcome for a case.

Examples:
- Keyword tier weights in `EnhancedRiskAssessment` / `SanctionsContentAnalyzer`
- Fuzzy-match thresholds (`rapidfuzz` cutoff values)
- False-positive phrase list (`_analyze_context`)
- Country variations list (`_get_country_variations`)
- `SANCTIONED_ENTITIES` jurisdiction list
- Sanctions-only term list (`SANCTIONS_ONLY_TERMS`)
- Brief-generation prompt in `InvestigatorBriefGenerator`
- Pydantic output schema for the brief
- HITL state machine transitions
- Audit-chain hash function or log schema
- Evidence packet manifest contents
- spaCy model promotion (e.g. `_lg` → `_trf`)

**Required approvals:** Engineering Owner + Compliance Reviewer + Validation Reviewer + Head of Compliance Engineering.
**Validation:** Full golden-dataset harness pass. No Critical-tier recall drop > 0.02. No HIGH-FPR rise > 0.05. Fairness suite passes.
**Rollout:** Staged — internal tenant → one pilot tenant → general availability. Minimum 14 days between stages.
**Rollback plan:** Mandatory and rehearsed. `ModelVersion` row enables tenant-scoped pinning.

### Tier B — Operational
Changes that affect *how* the system runs without altering its decision surface.

Examples:
- List-source URL updates (OFAC / UN / OFSI endpoint moves)
- Retry / backoff / timeout tuning
- Logging field additions (non-PII)
- Storage migrations that are additive-only
- New list-loader for a dataset already covered by OpenSanctions dedup

**Required approvals:** Engineering Owner + Compliance Reviewer.
**Validation:** Unit + integration suite passes. Smoke run of the demo script.
**Rollout:** Standard deploy.
**Rollback plan:** Git revert + redeploy is sufficient.

### Tier C — Administrative
UI copy (non-evidence-packet), dependency patch upgrades with no behavioural diff, internal docs, test-only changes.

**Required approvals:** Engineering Owner + one peer reviewer.
**Validation:** Unit suite + copy-audit test.
**Rollout:** Standard deploy.

---

## 4. Approval gate by artefact

| Artefact | Tier | File(s) |
|---|---|---|
| Brief prompt | A | `sanctions_engine.py` `InvestigatorBriefGenerator._build_prompt` |
| Brief output schema | A | `sanctions_engine.py` Pydantic schema; `schemas.py` |
| Risk-tier keyword weights | A | `sanctions_engine.py` `SanctionsContentAnalyzer._analyze_context` |
| Country variations | A | `sanctions_engine.py` `_get_country_variations` |
| Sanctioned entities list | A | `main.py` `SANCTIONED_ENTITIES`, `sanctions_engine.py` matching list |
| False-positive phrases | A | `sanctions_engine.py` false-positive array |
| Rapidfuzz thresholds | A | `sanctions_list_screener.py:701` + `NameCooccurrenceSearcher` |
| spaCy model selection | A | `SANCTIONSIGHT_SPACY_MODEL` env, `_load_spacy_model` |
| HITL state machine | A | `main.py` state-transition endpoints; `storage.FindingState` |
| Audit chain | A | `audit.py` hash computation |
| Evidence packet contents | A | `evidence_packet.py` |
| List-snapshot recording | A | `sanctions_list_screener._record_list_snapshots` |
| OpenSanctions dedup set | A | `opensanctions_client.ALREADY_COVERED_DATASETS` |
| Language detection threshold | B | `sanctions_engine.detect_language` `min_chars` |
| List-download URLs | B | `update_lists.py` |
| Google CSE query templates | B | `perform_enhanced_site_search` |
| Retry/backoff config | B | `with_retry` decorator arguments |
| UI components (non-evidence) | C | `frontend/src/components/*` |
| Landing page copy | C | `sanctions-tool/index.html` (audited by `test_copy_audit.py`) |

---

## 5. Model card revision gate

The model card is a point-in-time declaration. Revising it requires:

1. A PR that edits `backend/compliance/model_card.md` **and bumps §11 "Change history" with a new row** (date, version, change, approver).
2. Compliance Reviewer sign-off on the PR.
3. A companion entry in the `governance_log/` directory (see §7) containing the full change justification.
4. If the change reflects a Tier A behavioural change, the Tier A approval flow above takes precedence and runs first — the model card is updated as a consequence, not as a substitute.

Silent edits to the model card are a breach of this policy and are reversible by any reviewer without further approval.

---

## 6. Emergency change procedure

If a live defect is producing materially wrong risk tiers or missing evidence in a regulator-visible context:

1. On-call acknowledges the page within 15 minutes.
2. On-call may disable the affected component (feature flag off, LLM fallback to evidence-only brief, thresholds reverted to last-known-good) **without prior approval**. This is logged as `audit_event_type=emergency_override` with the on-call's actor id.
3. Within 24 hours of the override, a Tier A retrospective change ticket is filed. Compliance Reviewer + Head of Compliance Engineering sign off on the permanent fix.
4. Buyers whose tenants were affected are notified within 72 hours, with a written incident summary.

The emergency procedure is *narrow* — it reverts, it does not introduce new behaviour. A new rule, threshold, or keyword cannot enter production via this path.

---

## 7. Governance log

Every Tier A or Tier B change generates an entry in `backend/compliance/governance_log/YYYY-MM-DD-<slug>.md` containing:

- Change summary (1 paragraph)
- Tier classification + justification
- Affected artefacts (file paths)
- Validation harness output (attached or linked)
- Fairness suite output
- Approvers (GitHub usernames or compliance-ticket IDs)
- Rollout plan and rollback rehearsal evidence
- Post-deploy verification steps + who performed them

The log is append-only. Corrections are made by adding a new entry that references the prior one — never by editing history.

---

## 8. Periodic reviews

| Review | Cadence | Owner | Output |
|---|---|---|---|
| Golden-dataset recalibration | Quarterly | Validation Reviewer | New CSV rows; re-baselined thresholds |
| Fairness suite audit | Quarterly | Compliance Reviewer | Per-script recall, per-country FP delta report |
| Prompt regression replay | Monthly | Engineering Owner | Brief diff on 50 reference cases across model versions |
| List-snapshot staleness audit | Weekly | On-call | All `ListSnapshot.active_from` < 7 days |
| Access-control review | Quarterly | Buyer's IT/GRC (shared responsibility) | User-list reconciliation + role audit |
| Dependency vulnerability scan | Weekly | Engineering Owner | `pip-audit` clean; blocking CVEs triaged within 72h |
| SR 11-7 self-attestation | Annually | Head of Compliance Engineering | Signed statement sent to buyers |

Misses on any cadence are a Tier B change in their own right and require a governance-log entry explaining why and when it will be caught up.

---

## 9. Buyer responsibilities

SanctionSight provides the tool, the model card, this governance document, and the validation harness. The following are the **buyer's responsibility** (not ours):

- Primary sanctions-list screening workflow.
- User provisioning and role assignment.
- Tenant-level authentication integration (SSO).
- Encryption at rest (filesystem or database-level).
- Long-term audit-log retention beyond the default 7-year retention window.
- Incident disclosure to regulators.
- Integration with the buyer's internal model risk management committee.

The `soc2_readiness.md` checklist is explicit about which controls are ours versus the buyer's.

---

## 10. Versioning of this document

This document is governed by itself (Tier C change, but with mandatory Compliance Reviewer sign-off — the Tier ceiling is elevated for any document that defines approval gates). Revisions bump the version string at the top and append a row to the change history below.

### Change history

| Date | Version | Change | Approver |
|---|---|---|---|
| 2026-04-18 | 2.3-phase6 | Initial publication (SR 11-7 validation package) | Head of Compliance Engineering |
