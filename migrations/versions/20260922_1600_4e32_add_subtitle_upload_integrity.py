"""add unique fingerprint for subtitle uploads

Revision ID: 4e32d9b68c10
Revises: a1b2c3d4e5f6
Create Date: 2026-09-22 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4e32d9b68c10"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("subtitle_submissions", schema=None) as batch_op:
        # Nullable for legacy rows, which do not have the original bytes available
        # for a trustworthy fingerprint. New route writes always set this value.
        batch_op.add_column(sa.Column("dedupe_key", sa.String(length=64), nullable=True))
        batch_op.create_unique_constraint(
            "uq_subtitle_submissions_dedupe_key", ["dedupe_key"]
        )


def downgrade() -> None:
    with op.batch_alter_table("subtitle_submissions", schema=None) as batch_op:
        batch_op.drop_constraint("uq_subtitle_submissions_dedupe_key", type_="unique")
        batch_op.drop_column("dedupe_key")
