"""Historical reconciliation without commercial operations or backfills."""
import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = depends_on = None

OLD = {'broker_movement_kind': "kind IN ('in', 'operation', 'customer_payout')", 'broker_movement_sign': "(kind = 'customer_payout' AND balance_delta = 0) OR (broker_member_id IS NULL AND balance_delta = 0) OR (broker_member_id IS NOT NULL AND (kind = 'operation' OR (kind = 'in' AND (balance_delta > 0 OR (balance_delta = 0 AND related_payment_id IS NOT NULL AND portfolio_broker_member_id IS NULL)))))", 'broker_movement_customer_scope': "((settlement_status = 'account' OR (kind = 'in' AND related_payment_id IS NULL)) AND customer_id IS NULL) OR ((related_receivable_id IS NOT NULL OR related_payment_id IS NOT NULL) AND customer_id IS NOT NULL) OR (kind = 'operation' AND settlement_status <> 'account' AND related_receivable_id IS NULL) OR (kind = 'customer_payout' AND customer_id IS NOT NULL AND related_receivable_id IS NULL AND related_payment_id IS NULL)"}
NEW = {'broker_movement_kind': "kind IN ('in', 'operation', 'customer_payout', 'adjustment')", 'broker_movement_sign': "kind = 'adjustment' OR (kind = 'customer_payout' AND balance_delta = 0) OR (broker_member_id IS NULL AND balance_delta = 0) OR (broker_member_id IS NOT NULL AND (kind = 'operation' OR (kind = 'in' AND (balance_delta > 0 OR (balance_delta = 0 AND related_payment_id IS NOT NULL AND portfolio_broker_member_id IS NULL)))))", 'broker_movement_customer_scope': "kind = 'adjustment' OR ((settlement_status = 'account' OR (kind = 'in' AND related_payment_id IS NULL)) AND customer_id IS NULL) OR ((related_receivable_id IS NOT NULL OR related_payment_id IS NOT NULL) AND customer_id IS NOT NULL) OR (kind = 'operation' AND settlement_status <> 'account' AND related_receivable_id IS NULL) OR (kind = 'customer_payout' AND customer_id IS NOT NULL AND related_receivable_id IS NULL AND related_payment_id IS NULL)", 'broker_movement_adjustment_fields': "kind <> 'adjustment' OR (broker_member_id IS NOT NULL AND balance_delta <> 0 AND currency = 'USD' AND description IS NOT NULL AND description = 'Reconciliación de saldo' AND customer_id IS NULL AND portfolio_broker_member_id IS NULL AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL AND received_amount IS NULL AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND settlement_status IS NULL AND sales_balance_rule IS NULL AND customer_payout_amount IS NULL AND related_receivable_id IS NULL AND related_payment_id IS NULL AND operation_amount_bs IS NULL AND exchange_rate IS NULL AND exchange_rate_source IS NULL AND monetary_basis IS NULL AND monetary_provenance IS NULL)"}

def upgrade():
    for name in OLD:
        op.drop_constraint(op.f("ck_broker_movements_" + name), "broker_movements", type_="check")
    for name, expression in NEW.items():
        op.create_check_constraint(op.f("ck_broker_movements_" + name), "broker_movements", expression)
    op.create_table(
        "customer_balance_adjustments",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("business_id", sa.UUID(), sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", sa.UUID(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("portfolio_broker_member_id", sa.UUID(), sa.ForeignKey("business_members.id")),
        sa.Column("recorded_by_member_id", sa.UUID(), sa.ForeignKey("business_members.id"), nullable=False),
        sa.Column("source_message_id", sa.UUID(), sa.ForeignKey("inbound_messages.id", ondelete="SET NULL")),
        sa.Column("amount_delta", sa.Numeric(18, 4), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.String(300), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.CheckConstraint("amount_delta <> 0", name=op.f("ck_customer_balance_adjustments_customer_adjustment_nonzero")),
    )
    op.create_index("ix_customer_balance_adjustments_account_date", "customer_balance_adjustments",
                    ["business_id", "customer_id", "portfolio_broker_member_id", "business_date"])


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM broker_movements WHERE kind = 'adjustment') "
                                  "OR EXISTS (SELECT 1 FROM customer_balance_adjustments)")):
        raise RuntimeError("Cannot downgrade: historical adjustments must be preserved, including deleted records.")
    op.drop_table("customer_balance_adjustments")
    for name in NEW:
        op.drop_constraint(op.f("ck_broker_movements_" + name), "broker_movements", type_="check")
    for name, expression in OLD.items():
        op.create_check_constraint(op.f("ck_broker_movements_" + name), "broker_movements", expression)
