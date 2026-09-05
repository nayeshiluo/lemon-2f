"""add_social_redpacket_and_wheel

Revision ID: e5f83a2b1c47
Revises: c4d91e8a5b23
Create Date: 2026-09-05 08:15:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f83a2b1c47'
down_revision: Union[str, None] = 'c4d91e8a5b23'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. 创建 red_packets 表
    op.create_table(
        'red_packets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('sender_id', sa.Integer(), nullable=False),
        sa.Column('packet_type', sa.String(length=16), nullable=False, server_default='random'),
        sa.Column('passcode', sa.String(length=64), nullable=True),
        sa.Column('title', sa.String(length=128), nullable=False, server_default='二楼发红包喽！'),
        sa.Column('total_points', sa.Integer(), nullable=False),
        sa.Column('remaining_points', sa.Integer(), nullable=False),
        sa.Column('total_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('remaining_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('status', sa.String(length=16), nullable=False, server_default='active'),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['sender_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('red_packets', schema=None) as batch_op:
        batch_op.create_index('ix_red_packets_id', ['id'], unique=False)
        batch_op.create_index('ix_red_packets_sender_id', ['sender_id'], unique=False)
        batch_op.create_index('ix_red_packets_status', ['status'], unique=False)
        batch_op.create_index('idx_redpacket_status_created', ['status', 'created_at'], unique=False)

    # 2. 创建 red_packet_claims 表
    op.create_table(
        'red_packet_claims',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('packet_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('points', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['packet_id'], ['red_packets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('packet_id', 'user_id', name='uq_user_packet_claim')
    )
    with op.batch_alter_table('red_packet_claims', schema=None) as batch_op:
        batch_op.create_index('ix_red_packet_claims_id', ['id'], unique=False)
        batch_op.create_index('ix_red_packet_claims_packet_id', ['packet_id'], unique=False)
        batch_op.create_index('ix_red_packet_claims_user_id', ['user_id'], unique=False)

    # 3. 创建 lucky_wheel_records 表
    op.create_table(
        'lucky_wheel_records',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('cost_points', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('prize_name', sa.String(length=128), nullable=False),
        sa.Column('prize_type', sa.String(length=32), nullable=False),
        sa.Column('prize_points', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('prize_code', sa.String(length=128), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('lucky_wheel_records', schema=None) as batch_op:
        batch_op.create_index('ix_lucky_wheel_records_id', ['id'], unique=False)
        batch_op.create_index('ix_lucky_wheel_records_user_id', ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('lucky_wheel_records', schema=None) as batch_op:
        batch_op.drop_index('ix_lucky_wheel_records_user_id')
        batch_op.drop_index('ix_lucky_wheel_records_id')
    op.drop_table('lucky_wheel_records')

    with op.batch_alter_table('red_packet_claims', schema=None) as batch_op:
        batch_op.drop_index('ix_red_packet_claims_user_id')
        batch_op.drop_index('ix_red_packet_claims_packet_id')
        batch_op.drop_index('ix_red_packet_claims_id')
    op.drop_table('red_packet_claims')

    with op.batch_alter_table('red_packets', schema=None) as batch_op:
        batch_op.drop_index('idx_redpacket_status_created')
        batch_op.drop_index('ix_red_packets_status')
        batch_op.drop_index('ix_red_packets_sender_id')
        batch_op.drop_index('ix_red_packets_id')
    op.drop_table('red_packets')
