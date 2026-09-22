"""initial_schema

Revision ID: 79b7fd57a39e
Revises: 
Create Date: 2026-09-19 09:06:21.454228

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '79b7fd57a39e'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. accounts
    account_status_enum = postgresql.ENUM(
        'pending_validation',
        'field_created',
        'awaiting_manual_bot',
        'bot_linked',
        'awaiting_pipeline_setup',
        'configured',
        'verified',
        'error',
        name='account_status',
        create_type=False
    )
    account_status_enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        'accounts',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('name', sa.Text(), nullable=True),
        sa.Column('subdomain', sa.String(length=255), nullable=False),
        sa.Column('amo_account_id', sa.BigInteger(), nullable=True),
        sa.Column('encrypted_token', sa.LargeBinary(), nullable=False),
        sa.Column('encrypted_refresh_token', sa.LargeBinary(), nullable=True),
        sa.Column('token_created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('token_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'status',
            postgresql.ENUM(
                'pending_validation',
                'field_created',
                'awaiting_manual_bot',
                'bot_linked',
                'awaiting_pipeline_setup',
                'configured',
                'verified',
                'error',
                name='account_status',
                create_type=False
            ),
            nullable=False
        ),
        sa.Column('ai_reply_field_id', sa.BigInteger(), nullable=True),
        sa.Column('bot_id', sa.BigInteger(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('webhook_id', sa.BigInteger(), nullable=True),
        sa.Column('webhook_auto_registered', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('webhook_verified', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_accounts_subdomain'), 'accounts', ['subdomain'], unique=True)

    # 2. pipelines
    op.create_table(
        'pipelines',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('account_id', sa.UUID(), nullable=False),
        sa.Column('amo_pipeline_id', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('synced_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'amo_pipeline_id', name='uq_pipeline_account_amo_id')
    )
    op.create_index(op.f('ix_pipelines_account_id'), 'pipelines', ['account_id'], unique=False)

    # 3. field_mappings
    op.create_table(
        'field_mappings',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('account_id', sa.UUID(), nullable=False),
        sa.Column('amo_field_id', sa.BigInteger(), nullable=False),
        sa.Column('field_name', sa.Text(), nullable=False),
        sa.Column('field_type', sa.String(length=50), nullable=False),
        sa.Column('is_enabled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('ai_hint', sa.Text(), nullable=True),
        sa.Column('overwrite_if_filled', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('synced_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'amo_field_id', name='uq_field_account_amo_id')
    )
    op.create_index(op.f('ix_field_mappings_account_id'), 'field_mappings', ['account_id'], unique=False)

    # 4. ai_configs
    op.create_table(
        'ai_configs',
        sa.Column('account_id', sa.UUID(), nullable=False),
        sa.Column('communicator_prompt', sa.Text(), nullable=False),
        sa.Column('communicator_model', sa.String(length=100), server_default='gemini-3.1-flash-lite', nullable=False),
        sa.Column('extractor_model', sa.String(length=100), server_default='gemini-3.1-flash-lite', nullable=False),
        sa.Column('temperature', sa.Numeric(precision=3, scale=2), server_default='0.40', nullable=False),
        sa.Column('handover_after_stuck', sa.Integer(), server_default='4', nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('account_id')
    )

    # 5. leads
    op.create_table(
        'leads',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('account_id', sa.UUID(), nullable=False),
        sa.Column('amo_lead_id', sa.BigInteger(), nullable=False),
        sa.Column('amo_contact_id', sa.BigInteger(), nullable=True),
        sa.Column('stuck_count', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('handover_required', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'amo_lead_id', name='uq_lead_account_amo_id')
    )
    op.create_index(op.f('ix_leads_account_id'), 'leads', ['account_id'], unique=False)
    op.create_index(op.f('ix_leads_amo_lead_id'), 'leads', ['amo_lead_id'], unique=False)

    # 6. conversation_messages
    op.create_table(
        'conversation_messages',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('lead_id', sa.UUID(), nullable=False),
        sa.Column('role', sa.String(length=20), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_conversation_messages_lead_id'), 'conversation_messages', ['lead_id'], unique=False)
    op.create_index('idx_conv_lead_time', 'conversation_messages', ['lead_id', 'created_at'], unique=False)

    # 7. extraction_logs
    op.create_table(
        'extraction_logs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('lead_id', sa.UUID(), nullable=False),
        sa.Column('raw_response', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('applied_fields', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['lead_id'], ['leads.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_extraction_logs_lead_id'), 'extraction_logs', ['lead_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_extraction_logs_lead_id'), table_name='extraction_logs')
    op.drop_table('extraction_logs')
    op.drop_index('idx_conv_lead_time', table_name='conversation_messages')
    op.drop_index(op.f('ix_conversation_messages_lead_id'), table_name='conversation_messages')
    op.drop_table('conversation_messages')
    op.drop_index(op.f('ix_leads_amo_lead_id'), table_name='leads')
    op.drop_index(op.f('ix_leads_account_id'), table_name='leads')
    op.drop_table('leads')
    op.drop_table('ai_configs')
    op.drop_index(op.f('ix_field_mappings_account_id'), table_name='field_mappings')
    op.drop_table('field_mappings')
    op.drop_index(op.f('ix_pipelines_account_id'), table_name='pipelines')
    op.drop_table('pipelines')
    op.drop_index(op.f('ix_accounts_subdomain'), table_name='accounts')
    op.drop_table('accounts')
    op.execute('DROP TYPE IF EXISTS account_status')
