"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-15 01:53:09.985925
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table('businesses',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=True),
    sa.Column('what_sells', sa.Text(), nullable=True),
    sa.Column('timezone', sa.String(length=64), server_default='America/Caracas', nullable=False),
    sa.Column('display_currency', sa.String(length=32), server_default='usd_bcv', nullable=False),
    sa.Column('payment_methods', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('onboarding_state', sa.String(length=32), server_default='ask_business_name', nullable=False),
    sa.Column('settings', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_businesses'))
    )
    op.create_table('exchange_rates',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('source', sa.String(length=32), nullable=False),
    sa.Column('rate', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('rate_date', sa.DateTime(timezone=True), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_exchange_rates'))
    )
    op.create_index('ix_exchange_rates_source_fetched', 'exchange_rates', ['source', 'fetched_at'], unique=False)
    op.create_table('audit_log',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('entity_type', sa.String(length=32), nullable=False),
    sa.Column('entity_id', sa.UUID(), nullable=False),
    sa.Column('action', sa.String(length=32), nullable=False),
    sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('actor_type', sa.String(length=32), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('turn_id', sa.UUID(), nullable=True),
    sa.Column('command', sa.String(length=64), nullable=True),
    sa.Column('at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_audit_log_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log'))
    )
    op.create_index('ix_audit_entity', 'audit_log', ['entity_type', 'entity_id'], unique=False)
    op.create_index(op.f('ix_audit_log_business_id'), 'audit_log', ['business_id'], unique=False)
    op.create_index(op.f('ix_audit_log_turn_id'), 'audit_log', ['turn_id'], unique=False)
    op.create_table('business_members',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('phone', sa.String(length=32), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=True),
    sa.Column('role', sa.String(length=32), server_default='owner', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_business_members_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_business_members')),
    sa.UniqueConstraint('business_id', 'phone', name='business_phone')
    )
    op.create_index(op.f('ix_business_members_phone'), 'business_members', ['phone'], unique=False)
    op.create_table('cash_sessions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_by_actor_type', sa.String(length=32), nullable=False),
    sa.Column('summary', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('times_closed', sa.Integer(), server_default='1', nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_cash_sessions_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cash_sessions')),
    sa.UniqueConstraint('business_id', 'business_date', name='business_date')
    )
    op.create_table('custom_rates',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('rate', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('label', sa.String(length=60), nullable=True),
    sa.Column('set_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('cleared_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_custom_rates_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_custom_rates'))
    )
    op.create_index(op.f('ix_custom_rates_business_id'), 'custom_rates', ['business_id'], unique=False)
    op.create_table('customers',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=160), nullable=False),
    sa.Column('normalized_name', sa.String(length=160), nullable=False),
    sa.Column('phone', sa.String(length=32), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('merged_into_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_customers_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['merged_into_id'], ['customers.id'], name=op.f('fk_customers_merged_into_id_customers')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customers'))
    )
    op.create_index(op.f('ix_customers_business_id'), 'customers', ['business_id'], unique=False)
    op.create_index('ix_customers_trgm', 'customers', ['normalized_name'], unique=False, postgresql_using='gin', postgresql_ops={'normalized_name': 'gin_trgm_ops'})
    op.create_table('inbound_messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=True),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('provider_message_id', sa.String(length=160), nullable=True),
    sa.Column('from_phone', sa.String(length=32), nullable=False),
    sa.Column('contact_name', sa.String(length=160), nullable=True),
    sa.Column('type', sa.String(length=16), nullable=False),
    sa.Column('body', sa.Text(), nullable=True),
    sa.Column('transcript', sa.Text(), nullable=True),
    sa.Column('media_url', sa.Text(), nullable=True),
    sa.Column('media_mime_type', sa.String(length=80), nullable=True),
    sa.Column('reply_to_provider_message_id', sa.String(length=160), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_inbound_messages_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_inbound_messages')),
    sa.UniqueConstraint('provider_message_id', name=op.f('uq_inbound_messages_provider_message_id'))
    )
    op.create_index('ix_inbound_business_received', 'inbound_messages', ['business_id', 'received_at'], unique=False)
    op.create_index('ix_inbound_fts', 'inbound_messages', [sa.literal_column("to_tsvector('spanish', coalesce(body, '') || ' ' || coalesce(transcript, ''))")], unique=False, postgresql_using='gin')
    op.create_index(op.f('ix_inbound_messages_reply_to_provider_message_id'), 'inbound_messages', ['reply_to_provider_message_id'], unique=False)
    op.create_table('message_entity_links',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('message_kind', sa.String(length=32), nullable=False),
    sa.Column('message_id', sa.UUID(), nullable=False),
    sa.Column('entity_type', sa.String(length=32), nullable=False),
    sa.Column('entity_id', sa.UUID(), nullable=False),
    sa.Column('relation', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_message_entity_links_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_message_entity_links')),
    sa.UniqueConstraint('message_kind', 'message_id', 'entity_type', 'entity_id', 'relation', name='link')
    )
    op.create_index('ix_links_message', 'message_entity_links', ['message_kind', 'message_id'], unique=False)
    op.create_table('outbound_messages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=True),
    sa.Column('turn_id', sa.UUID(), nullable=True),
    sa.Column('to_phone', sa.String(length=32), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('provider_message_id', sa.String(length=160), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), server_default='sent', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_outbound_messages_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_outbound_messages')),
    sa.UniqueConstraint('provider_message_id', name=op.f('uq_outbound_messages_provider_message_id'))
    )
    op.create_index(op.f('ix_outbound_messages_business_id'), 'outbound_messages', ['business_id'], unique=False)
    op.create_index(op.f('ix_outbound_messages_turn_id'), 'outbound_messages', ['turn_id'], unique=False)
    op.create_table('products',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('normalized_name', sa.String(length=200), nullable=False),
    sa.Column('sku', sa.String(length=64), nullable=True),
    sa.Column('price_amount', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('price_currency', sa.String(length=32), nullable=True),
    sa.Column('last_unit_cost_amount', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('last_unit_cost_currency', sa.String(length=32), nullable=True),
    sa.Column('last_cost_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_products_business_id_businesses'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_products'))
    )
    op.create_index(op.f('ix_products_business_id'), 'products', ['business_id'], unique=False)
    op.create_index('ix_products_trgm', 'products', ['normalized_name'], unique=False, postgresql_using='gin', postgresql_ops={'normalized_name': 'gin_trgm_ops'})
    op.create_index('uq_products_business_name', 'products', ['business_id', 'normalized_name'], unique=True, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_table('agent_turns',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=True),
    sa.Column('inbound_message_id', sa.UUID(), nullable=True),
    sa.Column('lane', sa.String(length=16), nullable=True),
    sa.Column('route_reason', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=32), server_default='running', nullable=False),
    sa.Column('stages', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('reply_text', sa.Text(), nullable=True),
    sa.Column('model_text', sa.Text(), nullable=True),
    sa.Column('grounding_ok', sa.Boolean(), nullable=True),
    sa.Column('grounding_detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('output_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_agent_turns_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['inbound_message_id'], ['inbound_messages.id'], name=op.f('fk_agent_turns_inbound_message_id_inbound_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_turns'))
    )
    op.create_index('ix_agent_turns_business_created', 'agent_turns', ['business_id', 'created_at'], unique=False)
    op.create_table('business_notes',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('subject_entity_type', sa.String(length=32), nullable=True),
    sa.Column('subject_entity_id', sa.UUID(), nullable=True),
    sa.Column('subject_name', sa.String(length=160), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('source_message_id', sa.UUID(), nullable=True),
    sa.Column('created_by_actor_type', sa.String(length=32), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_business_notes_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_message_id'], ['inbound_messages.id'], name=op.f('fk_business_notes_source_message_id_inbound_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_business_notes'))
    )
    op.create_index(op.f('ix_business_notes_business_id'), 'business_notes', ['business_id'], unique=False)
    op.create_index('ix_business_notes_fts', 'business_notes', [sa.literal_column("to_tsvector('spanish', text)")], unique=False, postgresql_using='gin')
    op.create_table('customer_payments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('payment_method', sa.String(length=32), nullable=True),
    sa.Column('unapplied_amount', sa.Numeric(precision=18, scale=4), server_default='0', nullable=False),
    sa.Column('source_message_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('currency', sa.String(length=32), nullable=False),
    sa.Column('amount_ves', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('amount_usd_bcv', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('amount_usd_market', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('rate_bcv', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('rate_market', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('rate_market_source', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_customer_payments_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_customer_payments_customer_id_customers')),
    sa.ForeignKeyConstraint(['source_message_id'], ['inbound_messages.id'], name=op.f('fk_customer_payments_source_message_id_inbound_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customer_payments'))
    )
    op.create_index('ix_customer_payments_business_date', 'customer_payments', ['business_id', 'business_date'], unique=False)
    op.create_index(op.f('ix_customer_payments_customer_id'), 'customer_payments', ['customer_id'], unique=False)
    op.create_table('expenses',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('category', sa.String(length=32), nullable=False),
    sa.Column('description', sa.String(length=300), nullable=False),
    sa.Column('payment_method', sa.String(length=32), nullable=True),
    sa.Column('source_message_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('currency', sa.String(length=32), nullable=False),
    sa.Column('amount_ves', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('amount_usd_bcv', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('amount_usd_market', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('rate_bcv', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('rate_market', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('rate_market_source', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_expenses_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_message_id'], ['inbound_messages.id'], name=op.f('fk_expenses_source_message_id_inbound_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_expenses'))
    )
    op.create_index('ix_expenses_business_date', 'expenses', ['business_id', 'business_date'], unique=False)
    op.create_table('purchases',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('supplier_name', sa.String(length=160), nullable=True),
    sa.Column('description', sa.String(length=300), nullable=False),
    sa.Column('payment_method', sa.String(length=32), nullable=True),
    sa.Column('source_message_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('currency', sa.String(length=32), nullable=False),
    sa.Column('amount_ves', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('amount_usd_bcv', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('amount_usd_market', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('rate_bcv', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('rate_market', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('rate_market_source', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_purchases_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_message_id'], ['inbound_messages.id'], name=op.f('fk_purchases_source_message_id_inbound_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_purchases'))
    )
    op.create_index('ix_purchases_business_date', 'purchases', ['business_id', 'business_date'], unique=False)
    op.create_table('sales',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=True),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('payment_status', sa.String(length=32), nullable=False),
    sa.Column('payment_method', sa.String(length=32), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('served_by_member_id', sa.UUID(), nullable=True),
    sa.Column('source_message_id', sa.UUID(), nullable=True),
    sa.Column('total_amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('total_currency', sa.String(length=32), nullable=False),
    sa.Column('total_ves', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('total_usd_bcv', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('total_usd_market', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('paid_amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('rate_bcv', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('rate_market', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('rate_market_source', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_sales_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_sales_customer_id_customers')),
    sa.ForeignKeyConstraint(['served_by_member_id'], ['business_members.id'], name=op.f('fk_sales_served_by_member_id_business_members')),
    sa.ForeignKeyConstraint(['source_message_id'], ['inbound_messages.id'], name=op.f('fk_sales_source_message_id_inbound_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sales'))
    )
    op.create_index('ix_sales_business_date', 'sales', ['business_id', 'business_date'], unique=False)
    op.create_index(op.f('ix_sales_customer_id'), 'sales', ['customer_id'], unique=False)
    op.create_table('llm_calls',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('turn_id', sa.UUID(), nullable=False),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=120), nullable=True),
    sa.Column('provider', sa.String(length=60), nullable=True),
    sa.Column('request', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('input_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('output_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('thinking_tokens', sa.Integer(), server_default='0', nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('finish_reason', sa.String(length=40), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('seq', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['turn_id'], ['agent_turns.id'], name=op.f('fk_llm_calls_turn_id_agent_turns'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_llm_calls'))
    )
    op.create_index(op.f('ix_llm_calls_turn_id'), 'llm_calls', ['turn_id'], unique=False)
    op.create_table('purchase_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('purchase_id', sa.UUID(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.UUID(), nullable=True),
    sa.Column('description', sa.String(length=300), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=12, scale=3), nullable=False),
    sa.Column('unit_cost_amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('unit_cost_currency', sa.String(length=32), nullable=False),
    sa.Column('line_amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('line_ves', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('line_usd_bcv', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('line_usd_market', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_purchase_items_product_id_products')),
    sa.ForeignKeyConstraint(['purchase_id'], ['purchases.id'], name=op.f('fk_purchase_items_purchase_id_purchases'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_purchase_items'))
    )
    op.create_index(op.f('ix_purchase_items_product_id'), 'purchase_items', ['product_id'], unique=False)
    op.create_index(op.f('ix_purchase_items_purchase_id'), 'purchase_items', ['purchase_id'], unique=False)
    op.create_table('receivables',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('business_id', sa.UUID(), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('sale_id', sa.UUID(), nullable=True),
    sa.Column('business_date', sa.Date(), nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('description', sa.String(length=300), nullable=True),
    sa.Column('source_message_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('version', sa.Integer(), server_default='1', nullable=False),
    sa.Column('amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('currency', sa.String(length=32), nullable=False),
    sa.Column('amount_ves', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('amount_usd_bcv', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('amount_usd_market', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('rate_bcv', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('rate_market', sa.Numeric(precision=18, scale=6), nullable=False),
    sa.Column('rate_market_source', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['business_id'], ['businesses.id'], name=op.f('fk_receivables_business_id_businesses'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], name=op.f('fk_receivables_customer_id_customers')),
    sa.ForeignKeyConstraint(['sale_id'], ['sales.id'], name=op.f('fk_receivables_sale_id_sales')),
    sa.ForeignKeyConstraint(['source_message_id'], ['inbound_messages.id'], name=op.f('fk_receivables_source_message_id_inbound_messages'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_receivables')),
    sa.UniqueConstraint('sale_id', name=op.f('uq_receivables_sale_id'))
    )
    op.create_index('ix_receivables_customer', 'receivables', ['business_id', 'customer_id'], unique=False)
    op.create_table('sale_items',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('sale_id', sa.UUID(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.UUID(), nullable=True),
    sa.Column('description', sa.String(length=300), nullable=False),
    sa.Column('quantity', sa.Numeric(precision=12, scale=3), nullable=False),
    sa.Column('unit_price_amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('unit_price_currency', sa.String(length=32), nullable=False),
    sa.Column('price_source', sa.String(length=16), nullable=False),
    sa.Column('line_amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('line_currency', sa.String(length=32), nullable=False),
    sa.Column('line_ves', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('line_usd_bcv', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('line_usd_market', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.Column('cost_ves', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('cost_usd_bcv', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('cost_usd_market', sa.Numeric(precision=18, scale=4), nullable=True),
    sa.Column('cost_source', sa.String(length=16), server_default='unknown', nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], name=op.f('fk_sale_items_product_id_products')),
    sa.ForeignKeyConstraint(['sale_id'], ['sales.id'], name=op.f('fk_sale_items_sale_id_sales'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sale_items'))
    )
    op.create_index(op.f('ix_sale_items_product_id'), 'sale_items', ['product_id'], unique=False)
    op.create_index(op.f('ix_sale_items_sale_id'), 'sale_items', ['sale_id'], unique=False)
    op.create_table('tool_calls',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('turn_id', sa.UUID(), nullable=False),
    sa.Column('tool_call_id', sa.String(length=120), nullable=True),
    sa.Column('tool_name', sa.String(length=64), nullable=False),
    sa.Column('args', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('injected_args', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('outcome', sa.String(length=32), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('seq', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['turn_id'], ['agent_turns.id'], name=op.f('fk_tool_calls_turn_id_agent_turns'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tool_calls'))
    )
    op.create_index(op.f('ix_tool_calls_tool_name'), 'tool_calls', ['tool_name'], unique=False)
    op.create_index(op.f('ix_tool_calls_turn_id'), 'tool_calls', ['turn_id'], unique=False)
    op.create_table('payment_allocations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('payment_id', sa.UUID(), nullable=False),
    sa.Column('receivable_id', sa.UUID(), nullable=False),
    sa.Column('applied_amount', sa.Numeric(precision=18, scale=4), nullable=False),
    sa.ForeignKeyConstraint(['payment_id'], ['customer_payments.id'], name=op.f('fk_payment_allocations_payment_id_customer_payments'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['receivable_id'], ['receivables.id'], name=op.f('fk_payment_allocations_receivable_id_receivables')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payment_allocations'))
    )
    op.create_index(op.f('ix_payment_allocations_payment_id'), 'payment_allocations', ['payment_id'], unique=False)
    op.create_index(op.f('ix_payment_allocations_receivable_id'), 'payment_allocations', ['receivable_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_payment_allocations_receivable_id'), table_name='payment_allocations')
    op.drop_index(op.f('ix_payment_allocations_payment_id'), table_name='payment_allocations')
    op.drop_table('payment_allocations')
    op.drop_index(op.f('ix_tool_calls_turn_id'), table_name='tool_calls')
    op.drop_index(op.f('ix_tool_calls_tool_name'), table_name='tool_calls')
    op.drop_table('tool_calls')
    op.drop_index(op.f('ix_sale_items_sale_id'), table_name='sale_items')
    op.drop_index(op.f('ix_sale_items_product_id'), table_name='sale_items')
    op.drop_table('sale_items')
    op.drop_index('ix_receivables_customer', table_name='receivables')
    op.drop_table('receivables')
    op.drop_index(op.f('ix_purchase_items_purchase_id'), table_name='purchase_items')
    op.drop_index(op.f('ix_purchase_items_product_id'), table_name='purchase_items')
    op.drop_table('purchase_items')
    op.drop_index(op.f('ix_llm_calls_turn_id'), table_name='llm_calls')
    op.drop_table('llm_calls')
    op.drop_index(op.f('ix_sales_customer_id'), table_name='sales')
    op.drop_index('ix_sales_business_date', table_name='sales')
    op.drop_table('sales')
    op.drop_index('ix_purchases_business_date', table_name='purchases')
    op.drop_table('purchases')
    op.drop_index('ix_expenses_business_date', table_name='expenses')
    op.drop_table('expenses')
    op.drop_index(op.f('ix_customer_payments_customer_id'), table_name='customer_payments')
    op.drop_index('ix_customer_payments_business_date', table_name='customer_payments')
    op.drop_table('customer_payments')
    op.drop_index('ix_business_notes_fts', table_name='business_notes', postgresql_using='gin')
    op.drop_index(op.f('ix_business_notes_business_id'), table_name='business_notes')
    op.drop_table('business_notes')
    op.drop_index('ix_agent_turns_business_created', table_name='agent_turns')
    op.drop_table('agent_turns')
    op.drop_index('uq_products_business_name', table_name='products', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_index('ix_products_trgm', table_name='products', postgresql_using='gin', postgresql_ops={'normalized_name': 'gin_trgm_ops'})
    op.drop_index(op.f('ix_products_business_id'), table_name='products')
    op.drop_table('products')
    op.drop_index(op.f('ix_outbound_messages_turn_id'), table_name='outbound_messages')
    op.drop_index(op.f('ix_outbound_messages_business_id'), table_name='outbound_messages')
    op.drop_table('outbound_messages')
    op.drop_index('ix_links_message', table_name='message_entity_links')
    op.drop_table('message_entity_links')
    op.drop_index(op.f('ix_inbound_messages_reply_to_provider_message_id'), table_name='inbound_messages')
    op.drop_index('ix_inbound_fts', table_name='inbound_messages', postgresql_using='gin')
    op.drop_index('ix_inbound_business_received', table_name='inbound_messages')
    op.drop_table('inbound_messages')
    op.drop_index('ix_customers_trgm', table_name='customers', postgresql_using='gin', postgresql_ops={'normalized_name': 'gin_trgm_ops'})
    op.drop_index(op.f('ix_customers_business_id'), table_name='customers')
    op.drop_table('customers')
    op.drop_index(op.f('ix_custom_rates_business_id'), table_name='custom_rates')
    op.drop_table('custom_rates')
    op.drop_table('cash_sessions')
    op.drop_index(op.f('ix_business_members_phone'), table_name='business_members')
    op.drop_table('business_members')
    op.drop_index(op.f('ix_audit_log_turn_id'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_business_id'), table_name='audit_log')
    op.drop_index('ix_audit_entity', table_name='audit_log')
    op.drop_table('audit_log')
    op.drop_index('ix_exchange_rates_source_fetched', table_name='exchange_rates')
    op.drop_table('exchange_rates')
    op.drop_table('businesses')
