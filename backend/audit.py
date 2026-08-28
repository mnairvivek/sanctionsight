"""
Tamper-evident append-only audit log.

Each job owns one file at `audit/{job_id}.jsonl`. Every event is one line of
canonical JSON containing the fields below:

    seq          monotonically-increasing integer starting at 0
    job_id       job identifier
    event_type   one of the EVENT_TYPES constants
    occurred_at  ISO-8601 UTC timestamp
    actor        "system" or an analyst id
    payload      event-specific dict (can be empty)
    prev_hash    hash of the previous line (empty string for seq=0)
    hash         sha256(prev_hash + canonical_json_of_this_line_without_hash)

`verify_chain(job_id)` re-computes each line's hash and reports any
deviation, yielding the index of the first line where the chain breaks —
sufficient for a regulator to confirm integrity or locate tampering.

The JSONL file is the source of truth. `sync_to_db()` is a best-effort
mirror into the AuditEvent table for SQL querying; breaking that mirror
does not affect the integrity check.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

logger = logging.getLogger("audit")


# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

EVENT_JOB_STARTED = "job_started"
EVENT_JOB_COMPLETED = "job_completed"
EVENT_JOB_FAILED = "job_failed"
EVENT_SEARCH_EXECUTED = "search_executed"
EVENT_CONTENT_EXTRACTED = "content_extracted"
EVENT_NLP_ANALYZED = "nlp_analyzed"
EVENT_LIST_SCREENING = "list_screening"
EVENT_LLM_PROMPT_SENT = "llm_prompt_sent"
EVENT_LLM_RESPONSE_RECEIVED = "llm_response_received"
EVENT_ANALYST_ACTION = "analyst_action"

EVENT_TYPES = frozenset({
    EVENT_JOB_STARTED,
    EVENT_JOB_COMPLETED,
    EVENT_JOB_FAILED,
    EVENT_SEARCH_EXECUTED,
    EVENT_CONTENT_EXTRACTED,
    EVENT_NLP_ANALYZED,
    EVENT_LIST_SCREENING,
    EVENT_LLM_PROMPT_SENT,
    EVENT_LLM_RESPONSE_RECEIVED,
    EVENT_ANALYST_ACTION,
})


# ---------------------------------------------------------------------------
# Canonical JSON + hashing
# ---------------------------------------------------------------------------

def _canonical_json(obj) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_event(event_without_hash: dict, prev_hash: str) -> str:
    payload = prev_hash + _canonical_json(event_without_hash)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Audit directory resolution
# ---------------------------------------------------------------------------

_DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent / "audit"


def audit_dir() -> Path:
    """Return the configured audit directory, creating it if missing."""
    override = os.environ.get("SANCTIONSIGHT_AUDIT_DIR")
    d = Path(override) if override else _DEFAULT_AUDIT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job_file(job_id: str, directory: Optional[Path] = None) -> Path:
    directory = directory or audit_dir()
    return directory / f"{job_id}.jsonl"


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class AuditLogger:
    """Append-only hash-chained JSONL writer for one job."""

    def __init__(self, job_id: str, directory: Optional[Path] = None):
        self.job_id = job_id
        self._path = _job_file(job_id, directory)
        self._lock = threading.Lock()
        self._seq, self._last_hash = self._resume_from_disk()

    # -- lifecycle ----------------------------------------------------------

    def _resume_from_disk(self) -> tuple[int, str]:
        """If the file already has lines, pick up sequence and last_hash."""
        if not self._path.exists() or self._path.stat().st_size == 0:
            return 0, ""
        last_seq = -1
        last_hash = ""
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                last_seq = evt.get("seq", last_seq)
                last_hash = evt.get("hash", last_hash)
        return last_seq + 1, last_hash

    # -- write --------------------------------------------------------------

    def log(
        self,
        event_type: str,
        payload: Optional[dict] = None,
        actor: str = "system",
    ) -> dict:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown event_type: {event_type!r}")
        payload = payload or {}
        with self._lock:
            event = {
                "seq": self._seq,
                "job_id": self.job_id,
                "event_type": event_type,
                "occurred_at": _utc_now_iso(),
                "actor": actor,
                "payload": payload,
                "prev_hash": self._last_hash,
            }
            event["hash"] = _hash_event(event, self._last_hash)
            line = _canonical_json(event) + "\n"
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            self._seq += 1
            self._last_hash = event["hash"]
            return event

    # -- ergonomic helpers --------------------------------------------------

    def log_job_started(self, request: dict) -> dict:
        return self.log(EVENT_JOB_STARTED, {"request": request})

    def log_job_completed(self, summary: dict) -> dict:
        return self.log(EVENT_JOB_COMPLETED, summary)

    def log_job_failed(self, error: str) -> dict:
        return self.log(EVENT_JOB_FAILED, {"error": error})

    def log_search(self, query: str, result_count: int, elapsed_ms: float, source: str = "google_cse") -> dict:
        return self.log(
            EVENT_SEARCH_EXECUTED,
            {
                "query": query,
                "result_count": result_count,
                "elapsed_ms": round(elapsed_ms, 2),
                "source": source,
            },
        )

    def log_extraction(
        self,
        url: str,
        extraction_type: str,
        content_hash: Optional[str],
        content_length: int,
    ) -> dict:
        return self.log(
            EVENT_CONTENT_EXTRACTED,
            {
                "url": url,
                "extraction_type": extraction_type,
                "content_hash": content_hash,
                "content_length": content_length,
            },
        )

    def log_nlp(self, url: str, finding_ids: list, rules_version: str) -> dict:
        return self.log(
            EVENT_NLP_ANALYZED,
            {"url": url, "finding_ids": finding_ids, "rules_version": rules_version},
        )

    def log_list_screening(self, query_name: str, match_count: int, high_conf_count: int) -> dict:
        return self.log(
            EVENT_LIST_SCREENING,
            {"query_name": query_name, "match_count": match_count, "high_conf_count": high_conf_count},
        )

    def log_llm_prompt(self, model: str, prompt_hash: str, evidence_ids: list) -> dict:
        return self.log(
            EVENT_LLM_PROMPT_SENT,
            {"model": model, "prompt_hash": prompt_hash, "evidence_ids": evidence_ids},
        )

    def log_llm_response(self, response_hash: str, verification_result: dict) -> dict:
        return self.log(
            EVENT_LLM_RESPONSE_RECEIVED,
            {"response_hash": response_hash, "verification_result": verification_result},
        )

    def log_analyst_action(
        self,
        actor: str,
        target: str,
        action: str,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> dict:
        return self.log(
            EVENT_ANALYST_ACTION,
            {
                "target": target,
                "action": action,
                "from_state": from_state,
                "to_state": to_state,
                "reason": reason,
            },
            actor=actor,
        )


# ---------------------------------------------------------------------------
# Verification / reading
# ---------------------------------------------------------------------------

def read_events(job_id: str, directory: Optional[Path] = None) -> Iterator[dict]:
    path = _job_file(job_id, directory)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def verify_chain(job_id: str, directory: Optional[Path] = None) -> dict:
    """
    Re-hash every event in the job's log.

    Returns:
        {
          "status": "OK" | "INTEGRITY_BROKEN" | "EMPTY" | "MISSING",
          "job_id": ...,
          "event_count": int,
          "first_bad_seq": int | None,
          "reason": str | None
        }
    """
    path = _job_file(job_id, directory)
    if not path.exists():
        return {
            "status": "MISSING",
            "job_id": job_id,
            "event_count": 0,
            "first_bad_seq": None,
            "reason": f"No audit file for job {job_id}",
        }

    expected_seq = 0
    prev_hash = ""
    count = 0

    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh):
            raw = raw.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                return {
                    "status": "INTEGRITY_BROKEN",
                    "job_id": job_id,
                    "event_count": count,
                    "first_bad_seq": expected_seq,
                    "reason": f"Malformed JSON on line {line_no}: {exc}",
                }

            if event.get("seq") != expected_seq:
                return {
                    "status": "INTEGRITY_BROKEN",
                    "job_id": job_id,
                    "event_count": count,
                    "first_bad_seq": expected_seq,
                    "reason": f"Sequence mismatch at line {line_no}: expected {expected_seq}, got {event.get('seq')}",
                }

            if event.get("prev_hash", "") != prev_hash:
                return {
                    "status": "INTEGRITY_BROKEN",
                    "job_id": job_id,
                    "event_count": count,
                    "first_bad_seq": expected_seq,
                    "reason": f"prev_hash mismatch at seq {expected_seq}",
                }

            stored_hash = event.get("hash", "")
            event_copy = {k: v for k, v in event.items() if k != "hash"}
            recomputed = _hash_event(event_copy, prev_hash)
            if recomputed != stored_hash:
                return {
                    "status": "INTEGRITY_BROKEN",
                    "job_id": job_id,
                    "event_count": count,
                    "first_bad_seq": expected_seq,
                    "reason": f"hash mismatch at seq {expected_seq}",
                }

            prev_hash = stored_hash
            expected_seq += 1
            count += 1

    if count == 0:
        return {
            "status": "EMPTY",
            "job_id": job_id,
            "event_count": 0,
            "first_bad_seq": None,
            "reason": "Audit file is empty",
        }

    return {
        "status": "OK",
        "job_id": job_id,
        "event_count": count,
        "first_bad_seq": None,
        "reason": None,
    }


# ---------------------------------------------------------------------------
# DB mirror (best effort)
# ---------------------------------------------------------------------------

def sync_to_db(job_id: str, events: Iterable[dict]) -> int:
    """
    Insert JSONL events into the AuditEvent table for query convenience.
    The JSONL file remains the source of truth; failures here are logged
    but do not raise.

    Returns the number of rows inserted.
    """
    try:
        from storage import AuditEvent, get_session  # local import to keep audit.py dependency-light
    except Exception as exc:
        logger.debug("Skipping audit DB mirror (storage unavailable): %s", exc)
        return 0

    inserted = 0
    try:
        with get_session() as session:
            existing_seqs = {
                s for (s,) in session.query(AuditEvent.sequence).filter(
                    AuditEvent.job_id == job_id
                ).all()
            }
            for event in events:
                seq = event.get("seq")
                if seq in existing_seqs:
                    continue
                row = AuditEvent(
                    job_id=job_id,
                    sequence=seq,
                    event_type=event.get("event_type", ""),
                    occurred_at=_parse_iso(event.get("occurred_at")),
                    actor=event.get("actor", "system"),
                    payload_json=_canonical_json(event.get("payload", {})),
                    prev_hash=event.get("prev_hash", ""),
                    hash=event.get("hash", ""),
                )
                session.add(row)
                inserted += 1
            session.commit()
    except Exception as exc:
        logger.warning("Audit DB mirror failed for job %s: %s", job_id, exc)
    return inserted


def _parse_iso(s: Optional[str]) -> datetime:
    if not s:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.utcnow()
