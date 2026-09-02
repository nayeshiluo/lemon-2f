"""add_estimated_reward_points

Revision ID: d66ba7fe9bfb
Revises: f45f58b6aa07
Create Date: 2026-09-02 14:45:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'd66ba7fe9bfb'
down_revision: Union[str, None] = 'f45f58b6aa07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # 核心安全三步法：保证历史已有海量数据的生产库平滑升级，绝不在已有表上直接加 NOT NULL 无默认列
    op.add_column('submissions', sa.Column('estimated_reward_points', sa.Integer(), nullable=True, server_default='0'))
    op.execute("UPDATE submissions SET estimated_reward_points = 0 WHERE estimated_reward_points IS NULL")
    
    # 针对不同数据库引擎安全变更约束
    with op.batch_alter_table('submissions') as batch_op:
        batch_op.alter_column('estimated_reward_points', existing_type=sa.Integer(), nullable=False)

def downgrade() -> None:
    with op.batch_alter_table('submissions') as batch_op:
        batch_op.drop_column('estimated_reward_points')
