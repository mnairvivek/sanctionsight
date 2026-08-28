"""
Phase 1 storage layer — SQLAlchemy 2.x models + engine/session factory.

SQLite with WAL journal mode is the embedded default. When the analyst
headcount grows (≥10) this migrates to Postgres via `sqlite3 .dump`
without application changes — ORM models are portable.

Tables
------
Job                   One row per analysis run. Parent of everything else.
Finding               One NLP finding (sentence-level risk signal).
Excerpt               Context window text retained as citable evidence.
SanctionsListMatch    A hit from the fuzzy-name screener.
AuditEvent            Mirror of the JSONL audit line (queryable copy).
FindingState          Current HITL state for a finding (pending/in_review/…).
FindingStatusHistory  Append-only history of state changes.
JobState              Case-level workflow state (draft/in_review/signed_off).
JobStatusHistory      Append-only history of job workflow changes.
ModelVersion          Records of LLM model, prompt template, rules versions.
ListSnapshot          Version of each sanctions list at the time of analysis.
User                  HITL analyst/reviewer/admin with hashed password + role.
LinkVerdict           Per-URL LLM concern flag + analyst agree/disagree.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

logger = logging.getLogger("storage")


# ---------------------------------------------------------------------------
# Deterministic ID helpers — duplicated from schemas.py so storage has no
# dependency on the Phase 2 schema module.
# ---------------------------------------------------------------------------

def stable_source_id(url: str) -> str:
    if not url:
        return "src_empty"
    return f"src_{hashlib.sha256(url.strip().encode('utf-8')).hexdigest()[:16]}"


def stable_excerpt_id(url: str, trigger_sentence: str, index: int) -> str:
    key = f"{url or ''}||{trigger_sentence or ''}||{index}"
    return f"exc_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def url_hash(url: str) -> str:
    """Stable short hash used as the URL-safe key for LinkVerdict rows."""
    return hashlib.sha256((url or "").strip().encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Core analysis tables
# ---------------------------------------------------------------------------

class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    website: Mapped[str] = mapped_column(String(512), default="")
    business_name: Mapped[str] = mapped_column(String(512), default="")
    legal_name: Mapped[str] = mapped_column(String(512), default="")
    skip_content: Mapped[bool] = mapped_column(Boolean, default=False)
    run_name_cooccurrence: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    current_step: Mapped[str] = mapped_column(String(256), default="")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    is_sanctioned: Mapped[bool] = mapped_column(Boolean, default=False)
    html_report_path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    result_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("model_versions.id"), nullable=True
    )

    findings: Mapped[list["Finding"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    excerpts: Mapped[list["Excerpt"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    list_matches: Mapped[list["SanctionsListMatch"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    audit_events: Mapped[list["AuditEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    state: Mapped[Optional["JobState"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", uselist=False
    )


class Excerpt(Base):
    __tablename__ = "excerpts"
    __table_args__ = (UniqueConstraint("job_id", "excerpt_id", name="uq_excerpt_per_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)

    source_id: Mapped[str] = mapped_column(String(32), index=True)
    excerpt_id: Mapped[str] = mapped_column(String(32), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    country: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    risk_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_sentence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    extraction_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    language: Mapped[Optional[str]] = mapped_column(String(8), nullable=True, index=True)

    job: Mapped[Job] = relationship(back_populates="excerpts")
    findings: Mapped[list["Finding"]] = relationship(back_populates="excerpt")


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    excerpt_pk: Mapped[Optional[int]] = mapped_column(ForeignKey("excerpts.id"), nullable=True, index=True)

    country: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    url: Mapped[str] = mapped_column(String(2048))
    risk_type: Mapped[str] = mapped_column(String(64), default="GENERAL")
    risk_score: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    sentence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    list_snapshot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("list_snapshots.id"), nullable=True
    )

    job: Mapped[Job] = relationship(back_populates="findings")
    excerpt: Mapped[Optional[Excerpt]] = relationship(back_populates="findings")
    state: Mapped[Optional["FindingState"]] = relationship(
        back_populates="finding", cascade="all, delete-orphan", uselist=False
    )


class SanctionsListMatch(Base):
    __tablename__ = "sanctions_list_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)

    list_source: Mapped[str] = mapped_column(String(64), index=True)
    listed_name: Mapped[str] = mapped_column(String(512))
    query_name: Mapped[str] = mapped_column(String(512))
    score: Mapped[float] = mapped_column(Float, index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    programs: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source_ref: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    official_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    list_snapshot_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("list_snapshots.id"), nullable=True
    )

    job: Mapped[Job] = relationship(back_populates="list_matches")


# ---------------------------------------------------------------------------
# Audit (DB mirror of the JSONL chain)
# ---------------------------------------------------------------------------

class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (UniqueConstraint("job_id", "sequence", name="uq_audit_sequence_per_job"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    actor: Mapped[str] = mapped_column(String(128), default="system")
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    hash: Mapped[str] = mapped_column(String(64), index=True)

    job: Mapped[Job] = relationship(back_populates="audit_events")


# ---------------------------------------------------------------------------
# HITL workflow (tables exist in Phase 1 but activated in Phase 3)
# ---------------------------------------------------------------------------

FINDING_STATUS_VALUES = (
    "pending", "in_review", "cleared_fp", "confirmed_match", "escalated",
)

JOB_WORKFLOW_VALUES = (
    "draft", "in_review", "signed_off", "reopened",
)


class FindingState(Base):
    __tablename__ = "finding_states"

    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    assigned_analyst_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    fp_override: Mapped[bool] = mapped_column(Boolean, default=False)
    notes_md: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    finding: Mapped[Finding] = relationship(back_populates="state")


class FindingStatusHistory(Base):
    __tablename__ = "finding_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("findings.id"), index=True)
    from_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    changed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class JobState(Base):
    __tablename__ = "job_states"

    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    workflow_status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    final_disposition_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    signed_off_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    signed_off_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Case-level analyst review, distinct from the sign-off rationale above.
    # analyst_agrees_with_brief: null = not yet reviewed, true/false = explicit.
    analyst_case_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    analyst_agrees_with_brief: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    analyst_case_disagree_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    analyst_case_updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    analyst_case_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    job: Mapped[Job] = relationship(back_populates="state")


class JobStatusHistory(Base):
    __tablename__ = "job_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    from_status: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32))
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    changed_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class LinkVerdict(Base):
    """Per-URL LLM concern flag + reasoning and analyst agree/disagree state.

    One row per analyzed URL per job. Populated at pipeline time with the LLM's
    binary concern read + 1–2 sentence rationale; analyst agreement is written
    later via the HITL endpoint. Disagreements require a reason so the audit
    record captures *why* the analyst overrode the machine read.
    """
    __tablename__ = "link_verdicts"
    __table_args__ = (
        UniqueConstraint("job_id", "url_hash", name="uq_link_verdict_per_job"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    url_hash: Mapped[str] = mapped_column(String(32), index=True)
    url: Mapped[str] = mapped_column(String(2048))

    llm_concern: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    llm_reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    llm_model: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    llm_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    analyst_agrees: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    analyst_disagree_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    analyst_updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    analyst_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# ---------------------------------------------------------------------------
# Provenance / versioning
# ---------------------------------------------------------------------------

class ModelVersion(Base):
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("model_version_hash", name="uq_model_version_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[str] = mapped_column(String(128))
    model_version_hash: Mapped[str] = mapped_column(String(64))
    prompt_template_version: Mapped[str] = mapped_column(String(64), default="")
    schema_version: Mapped[str] = mapped_column(String(64), default="")
    spacy_model: Mapped[str] = mapped_column(String(64), default="")
    rules_version: Mapped[str] = mapped_column(String(64), default="")
    deployed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ListSnapshot(Base):
    __tablename__ = "list_snapshots"
    __table_args__ = (UniqueConstraint("list_name", "sha256", name="uq_list_snapshot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    list_name: Mapped[str] = mapped_column(String(64), index=True)
    downloaded_at: Mapped[datetime] = mapped_column(DateTime)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    entity_count: Mapped[int] = mapped_column(Integer, default=0)
    path: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    active_from: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    active_to: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Users (Phase 3 HITL)
# ---------------------------------------------------------------------------

USER_ROLES = ("analyst", "reviewer", "admin")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(256), index=True)
    hashed_password: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(32), default="analyst", index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Engine / session
# ---------------------------------------------------------------------------

_BASE_DIR = Path(__file__).resolve().parent
_DEFAULT_DB_PATH = _BASE_DIR / "data" / "sanctionsight.db"


def _db_url_from_env() -> str:
    explicit = os.environ.get("SANCTIONSIGHT_DB_URL")
    if explicit:
        return explicit
    db_path = os.environ.get("SANCTIONSIGHT_DB_PATH", str(_DEFAULT_DB_PATH))
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def _apply_sqlite_pragmas(dbapi_connection, _):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


_engine = None
_SessionLocal: Optional[sessionmaker[Session]] = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        url = _db_url_from_env()
        _engine = create_engine(url, future=True, echo=False)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _apply_sqlite_pragmas)
        _SessionLocal = sessionmaker(_engine, expire_on_commit=False, future=True)
        logger.info("Storage engine initialised at %s", url)
    return _engine


def get_session() -> Session:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


def init_db() -> None:
    """Create all tables. Used for tests and first-run bootstrap; production
    uses Alembic migrations."""
    engine = get_engine()
    Base.metadata.create_all(engine)
