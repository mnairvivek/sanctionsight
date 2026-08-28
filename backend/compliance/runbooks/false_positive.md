# Runbook: False Positive

**Scenario:** A finding is surfaced at a risk tier (HIGH or MEDIUM) that an analyst determines is not a genuine sanctions concern. Normal case — expected to happen at a controlled rate. Becomes an incident when the rate exceeds the ceiling in `model_card.md` §6, or when a single case receives legal or customer escalation.

**Severity classification:**
- SEV-3 for a single routine FP (handled by analyst disposition, no engineering action).
- SEV-2 when the FP rate over a 7-day window crosses the HIGH-FPR ceiling for any tier.
- SEV-1 when a FP has caused material customer impact (wrongful account closure, public-facing allegation in a shared evidence packet, regulator complaint).

---

## Routine path (SEV-3)

No engineering action. Analyst:

1. Transitions the finding from `pending` → `in_review`.
2. Dispositions as `cleared_fp` with a free-text `reason` naming the root cause (e.g. "shipping policy disclaimer, not a business relationship").
3. Reviewer signs off the case when no `pending` findings remain.

The disposition reasons are **the primary input to systemic FP tuning** — they are audited monthly and feed the per-quarter false-positive-phrase review.

---

## Rate-exceeded response (SEV-2)

Trigger: the monitoring dashboard or a manual query shows HIGH-FPR > ceiling for a tier over 7 days (Critical ≤ 0.15 / Material ≤ 0.20 / Advisory ≤ 0.25).

1. **Acknowledge and scope.** Page on-call. Run:
   ```sql
   SELECT country, risk_type, COUNT(*) AS fp_count
     FROM findings f
     JOIN finding_states fs ON fs.finding_id = f.id
    WHERE fs.to_status = 'cleared_fp'
      AND fs.transitioned_at > NOW() - INTERVAL '7 days'
    GROUP BY country, risk_type
    ORDER BY fp_count DESC LIMIT 20;
   ```
   Identify the top (country, risk_type) pair — it is almost always 80% of the volume.

2. **Read the disposition reasons.** Pull the `reason` strings for the top bucket. The pattern will usually point at one of:
   - A false-positive phrase not yet in the suppression list (e.g. a new viral slang term).
   - A scoring rule firing on a compliance disclaimer.
   - An extractor pulling boilerplate from a CMS template across many tenants' pages.
   - A genuine regional concentration of cultural-vocabulary false positives (see the fairness suite's Cuba / Persian / Korean cases).

3. **Short-term mitigation.** Options, in order of preference:
   - Add a false-positive phrase to `_analyze_context` — lowest blast radius.
   - Adjust a keyword weight for a specific (country, risk_type) — Tier A but scoped.
   - Tighten the HIGH-tier threshold (e.g. 80 → 85) for one risk_type — Tier A, measurable.
   - Rate-limit a specific query template in retrieval — Tier B.

   Do **not** mass-delete existing findings. The audit trail is sacred.

4. **Validate.** Re-run the golden-dataset harness and the fairness suite. Both must pass.

5. **Ship under Tier A governance** (same process as false-negative fixes).

6. **Notify buyers** whose tenants were materially affected by the mitigation — specifically those whose analysts spent non-trivial time on the FP bucket. The disposition reasons are the input; no PII leaves the tenant.

---

## Material-impact response (SEV-1)

Trigger: a FP reached a customer or regulator in a form that caused harm. Examples:
- An evidence packet was delivered to a buyer's counsel with an incorrect HIGH-tier allegation.
- A buyer's downstream decision (account closure, SAR filing) rested on the FP.
- The FP appears in regulatory correspondence.

1. **Page the Head of Compliance Engineering immediately.** Not the on-call — this needs the senior accountable person in the loop.

2. **Do not silently delete or modify the offending record.** Any correction is a new audit event; the prior state is preserved. Editing history is a breach of `governance.md` §7.

3. **Issue a corrective record.** The HITL workflow supports `reopened` state for exactly this purpose — an admin re-opens the signed-off case, the new analyst disposition supersedes the prior one, and the audit chain records both.

4. **Draft the customer disclosure** within 24 hours. Template:
   - What finding was incorrect.
   - What corrective action has been taken in the tool.
   - What the correct assessment is.
   - What the customer should do (re-evaluate, reverse decision, notify affected downstream parties).

5. **Root-cause the miss.** The SEV-1 path implies the tool produced output that a reviewer signed off on when it shouldn't have. That means *either* the scoring was wrong *or* the reviewer process was wrong. Both need interrogation.
   - Engineering: was a Tier A change that should have been caught by the harness released? Check the pre-release harness output for that `ModelVersion`.
   - Process: did the reviewer override the pending-findings block? Review the audit trail for anomalous transitions.

6. **Post-mortem within 10 business days.** Blameless, written, filed in `governance_log/`. Regulator-facing if applicable.

---

## Preventive controls that catch most FPs early

| Control | Cadence | Location |
|---|---|---|
| Golden-dataset HIGH-FPR gate | Pre-release | `compliance/validation/run.py` |
| Fairness suite per-country cap | Pre-release | `compliance/fairness_tests/test_fp_by_country.py` |
| Copy-audit tests | Pre-release | `tests/unit/test_copy_audit.py` |
| Analyst disposition review | Weekly | Compliance ops |
| Per-(country,risk_type) FP rate dashboard | Daily | (monitoring, not in repo) |
| Quarterly FP-phrase list audit | Quarterly | Compliance ops |

The analyst disposition review is the single most effective control — it converts individual noise into systemic signal within 7 days.

---

## What *not* to do

- **Do not** add a broad "downgrade everything in a shipping policy" rule. It will mask real findings. Specific phrases only.
- **Do not** adjust the HIGH threshold globally. Do it per risk_type, measured, governed.
- **Do not** retroactively clear FPs across a tenant via a script. Every clear needs a human disposition with a reason. That's the regulator-facing defence.
- **Do not** remove the offending URL from Google's index or from the snapshot store. The evidence that the system saw the page is part of the audit trail even when the finding was wrong.
