"""add_personal_token_and_tg_login_codes

EMOS 式个人长期登录 Token（users.personal_token_*）+ 一次性 TG 登录码表。

Revision ID: a1b2c3d4e5f6
Revises: e5f83a2b1c47
Create Date: 2026-09-06 01:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'e5f83a2b1c47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. users 表新增个人长期登录 Token 字段（只存 SHA-256 哈希，绝不落明文）
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('personal_token_hash', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('personal_token_created_at', sa.DateTime(timezone=True), nullable=True))

    # 2. 一次性 TG 登录码表（面板输 TG ID/@用户名 + 验证码免密登录）
    op.create_table(
        'tg_login_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tg_user_id', sa.BigInteger(), nullable=False),
        sa.Column('tg_username', sa.String(length=64), nullable=True),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('fail_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tg_login_codes_id'), 'tg_login_codes', ['id'], unique=False)
    op.create_index(op.f('ix_tg_login_codes_code_hash'), 'tg_login_codes', ['code_hash'], unique=True)
    op.create_index(op.f('ix_tg_login_codes_tg_user_id'), 'tg_login_codes', ['tg_user_id'], unique=False)
    op.create_index(op.f('ix_tg_login_codes_expires_at'), 'tg_login_codes', ['expires_at'], unique=False)
    # 同一 TG 同时仅允许一个未消费的登录码
    op.create_index('uq_tg_login_active_code', 'tg_login_codes', ['tg_user_id'], unique=True,
                    postgresql_where=sa.text('consumed_at IS NULL'),
                    sqlite_where=sa.text('consumed_at IS NULL'))


def downgrade() -> None:
    op.drop_index('uq_tg_login_active_code', table_name='tg_login_codes',
                  postgresql_where=sa.text('consumed_at IS NULL'),
                  sqlite_where=sa.text('consumed_at IS NULL'))
    op.drop_index(op.f('ix_tg_login_codes_expires_at'), table_name='tg_login_codes')
    op.drop_index(op.f('ix_tg_login_codes_tg_user_id'), table_name='tg_login_codes')
    op.drop_index(op.f('ix_tg_login_codes_code_hash'), table_name='tg_login_codes')
    op.drop_index(op.f('ix_tg_login_codes_id'), table_name='tg_login_codes')
    op.drop_table('tg_login_codes')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('personal_token_created_at')
        batch_op.drop_column('personal_token_hash')