"""initial Phase 1 schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-04-18

All Phase 1 tables — jobs, excerpts, findings, list matches, audit events,
HITL workflow, provenance, and list snapshots. Kept in one migration so the
first install is a single `alembic upgrade head`.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("model_version_hash", sa.String(64), nullable=False),
        sa.Column("prompt_template_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("schema_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("spacy_model", sa.String(64), nullable=False, server_default=""),
        sa.Column("rules_version", sa.String(64), nullable=False, server_default=""),
        sa.Column("deployed_at", sa.DateTime, nullable=False),
        sa.Column("notes", sa.Text, nullable=True),
        sa.UniqueConstraint("model_version_hash", name="uq_model_version_hash"),
    )

    op.create_table(
        "list_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("list_name", sa.String(64), nullable=False),
        sa.Column("downloaded_at", sa.DateTime, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("entity_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("path", sa.String(512), nullable=True),
        sa.Column("active_from", sa.DateTime, nullable=False),
        sa.Column("active_to", sa.DateTime, nullable=True),
        sa.UniqueConstraint("list_name", "sha256", name="uq_list_snapshot"),
    )
    op.create_index("ix_list_snapshots_list_name", "list_snapshots", ["list_name"])
    op.create_index("ix_list_snapshots_sha256", "list_snapshots", ["sha256"])
    op.create_index("ix_list_snapshots_active_from", "list_snapshots", ["active_from"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("completed_at", sa.DateTime, nullable=True),
        sa.Column("website", sa.String(512), nullable=False, server_default=""),
        sa.Column("business_name", sa.String(512), nullable=False, server_default=""),
        sa.Column("legal_name", sa.String(512), nullable=False, server_default=""),
        sa.Column("skip_content", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("run_name_cooccurrence", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("progress", sa.Float, nullable=False, server_default="0"),
        sa.Column("current_step", sa.String(256), nullable=False, server_default=""),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("is_sanctioned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("html_report_path", sa.String(512), nullable=True),
        sa.Column("result_json", sa.Text, nullable=True),
        sa.Column("model_version_id", sa.Integer, sa.ForeignKey("model_versions.id"), nullable=True),
    )
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "excerpts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("source_id", sa.String(32), nullable=False),
        sa.Column("excerpt_id", sa.String(32), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("country", sa.String(64), nullable=True),
        sa.Column("risk_type", sa.String(64), nullable=True),
        sa.Column("risk_score", sa.Float, nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("trigger_sentence", sa.Text, nullable=True),
        sa.Column("text", sa.Text, nullable=False, server_default=""),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("extraction_type", sa.String(32), nullable=True),
        sa.UniqueConstraint("job_id", "excerpt_id", name="uq_excerpt_per_job"),
    )
    op.create_index("ix_excerpts_job_id", "excerpts", ["job_id"])
    op.create_index("ix_excerpts_source_id", "excerpts", ["source_id"])
    op.create_index("ix_excerpts_excerpt_id", "excerpts", ["excerpt_id"])
    op.create_index("ix_excerpts_country", "excerpts", ["country"])
    op.create_index("ix_excerpts_content_hash", "excerpts", ["content_hash"])

    op.create_table(
        "findings",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("excerpt_pk", sa.Integer, sa.ForeignKey("excerpts.id"), nullable=True),
        sa.Column("country", sa.String(64), nullable=True),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("risk_type", sa.String(64), nullable=False, server_default="GENERAL"),
        sa.Column("risk_score", sa.Float, nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float, nullable=False, server_default="0"),
        sa.Column("sentence", sa.Text, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("list_snapshot_id", sa.Integer, sa.ForeignKey("list_snapshots.id"), nullable=True),
    )
    op.create_index("ix_findings_job_id", "findings", ["job_id"])
    op.create_index("ix_findings_excerpt_pk", "findings", ["excerpt_pk"])
    op.create_index("ix_findings_country", "findings", ["country"])

    op.create_table(
        "sanctions_list_matches",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("list_source", sa.String(64), nullable=False),
        sa.Column("listed_name", sa.String(512), nullable=False),
        sa.Column("query_name", sa.String(512), nullable=False),
        sa.Column("score", sa.Float, nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=True),
        sa.Column("country", sa.String(128), nullable=True),
        sa.Column("programs", sa.Text, nullable=True),
        sa.Column("source_ref", sa.String(256), nullable=True),
        sa.Column("official_url", sa.String(1024), nullable=True),
        sa.Column("list_snapshot_id", sa.Integer, sa.ForeignKey("list_snapshots.id"), nullable=True),
    )
    op.create_index("ix_sanctions_list_matches_job_id", "sanctions_list_matches", ["job_id"])
    op.create_index("ix_sanctions_list_matches_list_source", "sanctions_list_matches", ["list_source"])
    op.create_index("ix_sanctions_list_matches_score", "sanctions_list_matches", ["score"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime, nullable=False),
        sa.Column("actor", sa.String(128), nullable=False, server_default="system"),
        sa.Column("payload_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("prev_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("job_id", "sequence", name="uq_audit_sequence_per_job"),
    )
    op.create_index("ix_audit_events_job_id", "audit_events", ["job_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_hash", "audit_events", ["hash"])

    op.create_table(
        "finding_states",
        sa.Column("finding_id", sa.Integer, sa.ForeignKey("findings.id"), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("assigned_analyst_id", sa.String(64), nullable=True),
        sa.Column("fp_override", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("notes_md", sa.Text, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("updated_by", sa.String(64), nullable=True),
    )
    op.create_index("ix_finding_states_status", "finding_states", ["status"])
    op.create_index("ix_finding_states_assigned_analyst_id", "finding_states", ["assigned_analyst_id"])

    op.create_table(
        "finding_status_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("finding_id", sa.Integer, sa.ForeignKey("findings.id"), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("changed_at", sa.DateTime, nullable=False),
        sa.Column("changed_by", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
    )
    op.create_index("ix_finding_status_history_finding_id", "finding_status_history", ["finding_id"])

    op.create_table(
        "job_states",
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), primary_key=True),
        sa.Column("workflow_status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("final_disposition_notes", sa.Text, nullable=True),
        sa.Column("signed_off_by", sa.String(64), nullable=True),
        sa.Column("signed_off_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_job_states_workflow_status", "job_states", ["workflow_status"])

    op.create_table(
        "job_status_history",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("from_status", sa.String(32), nullable=True),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("changed_at", sa.DateTime, nullable=False),
        sa.Column("changed_by", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
    )
    op.create_index("ix_job_status_history_job_id", "job_status_history", ["job_id"])


def downgrade() -> None:
    op.drop_table("job_status_history")
    op.drop_table("job_states")
    op.drop_table("finding_status_history")
    op.drop_table("finding_states")
    op.drop_table("audit_events")
    op.drop_table("sanctions_list_matches")
    op.drop_table("findings")
    op.drop_table("excerpts")
    op.drop_table("jobs")
    op.drop_table("list_snapshots")
    op.drop_table("model_versions")
