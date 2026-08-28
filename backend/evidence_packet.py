"""
Phase 3 — evidence packet bundler.

Produces a single ZIP per job containing everything a regulator or
downstream auditor needs to reproduce a case decision:

    report.html               human-readable HTML produced by the engine
    brief.json                investigator brief + verification report
    findings.csv              every Finding row with HITL disposition
    excerpts.jsonl            every persisted Excerpt (evidence text)
    snapshots/*               raw extracted page bodies (content-addressed)
    audit.jsonl               tamper-evident audit chain (source of truth)
    audit_verification.json   result of verify_chain at bundle time
    list_snapshots.json       sanctions list versions active when the
                              case ran (name, sha256, entity_count)
    model_card.md             ModelVersion row formatted for humans
    README.md                 tiny cover sheet describing each file

The bundle is built entirely from durable state (DB + JSONL + snapshot
files), so it can be regenerated long after the analysis itself has
completed.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("evidence_packet")

_BASE_DIR = Path(__file__).resolve().parent


def _snapshots_dir() -> Path:
    override = os.environ.get("SANCTIONSIGHT_SNAPSHOTS_DIR")
    return Path(override) if override else _BASE_DIR / "snapshots"


def _audit_dir() -> Path:
    override = os.environ.get("SANCTIONSIGHT_AUDIT_DIR")
    return Path(override) if override else _BASE_DIR / "audit"


# ---------------------------------------------------------------------------
# Model card formatting
# ---------------------------------------------------------------------------

def _format_model_card(model_version) -> str:
    if model_version is None:
        return "# Model card\n\n_No ModelVersion row recorded for this job._\n"
    return (
        "# Model card\n\n"
        f"- **model_id:** {model_version.model_id}\n"
        f"- **model_version_hash:** {model_version.model_version_hash}\n"
        f"- **spacy_model:** {model_version.spacy_model}\n"
        f"- **rules_version:** {model_version.rules_version}\n"
        f"- **prompt_template_version:** {model_version.prompt_template_version}\n"
        f"- **schema_version:** {model_version.schema_version}\n"
        f"- **deployed_at:** {model_version.deployed_at.isoformat() if model_version.deployed_at else ''}\n"
        f"- **notes:** {model_version.notes or ''}\n"
    )


_README_TEMPLATE = """# SanctionSight evidence packet

- Job: {job_id}
- Generated: {generated_at}
- Workflow state: {workflow_status}
- Signed off by: {signed_off_by}

## Files

| File | Meaning |
|------|---------|
| report.html | Human-readable HTML report produced at analysis time. |
| brief.json | Investigator brief + per-claim verification report. |
| findings.csv | Every finding with current HITL disposition. |
| excerpts.jsonl | Every persisted excerpt (evidence text) with stable IDs. |
| snapshots/ | Raw gzipped page text, content-addressed by SHA-256. |
| audit.jsonl | Append-only tamper-evident audit chain (source of truth). |
| audit_verification.json | Result of verify_chain at bundle time. |
| list_snapshots.json | Sanctions list versions in force when the case ran. |
| model_card.md | Analyser fingerprint (model, rules, prompt, schema versions). |

Recompute the audit hashes with any SHA-256 tool:
  python3 -c "import audit, sys; print(audit.verify_chain(sys.argv[1]))" {job_id}
