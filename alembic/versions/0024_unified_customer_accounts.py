"""Unified customer accounts: pending sales and real customer payouts."""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = depends_on = None

OLD = {'broker_movement_kind': "kind IN ('in', 'operation')",
 'broker_movement_sign': '(broker_member_id IS NULL AND balance_delta = 0) OR (broker_member_id IS NOT NULL '
                         "AND (kind = 'operation' OR (kind = 'in' AND (balance_delta > 0 OR (balance_delta = "
                         '0 AND related_payment_id IS NOT NULL AND portfolio_broker_member_id IS NULL)))))',
 'broker_movement_operation_fields': "deleted_at IS NOT NULL OR (kind <> 'operation' AND operation_type IS "
                                     'NULL AND operation_amount IS NULL AND delivered_amount IS NULL AND '
                                     'excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND '
                                     'settlement_status IS NULL AND related_receivable_id IS NULL) OR (kind '
                                     "= 'operation' AND operation_type IN ('buy', 'sell') AND "
                                     "settlement_status IN ('settled', 'partial', 'pending', 'account') AND "
                                     'related_payment_id IS NULL AND operation_amount > 0 AND '
                                     "((operation_type = 'buy' AND ((settlement_status = 'settled' AND "
                                     'delivered_amount = operation_amount AND related_receivable_id IS NULL '
                                     'AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE '
                                     'commission + excess END) OR (related_receivable_id IS NOT NULL AND '
                                     'balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 WHEN '
                                     'portfolio_broker_member_id IS NULL THEN commission + excess ELSE '
                                     'commission + excess - operation_amount END AND ((settlement_status = '
                                     "'pending' AND delivered_amount = 0) OR (settlement_status = 'partial' "
                                     'AND delivered_amount > 0 AND delivered_amount < operation_amount) OR '
                                     "(settlement_status = 'settled' AND delivered_amount = "
                                     "operation_amount))) OR (settlement_status = 'account' AND "
                                     'delivered_amount = 0 AND broker_member_id IS NOT NULL AND '
                                     'related_receivable_id IS NULL AND balance_delta = commission + excess '
                                     "- operation_amount))) OR (operation_type = 'sell' AND delivered_amount "
                                     "= operation_amount AND excess = 0 AND settlement_status = 'settled' "
                                     'AND related_receivable_id IS NULL AND balance_delta = CASE WHEN '
                                     'broker_member_id IS NULL THEN 0 ELSE commission - operation_amount '
                                     'END)) AND commission IS NOT NULL AND commission_rate IS NOT NULL )',
 'broker_movement_house_scope': "broker_member_id IS NOT NULL OR ((kind = 'operation' AND settlement_status "
                                "<> 'account' AND commission = 0 AND commission_rate = 0) OR (kind = 'in' "
                                'AND related_payment_id IS NOT NULL))',
 'broker_movement_customer_scope': "((settlement_status = 'account' OR (kind = 'in' AND related_payment_id "
                                   'IS NULL)) AND customer_id IS NULL) OR ((related_receivable_id IS NOT '
                                   'NULL OR related_payment_id IS NOT NULL) AND customer_id IS NOT NULL) OR '
                                   "(kind = 'operation' AND settlement_status <> 'account' AND "
                                   'related_receivable_id IS NULL)'}
