# Runbook: False Negative

**Scenario:** A sanctions-relevant connection existed on the open web at the time of analysis, but SanctionSight did not surface it. Discovery path is typically: buyer's internal QA, regulator sample review, or a post-incident audit flagging a missed SDN hit.

**Severity classification:** SEV-1 if the missed finding would have changed a customer onboarding or exit decision. SEV-2 otherwise.

---

## Immediate response (first 60 minutes)

1. **Acknowledge the page.** On-call acknowledges within 15 minutes and logs a `audit_event_type=incident_opened` row with a reference to the escalating ticket.

2. **Confirm the miss is real.** Pull the original job from the database and re-run the pipeline against the specific URL that was missed:
   ```bash
   cd sanctions-tool/backend
   python -c "from sanctions_engine import get_analyzer, SanctionsContentAnalyzer; \
       a = get_analyzer('Iran'); \
       ext = SanctionsContentAnalyzer._shared().content_analyzer.extract_content_from_url('<url>'); \
       print(a.analyze_content(ext, '<url>'))"
   ```
   - If current code now flags it: the miss was a model-version issue. Capture the `ModelVersion` row used for the original run.
   - If current code also misses it: the miss is a scoring or retrieval issue, present in today's build too.

3. **Preserve evidence.** Snapshot the live page (`trafilatura.fetch_url` + save to `data/snapshots/incident-<id>.txt`). Google de-indexes, pages move — without a snapshot the post-mortem has no ground truth.

4. **Scope the blast radius.** Run:
   ```sql
   SELECT job_id, completed_at, workflow_status FROM jobs
    WHERE completed_at > '<2026-XX-XX>'
      AND <target_entity_or_url_filter>;
   ```
   to find other jobs that ran under the same `ModelVersion` and may have the same miss. Flag them to the buyer's analyst team for review.

---

## Root-cause triage

Work the decision tree top-to-bottom:

1. **Was the URL in the Google CSE result set?**
   Check `SearchResult` rows for the job. If the URL is absent, the retrieval layer never saw it.
   - **Fix path:** add a targeted query that would have retrieved it (sanctions-term variant, alternate spelling of entity name, broader snippet search).
   - **Systemic fix:** propose a new query template. This is a Tier A governance change — log it.

2. **Was the URL retrieved but extraction returned empty?**
   Check the job payload for `extraction_type=UNKNOWN` or `extraction_message` on this URL.
   - **Fix path:** improve the extractor (trafilatura fallback to cached HTML, JS-rendered page via Playwright — currently deferred).
   - **Short-term mitigation:** ensure the Google snippet fallback (`SNIPPET_FALLBACK`) is running and re-run the analysis.

3. **Was the content analysed but scored below threshold?**
   Check `relevant_excerpts` for the URL. If excerpts exist but `risk_level` is below MEDIUM, the scoring missed it.
   - Inspect the matched context window. Common causes:
     - False-positive phrase incorrectly downgraded the finding (e.g. the page contained "shipping policy" as boilerplate plus a real business mention).
     - The sanctioned-entity variation list didn't include the name used on the page (e.g. an NIOC subsidiary by its local trade name).
     - Negation window of 6 words clipped an important verb.
   - **Fix path:** propose the smallest-possible change (remove FP phrase, add entity variation, extend negation window). Measure impact on the golden dataset before shipping.

4. **Was the content scored correctly but the HITL workflow suppressed it?**
   Check `FindingState` history — was the finding ever in `pending` / `in_review`? Did an analyst `clear_fp` it?
   - If the analyst cleared it in error: this is a training issue, not a model issue. Update the evidence packet's disposition audit trail; no code change.
   - If the system never created the finding: back to step 3.

---

## Permanent fix

1. **Reproduce in a test.** Add a case to `compliance/validation/golden_dataset.csv` that encodes the missed scenario. Tag it with the incident ID in the `notes` column. If the fix is in retrieval, this goes to the integration test suite instead.

2. **Implement the fix.** Scoped to the smallest change that resolves the regression. Prefer adding a keyword / variation over rewriting scoring logic.

3. **Re-run the validation harness.**
   ```bash
   python -m compliance.validation.run --strict --json-out /tmp/incident-<id>.json
   ```
   The Critical-tier recall must not drop for other types — a targeted fix that boosts Iran recall by 0.05 but drops Syria recall by 0.03 is not acceptable without a governance discussion.

4. **Re-run the fairness suite.**
   ```bash
   pytest compliance/fairness_tests/ -v
   ```

5. **Ship under Tier A governance.** File the change, attach the harness JSON, obtain Compliance Reviewer + Validation Reviewer + Head of Compliance Engineering sign-off per `governance.md` §3.

6. **Backfill affected tenants.** Re-run any jobs completed under the buggy `ModelVersion` for affected customer segments. Surface the new findings to buyers with a written summary.

---

## Post-incident

Within 5 business days of closure:

- Write an incident report in `backend/compliance/governance_log/YYYY-MM-DD-fn-<incident-id>.md`.
- If the incident was reported by a regulator, prepare a customer-facing disclosure per the buyer contract (typically 72 hours; check the specific MSA).
- If the same root cause has occurred before, escalate to the Head of Compliance Engineering for a systemic review — two incidents from the same root cause is a pattern.

---

## Metrics that matter

- **Time to reproduce** — how long from page open to confirmed reproduction in a sandbox? Target ≤ 2 hours.
- **Time to fix in test** — how long from reproduction to a failing golden-dataset row? Target ≤ 1 business day.
- **Time to production** — target ≤ 5 business days including governance review. Emergency override path (see `governance.md` §6) is available if the miss is actively producing further SEV-1s.
