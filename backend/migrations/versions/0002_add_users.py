"""add users table for Phase 3 HITL

Revision ID: 0002_add_users
Revises: 0001_initial_schema
Create Date: 2026-04-18

Introduces the `users` table — analyst/reviewer/admin accounts with hashed
passwords. Admin bootstrap happens from env vars on FastAPI startup.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_add_users"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(256), nullable=False),
        sa.Column("hashed_password", sa.String(256), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="analyst"),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("last_login_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("email", name="uq_user_email"),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_role", "users", ["role"])


def downgrade() -> None:
    op.drop_index("ix_users_role", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
