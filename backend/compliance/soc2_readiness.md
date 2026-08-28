# SOC 2 Type II Readiness Checklist

**Target audit window:** Q3 2026
**Trust Services Criteria in scope:** Security (CC), Availability (A), Confidentiality (C), Processing Integrity (PI). Privacy (P) is out-of-scope at this stage (no consumer PII processed).
**Auditor:** TBD — shortlist pending.
**Auditable entity:** SanctionSight Ltd — the SaaS product and supporting infrastructure. Buyer-hosted deployments are out of scope of *our* audit; they inherit the buyer's own SOC 2.

This document is a **living checklist**. It tracks which controls are implemented, which are partially implemented, and which remain open for Q3. A control marked "done" still requires evidence (logs, screenshots, tickets) collected in the audit window.

---

## Legend

- `[✓]` Implemented and producing evidence.
- `[~]` Partially implemented — evidence incomplete or manual.
- `[ ]` Not yet implemented.
- `[buyer]` Not our responsibility — the buyer must evidence this on their side.

---

## Common Criteria (CC) — Security

### CC1: Control Environment

- `[✓]` **CC1.1** Organisation structure documented. Head of Compliance Engineering is accountable for governance; see `governance.md` §2.
- `[✓]` **CC1.2** Code of conduct / acceptable use policy. Inherits the employing entity's policy; referenced in the employment contract.
- `[~]` **CC1.3** Organisational commitment to competence. Engineering onboarding documented; formal competency matrix deferred to Q2.
- `[✓]` **CC1.4** Accountability enforced via two-person integrity on Tier A changes (`governance.md` §2).
- `[~]` **CC1.5** Performance management / discipline. Informal; formalise before audit window.

### CC2: Communication and Information

- `[✓]` **CC2.1** Security policy published — this document plus `governance.md` plus `model_card.md`.
- `[✓]` **CC2.2** Internal communications — incident channel, on-call rotation, weekly compliance review cadence.
- `[✓]` **CC2.3** External communications — `enquiries@sanctionsight.com`, public incident disclosure template in `runbooks/`.

### CC3: Risk Assessment

- `[✓]` **CC3.1** Risk identification — documented in `model_card.md` §4 (known limitations) and §8 (bias/fairness).
- `[✓]` **CC3.2** Risk assessment cadence — quarterly per `governance.md` §8.
- `[~]` **CC3.3** Third-party risk — Google Gemma API, Google CSE, OpenSanctions, OFAC/UN/OFSI/EU sources. Due-diligence one-pagers drafted but not yet in the vendor management record. Target: July 2026.
- `[✓]` **CC3.4** Fraud risk — role separation + audit chain. The audit chain's hash-chained append-only log is designed to detect tampering; see `audit.py`.

### CC4: Monitoring Activities

- `[✓]` **CC4.1** Ongoing monitoring — the audit chain is the system of record. List-staleness alarms, LLM-error rate, and FP-rate dashboards operational.
- `[~]` **CC4.2** Separate evaluations — pen test scheduled for May 2026. Internal validation runs quarterly per `governance.md` §8.

### CC5: Control Activities

- `[✓]` **CC5.1** Control selection documented in `governance.md` §3-4.
- `[✓]` **CC5.2** Technology controls — authentication, RBAC, audit chain, data encryption in transit, list snapshot hashing.
- `[✓]` **CC5.3** Policies deploying controls — `governance.md`, `model_card.md`, this document.

### CC6: Logical and Physical Access Controls

- `[✓]` **CC6.1** Logical access restrictions — OAuth2 password flow, JWT session tokens, role-based endpoint gating (`main.py`).
- `[~]` **CC6.2** Registration / authorisation of new users — manual today via admin UI. SSO integration per buyer; see `[buyer]` rows below.
- `[✓]` **CC6.3** Removal of access — admin can soft-delete users; `storage.User.disabled` flips the auth check.
- `[~]` **CC6.4** Physical access — all infrastructure is cloud-hosted. Inherits provider controls; need vendor attestations filed.
- `[ ]` **CC6.5** Data transmission — TLS terminated at the load balancer. Production-ready SSL config not yet hardened for HSTS / cipher-suite policy. Target: May 2026.
- `[✓]` **CC6.6** Protection from unauthorised software — dependency lock file; CI pip-audit; no arbitrary package install in production.
- `[✓]` **CC6.7** Data movement — evidence packets SHA-256 checksummed; audit chain integrity check before packet finalisation.
- `[buyer]` **CC6.8** Encryption at rest — buyer-hosted SQLite or managed database is out of our scope.

### CC7: System Operations

