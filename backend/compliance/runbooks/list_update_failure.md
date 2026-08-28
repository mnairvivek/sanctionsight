# Runbook: Sanctions List Update Failure

**Scenario:** The scheduled `update_lists.py` run failed to refresh one or more sanctions lists. Findings produced during the staleness window reference a `ListSnapshot` older than the current official designations.

**Severity classification:**
- SEV-3 if a single list source is stale for < 24 hours (normal weekend / holiday / upstream flakiness).
- SEV-2 if any list is stale > 36 hours, or if OFAC or OpenSanctions specifically is stale > 12 hours.
- SEV-1 if the staleness coincides with a newly published designation that materially affects an active case.

OFAC and OpenSanctions have the tightest SLAs because they are the densest sources — most active designations flow through one or the other first.

---

## Key architectural fact

**Every finding in the system is joined to the `ListSnapshot` that was active when analysis ran.** A stale snapshot does not produce wrong historical findings — it just means *today's* analyses cannot see designations made after the snapshot's `active_from` timestamp.

This matters for the disclosure path: when a list catches up, the system does not retroactively re-score old jobs. It offers the analyst queue a re-run button, and the buyer decides whether to exercise it.

---

## Detection

Signals from most to least reliable:

| Signal | Location | Threshold |
|---|---|---|
| `_last_updated.txt` mtime | `backend/data/_last_updated.txt` | > 36 hours |
| `ListSnapshot.active_from` max age per `list_name` | DB query | > 36 hours |
| `update_lists.py` exit code | CI / cron scheduler | non-zero |
| File size mismatch after download | `download_file` > 0.1 KB check | `_size_kb < 0.1` log line |
| OFAC / OpenSanctions / OFSI public status pages | External | Manual check |

A recommended monitoring SQL query:

```sql
SELECT list_name,
       MAX(active_from)                AS latest_snapshot,
       NOW() - MAX(active_from)        AS age
  FROM list_snapshots
 GROUP BY list_name
 ORDER BY age DESC;
```

Any row with `age > INTERVAL '36 hours'` should page.

---

## Immediate response

1. **Acknowledge within 15 min.** Log `audit_event_type=list_staleness_detected`.

2. **Re-run the downloader manually.**
   ```bash
   cd sanctions-tool/backend
   python update_lists.py
   ```
   Watch for per-list success / failure lines. Network blips resolve themselves; a persistent failure points at an upstream URL change.

3. **If a single list failed:** identify which one from the script output and try the per-list URL in a browser. Upstream sources move — OFAC reorganised their `downloads/` path in Q1 2025 (`cons_prim.csv` vs `cons.csv`), OFSI restructured in 2024. The fix is usually a URL update.

4. **If OpenSanctions failed:** the bulk FtM file is ~200MB. Common causes:
   - Transient 502 from Cloudflare — retry with exponential backoff.
   - Network-level timeout on the buyer's egress — lift the 120s timeout in `download_opensanctions`.
   - CC BY-NC-4.0 attribution URL change — check `https://data.opensanctions.org/datasets/latest/sanctions/`.
   - Schema change in the FtM JSONL — the `opensanctions_client.parse_entity` function guards against this; if its accepted-entity count collapses, log the `load_stats()` output and open a Tier B ticket.

5. **If EU list failed:** the EU token URL (`token=dG9rZW4tMjAxNw`) rotates periodically. The downloader logs `(EU list may require updated token — check manually)` for exactly this. Pull the current token from the EU Financial Sanctions Database (`webgate.ec.europa.eu/fsd/`) public documentation and update `EU_URL` in `update_lists.py`. Tier B change, log the new URL in the governance log.

6. **If US CSL (trade.gov) failed:** the DEMO_KEY is rate-limited. If CSL is staling repeatedly, buyer needs to provision a real trade.gov API key and set `CSL_API_KEY` env var (support may need to be added — file a Tier B ticket).

---

## After repair

Once `update_lists.py` succeeds:

1. **Confirm ListSnapshot rows were written.** The script instantiates `SanctionsListScreener(DATA_DIR)` at the end specifically to trigger `_record_list_snapshots`. Query:
   ```sql
   SELECT list_name, downloaded_at, entity_count, sha256
     FROM list_snapshots
    WHERE downloaded_at > NOW() - INTERVAL '1 hour';
   ```
   Every list from `_loaders` should have a fresh row (or an unchanged-hash no-op if the upstream file didn't change — that's also fine).

2. **Capture the delta.** For each list with a new hash, diff the entity count vs the prior snapshot:
   ```sql
   SELECT list_name, entity_count,
          LAG(entity_count) OVER (PARTITION BY list_name ORDER BY active_from) AS prior_count
     FROM list_snapshots
    WHERE downloaded_at > NOW() - INTERVAL '48 hours';
   ```
   - A > 10% drop in entity count is a **red flag** — the upstream may have served a truncated file. Investigate before trusting the snapshot.
   - A > 5% increase is normal after a major designation round (OFAC batch, EU CFSP renewal).

3. **Notify buyers of staleness impact.** If the staleness was > 36 hours, write a one-paragraph bulletin for affected buyers:

   > SanctionSight's <list_name> snapshot was stale from <start> to <end> UTC due to <cause>. Analyses completed in that window reference the prior snapshot and may not reflect designations made during the gap. A refresh has now been applied. Buyers with active cases should review any designated entities added to <list_name> during this window.

4. **Offer opt-in re-analysis.** The admin UI has a "Reopen → Re-run against current lists" action. Do not force re-runs — they invalidate cases the analyst team may have already dispositioned.

---

## If staleness exceeds 72 hours

At 72 hours the tool is materially degraded. Escalation path:

1. Head of Compliance Engineering declares a service advisory.
2. A banner is shown in the UI (wire this up via `main.py` `/api/health`):
   > **Sanctions list refresh degraded.** Current snapshot is N hours old. New findings may not reflect designations made since <timestamp>.
3. Buyers are notified in writing with an SLA-clock start time for the advisory.
4. Cases already in `signed_off` state are not auto-reopened. Buyers must explicitly request regeneration.

The advisory banner is a Tier B change but has a standing pre-approved template — it can be flipped by on-call without additional governance review.

---

## Preventive controls

| Control | Cadence | Owner |
|---|---|---|
| `update_lists.py` scheduled run | Daily at 04:00 UTC | Infra / cron |
| Staleness alarm | Every 15 min | Monitoring |
| Per-list size-delta sanity check | On each download | `download_file` |
| Weekly manual OFAC cross-check | Weekly | Compliance ops |
| Upstream URL change review | Monthly | Engineering Owner |

The monthly upstream URL review catches the "EU rotated the token three weeks ago" class of incident before it produces staleness.

---

## What *not* to do

- **Do not** disable the staleness alarm to stop pages. The alarm is the regulator-facing evidence that we detect and respond to this failure class.
- **Do not** retroactively modify the `active_from` timestamp to paper over a gap. That is falsification of the audit trail.
- **Do not** skip the `ListSnapshot` recording step when applying a hotfix download. The snapshot is what the findings join against; without it they will reference a stale snapshot forever.
- **Do not** delete the stale snapshot file after a refresh. Historical jobs reference it; deletion breaks evidence-packet regeneration for old cases.
