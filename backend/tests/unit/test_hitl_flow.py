"""Phase 3 — HITL transitions, reviewer-only sign-off, admin-only reopen.

These tests exercise the endpoints through the FastAPI TestClient so the
role-check dependencies, audit-event side effects, and workflow guards are
all covered together.
"""

from __future__ import annotations

from datetime import datetime

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("jwt")
pytest.importorskip("passlib")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Fresh FastAPI app backed by a throwaway SQLite DB + audit dir."""
    monkeypatch.setenv("SANCTIONSIGHT_DB_PATH", str(tmp_path / "hitl.db"))
    monkeypatch.setenv("SANCTIONSIGHT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("SANCTIONSIGHT_SNAPSHOTS_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("SANCTIONSIGHT_JWT_SECRET", "hitl-test-secret")

    import importlib
    import storage
    monkeypatch.setattr(storage, "_engine", None, raising=False)
    monkeypatch.setattr(storage, "_SessionLocal", None, raising=False)
    importlib.reload(storage)
    storage.init_db()

    import auth
    importlib.reload(auth)

    import main
    importlib.reload(main)

    from fastapi.testclient import TestClient
    return TestClient(main.app), storage, auth


def _seed_users(storage, auth):
    from sqlalchemy.exc import IntegrityError
    users = [
        ("analyst@test", "analyst", "analyst-pw"),
        ("reviewer@test", "reviewer", "reviewer-pw"),
        ("admin@test", "admin", "admin-pw"),
    ]
    with storage.get_session() as session:
        for email, role, pw in users:
            try:
                session.add(storage.User(
                    email=email, hashed_password=auth.hash_password(pw),
                    role=role, disabled=False, created_at=datetime.utcnow(),
                ))
                session.commit()
            except IntegrityError:
                session.rollback()


def _login(client, email: str, password: str) -> str:
    resp = client.post("/api/auth/login", data={"username": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _seed_job_with_finding(storage) -> tuple[str, int]:
    job_id = "job_hitl_1"
    with storage.get_session() as session:
        session.add(storage.Job(id=job_id, website="example.com", status="completed"))
        finding = storage.Finding(
            job_id=job_id, country="Iran", url="https://example.com/about",
            risk_type="GENERAL", risk_score=60.0, confidence=70.0,
            sentence="Some sentence mentioning Iran.",
        )
        session.add(finding)
        session.flush()
        session.add(storage.FindingState(
            finding_id=finding.id, status="pending", updated_at=datetime.utcnow(),
        ))
        session.add(storage.JobState(
            job_id=job_id, workflow_status="draft", updated_at=datetime.utcnow(),
        ))
        session.commit()
        return job_id, finding.id


def test_analyst_transitions_finding(client):
    tc, storage, auth = client
    _seed_users(storage, auth)
    _, finding_id = _seed_job_with_finding(storage)

    token = _login(tc, "analyst@test", "analyst-pw")
    resp = tc.post(
        f"/api/findings/{finding_id}/state",
        headers={"Authorization": f"Bearer {token}"},
        json={"to_status": "in_review", "reason": "picked up"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from_status"] == "pending"
    assert body["to_status"] == "in_review"


def test_analyst_cannot_confirm_match(client):
    tc, storage, auth = client
    _seed_users(storage, auth)
    _, finding_id = _seed_job_with_finding(storage)

    token = _login(tc, "analyst@test", "analyst-pw")
    resp = tc.post(
        f"/api/findings/{finding_id}/state",
        headers={"Authorization": f"Bearer {token}"},
        json={"to_status": "confirmed_match"},
    )
    assert resp.status_code == 403


def test_fp_override_flips_state(client):
    tc, storage, auth = client
    _seed_users(storage, auth)
    _, finding_id = _seed_job_with_finding(storage)

    token = _login(tc, "analyst@test", "analyst-pw")
    resp = tc.post(
        f"/api/findings/{finding_id}/fp-override",
        headers={"Authorization": f"Bearer {token}"},
        json={"reason": "Cuban sandwich menu, not a sanctions hit"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cleared_fp"

    with storage.get_session() as session:
        state = session.get(storage.FindingState, finding_id)
        assert state.status == "cleared_fp"
        assert state.fp_override is True


def test_sign_off_blocked_while_pending_findings_exist(client):
    tc, storage, auth = client
    _seed_users(storage, auth)
    job_id, _ = _seed_job_with_finding(storage)

    token = _login(tc, "reviewer@test", "reviewer-pw")
    resp = tc.post(
        f"/api/jobs/{job_id}/sign-off",
        headers={"Authorization": f"Bearer {token}"},
        json={"final_disposition_notes": "Looks clear."},
    )
    assert resp.status_code == 409


def test_sign_off_after_dispositioning(client):
    tc, storage, auth = client
    _seed_users(storage, auth)
    job_id, finding_id = _seed_job_with_finding(storage)

    analyst_token = _login(tc, "analyst@test", "analyst-pw")
    tc.post(
        f"/api/findings/{finding_id}/fp-override",
        headers={"Authorization": f"Bearer {analyst_token}"},
        json={"reason": "false positive"},
    )

    reviewer_token = _login(tc, "reviewer@test", "reviewer-pw")
    resp = tc.post(
        f"/api/jobs/{job_id}/sign-off",
        headers={"Authorization": f"Bearer {reviewer_token}"},
        json={"final_disposition_notes": "All findings dispositioned."},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["workflow_status"] == "signed_off"


def test_only_admin_can_reopen(client):
    tc, storage, auth = client
    _seed_users(storage, auth)
    job_id, finding_id = _seed_job_with_finding(storage)

    # First get the job signed off.
    analyst_token = _login(tc, "analyst@test", "analyst-pw")
    tc.post(
        f"/api/findings/{finding_id}/fp-override",
        headers={"Authorization": f"Bearer {analyst_token}"},
        json={"reason": "fp"},
    )
    reviewer_token = _login(tc, "reviewer@test", "reviewer-pw")
    tc.post(
        f"/api/jobs/{job_id}/sign-off",
        headers={"Authorization": f"Bearer {reviewer_token}"},
        json={"final_disposition_notes": "done"},
    )

    # Reviewer can't reopen.
    resp = tc.post(
        f"/api/jobs/{job_id}/reopen",
        headers={"Authorization": f"Bearer {reviewer_token}"},
        json={"reason": "need another look"},
    )
    assert resp.status_code == 403

    # Admin can.
    admin_token = _login(tc, "admin@test", "admin-pw")
    resp = tc.post(
        f"/api/jobs/{job_id}/reopen",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"reason": "regulator question"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["workflow_status"] == "reopened"


def test_analyst_queue_includes_pending_and_assigned(client):
    tc, storage, auth = client
    _seed_users(storage, auth)
    _, finding_id = _seed_job_with_finding(storage)

    token = _login(tc, "analyst@test", "analyst-pw")
    resp = tc.get("/api/analysts/me/queue", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert any(item["finding_id"] == finding_id for item in items)
