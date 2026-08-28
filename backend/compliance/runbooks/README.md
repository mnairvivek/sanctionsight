# Runbooks

Incident-response procedures for SanctionSight production incidents. Each runbook is self-contained: it names the scenario, severity classification, detection signals, immediate response, permanent-fix path, and preventive controls.

## Index

| Runbook | Scenario | Typical severity |
|---|---|---|
| [`false_negative.md`](false_negative.md) | Sanctions-relevant connection existed on the open web but was not surfaced | SEV-1 / SEV-2 |
| [`false_positive.md`](false_positive.md) | Finding flagged at HIGH/MEDIUM is not a genuine concern | SEV-3 routine / SEV-2 rate-exceeded / SEV-1 material impact |
| [`llm_outage.md`](llm_outage.md) | Gemma API unavailable, degraded, or producing hallucinated citations | SEV-2 default / SEV-1 with queue impact |
| [`list_update_failure.md`](list_update_failure.md) | `update_lists.py` failed — findings reference a stale snapshot | SEV-3 < 24h / SEV-2 > 36h / SEV-1 with active-case impact |

## Common principles across all runbooks

1. **Acknowledge in 15 minutes.** Every runbook. Every severity. The acknowledgement is an audit-log event.
2. **Preserve evidence.** Never delete, never silently modify. Corrections are new events that supersede prior ones — they do not replace them.
3. **The HITL workflow is the backstop.** Most issues surface through it; most fixes flow back to it; the audit chain is how we prove it worked.
4. **Tier A changes need governance.** Even in an incident. The emergency override path in `../governance.md` §6 reverts; it does not introduce new behaviour.
5. **Notify buyers when material impact is possible**, not only when it is confirmed. Silence breeds surprise.

## Adding a new runbook

When you add a runbook:

- Use the existing four as structural templates (Scenario / Severity / Detection / Immediate response / Permanent fix / Preventive controls / What *not* to do).
- Add it to the index table in this README.
- Link it from any monitoring alert it supports.
- File a one-line Tier C governance-log entry confirming it has been reviewed by Compliance.

## On-call expectations

On-call engineers must have read all four runbooks in the last 30 days. The on-call rotation calendar is maintained by Engineering Ops (not in this repo). Drill the "unfamiliar failure" scenario monthly — a runbook you have never read during an outage is a runbook that will be skimmed, not followed.
