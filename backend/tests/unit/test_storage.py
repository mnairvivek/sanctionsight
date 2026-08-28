"""Smoke tests for the Phase 1 storage layer.

These tests are skipped when SQLAlchemy is not installed, so the rest of
the test suite still runs cleanly in a bare-stdlib environment.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")


@pytest.fixture
def storage_module(tmp_path, monkeypatch):
    """Give the storage module an isolated on-disk SQLite per test."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("SANCTIONSIGHT_DB_PATH", str(db_path))

    # Reload storage so the module picks up our env var and rebuilds the engine.
    import importlib
    import storage as _storage

    monkeypatch.setattr(_storage, "_engine", None, raising=False)
    monkeypatch.setattr(_storage, "_SessionLocal", None, raising=False)
    importlib.reload(_storage)

    _storage.init_db()
    return _storage


def test_job_roundtrip(storage_module):
    job_id = "job_rt_1"
    with storage_module.get_session() as session:
        session.add(storage_module.Job(
            id=job_id,
            website="example.com",
            business_name="Acme Trading",
            status="running",
        ))
        session.commit()

    with storage_module.get_session() as session:
        row = session.get(storage_module.Job, job_id)
        assert row is not None
        assert row.website == "example.com"
        assert row.status == "running"
        assert isinstance(row.created_at, datetime)


def test_audit_event_unique_sequence_per_job(storage_module):
    job_id = "job_rt_audit"
    with storage_module.get_session() as session:
        session.add(storage_module.Job(id=job_id, website="example.com"))
        session.add(storage_module.AuditEvent(
            job_id=job_id, sequence=0, event_type="job_started",
            occurred_at=datetime.utcnow(), actor="system",
            payload_json="{}", prev_hash="", hash="h0",
        ))
        session.commit()

    # Inserting another event with sequence=0 must violate uq_audit_sequence_per_job.
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        with storage_module.get_session() as session:
            session.add(storage_module.AuditEvent(
                job_id=job_id, sequence=0, event_type="job_completed",
                occurred_at=datetime.utcnow(), actor="system",
                payload_json="{}", prev_hash="h0", hash="h1",
            ))
            session.commit()


def test_wal_journal_mode_applied(storage_module):
    from sqlalchemy import text
    engine = storage_module.get_engine()
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert str(mode).lower() == "wal"


def test_list_snapshot_uniqueness(storage_module):
    from sqlalchemy.exc import IntegrityError

    now = datetime.utcnow()
    with storage_module.get_session() as session:
        session.add(storage_module.ListSnapshot(
            list_name="ofac_sdn",
            downloaded_at=now,
            sha256="deadbeef",
            entity_count=10,
            active_from=now,
        ))
        session.commit()

    with pytest.raises(IntegrityError):
        with storage_module.get_session() as session:
            session.add(storage_module.ListSnapshot(
                list_name="ofac_sdn",
                downloaded_at=now,
                sha256="deadbeef",
                entity_count=11,
                active_from=now,
            ))
            session.commit()


def test_stable_id_helpers_match_schemas_module(storage_module):
    """Storage has its own ID helpers — they must produce the same output
    as schemas.py so Phase 2 citations continue to line up."""
    from schemas import stable_excerpt_id as schema_eid, stable_source_id as schema_sid

    url = "https://example.com/about"
    trigger = "Acme exports to Iran."
    assert storage_module.stable_source_id(url) == schema_sid(url)
    assert storage_module.stable_excerpt_id(url, trigger, 0) == schema_eid(url, trigger, 0)
