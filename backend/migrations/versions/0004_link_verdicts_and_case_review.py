"""per-link LLM verdicts + case-level analyst review

Revision ID: 0004_link_verdicts_and_case_review
Revises: 0003_add_excerpt_language
Create Date: 2026-04-20

Adds the ``link_verdicts`` table (one row per analyzed URL per job carrying
the LLM concern flag, its short reasoning, and analyst agree/disagree state)
plus five nullable columns on ``job_states`` for a case-level analyst summary
and agree/disagree vote against the aggregate investigator brief. All new
columns are nullable so the upgrade is safe against existing data; the
disagree-reason NOT-EMPTY check lives in the API layer, not the schema.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_link_verdicts_and_case_review"
down_revision: Union[str, None] = "0003_add_excerpt_language"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "link_verdicts",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("jobs.id"), nullable=False, index=True),
        sa.Column("url_hash", sa.String(32), nullable=False, index=True),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("llm_concern", sa.Boolean, nullable=True),
        sa.Column("llm_reasoning", sa.Text, nullable=True),
        sa.Column("llm_model", sa.String(128), nullable=True),
        sa.Column("llm_error", sa.Text, nullable=True),
        sa.Column("analyst_agrees", sa.Boolean, nullable=True),
        sa.Column("analyst_disagree_reason", sa.Text, nullable=True),
        sa.Column("analyst_updated_by", sa.String(64), nullable=True),
        sa.Column("analyst_updated_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("job_id", "url_hash", name="uq_link_verdict_per_job"),
    )

    with op.batch_alter_table("job_states") as batch_op:
        batch_op.add_column(sa.Column("analyst_case_summary", sa.Text, nullable=True))
        batch_op.add_column(sa.Column("analyst_agrees_with_brief", sa.Boolean, nullable=True))
        batch_op.add_column(sa.Column("analyst_case_disagree_reason", sa.Text, nullable=True))
        batch_op.add_column(sa.Column("analyst_case_updated_by", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("analyst_case_updated_at", sa.DateTime, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("job_states") as batch_op:
        batch_op.drop_column("analyst_case_updated_at")
        batch_op.drop_column("analyst_case_updated_by")
        batch_op.drop_column("analyst_case_disagree_reason")
        batch_op.drop_column("analyst_agrees_with_brief")
        batch_op.drop_column("analyst_case_summary")

    op.drop_table("link_verdicts")
