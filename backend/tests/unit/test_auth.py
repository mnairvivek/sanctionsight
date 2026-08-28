"""Phase 3 — password hashing + JWT roundtrip + role dependency behaviour.

Skipped cleanly when pyjwt/passlib aren't installed, so the rest of the
suite keeps running in a bare-stdlib environment.
"""

from __future__ import annotations

import pytest

pytest.importorskip("jwt")
pytest.importorskip("passlib")


def test_hash_and_verify_roundtrip(monkeypatch):
    import auth
    password = "correct horse battery staple"
    hashed = auth.hash_password(password)
    assert hashed != password
    assert auth.verify_password(password, hashed)
    assert not auth.verify_password("wrong", hashed)


def test_token_encodes_role_and_exp(monkeypatch):
    import auth
    monkeypatch.setenv("SANCTIONSIGHT_JWT_SECRET", "test-secret")
    monkeypatch.setenv("SANCTIONSIGHT_JWT_EXP_MINUTES", "15")

    token = auth.create_access_token(subject="alice@bank.test", role="reviewer")
    claims = auth.decode_token(token)

    assert claims["sub"] == "alice@bank.test"
    assert claims["role"] == "reviewer"
    assert "exp" in claims and "iat" in claims
    assert claims["exp"] > claims["iat"]


def test_decode_rejects_wrong_secret(monkeypatch):
    import auth
    monkeypatch.setenv("SANCTIONSIGHT_JWT_SECRET", "first-secret")
    token = auth.create_access_token(subject="bob@bank.test", role="analyst")

    monkeypatch.setenv("SANCTIONSIGHT_JWT_SECRET", "different-secret")
    # Re-import forces the module to re-read the env; using decode_token
    # directly still picks up the live env var via _jwt_secret().
    import importlib
    importlib.reload(auth)

    with pytest.raises(Exception):
        auth.decode_token(token)


def test_seed_admin_is_idempotent(tmp_path, monkeypatch):
    pytest.importorskip("sqlalchemy")
    monkeypatch.setenv("SANCTIONSIGHT_DB_PATH", str(tmp_path / "admin.db"))

    import importlib
    import storage
    monkeypatch.setattr(storage, "_engine", None, raising=False)
    monkeypatch.setattr(storage, "_SessionLocal", None, raising=False)
    importlib.reload(storage)
    storage.init_db()

    monkeypatch.setenv("SANCTIONSIGHT_ADMIN_EMAIL", "admin@bank.test")
    monkeypatch.setenv("SANCTIONSIGHT_ADMIN_PASSWORD", "init-pw")

    import auth
    importlib.reload(auth)

    first = auth.seed_admin_from_env()
    second = auth.seed_admin_from_env()
    assert first == "admin@bank.test"
    assert second == "admin@bank.test"  # idempotent — same email returned

    from sqlalchemy import select
    with storage.get_session() as session:
        rows = session.execute(select(storage.User)).scalars().all()
    assert len(rows) == 1
    assert rows[0].role == "admin"
