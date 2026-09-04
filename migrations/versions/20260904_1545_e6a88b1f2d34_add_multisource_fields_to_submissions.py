"""add_multisource_fields_to_submissions

Revision ID: e6a88b1f2d34
Revises: c0a46cb4cea9
Create Date: 2026-09-04 15:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6a88b1f2d34'
down_revision: Union[str, None] = 'c0a46cb4cea9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('submissions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_type', sa.String(length=32), nullable=False, server_default='magnet'))
        batch_op.add_column(sa.Column('resource_url', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('pan_type', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('share_code', sa.String(length=64), nullable=True))
        batch_op.alter_column('magnet_uri', existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('submissions', schema=None) as batch_op:
        batch_op.alter_column('magnet_uri', existing_type=sa.Text(), nullable=False)
        batch_op.drop_column('share_code')
        batch_op.drop_column('pan_type')
        batch_op.drop_column('resource_url')
        batch_op.drop_column('source_type')
