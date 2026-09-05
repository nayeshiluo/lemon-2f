"""add_watch_records_and_daily_rewards

Revision ID: c4d91e8a5b23
Revises: b3c81e7a4d92
Create Date: 2026-09-05 07:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d91e8a5b23'
down_revision: Union[str, None] = 'b3c81e7a4d92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. 创建 watch_records 表
    op.create_table(
        'watch_records',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('emby_user_id', sa.String(length=64), nullable=True),
        sa.Column('item_id', sa.String(length=64), nullable=True),
        sa.Column('tmdb_id', sa.Integer(), nullable=True),
        sa.Column('media_type', sa.String(length=32), nullable=False, server_default='tv'),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('season', sa.Integer(), nullable=True),
        sa.Column('episode', sa.Integer(), nullable=True),
        sa.Column('playback_seconds', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_completed', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('device_name', sa.String(length=128), nullable=True),
        sa.Column('client_name', sa.String(length=128), nullable=True),
        sa.Column('watched_date', sa.String(length=10), nullable=False),
        sa.Column('watched_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('watch_records', schema=None) as batch_op:
        batch_op.create_index('ix_watch_records_id', ['id'], unique=False)
        batch_op.create_index('ix_watch_records_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_watch_records_emby_user_id', ['emby_user_id'], unique=False)
        batch_op.create_index('ix_watch_records_item_id', ['item_id'], unique=False)
        batch_op.create_index('ix_watch_records_tmdb_id', ['tmdb_id'], unique=False)
        batch_op.create_index('ix_watch_records_watched_date', ['watched_date'], unique=False)
        batch_op.create_index('idx_watch_user_date', ['user_id', 'watched_date'], unique=False)
        batch_op.create_index('idx_watch_media_target', ['tmdb_id', 'media_type'], unique=False)

    # 2. 创建 daily_watch_rewards 表
    op.create_table(
        'daily_watch_rewards',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('reward_date', sa.String(length=10), nullable=False),
        sa.Column('points', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'reward_date', name='uq_user_daily_watch_reward')
    )
    with op.batch_alter_table('daily_watch_rewards', schema=None) as batch_op:
        batch_op.create_index('ix_daily_watch_rewards_id', ['id'], unique=False)
        batch_op.create_index('ix_daily_watch_rewards_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_daily_watch_rewards_reward_date', ['reward_date'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('daily_watch_rewards', schema=None) as batch_op:
        batch_op.drop_index('ix_daily_watch_rewards_reward_date')
        batch_op.drop_index('ix_daily_watch_rewards_user_id')
        batch_op.drop_index('ix_daily_watch_rewards_id')
    op.drop_table('daily_watch_rewards')

    with op.batch_alter_table('watch_records', schema=None) as batch_op:
        batch_op.drop_index('idx_watch_media_target')
        batch_op.drop_index('idx_watch_user_date')
        batch_op.drop_index('ix_watch_records_watched_date')
        batch_op.drop_index('ix_watch_records_tmdb_id')
        batch_op.drop_index('ix_watch_records_item_id')
        batch_op.drop_index('ix_watch_records_emby_user_id')
        batch_op.drop_index('ix_watch_records_user_id')
        batch_op.drop_index('ix_watch_records_id')
    op.drop_table('watch_records')
