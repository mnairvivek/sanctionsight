"""End-to-end test of the analyze pipeline with external I/O stubbed.

The goal is to exercise the full ``/api/analyze`` → background task →
findings persisted → brief generated path without hitting Google CSE, the
target website, or the LLM. Each external boundary is patched with a
deterministic stub so the run is hermetic and safe for CI.

Scope: happy path only. Failure modes (LLM outage, extraction error) have
dedicated unit tests and are not duplicated here.
"""
from __future__ import annotations

import time

import pytest


def _wait_for_job(client, job_id: str, timeout: float = 10.0) -> dict:
    """Poll /api/status until the job reaches a terminal state."""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        resp = client.get(f"/api/status/{job_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last.get("status") in {"completed", "failed"}:
            return last
        time.sleep(0.05)
    pytest.fail(f"Job {job_id} did not complete within {timeout}s; last={last}")


def _fake_report(country: str) -> dict:
    """Shape matches what ``engine.process_single_entity`` returns."""
    if country != "Iran":
        return {
            "country": country,
            "search_results": [],
            "analyzed_results": [],
            "total_urls_analyzed": 0,
        }
    return {
        "country": "Iran",
        "search_results": [
            {
                "title": "Example Corp — Tehran office",
                "link": "https://example.com/tehran",
                "snippet": "Our Tehran office handles regional operations.",
            }
        ],
        "analyzed_results": [
            {
                "url": "https://example.com/tehran",
                "title": "Tehran office",
                "risk_level": "HIGH",
                "risk_score": 85,
                "confidence": 90,
                "extraction_type": "HTML",
                "extraction_message": None,
                "language": "en",
                "findings": [
                    {
                        "sentence": "We operate a subsidiary in Tehran servicing regional clients.",
                        "context": "We operate a subsidiary in Tehran servicing regional clients.",
                        "risk_type": "DIRECT_BUSINESS",
                        "risk_score": 85,
                        "confidence": 90,
                        "source_url": "https://example.com/tehran",
                    }
                ],
            }
        ],
        "total_urls_analyzed": 1,
    }


@pytest.fixture
def stubbed_pipeline(app_env, monkeypatch):
    """Patch every external boundary of _run_analysis."""
    _, _, _, main = app_env
    import sanctions_engine as engine

    monkeypatch.setattr(
        engine, "search_website_for_social_media", lambda _url: {}
    )
    monkeypatch.setattr(
        engine,
        "process_single_entity",
        lambda entity, *a, **kw: _fake_report(entity),
    )
    monkeypatch.setattr(
        engine,
        "perform_global_ofac_search",
        lambda *a, **kw: None,
    )

    class _StubBrief:
        def __init__(self, *a, **kw):  # noqa: D401 — mirror the real signature
            pass

        def generate(self) -> dict:
            return {
                "recommendation": "ESCALATE_FOR_REVIEW",
                "confidence_band": "HIGH",
                "summary_claims": [
                    {"text": "Tehran office mentioned", "citations": ["https://example.com/tehran"]}
                ],
                "risk_factor_claims": [],
                "suggested_next_steps": ["Contact the entity for clarification."],
                "unverified_claims_dropped": 0,
                "verification_report": {
                    "total_claims": 1, "verified_claims": 1,
                    "dropped_claims": 0, "per_claim": [],
                },
                "evidence_count": 1,
            }

    monkeypatch.setattr(engine, "InvestigatorBriefGenerator", _StubBrief)

    # Silence screener: no real list files in a tmp test env.
    monkeypatch.setattr(main, "_get_screener", lambda: None)

    return app_env


def test_analyze_endpoint_runs_pipeline_to_completion(stubbed_pipeline):
    client, storage, _, _ = stubbed_pipeline
    resp = client.post(
        "/api/analyze",
        json={
            "website": "example.com",
            "business_name": "Example Corp",
            "legal_name": "",
            "skip_content": False,
            "run_name_cooccurrence": False,
        },
    )
    assert resp.status_code == 200, resp.text
    job_id = resp.json()["job_id"]

    status = _wait_for_job(client, job_id)
    assert status["status"] == "completed", status

    # Result payload round-trips via the DB.
    result_resp = client.get(f"/api/result/{job_id}")
    assert result_resp.status_code == 200, result_resp.text
    result = result_resp.json()
    assert result["llm_verdict"]["recommendation"] == "ESCALATE_FOR_REVIEW"

    iran_report = next(r for r in result["reports"] if r["country"] == "Iran")
    assert iran_report["total_urls_analyzed"] == 1

    # Findings persisted to the DB so the HITL queue sees them.
    with storage.get_session() as session:
        findings = (
            session.query(storage.Finding)
            .filter(storage.Finding.job_id == job_id)
            .all()
        )
        assert len(findings) >= 1
        assert any(f.risk_type == "DIRECT_BUSINESS" for f in findings)


def test_analyze_rejects_empty_request(app_env):
    client, _, _, _ = app_env
    resp = client.post(
        "/api/analyze",
        json={
            "website": "",
            "business_name": "",
            "legal_name": "",
            "skip_content": False,
            "run_name_cooccurrence": False,
        },
    )
    assert resp.status_code == 400


def test_audit_chain_is_intact_after_run(stubbed_pipeline):
    client, _, _, _ = stubbed_pipeline
    resp = client.post(
        "/api/analyze",
        json={"website": "example.com", "business_name": "", "legal_name": "",
              "skip_content": False, "run_name_cooccurrence": False},
    )
    job_id = resp.json()["job_id"]
    _wait_for_job(client, job_id)

    chain_resp = client.get(f"/api/jobs/{job_id}/audit-chain")
    assert chain_resp.status_code == 200, chain_resp.text
    body = chain_resp.json()
    assert body.get("valid") is True, body
