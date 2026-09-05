"""add_subtitle_submissions_table

Revision ID: b3c81e7a4d92
Revises: d8f72a1e9b41
Create Date: 2026-09-05 07:15:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c81e7a4d92'
down_revision: Union[str, None] = 'd8f72a1e9b41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'subtitle_submissions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('tmdb_id', sa.Integer(), nullable=False),
        sa.Column('media_type', sa.String(length=32), nullable=False, server_default='tv'),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('year', sa.Integer(), nullable=True),
        sa.Column('season', sa.Integer(), nullable=True),
        sa.Column('episode', sa.Integer(), nullable=True),
        sa.Column('language', sa.String(length=32), nullable=False, server_default='zh-CN'),
        sa.Column('is_default', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('is_forced', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('file_format', sa.String(length=16), nullable=False),
        sa.Column('file_size', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('dest_path', sa.String(length=512), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='accepted'),
        sa.Column('error_message', sa.String(length=255), nullable=True),
        sa.Column('reward_points', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('subtitle_submissions', schema=None) as batch_op:
        batch_op.create_index('ix_subtitle_submissions_id', ['id'], unique=False)
        batch_op.create_index('ix_subtitle_submissions_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_subtitle_submissions_tmdb_id', ['tmdb_id'], unique=False)
        batch_op.create_index('ix_subtitle_submissions_status', ['status'], unique=False)
        batch_op.create_index('idx_subtitles_exact_target', ['tmdb_id', 'media_type', 'season', 'episode'], unique=False)
        batch_op.create_index('idx_subtitles_user', ['user_id', 'status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('subtitle_submissions', schema=None) as batch_op:
        batch_op.drop_index('idx_subtitles_user')
        batch_op.drop_index('idx_subtitles_exact_target')
        batch_op.drop_index('ix_subtitle_submissions_status')
        batch_op.drop_index('ix_subtitle_submissions_tmdb_id')
        batch_op.drop_index('ix_subtitle_submissions_user_id')
        batch_op.drop_index('ix_subtitle_submissions_id')
    op.drop_table('subtitle_submissions')