- `[✓]` **CC7.1** Detection of vulnerabilities — pip-audit weekly, documented in `governance.md` §8.
- `[✓]` **CC7.2** Monitoring for anomalies — audit chain anomaly detection on hash-chain breaks.
- `[✓]` **CC7.3** Evaluation of security events — incident response via `runbooks/`.
- `[✓]` **CC7.4** Incident response plan — four runbooks in `runbooks/`. Tabletop exercise scheduled for June 2026.
- `[~]` **CC7.5** Business continuity — backup strategy documented in `backup.py`; offsite copy cadence not yet automated.

### CC8: Change Management

- `[✓]` **CC8.1** Change management — `governance.md` §3 defines tiers; every Tier A/B change requires a governance log entry.

### CC9: Risk Mitigation

- `[✓]` **CC9.1** Risk mitigation activities — golden-dataset validation, fairness suite, copy-audit, LLM fallback.
- `[✓]` **CC9.2** Vendor risk management at the procurement stage. Due-diligence docs in progress.

---

## Availability (A)

- `[~]` **A1.1** Capacity planning — current architecture comfortably serves the pilot customer cohort; load testing before GA expansion.
- `[✓]` **A1.2** Environmental protections — cloud-hosted; inherits provider SLA.
- `[✓]` **A1.3** Recovery — evidence packets are the primary recovery artefact; jobs can be re-run deterministically from a URL list. Database backup via `backup.py`.

---

## Confidentiality (C)

- `[✓]` **C1.1** Data classification — no classification beyond "public-web scraped content" and "customer tenant metadata." No PII, no transaction data.
- `[✓]` **C1.2** Disposal of confidential information — tenant-level data purge on contract termination; procedure documented (not yet automated).

---

## Processing Integrity (PI)

This is where the validation package does the heavy lifting. Most PI controls are already covered by the other documents in `backend/compliance/`.

- `[✓]` **PI1.1** Data input quality — list-snapshot hashing, source-URL hashing, stored text snapshots.
- `[✓]` **PI1.2** System processing defined — `model_card.md` §2 pipeline diagram and component inventory.
- `[✓]` **PI1.3** Processing accuracy — golden-dataset validation gates (`compliance/validation/`).
- `[✓]` **PI1.4** Output completeness — evidence packets include the full finding set with per-excerpt hashes. Audit chain records every brief generated.
- `[✓]` **PI1.5** Output accuracy — claim verifier enforces citation grounding; fallback brief path available.

---

## Privacy (P) — out of scope

No consumer PII is processed. Subject names handled are those already designated publicly on sanctions lists or appearing in public web content. Privacy TSCs will be considered for a future audit iteration if the product expands into individual-screening contexts.

---

## Evidence-collection plan for the audit window

| Evidence type | Source | Frequency | Retention |
|---|---|---|---|
| Audit log samples | `audit.jsonl` + `AuditEvent` table | Continuous | 7 years |
| `ListSnapshot` rows | Database | Continuous | 7 years |
| Validation harness runs | `compliance/validation/run.py` output JSON | Per release | 3 years |
| Fairness suite runs | `pytest compliance/fairness_tests/` output | Per release | 3 years |
| Governance log entries | `compliance/governance_log/` | Per change | Indefinite |
| Incident reports | `compliance/governance_log/` | Per incident | Indefinite |
| Access reviews | Quarterly signed PDF | Quarterly | 7 years |
| Pen test reports | External vendor | Annual | Indefinite |
| Vendor attestations (Google, etc.) | Vendor | Annual | Indefinite |

---

## Open items before auditor kickoff

Sequenced by dependency:

1. **Q2 2026 (by end of May):** HSTS / cipher policy, formal competency matrix, automated offsite backup cadence, vendor due-diligence record complete.
2. **Q2 2026 (by end of June):** Incident response tabletop exercise, pen test engagement kicked off, trust centre / security page on the public site.
3. **Q3 2026 (audit kickoff):** Auditor readiness review. All `[~]` rows should flip to `[✓]` by this date.

Closure of this checklist is tracked in `governance_log/` under the tag `soc2-readiness`. Each row's flip from `[~]` / `[ ]` to `[✓]` requires an evidence pointer and a date in the corresponding governance log entry.

---

## Shared-responsibility summary

Buyers receive a shared-responsibility matrix at contract signing listing which controls we own and which they own for their tenant. The `[buyer]` tagged rows above are the headline items. The full matrix covers:

- Authentication integration (SSO)
- User provisioning
- Network perimeter
- Data retention policy per their regulator
- Long-term audit-log retention beyond our default
- SAR / regulator disclosure obligations

This is referenced in §9 of `governance.md` ("Buyer responsibilities").

---

## Change history

| Date | Change | Approver |
|---|---|---|
| 2026-04-18 | Initial publication alongside the SR 11-7 validation package | Head of Compliance Engineering |