"""


# ---------------------------------------------------------------------------
# Bundling
# ---------------------------------------------------------------------------

def build_evidence_zip(job_id: str) -> Optional[bytes]:
    """Build the ZIP in memory and return its bytes.

    Returns None when storage isn't available — the caller should treat that
    as an operational error, not a 404. All other failures are logged with
    a placeholder entry in the ZIP so the bundle is still well-formed.
    """
    try:
        import storage
    except Exception as exc:
        logger.warning("Evidence packet unavailable — storage missing: %s", exc)
        return None

    from sqlalchemy import select

    buf = io.BytesIO()
    with storage.get_session() as session, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        job = session.get(storage.Job, job_id)
        if job is None:
            zf.writestr("README.md", f"Job {job_id} not found in storage.\n")
            return buf.getvalue()

        # -- cover + report --------------------------------------------------
        job_state = session.execute(
            select(storage.JobState).where(storage.JobState.job_id == job_id)
        ).scalar_one_or_none()
        workflow_status = job_state.workflow_status if job_state else "draft"
        signed_off_by = job_state.signed_off_by if job_state and job_state.signed_off_by else "—"

        zf.writestr("README.md", _README_TEMPLATE.format(
            job_id=job_id,
            generated_at=datetime.utcnow().isoformat(),
            workflow_status=workflow_status,
            signed_off_by=signed_off_by,
        ))

        if job.html_report_path and Path(job.html_report_path).exists():
            zf.write(job.html_report_path, "report.html")
        else:
            zf.writestr("report.html", "<!-- no HTML report was generated for this job -->")

        # -- brief + structured result --------------------------------------
        result = json.loads(job.result_json) if job.result_json else {}
        brief = result.get("investigator_brief") or {}
        zf.writestr("brief.json", json.dumps(brief, indent=2, ensure_ascii=False))
        zf.writestr("result.json", json.dumps(result, indent=2, ensure_ascii=False))

        # -- findings.csv ---------------------------------------------------
        findings = session.execute(
            select(storage.Finding, storage.FindingState)
            .join(storage.FindingState, storage.FindingState.finding_id == storage.Finding.id, isouter=True)
            .where(storage.Finding.job_id == job_id)
            .order_by(storage.Finding.id)
        ).all()

        csv_buf = io.StringIO()
        writer = csv.writer(csv_buf)
        writer.writerow([
            "finding_id", "country", "url", "risk_type", "risk_score", "confidence",
            "sentence", "state", "fp_override", "assigned_analyst", "updated_by",
        ])
        for f, s in findings:
            writer.writerow([
                f.id, f.country or "", f.url, f.risk_type, f.risk_score, f.confidence,
                (f.sentence or "").replace("\n", " "),
                s.status if s else "",
                bool(s.fp_override) if s else "",
                s.assigned_analyst_id if s else "",
                s.updated_by if s else "",
            ])
        zf.writestr("findings.csv", csv_buf.getvalue())

        # -- excerpts.jsonl -------------------------------------------------
        excerpts = session.execute(
            select(storage.Excerpt).where(storage.Excerpt.job_id == job_id)
        ).scalars().all()
        excerpt_lines = []
        content_hashes = set()
        for ex in excerpts:
            if ex.content_hash:
                content_hashes.add(ex.content_hash)
            excerpt_lines.append(json.dumps({
                "excerpt_id": ex.excerpt_id,
                "source_id": ex.source_id,
                "url": ex.url,
                "country": ex.country,
                "risk_type": ex.risk_type,
                "risk_score": ex.risk_score,
                "confidence": ex.confidence,
                "trigger_sentence": ex.trigger_sentence,
                "text": ex.text,
                "content_hash": ex.content_hash,
                "extraction_type": ex.extraction_type,
            }, ensure_ascii=False))
        zf.writestr("excerpts.jsonl", "\n".join(excerpt_lines) + ("\n" if excerpt_lines else ""))

        # -- snapshots/ -----------------------------------------------------
        snaps_dir = _snapshots_dir()
        if content_hashes and snaps_dir.is_dir():
            for h in sorted(content_hashes):
                candidate = snaps_dir / f"{h}.txt.gz"
                if candidate.exists():
                    zf.write(candidate, f"snapshots/{candidate.name}")

        # -- audit log + verification --------------------------------------
        audit_path = _audit_dir() / f"{job_id}.jsonl"
        if audit_path.exists():
            zf.write(audit_path, "audit.jsonl")
        else:
            zf.writestr("audit.jsonl", "")

        try:
            import audit as audit_mod
            verification = audit_mod.verify_chain(job_id)
        except Exception as exc:
            verification = {"status": "ERROR", "reason": str(exc)}
        zf.writestr("audit_verification.json", json.dumps(verification, indent=2))

        # -- list_snapshots.json -------------------------------------------
        list_rows = session.execute(select(storage.ListSnapshot)).scalars().all()
        zf.writestr("list_snapshots.json", json.dumps([
            {
                "list_name": ls.list_name,
                "sha256": ls.sha256,
                "entity_count": ls.entity_count,
                "downloaded_at": ls.downloaded_at.isoformat() if ls.downloaded_at else None,
                "active_from": ls.active_from.isoformat() if ls.active_from else None,
                "active_to": ls.active_to.isoformat() if ls.active_to else None,
            }
            for ls in list_rows
        ], indent=2))

        # -- model card -----------------------------------------------------
        mv = None
        if job.model_version_id is not None:
            mv = session.get(storage.ModelVersion, job.model_version_id)
        zf.writestr("model_card.md", _format_model_card(mv))

    return buf.getvalue()
