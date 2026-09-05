"""add_wanted_crowdfunding_and_claim_fields

Revision ID: d8f72a1e9b41
Revises: e6a88b1f2d34
Create Date: 2026-09-05 06:35:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8f72a1e9b41'
down_revision: Union[str, None] = 'e6a88b1f2d34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. 给 wanted_tasks 增加众筹与认领相关字段
    with op.batch_alter_table('wanted_tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('backer_count', sa.Integer(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('claimed_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('claim_expires_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index('idx_wanted_status_bounty', ['status', 'bounty_points'], unique=False)

    # 2. 创建 wanted_backers 表
    op.create_table(
        'wanted_backers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('wanted_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('points', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['wanted_id'], ['wanted_tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('wanted_backers', schema=None) as batch_op:
        batch_op.create_index('ix_wanted_backers_id', ['id'], unique=False)
        batch_op.create_index('ix_wanted_backers_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_wanted_backers_wanted_id', ['wanted_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('wanted_backers', schema=None) as batch_op:
        batch_op.drop_index('ix_wanted_backers_wanted_id')
        batch_op.drop_index('ix_wanted_backers_user_id')
        batch_op.drop_index('ix_wanted_backers_id')
    op.drop_table('wanted_backers')

    with op.batch_alter_table('wanted_tasks', schema=None) as batch_op:
        batch_op.drop_index('idx_wanted_status_bounty')
        batch_op.drop_column('claim_expires_at')
        batch_op.drop_column('claimed_at')
        batch_op.drop_column('backer_count')
