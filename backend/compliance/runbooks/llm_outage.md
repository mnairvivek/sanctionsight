# Runbook: LLM Outage

**Scenario:** The Gemma API is unavailable, rate-limited, returning malformed responses, or producing briefs that fail the `claim_verifier` citation check. The decision-material core of the system (retrieval + scoring + evidence) is unaffected, but the *brief* — the analyst-facing narrative layer — cannot be generated.

**Severity classification:**
- SEV-2 on initial detection (analysts can still work off the evidence packet without a brief).
- SEV-1 if combined with a queue backlog that prevents reviewer sign-off within buyer SLAs.

---

## Key architectural fact

**SanctionSight is not decision-coupled to the LLM.** The brief is narrative synthesis; the scoring, evidence, citations, and audit chain all stand on their own. An LLM outage does not invalidate prior findings or block analysts from dispositioning them.

This runbook's goal is graceful degradation, not emergency model-swap heroics.

---

## Detection

Signals from most to least reliable:

| Signal | Location | Threshold |
|---|---|---|
| `audit.jsonl` `event_type=llm_error` | Audit log | > 5% of requests in 15 min |
| Gemma API 5xx rate | `sanctions_engine.py` `InvestigatorBriefGenerator` log lines | > 10% in 5 min |
| `claim_verifier` rejections | `claim_verifier.py` reject counter | > 20% of briefs in 15 min |
| Analyst reports "brief missing" | Support ticket | Any |

The `claim_verifier` metric is the most decision-useful — it catches the silent failure where Gemma returns JSON but hallucinates citations.

---

## Immediate response

1. **Acknowledge the page within 15 min.** Log an `incident_opened` audit event.

2. **Check upstream status.** `https://status.cloud.google.com/` — look for Gemma / Vertex / Generative AI entries. If a confirmed upstream incident, your work is coordination, not code.

3. **Enable fallback briefs.** Set env var `SANCTIONSIGHT_BRIEF_FALLBACK=evidence_only` and restart. This produces briefs that contain:
   - The structured evidence (URLs, excerpts, per-country counts) exactly as the scorer produced them.
   - A standard cover-page note: *"Narrative synthesis unavailable — brief contents are machine-assembled from the structured evidence. Analyst review required as usual."*
   - No LLM-authored paragraphs.

   The HITL workflow is unchanged. Analysts still disposition findings; reviewers still sign off; evidence packets still export. The brief just looks different.

4. **Confirm the fallback is being served.** Trigger a test job and inspect the returned brief's `generator: "fallback_evidence_only"` field.

5. **Notify buyers** whose current jobs-in-flight will receive fallback briefs. Template notice:

   > Between <start> and <end> UTC the narrative-synthesis layer of SanctionSight was unavailable due to an upstream incident. Jobs completed during this window received evidence-only briefs. All findings and their underlying evidence are unaffected. No re-run is required; if you prefer a full narrative brief once service is restored, use the **Reopen → Regenerate Brief** admin action.

---

## Sustained outage (> 4 hours)

If the upstream incident extends, or if the failure mode is not a pure outage but a quality regression (e.g. a model update producing briefs that fail citation verification):

1. **Evaluate model pinning.** The `LLM_MODEL` env var is explicitly for this. If the outage is a new model version, pin to a prior model snapshot:
   ```bash
   export LLM_MODEL=gemini-2.5-flash-preview-09-2025
   ```
   This is a Tier B operational change (no decision-surface impact) but must be logged in the governance log.

2. **Evaluate alternate provider.** If upstream is expected to remain down for > 24 hours, the project has a documented alternate path through `vertex_genai` — see `sanctions_engine.py` `_google_genai` comments. **This is a Tier A change** because the prompt / output schema compatibility between providers has not been validated against the golden dataset. It requires:
   - Compliance Reviewer + Head of Compliance Engineering sign-off.
   - A subset of the golden dataset (brief-quality cases) re-run against the alternate provider with manual brief-quality review.
   - Explicit buyer notification before any tenant is switched.

   **Do not** flip the provider in an emergency without this process. The risk of regulator-visible brief quality drift exceeds the cost of prolonged fallback-brief mode.

3. **Keep fallback-brief mode running** until the primary path is fully restored AND the `claim_verifier` rejection rate is back below the 5% floor for at least 2 hours of steady traffic.

---

## Citation-verification failure (different from API outage)

If `claim_verifier.py` is rejecting Gemma briefs because citations do not ground in any stored excerpt, the LLM is **still working** but producing hallucinated claims. This is more dangerous than a clean outage because without the verifier it would ship.

1. **Confirm the verifier is catching the issue.** The verifier writes `audit_event_type=brief_rejected_citation_mismatch` rows. The count of these is the leading signal.

2. **Do not disable the verifier.** It is a gating control. If you are tempted to disable it to clear a backlog, stop — you are about to ship unverified claims to a regulated workflow.

3. **Fall back to evidence-only briefs** (same mechanism as an API outage).

4. **Root-cause.** Common causes:
   - Prompt change that no longer instructs Gemma to cite excerpt IDs.
   - Excerpt ID scheme changed and the prompt still references the old scheme.
   - Gemma model update that became less instruction-following.
   - Input evidence that contains JSON-like content confusing the response parser.

5. **Apply the smallest-possible fix** and re-run the validation harness and a brief-quality sample before re-enabling the LLM path.

---

## Post-incident

- Incident report in `governance_log/YYYY-MM-DD-llm-<incident-id>.md`.
- Update monitoring thresholds if the incident surfaced a blind spot.
- If fallback briefs were served, confirm with each affected buyer whether they want any completed jobs regenerated once the primary path is restored. Regeneration is opt-in — we do not silently rewrite delivered briefs.

---

## What a "fallback brief" looks like end-to-end

```
Target: example.com
Jurisdictions analysed: Iran, Russia, North Korea
Model version: sanctionsight-v2.3-phase6 (fallback brief — LLM unavailable)

Evidence summary:
  Iran       3 HIGH / 1 MEDIUM / 0 LOW    (cf. excerpts 1-4)
  Russia     0 HIGH / 2 MEDIUM / 1 LOW    (cf. excerpts 5-7)
  North Korea  0 HIGH / 0 MEDIUM / 0 LOW  (no findings)

Citations:
  [1] news-outlet.example / 2026-01-14 / "The firm's Tehran representative office has processed …"
  [2] …

Recommendation: manual review required — see individual findings.
```

Every excerpt, citation, and recommendation is the analyst-facing output the system produced *anyway*. The LLM's role is to weave narrative around this structured core; if it cannot, the core is delivered raw. No decision downgrade.
