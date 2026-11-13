"""Commission-only sales; retain frozen historical deltas, with no financial backfill."""
import sqlalchemy as sa

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = depends_on = None

OLD = "deleted_at IS NOT NULL OR (kind <> 'operation' AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND settlement_status IS NULL AND related_receivable_id IS NULL) OR (kind = 'operation' AND operation_type IN ('buy', 'sell') AND settlement_status IN ('settled', 'partial', 'pending', 'account') AND related_payment_id IS NULL AND operation_amount > 0 AND ((operation_type = 'buy' AND ((settlement_status = 'settled' AND delivered_amount = operation_amount AND related_receivable_id IS NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE commission + excess END) OR (related_receivable_id IS NOT NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 WHEN portfolio_broker_member_id IS NULL THEN commission + excess ELSE commission + excess - operation_amount END AND ((settlement_status = 'pending' AND delivered_amount = 0) OR (settlement_status = 'partial' AND delivered_amount > 0 AND delivered_amount < operation_amount) OR (settlement_status = 'settled' AND delivered_amount = operation_amount))) OR (settlement_status = 'account' AND delivered_amount = 0 AND broker_member_id IS NOT NULL AND related_receivable_id IS NULL AND balance_delta = commission + excess - operation_amount))) OR (operation_type = 'sell' AND excess = 0 AND related_receivable_id IS NULL AND ((settlement_status = 'settled' AND delivered_amount = operation_amount AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE commission - operation_amount END) OR (settlement_status = 'pending' AND delivered_amount = 0 AND customer_id IS NOT NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 WHEN portfolio_broker_member_id IS NULL THEN commission ELSE commission - operation_amount END)))) AND commission IS NOT NULL AND commission_rate IS NOT NULL )"
NEW = "deleted_at IS NOT NULL OR (kind <> 'operation' AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND settlement_status IS NULL AND related_receivable_id IS NULL) OR (kind = 'operation' AND operation_type IN ('buy', 'sell') AND settlement_status IN ('settled', 'partial', 'pending', 'account') AND related_payment_id IS NULL AND operation_amount > 0 AND ((operation_type = 'buy' AND ((settlement_status = 'settled' AND delivered_amount = operation_amount AND related_receivable_id IS NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE commission + excess END) OR (related_receivable_id IS NOT NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 WHEN portfolio_broker_member_id IS NULL THEN commission + excess ELSE commission + excess - operation_amount END AND ((settlement_status = 'pending' AND delivered_amount = 0) OR (settlement_status = 'partial' AND delivered_amount > 0 AND delivered_amount < operation_amount) OR (settlement_status = 'settled' AND delivered_amount = operation_amount))) OR (settlement_status = 'account' AND delivered_amount = 0 AND broker_member_id IS NOT NULL AND related_receivable_id IS NULL AND balance_delta = commission + excess - operation_amount))) OR (operation_type = 'sell' AND excess = 0 AND related_receivable_id IS NULL AND ((settlement_status = 'settled' AND delivered_amount = operation_amount AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 WHEN sales_balance_rule = 'commission_only' THEN commission ELSE commission - operation_amount END) OR (settlement_status = 'pending' AND delivered_amount = 0 AND customer_id IS NOT NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 WHEN sales_balance_rule = 'commission_only' OR portfolio_broker_member_id IS NULL THEN commission ELSE commission - operation_amount END)))) AND commission IS NOT NULL AND commission_rate IS NOT NULL )"
RULE = "sales_balance_rule IS NULL OR (sales_balance_rule = 'commission_only' AND kind = 'operation' AND operation_type = 'sell')"


def upgrade():
    op.add_column("broker_movements", sa.Column("sales_balance_rule", sa.String(24), nullable=True))
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_operation_fields"), "broker_movements", type_="check")
    op.create_check_constraint("broker_movement_operation_fields", "broker_movements", NEW)
    op.create_check_constraint("broker_movement_sales_balance_rule", "broker_movements", RULE)


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM broker_movements WHERE sales_balance_rule IS NOT NULL)")).scalar():
        raise RuntimeError("Cannot discard commission-only sales, including archived rows. Export and plan an explicit transition first.")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_sales_balance_rule"), "broker_movements", type_="check")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_operation_fields"), "broker_movements", type_="check")
    op.create_check_constraint("broker_movement_operation_fields", "broker_movements", OLD)
    op.drop_column("broker_movements", "sales_balance_rule")
