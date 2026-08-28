"""Phase 3 — evidence packet ZIP contents.

The packet is built from DB + JSONL + snapshot files. This test seeds a
completed job, runs the bundler, and asserts the required entries are
present and non-empty where they should be.
"""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from datetime import datetime

import pytest

pytest.importorskip("sqlalchemy")


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("SANCTIONSIGHT_DB_PATH", str(tmp_path / "ep.db"))
    monkeypatch.setenv("SANCTIONSIGHT_AUDIT_DIR", str(tmp_path / "audit"))
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SANCTIONSIGHT_SNAPSHOTS_DIR", str(snapshots_dir))

    import importlib
    import storage
    monkeypatch.setattr(storage, "_engine", None, raising=False)
    monkeypatch.setattr(storage, "_SessionLocal", None, raising=False)
    importlib.reload(storage)
    storage.init_db()

    # -- Seed a full job with finding, state, snapshot, audit events. ----
    job_id = "job_ep_1"
    content_hash = "aaaabbbbccccdddd0000111122223333"

    # write a fake snapshot file so the bundle can pick it up
    snap = snapshots_dir / f"{content_hash}.txt.gz"
    with gzip.open(snap, "wb") as fh:
        fh.write(b"snapshot body for evidence packet test")

    # write a tiny HTML report
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    html_path = reports_dir / f"{job_id}.html"
    html_path.write_text("<html><body>report</body></html>", encoding="utf-8")

    # build an audit chain for the job
    import audit
    logger = audit.AuditLogger(job_id)
    logger.log_job_started({"website": "example.com"})
    logger.log_job_completed({"recommendation": "NO_FURTHER_ACTION_RECOMMENDED"})

    with storage.get_session() as session:
        session.add(storage.Job(
            id=job_id, website="example.com", status="completed",
            html_report_path=str(html_path),
            result_json=json.dumps({
                "website": "example.com",
                "investigator_brief": {"recommendation": "NO_FURTHER_ACTION_RECOMMENDED"},
            }),
        ))
        excerpt = storage.Excerpt(
            job_id=job_id,
            source_id="src_123",
            excerpt_id="exc_abc",
            url="https://example.com/about",
            country="Iran",
            risk_type="GENERAL",
            risk_score=30.0,
            confidence=50.0,
            trigger_sentence="trigger",
            text="context window text",
            content_hash=content_hash,
            extraction_type="HTML",
        )
        session.add(excerpt)
        session.flush()
        finding = storage.Finding(
            job_id=job_id, country="Iran", url="https://example.com/about",
            risk_type="GENERAL", risk_score=30.0, confidence=50.0,
            sentence="trigger", excerpt_pk=excerpt.id,
        )
        session.add(finding)
        session.flush()
        session.add(storage.FindingState(
            finding_id=finding.id, status="cleared_fp", fp_override=True,
            updated_at=datetime.utcnow(), updated_by="analyst@test",
        ))
        session.add(storage.JobState(
            job_id=job_id, workflow_status="signed_off",
            signed_off_by="reviewer@test", signed_off_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
        session.add(storage.ListSnapshot(
            list_name="ofac_sdn", sha256="deadbeef",
            downloaded_at=datetime.utcnow(), entity_count=10,
            active_from=datetime.utcnow(),
        ))
        mv = storage.ModelVersion(
            model_id="gemma-3-27b-it",
            model_version_hash="hash123",
            prompt_template_version="v1", schema_version="v1",
            spacy_model="en_core_web_sm", rules_version="v2.3",
            deployed_at=datetime.utcnow(),
        )
        session.add(mv)
        session.flush()
        session.get(storage.Job, job_id).model_version_id = mv.id
        session.commit()

    return job_id, content_hash


def test_bundle_contains_all_required_files(seeded):
    job_id, content_hash = seeded
    import evidence_packet
    data = evidence_packet.build_evidence_zip(job_id)
    assert data is not None and len(data) > 0

    zf = zipfile.ZipFile(io.BytesIO(data))
    names = set(zf.namelist())

    for required in [
        "README.md", "report.html", "brief.json", "result.json",
        "findings.csv", "excerpts.jsonl",
        "audit.jsonl", "audit_verification.json",
        "list_snapshots.json", "model_card.md",
    ]:
        assert required in names, f"missing {required} in bundle: {names}"

    # snapshot file was copied in under snapshots/
    snapshot_entries = [n for n in names if n.startswith("snapshots/")]
    assert any(content_hash in n for n in snapshot_entries), f"snapshot {content_hash} not bundled: {snapshot_entries}"

    # findings.csv has a header + at least one row
    csv_lines = zf.read("findings.csv").decode().strip().splitlines()
    assert csv_lines[0].startswith("finding_id,")
    assert len(csv_lines) >= 2

    # audit_verification.json reports OK
    verification = json.loads(zf.read("audit_verification.json"))
    assert verification["status"] == "OK"

    # model_card contains the fingerprint
    card = zf.read("model_card.md").decode()
    assert "gemma-3-27b-it" in card
