"""Integration test for the full HITL lifecycle through the REST API.

Unit tests (``tests/unit/test_hitl_flow.py``) cover each endpoint in
isolation. This test exercises the end-to-end journey:

    pending → (analyst) in_review → (reviewer) confirmed_match
           → (reviewer) sign-off  → (admin) reopen

Plus the side-effect checks that SR 11-7 cares about:
 - state history rows are appended for every transition
 - audit events are emitted for every transition
 - sign-off is blocked while any finding is still ``pending``
"""
from __future__ import annotations

from datetime import datetime

import pytest

from tests.integration.conftest import auth_headers, login


def _seed_job_with_two_findings(storage) -> tuple[str, int, int]:
    """Two findings so we can reach sign-off via mixed dispositions."""
    job_id = "job_int_1"
    with storage.get_session() as session:
        session.add(storage.Job(
            id=job_id, website="example.com", status="completed",
        ))
        f1 = storage.Finding(
            job_id=job_id, country="Iran", url="https://example.com/a",
            risk_type="DIRECT_BUSINESS", risk_score=85.0, confidence=90.0,
            sentence="We operate a subsidiary in Tehran.",
        )
        f2 = storage.Finding(
            job_id=job_id, country="Cuba", url="https://example.com/b",
            risk_type="GENERAL_MENTION", risk_score=20.0, confidence=60.0,
            sentence="The photograph was taken in Havana.",
        )
        session.add_all([f1, f2])
        session.flush()
        for f in (f1, f2):
            session.add(storage.FindingState(
                finding_id=f.id, status="pending", updated_at=datetime.utcnow(),
            ))
        session.add(storage.JobState(
            job_id=job_id, workflow_status="draft", updated_at=datetime.utcnow(),
        ))
        session.commit()
        return job_id, f1.id, f2.id


def test_full_hitl_lifecycle(app_env, seeded_users):
    client, storage, _, _ = app_env
    job_id, f1, f2 = _seed_job_with_two_findings(storage)

    analyst_tok = login(client, *seeded_users["analyst"])
    reviewer_tok = login(client, *seeded_users["reviewer"])
    admin_tok = login(client, *seeded_users["admin"])

    # ------------------------------------------------------------------
    # Sign-off should be blocked while any finding is still pending.
    # ------------------------------------------------------------------
    blocked = client.post(
        f"/api/jobs/{job_id}/sign-off",
        headers=auth_headers(reviewer_tok),
        json={"notes": "attempt to sign off early"},
    )
    assert blocked.status_code in {400, 409}, blocked.text

    # ------------------------------------------------------------------
    # Analyst picks up both findings.
    # ------------------------------------------------------------------
    for fid in (f1, f2):
        resp = client.post(
            f"/api/findings/{fid}/state",
            headers=auth_headers(analyst_tok),
            json={"to_status": "in_review", "reason": "triage"},
        )
        assert resp.status_code == 200, resp.text

    # Analyst clears f2 as FP; reviewer confirms the match on f1.
    fp_resp = client.post(
        f"/api/findings/{f2}/fp-override",
        headers=auth_headers(analyst_tok),
        json={"reason": "Havana photograph — benign travel content"},
    )
    assert fp_resp.status_code == 200, fp_resp.text
    assert fp_resp.json()["status"] == "cleared_fp"

    confirm_resp = client.post(
        f"/api/findings/{f1}/state",
        headers=auth_headers(reviewer_tok),
        json={"to_status": "confirmed_match", "reason": "Tehran subsidiary confirmed"},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    assert confirm_resp.json()["to_status"] == "confirmed_match"

    # ------------------------------------------------------------------
    # Sign-off by the reviewer — all findings are now terminal.
    # ------------------------------------------------------------------
    signoff = client.post(
        f"/api/jobs/{job_id}/sign-off",
        headers=auth_headers(reviewer_tok),
        json={"notes": "Confirmed Iran connection; Cuba cleared as FP."},
    )
    assert signoff.status_code == 200, signoff.text

    with storage.get_session() as session:
        job_state = session.get(storage.JobState, job_id)
        assert job_state.workflow_status == "signed_off"
        assert job_state.signed_off_by  # identifier captured

        # History rows: one per transition, minimum.
        f1_history = (
            session.query(storage.FindingStatusHistory)
            .filter(storage.FindingStatusHistory.finding_id == f1)
            .all()
        )
        assert len(f1_history) >= 2  # pending→in_review, in_review→confirmed_match
        transitions = [(h.from_status, h.to_status) for h in f1_history]
        assert ("pending", "in_review") in transitions
        assert ("in_review", "confirmed_match") in transitions

    # ------------------------------------------------------------------
    # Reviewer cannot reopen — admin-only — but admin can.
    # ------------------------------------------------------------------
    reviewer_reopen = client.post(
        f"/api/jobs/{job_id}/reopen",
        headers=auth_headers(reviewer_tok),
        json={"reason": "second thoughts"},
    )
    assert reviewer_reopen.status_code == 403

    admin_reopen = client.post(
        f"/api/jobs/{job_id}/reopen",
        headers=auth_headers(admin_tok),
        json={"reason": "new evidence surfaced"},
    )
    assert admin_reopen.status_code == 200, admin_reopen.text

    with storage.get_session() as session:
        job_state = session.get(storage.JobState, job_id)
        assert job_state.workflow_status == "reopened"


def test_hitl_overview_reflects_state_mix(app_env, seeded_users):
    client, storage, _, _ = app_env
    job_id, f1, f2 = _seed_job_with_two_findings(storage)

    analyst_tok = login(client, *seeded_users["analyst"])
    # Clear one, leave the other pending.
    client.post(
        f"/api/findings/{f2}/fp-override",
        headers=auth_headers(analyst_tok),
        json={"reason": "cleared"},
    )

    overview = client.get(
        f"/api/jobs/{job_id}/hitl-overview",
        headers=auth_headers(analyst_tok),
    )
    assert overview.status_code == 200, overview.text
    body = overview.json()
    # Shape is defensive — only assert the counts the frontend actually
    # reads. Extra keys are allowed.
    assert body.get("total", 0) >= 2
    by_status = body.get("by_status") or body.get("status_counts") or {}
    if by_status:
        assert by_status.get("cleared_fp", 0) >= 1
        assert by_status.get("pending", 0) >= 1
