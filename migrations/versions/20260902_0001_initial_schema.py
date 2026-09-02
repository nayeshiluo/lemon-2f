"""initial schema

Revision ID: 20260902_0001
Revises: 
Create Date: 2026-09-02 11:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '20260902_0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 1. users
    op.create_table(
        'users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=True),
        sa.Column('emby_user_id', sa.String(length=128), nullable=True),
        sa.Column('emby_username', sa.String(length=64), nullable=True),
        sa.Column('tg_user_id', sa.BigInteger(), nullable=True),
        sa.Column('tg_username', sa.String(length=64), nullable=True),
        sa.Column('role', sa.String(length=32), nullable=False, server_default='user'),
        sa.Column('is_whitelisted', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('balance', sa.Integer(), nullable=False, server_default='100'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('sign_in_streak', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_sign_in', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_users_id', 'users', ['id'], unique=False)
    op.create_index('ix_users_username', 'users', ['username'], unique=True)
    op.create_index('ix_users_emby_user_id', 'users', ['emby_user_id'], unique=True)
    op.create_index('ix_users_tg_user_id', 'users', ['tg_user_id'], unique=True)

    # 2. media_tasks
    op.create_table(
        'media_tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tmdb_id', sa.Integer(), nullable=False),
        sa.Column('media_type', sa.String(length=32), nullable=False, server_default='movie'),
        sa.Column('category', sa.String(length=64), nullable=True),
        sa.Column('region', sa.String(length=64), nullable=True),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('original_title', sa.String(length=255), nullable=True),
        sa.Column('year', sa.Integer(), nullable=True),
        sa.Column('poster_path', sa.String(length=255), nullable=True),
        sa.Column('overview', sa.String(length=2048), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='missing'),
        sa.Column('total_items_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('accepted_items_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_media_tasks_tmdb_id', 'media_tasks', ['tmdb_id'], unique=False)

    # 3. task_items
    op.create_table(
        'task_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.Integer(), sa.ForeignKey('media_tasks.id', ondelete='CASCADE'), nullable=False),
        sa.Column('season', sa.Integer(), nullable=True),
        sa.Column('episode', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='missing'),
        sa.Column('reserved_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('reserved_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('accepted_submission_item_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id')
    )

    # 4. submissions
    op.create_table(
        'submissions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('task_id', sa.Integer(), sa.ForeignKey('media_tasks.id'), nullable=True),
        sa.Column('tmdb_id', sa.Integer(), nullable=False),
        sa.Column('media_type', sa.String(length=32), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('year', sa.Integer(), nullable=True),
        sa.Column('magnet_uri', sa.Text(), nullable=False),
        sa.Column('torrent_hash', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('reward_points', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id')
    )

    # 5. submission_items
    op.create_table(
        'submission_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('submission_id', sa.Integer(), sa.ForeignKey('submissions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('task_id', sa.Integer(), sa.ForeignKey('media_tasks.id'), nullable=False),
        sa.Column('task_item_id', sa.Integer(), sa.ForeignKey('task_items.id'), nullable=True),
        sa.Column('media_type', sa.String(length=32), nullable=False),
        sa.Column('season', sa.Integer(), nullable=True),
        sa.Column('episode', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='pending'),
        sa.Column('source_file', sa.Text(), nullable=True),
        sa.Column('dest_file', sa.Text(), nullable=True),
        sa.Column('file_size', sa.BigInteger(), server_default='0'),
        sa.Column('duration_seconds', sa.Float(), server_default='0.0'),
        sa.Column('width', sa.Integer(), server_default='0'),
        sa.Column('height', sa.Integer(), server_default='0'),
        sa.Column('video_codec', sa.String(length=32), nullable=True),
        sa.Column('audio_codec', sa.String(length=32), nullable=True),
        sa.Column('bitrate_kbps', sa.Integer(), server_default='0'),
        sa.Column('is_4k', sa.Boolean(), server_default='false'),
        sa.Column('raw_qc_json', sa.Text(), nullable=True),
        sa.Column('reward_points', sa.Integer(), server_default='0'),
        sa.Column('is_rewarded', sa.Boolean(), server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id')
    )

    # 6. download_jobs
    op.create_table(
        'download_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('submission_id', sa.Integer(), sa.ForeignKey('submissions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('torrent_hash', sa.String(length=64), nullable=False),
        sa.Column('save_path', sa.Text(), nullable=True),
        sa.Column('content_path', sa.Text(), nullable=True),
        sa.Column('progress', sa.Float(), server_default='0.0'),
        sa.Column('download_speed', sa.BigInteger(), server_default='0'),
        sa.Column('eta_seconds', sa.Integer(), server_default='0'),
        sa.Column('downloaded_bytes', sa.BigInteger(), server_default='0'),
        sa.Column('last_progress_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='queued'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('submission_id')
    )

    # 7. points_ledger
    op.create_table(
        'points_ledger',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('amount', sa.Integer(), nullable=False),
        sa.Column('balance_after', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=64), nullable=False),
        sa.Column('ref_type', sa.String(length=64), nullable=True),
        sa.Column('ref_id', sa.String(length=128), nullable=True),
        sa.Column('idempotency_key', sa.String(length=128), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('idempotency_key')
    )

    # 8. sign_in_records
    op.create_table(
        'sign_in_records',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sign_date', sa.Date(), nullable=False),
        sa.Column('reward_coins', sa.Integer(), nullable=False),
        sa.Column('streak', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'sign_date', name='uq_user_sign_date')
    )

    # 9. wanted_tasks
    op.create_table(
        'wanted_tasks',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('creator_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('tmdb_id', sa.Integer(), nullable=False),
        sa.Column('media_type', sa.String(length=32), nullable=False, server_default='tv'),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('year', sa.Integer(), nullable=True),
        sa.Column('season', sa.Integer(), nullable=True),
        sa.Column('episode', sa.Integer(), nullable=True),
        sa.Column('bounty_points', sa.Integer(), nullable=False, server_default='50'),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='open'),
        sa.Column('claimant_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('submission_item_id', sa.Integer(), sa.ForeignKey('submission_items.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id')
    )

    # 10. shop_items & shop_orders
    op.create_table(
        'shop_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=128), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(length=64), nullable=False, server_default='emby_vip'),
        sa.Column('cost_points', sa.Integer(), nullable=False),
        sa.Column('stock', sa.Integer(), nullable=False, server_default='-1'),
        sa.Column('fulfillment_type', sa.String(length=32), nullable=False, server_default='manual'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('payload', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'shop_orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('shop_items.id'), nullable=False),
        sa.Column('cost_points', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='pending_fulfillment'),
        sa.Column('delivery_info', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id')
    )

    # 11. audit_logs & system_settings
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('actor_id', sa.Integer(), nullable=True),
        sa.Column('actor_username', sa.String(length=64), nullable=False),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('target_type', sa.String(length=64), nullable=True),
        sa.Column('target_id', sa.String(length=128), nullable=True),
        sa.Column('before_state', sa.Text(), nullable=True),
        sa.Column('after_state', sa.Text(), nullable=True),
        sa.Column('ip_address', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'system_settings',
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.PrimaryKeyConstraint('key')
    )

def downgrade() -> None:
    op.drop_table('system_settings')
    op.drop_table('audit_logs')
    op.drop_table('shop_orders')
    op.drop_table('shop_items')
    op.drop_table('wanted_tasks')
    op.drop_table('sign_in_records')
    op.drop_table('points_ledger')
    op.drop_table('download_jobs')
    op.drop_table('submission_items')
    op.drop_table('submissions')
    op.drop_table('task_items')
    op.drop_table('media_tasks')
    op.drop_table('users')
