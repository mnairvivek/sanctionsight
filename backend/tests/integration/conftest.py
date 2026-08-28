"""Shared fixtures for integration tests.

Integration tests run the FastAPI app against a throwaway SQLite database
and stub external I/O (Google CSE, page fetches, the LLM) so they are
hermetic and safe for CI.
"""
from __future__ import annotations

from datetime import datetime

import pytest


def _require_deps() -> None:
    """Skip tests in environments where web/db dependencies aren't installed."""
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    _require_deps()
    """Fresh app + DB + audit directory per test.

    Returns ``(TestClient, storage_module, auth_module, main_module)``. The
    app is reloaded so the env-var-driven singletons (DB engine, JWT secret)
    pick up the test overrides.
    """
    monkeypatch.setenv("SANCTIONSIGHT_DB_PATH", str(tmp_path / "integration.db"))
    monkeypatch.setenv("SANCTIONSIGHT_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("SANCTIONSIGHT_SNAPSHOTS_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("SANCTIONSIGHT_JWT_SECRET", "integration-test-secret")

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
    return TestClient(main.app), storage, auth, main


@pytest.fixture
def seeded_users(app_env):
    """Create the three canonical HITL roles and return their credentials."""
    _, storage, auth, _ = app_env
    from sqlalchemy.exc import IntegrityError

    creds = {
        "analyst": ("analyst@test", "analyst-pw"),
        "reviewer": ("reviewer@test", "reviewer-pw"),
        "admin": ("admin@test", "admin-pw"),
    }
    with storage.get_session() as session:
        for role, (email, pw) in creds.items():
            try:
                session.add(storage.User(
                    email=email,
                    hashed_password=auth.hash_password(pw),
                    role=role,
                    disabled=False,
                    created_at=datetime.utcnow(),
                ))
                session.commit()
            except IntegrityError:
                session.rollback()
    return creds


def login(client, email: str, password: str) -> str:
    resp = client.post(
        "/api/auth/login",
        data={"username": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
