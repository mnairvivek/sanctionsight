"""
Phase 3 — lightweight JWT auth for the HITL workflow.

Design notes
------------
We chose PyJWT + passlib-bcrypt over `fastapi-users` for the MVP. Three roles
(`analyst`, `reviewer`, `admin`) are enforced by a role-check dependency.
Reviewer+admin can sign jobs off; only admin can reopen a signed-off job or
register new users. Anything that mutates state is audit-logged by the caller
via `audit.log_analyst_action`.

All tokens are short-lived JWTs signed with HS256. The secret is read from
`SANCTIONSIGHT_JWT_SECRET`. There is no refresh token flow — if sessions
matter more than simplicity later, bolt one on top of this module.

Env vars
--------
SANCTIONSIGHT_JWT_SECRET       required in production; a dev fallback is
                                generated at import time so tests still run.
SANCTIONSIGHT_JWT_EXP_MINUTES  default 480 (8h shift).
SANCTIONSIGHT_ADMIN_EMAIL      seeds the first admin on startup if present.
SANCTIONSIGHT_ADMIN_PASSWORD   paired with the above. Plain text; rotate asap.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("auth")

# ---------------------------------------------------------------------------
# Optional-dependency import shim
# ---------------------------------------------------------------------------
# The auth module only works when jwt + passlib are installed. The rest of
# the app (analysis, audit log, reports) must keep booting without them, so
# we guard imports and expose `AUTH_AVAILABLE` for callers.

try:
    import jwt  # PyJWT
    _JWT_AVAILABLE = True
except Exception:  # pragma: no cover — install-time branch
    jwt = None  # type: ignore[assignment]
    _JWT_AVAILABLE = False

try:
    from passlib.context import CryptContext
    _PASSLIB_AVAILABLE = True
    _pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
except Exception:  # pragma: no cover
    CryptContext = None  # type: ignore[assignment]
    _PASSLIB_AVAILABLE = False
    _pwd_context = None  # type: ignore[assignment]

AUTH_AVAILABLE = _JWT_AVAILABLE and _PASSLIB_AVAILABLE


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JWT_ALGORITHM = "HS256"


def _jwt_secret() -> str:
    explicit = os.environ.get("SANCTIONSIGHT_JWT_SECRET")
    if explicit:
        return explicit
    # Dev fallback — stable for the lifetime of the process but never reused
    # across restarts, so tokens issued before a restart are rejected after.
    global _EPHEMERAL_SECRET
    try:
        return _EPHEMERAL_SECRET  # type: ignore[name-defined]
    except NameError:
        _EPHEMERAL_SECRET = secrets.token_urlsafe(48)  # type: ignore[assignment]
        logger.warning(
            "SANCTIONSIGHT_JWT_SECRET not set — using ephemeral secret. "
            "Set this env var in production to keep sessions across restarts."
        )
        return _EPHEMERAL_SECRET  # type: ignore[name-defined]


def _jwt_exp_minutes() -> int:
    try:
        return int(os.environ.get("SANCTIONSIGHT_JWT_EXP_MINUTES", "480"))
    except ValueError:
        return 480


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    if not _PASSLIB_AVAILABLE:
        raise RuntimeError("passlib is not installed — cannot hash passwords")
    return _pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    if not _PASSLIB_AVAILABLE:
        return False
    try:
        return _pwd_context.verify(plain, hashed)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------

def create_access_token(
    *, subject: str, role: str, extra: Optional[dict] = None
) -> str:
    if not _JWT_AVAILABLE:
        raise RuntimeError("PyJWT is not installed — cannot issue tokens")
    now = datetime.utcnow()
    payload = {
        "sub": subject,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=_jwt_exp_minutes())).timestamp()),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    if not _JWT_AVAILABLE:
        raise RuntimeError("PyJWT is not installed")
    return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])


# ---------------------------------------------------------------------------
# FastAPI dependencies — only usable when AUTH_AVAILABLE
# ---------------------------------------------------------------------------

def _build_fastapi_deps():
    """Constructed lazily so that importing this module doesn't fail when
    FastAPI isn't the caller (e.g. CLI scripts, tests that exercise helpers
    directly)."""
    from fastapi import Depends, HTTPException, status
    from fastapi.security import OAuth2PasswordBearer

    oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

    def get_current_user(token: Optional[str] = Depends(oauth2_scheme)) -> dict:
        if not AUTH_AVAILABLE:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Auth is not configured on this server")
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token",
                                headers={"WWW-Authenticate": "Bearer"})
        try:
            claims = decode_token(token)
        except Exception as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {exc}",
                                headers={"WWW-Authenticate": "Bearer"})
        # Defensive: confirm user still exists + not disabled. Lazy-import
        # storage so auth.py stays importable without SQLAlchemy.
        email = claims.get("sub", "")
        try:
            import storage
            with storage.get_session() as session:
                from sqlalchemy import select
                user = session.execute(select(storage.User).where(storage.User.email == email)).scalar_one_or_none()
        except Exception:
            user = None
        if user is None or user.disabled:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer valid")
        return {
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "display_name": user.display_name,
        }

    def require_role(*roles: str):
        """Return a dep that rejects users whose role isn't in `roles`.

        Pass multiple roles to accept any of them (e.g. reviewer AND admin can
        sign off; admin-only routes use just `require_role("admin")`)."""

        def _dep(user: dict = Depends(get_current_user)) -> dict:
            if user["role"] not in roles:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"Requires role in {roles}; caller is {user['role']}",
                )
            return user

        return _dep

    return oauth2_scheme, get_current_user, require_role


# Lazily materialise on first access so importing this module never requires
# FastAPI (e.g. unit tests that only exercise password hashing).
_oauth2_scheme = None
_get_current_user = None
_require_role = None


def _ensure_deps():
    global _oauth2_scheme, _get_current_user, _require_role
    if _get_current_user is None:
        _oauth2_scheme, _get_current_user, _require_role = _build_fastapi_deps()


def get_current_user_dep():
    _ensure_deps()
    return _get_current_user


def require_role(*roles: str):
    _ensure_deps()
    return _require_role(*roles)


def oauth2_scheme():
    _ensure_deps()
    return _oauth2_scheme


# ---------------------------------------------------------------------------
# Admin bootstrap
# ---------------------------------------------------------------------------

def seed_admin_from_env() -> Optional[str]:
    """If SANCTIONSIGHT_ADMIN_EMAIL/_PASSWORD are set and no admin exists yet,
    create one. Returns the email of the seeded admin, or None if no action
    was taken. Safe to call repeatedly (idempotent by email)."""
    email = os.environ.get("SANCTIONSIGHT_ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("SANCTIONSIGHT_ADMIN_PASSWORD", "")
    if not email or not password:
        return None
    if not AUTH_AVAILABLE:
        logger.warning("Admin seed skipped — auth dependencies not installed.")
        return None
    try:
        import storage
        from sqlalchemy import select
        with storage.get_session() as session:
            existing = session.execute(select(storage.User).where(storage.User.email == email)).scalar_one_or_none()
            if existing:
                return email
            user = storage.User(
                email=email,
                hashed_password=hash_password(password),
                role="admin",
                display_name="Bootstrap admin",
                disabled=False,
                created_at=datetime.utcnow(),
            )
            session.add(user)
            session.commit()
            logger.info("Seeded bootstrap admin: %s", email)
            return email
    except Exception as exc:
        logger.warning("Admin seed failed: %s", exc)
        return None
