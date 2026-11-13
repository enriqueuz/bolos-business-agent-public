"""Direct broker withdrawals, without restating historical financial records."""

import sqlalchemy as sa

from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = depends_on = None

OLD = {'broker_movement_kind': "kind IN ('in', 'operation', 'customer_payout', 'adjustment')", 'broker_movement_sign': "kind = 'adjustment' OR (kind = 'customer_payout' AND balance_delta = 0) OR (broker_member_id IS NULL AND balance_delta = 0) OR (broker_member_id IS NOT NULL AND (kind = 'operation' OR (kind = 'in' AND (balance_delta > 0 OR (balance_delta = 0 AND related_payment_id IS NOT NULL AND portfolio_broker_member_id IS NULL)))))", 'broker_movement_customer_scope': "kind = 'adjustment' OR ((settlement_status = 'account' OR (kind = 'in' AND related_payment_id IS NULL)) AND customer_id IS NULL) OR ((related_receivable_id IS NOT NULL OR related_payment_id IS NOT NULL) AND customer_id IS NOT NULL) OR (kind = 'operation' AND settlement_status <> 'account' AND related_receivable_id IS NULL) OR (kind = 'customer_payout' AND customer_id IS NOT NULL AND related_receivable_id IS NULL AND related_payment_id IS NULL)"}
NEW = {'broker_movement_kind': "kind IN ('in', 'out', 'operation', 'customer_payout', 'adjustment')", 'broker_movement_sign': "kind = 'adjustment' OR (kind = 'out' AND broker_member_id IS NOT NULL AND balance_delta < 0) OR (kind = 'customer_payout' AND balance_delta = 0) OR (broker_member_id IS NULL AND balance_delta = 0) OR (broker_member_id IS NOT NULL AND (kind = 'operation' OR (kind = 'in' AND (balance_delta > 0 OR (balance_delta = 0 AND related_payment_id IS NOT NULL AND portfolio_broker_member_id IS NULL)))))", 'broker_movement_customer_scope': "kind = 'adjustment' OR ((settlement_status = 'account' OR (kind IN ('in', 'out') AND related_payment_id IS NULL)) AND customer_id IS NULL) OR ((related_receivable_id IS NOT NULL OR related_payment_id IS NOT NULL) AND customer_id IS NOT NULL) OR (kind = 'operation' AND settlement_status <> 'account' AND related_receivable_id IS NULL) OR (kind = 'customer_payout' AND customer_id IS NOT NULL AND related_receivable_id IS NULL AND related_payment_id IS NULL)", 'broker_movement_withdrawal_fields': "kind <> 'out' OR (broker_member_id IS NOT NULL AND balance_delta < 0 AND currency = 'USD' AND customer_id IS NULL AND portfolio_broker_member_id IS NULL AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL AND received_amount IS NULL AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND settlement_status IS NULL AND sales_balance_rule IS NULL AND customer_payout_amount IS NULL AND related_receivable_id IS NULL AND related_payment_id IS NULL AND operation_amount_bs IS NULL AND exchange_rate IS NULL AND exchange_rate_source IS NULL AND monetary_basis IS NULL AND monetary_provenance IS NULL)"}


def upgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name in OLD:
        op.drop_constraint(op.f("ck_broker_movements_" + name), "broker_movements", type_="check")
    for name, expression in NEW.items():
        op.create_check_constraint(op.f("ck_broker_movements_" + name), "broker_movements", expression)


def downgrade():
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("LOCK TABLE broker_movements IN SHARE ROW EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM broker_movements WHERE kind = 'out')")):
        raise RuntimeError("Cannot downgrade: direct withdrawals exist, including deleted history. No data was changed.")
    for name in NEW:
        op.drop_constraint(op.f("ck_broker_movements_" + name), "broker_movements", type_="check")
    for name, expression in OLD.items():
        op.create_check_constraint(op.f("ck_broker_movements_" + name), "broker_movements", expression)