NEW = {'broker_movement_kind': "kind IN ('in', 'operation', 'customer_payout')",
 'broker_movement_sign': "(kind = 'customer_payout' AND balance_delta = 0) OR (broker_member_id IS NULL AND "
                         "balance_delta = 0) OR (broker_member_id IS NOT NULL AND (kind = 'operation' OR "
                         "(kind = 'in' AND (balance_delta > 0 OR (balance_delta = 0 AND related_payment_id "
                         'IS NOT NULL AND portfolio_broker_member_id IS NULL)))))',
 'broker_movement_operation_fields': "deleted_at IS NOT NULL OR (kind <> 'operation' AND operation_type IS "
                                     'NULL AND operation_amount IS NULL AND delivered_amount IS NULL AND '
                                     'excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND '
                                     'settlement_status IS NULL AND related_receivable_id IS NULL) OR (kind '
                                     "= 'operation' AND operation_type IN ('buy', 'sell') AND "
                                     "settlement_status IN ('settled', 'partial', 'pending', 'account') AND "
                                     'related_payment_id IS NULL AND operation_amount > 0 AND '
                                     "((operation_type = 'buy' AND ((settlement_status = 'settled' AND "
                                     'delivered_amount = operation_amount AND related_receivable_id IS NULL '
                                     'AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE '
                                     'commission + excess END) OR (related_receivable_id IS NOT NULL AND '
                                     'balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 WHEN '
                                     'portfolio_broker_member_id IS NULL THEN commission + excess ELSE '
                                     'commission + excess - operation_amount END AND ((settlement_status = '
                                     "'pending' AND delivered_amount = 0) OR (settlement_status = 'partial' "
                                     'AND delivered_amount > 0 AND delivered_amount < operation_amount) OR '
                                     "(settlement_status = 'settled' AND delivered_amount = "
                                     "operation_amount))) OR (settlement_status = 'account' AND "
                                     'delivered_amount = 0 AND broker_member_id IS NOT NULL AND '
                                     'related_receivable_id IS NULL AND balance_delta = commission + excess '
                                     "- operation_amount))) OR (operation_type = 'sell' AND excess = 0 AND "
                                     "related_receivable_id IS NULL AND ((settlement_status = 'settled' AND "
                                     'delivered_amount = operation_amount AND balance_delta = CASE WHEN '
                                     'broker_member_id IS NULL THEN 0 ELSE commission - operation_amount '
                                     "END) OR (settlement_status = 'pending' AND delivered_amount = 0 AND "
                                     'customer_id IS NOT NULL AND balance_delta = CASE WHEN broker_member_id '
                                     'IS NULL THEN 0 WHEN portfolio_broker_member_id IS NULL THEN commission '
                                     'ELSE commission - operation_amount END)))) AND commission IS NOT NULL '
                                     'AND commission_rate IS NOT NULL )',
 'broker_movement_house_scope': "broker_member_id IS NOT NULL OR ((kind = 'operation' AND settlement_status "
                                "<> 'account' AND commission = 0 AND commission_rate = 0) OR (kind = 'in' "
                                "AND related_payment_id IS NOT NULL) OR kind = 'customer_payout')",
 'broker_movement_customer_scope': "((settlement_status = 'account' OR (kind = 'in' AND related_payment_id "
                                   'IS NULL)) AND customer_id IS NULL) OR ((related_receivable_id IS NOT '
                                   'NULL OR related_payment_id IS NOT NULL) AND customer_id IS NOT NULL) OR '
                                   "(kind = 'operation' AND settlement_status <> 'account' AND "
                                   "related_receivable_id IS NULL) OR (kind = 'customer_payout' AND "
                                   'customer_id IS NOT NULL AND related_receivable_id IS NULL AND '
                                   'related_payment_id IS NULL)',
 'broker_movement_customer_payout': "(kind = 'customer_payout' AND customer_payout_amount IS NOT NULL AND "
                                    "customer_payout_amount > 0 AND currency = 'USD' AND balance_delta = 0) "
                                    "OR (kind <> 'customer_payout' AND customer_payout_amount IS NULL)"}


def upgrade():
    op.add_column("broker_movements", sa.Column("customer_payout_amount", sa.Numeric(18, 4), nullable=True))
    for name in OLD:
        op.drop_constraint(op.f("ck_broker_movements_" + name), "broker_movements", type_="check")
    for name, expression in NEW.items():
        op.create_check_constraint(name, "broker_movements", expression)


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM broker_movements WHERE kind = 'customer_payout' OR (operation_type = 'sell' AND settlement_status = 'pending'))")).scalar():
        raise RuntimeError("Cannot discard customer payouts or pending sales. Resolve and export them explicitly first.")
    for name in NEW:
        op.drop_constraint(op.f("ck_broker_movements_" + name), "broker_movements", type_="check")
    for name, expression in OLD.items():
        op.create_check_constraint(name, "broker_movements", expression)
    op.drop_column("broker_movements", "customer_payout_amount")
