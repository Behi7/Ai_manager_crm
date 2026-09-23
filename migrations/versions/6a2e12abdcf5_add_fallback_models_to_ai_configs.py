"""add_fallback_models_to_ai_configs

Revision ID: 6a2e12abdcf5
Revises: ec6d1945fb50
Create Date: 2026-09-23 09:12:34.673513

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6a2e12abdcf5'
down_revision: Union[str, Sequence[str], None] = 'ec6d1945fb50'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('ai_configs', sa.Column('fallback_communicator_model', sa.String(length=100), server_default='gemini-2.5-flash', nullable=False))
    op.add_column('ai_configs', sa.Column('fallback_extractor_model', sa.String(length=100), server_default='gemini-2.5-flash', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('ai_configs', 'fallback_extractor_model')
    op.drop_column('ai_configs', 'fallback_communicator_model')
