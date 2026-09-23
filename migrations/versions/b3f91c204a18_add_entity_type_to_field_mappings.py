"""add entity_type to field_mappings

Revision ID: b3f91c204a18
Revises: 6a2e12abdcf5
Create Date: 2026-09-23 09:37:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'b3f91c204a18'
down_revision: Union[str, None] = '6a2e12abdcf5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'field_mappings',
        sa.Column('entity_type', sa.String(20), nullable=False, server_default='lead')
    )


def downgrade() -> None:
    op.drop_column('field_mappings', 'entity_type')

