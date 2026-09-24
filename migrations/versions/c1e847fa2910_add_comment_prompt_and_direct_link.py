"""add comment_prompt and direct_link to ai_configs

Revision ID: c1e847fa2910
Revises: b3f91c204a18
Create Date: 2026-09-24 06:52:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'c1e847fa2910'
down_revision: Union[str, None] = 'b3f91c204a18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ai_configs', sa.Column('comment_prompt', sa.Text(), nullable=True))
    op.add_column('ai_configs', sa.Column('direct_link', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column('ai_configs', 'direct_link')
    op.drop_column('ai_configs', 'comment_prompt')

