"""add language column to excerpts for Phase 5

Revision ID: 0003_add_excerpt_language
Revises: 0002_add_users
Create Date: 2026-04-18

Phase 5 multilingual flagging: store the langdetect ISO-639-1 code per
excerpt so the UI can badge non-English sources and surface the honest
"machine translation not yet included" caveat. SQLite batch-mode ALTER
for the nullable add + index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_add_excerpt_language"
down_revision: Union[str, None] = "0002_add_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("excerpts") as batch_op:
        batch_op.add_column(sa.Column("language", sa.String(8), nullable=True))
    op.create_index("ix_excerpts_language", "excerpts", ["language"])


def downgrade() -> None:
    op.drop_index("ix_excerpts_language", table_name="excerpts")
    with op.batch_alter_table("excerpts") as batch_op:
        batch_op.drop_column("language")
