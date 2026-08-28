"""Tests for the tamper-evident audit chain (Phase 1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import audit


def _job_path(directory: Path, job_id: str) -> Path:
    return directory / f"{job_id}.jsonl"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_chain_validates_after_sequential_writes(tmp_path):
    job_id = "job_ok"
    logger = audit.AuditLogger(job_id, directory=tmp_path)

    logger.log_job_started({"website": "example.com"})
    logger.log_search("example.com OFAC", result_count=3, elapsed_ms=120.5)
    logger.log_extraction(
        url="https://example.com/about",
        extraction_type="HTML",
        content_hash="abc123",
        content_length=4096,
    )
    logger.log_job_completed({"recommendation": "NO_FURTHER_ACTION_RECOMMENDED"})

    result = audit.verify_chain(job_id, directory=tmp_path)
    assert result["status"] == "OK"
    assert result["event_count"] == 4
    assert result["first_bad_seq"] is None


def test_read_events_returns_lines_in_order(tmp_path):
    job_id = "job_read"
    logger = audit.AuditLogger(job_id, directory=tmp_path)
    logger.log_job_started({"x": 1})
    logger.log_search("q", 1, 10.0)
    logger.log_job_completed({"ok": True})

    events = list(audit.read_events(job_id, directory=tmp_path))
    assert [e["seq"] for e in events] == [0, 1, 2]
    assert events[0]["event_type"] == "job_started"
    assert events[-1]["event_type"] == "job_completed"


def test_logger_resumes_seq_and_prev_hash_after_reopen(tmp_path):
    job_id = "job_resume"
    first = audit.AuditLogger(job_id, directory=tmp_path)
    e1 = first.log_job_started({"ok": True})
    del first

    second = audit.AuditLogger(job_id, directory=tmp_path)
    e2 = second.log_search("more", 0, 1.0)

    assert e2["seq"] == 1
    assert e2["prev_hash"] == e1["hash"]

    assert audit.verify_chain(job_id, directory=tmp_path)["status"] == "OK"


# ---------------------------------------------------------------------------
# Tampering detection
# ---------------------------------------------------------------------------

def test_tampering_with_payload_is_detected(tmp_path):
    job_id = "job_tamper"
    logger = audit.AuditLogger(job_id, directory=tmp_path)
    logger.log_job_started({"website": "example.com"})
    logger.log_search("q1", 5, 15.0)
    logger.log_search("q2", 2, 18.0)
    logger.log_job_completed({"ok": True})

    path = _job_path(tmp_path, job_id)
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["payload"]["result_count"] = 9999  # alter without recomputing hash
    lines[1] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    result = audit.verify_chain(job_id, directory=tmp_path)
    assert result["status"] == "INTEGRITY_BROKEN"
    assert result["first_bad_seq"] == 1


def test_deleted_middle_line_breaks_the_chain(tmp_path):
    job_id = "job_delete"
    logger = audit.AuditLogger(job_id, directory=tmp_path)
    logger.log_job_started({})
    logger.log_search("q1", 1, 1.0)
    logger.log_search("q2", 1, 1.0)
    logger.log_job_completed({})

    path = _job_path(tmp_path, job_id)
    lines = path.read_text().splitlines()
    del lines[1]
    path.write_text("\n".join(lines) + "\n")

    result = audit.verify_chain(job_id, directory=tmp_path)
    assert result["status"] == "INTEGRITY_BROKEN"


def test_missing_file_reported(tmp_path):
    result = audit.verify_chain("never_existed", directory=tmp_path)
    assert result["status"] == "MISSING"


def test_reordering_two_lines_is_detected(tmp_path):
    job_id = "job_reorder"
    logger = audit.AuditLogger(job_id, directory=tmp_path)
    logger.log_job_started({})
    logger.log_search("q1", 1, 1.0)
    logger.log_search("q2", 1, 1.0)

    path = _job_path(tmp_path, job_id)
    lines = path.read_text().splitlines()
    lines[1], lines[2] = lines[2], lines[1]
    path.write_text("\n".join(lines) + "\n")

    result = audit.verify_chain(job_id, directory=tmp_path)
    assert result["status"] == "INTEGRITY_BROKEN"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

def test_unknown_event_type_raises(tmp_path):
    logger = audit.AuditLogger("job_bad_event", directory=tmp_path)
    with pytest.raises(ValueError):
        logger.log("not_a_real_event", {"x": 1})
