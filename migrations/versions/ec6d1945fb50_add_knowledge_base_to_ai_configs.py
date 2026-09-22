"""add_knowledge_base_to_ai_configs

Revision ID: ec6d1945fb50
Revises: 79b7fd57a39e
Create Date: 2026-09-22 10:21:49.586269

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ec6d1945fb50'
down_revision: Union[str, Sequence[str], None] = '79b7fd57a39e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ai_configs', sa.Column('knowledge_base', sa.Text(), nullable=True))
    op.add_column('ai_configs', sa.Column('knowledge_mode', sa.String(length=50), server_default='plain_text', nullable=False))
    op.add_column('ai_configs', sa.Column('gemini_cache_name', sa.String(length=255), nullable=True))
    op.add_column('ai_configs', sa.Column('gemini_cache_expires_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('ai_configs', 'gemini_cache_expires_at')
    op.drop_column('ai_configs', 'gemini_cache_name')
    op.drop_column('ai_configs', 'knowledge_mode')
    op.drop_column('ai_configs', 'knowledge_base')
