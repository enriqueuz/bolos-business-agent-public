"""Definitive nominal sales and independent purchase receipts. Never deletes history."""

import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = depends_on = None

BROKER_MOVEMENT_REAL_MONEY = "(operation_amount_bs IS NULL AND exchange_rate IS NULL AND exchange_rate_source IS NULL AND monetary_basis IS NULL) OR (kind = 'operation' AND operation_amount_bs IS NOT NULL AND exchange_rate IS NOT NULL AND operation_amount_bs > 0 AND exchange_rate > 0 AND exchange_rate_source IS NOT NULL AND monetary_basis IS NOT NULL AND exchange_rate_source IN ('explicit', 'calculated') AND monetary_basis IN ('usd_bs', 'usd_rate'))"

BROKER_MOVEMENT_SIGN = "(broker_member_id IS NULL AND balance_delta = 0) OR (broker_member_id IS NOT NULL AND (kind = 'operation' OR (kind = 'in' AND (balance_delta > 0 OR (balance_delta = 0 AND related_payment_id IS NOT NULL AND portfolio_broker_member_id IS NULL)))))"

BROKER_MOVEMENT_OPERATION_FIELDS = "deleted_at IS NOT NULL OR (kind <> 'operation' AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND settlement_status IS NULL AND related_receivable_id IS NULL) OR (kind = 'operation' AND operation_type IN ('buy', 'sell') AND settlement_status IN ('settled', 'partial', 'pending', 'account') AND related_payment_id IS NULL AND operation_amount > 0 AND ((operation_type = 'buy' AND ((settlement_status = 'settled' AND delivered_amount = operation_amount AND related_receivable_id IS NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE commission + excess END) OR (related_receivable_id IS NOT NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 WHEN portfolio_broker_member_id IS NULL THEN commission + excess ELSE commission + excess - operation_amount END AND ((settlement_status = 'pending' AND delivered_amount = 0) OR (settlement_status = 'partial' AND delivered_amount > 0 AND delivered_amount < operation_amount) OR (settlement_status = 'settled' AND delivered_amount = operation_amount))) OR (settlement_status = 'account' AND delivered_amount = 0 AND broker_member_id IS NOT NULL AND related_receivable_id IS NULL AND balance_delta = commission + excess - operation_amount))) OR (operation_type = 'sell' AND delivered_amount = operation_amount AND excess = 0 AND settlement_status = 'settled' AND related_receivable_id IS NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE commission - operation_amount END)) AND commission IS NOT NULL AND commission_rate IS NOT NULL )"

BROKER_MOVEMENT_RECEIVED = "deleted_at IS NOT NULL OR (kind <> 'operation' AND received_amount IS NULL) OR (kind = 'operation' AND operation_amount IS NOT NULL AND excess IS NOT NULL AND commission_rate BETWEEN 0 AND 1 AND commission = round(operation_amount * commission_rate, 2) AND ((received_amount IS NULL AND excess = 0) OR (operation_type = 'buy' AND received_amount IS NOT NULL AND received_amount >= operation_amount AND excess = CASE WHEN broker_member_id IS NULL THEN 0 ELSE received_amount - operation_amount END)))"


def upgrade():
    connection = op.get_bind()
    incompatible = connection.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM broker_movements WHERE deleted_at IS NULL AND operation_type = 'sell' "
        "AND (excess <> 0 OR delivered_amount <> operation_amount OR "
        "balance_delta <> CASE WHEN broker_member_id IS NULL THEN 0 ELSE commission - operation_amount END))"
    )).scalar()
    if incompatible:
        raise RuntimeError("Hay ventas activas incompatibles. Ejecuta el reinicio autorizado con respaldo antes de migrar; no se recalcula ni borra historia automáticamente.")
    op.add_column("broker_movements", sa.Column("received_amount", sa.Numeric(18, 4)))
    for name, expression in CONSTRAINTS.items():
        if name != "broker_movement_received":
            op.drop_constraint(op.f("ck_broker_movements_" + name), "broker_movements", type_="check")
        op.create_check_constraint(name, "broker_movements", expression)


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM broker_movements WHERE received_amount IS NOT NULL OR "
        "(operation_type = 'sell' AND broker_member_id IS NOT NULL AND balance_delta <> excess + commission))"
    )).scalar():
        raise RuntimeError("No se puede revertir contabilidad nominal ni USD recibidos sin una estrategia explícita.")
    for name in CONSTRAINTS:
        op.drop_constraint(op.f("ck_broker_movements_" + name), "broker_movements", type_="check")
    op.drop_column("broker_movements", "received_amount")
    for name, expression in LEGACY.items():
        op.create_check_constraint(name, "broker_movements", expression)

CONSTRAINTS = {
    'broker_movement_real_money': BROKER_MOVEMENT_REAL_MONEY,
    'broker_movement_sign': BROKER_MOVEMENT_SIGN,
    'broker_movement_operation_fields': BROKER_MOVEMENT_OPERATION_FIELDS,
    'broker_movement_received': BROKER_MOVEMENT_RECEIVED,
}

LEGACY = {'broker_movement_real_money': "(operation_amount_bs IS NULL AND exchange_rate IS NULL AND exchange_rate_source IS NULL AND monetary_basis IS NULL) OR (kind = 'operation' AND excess = 0 AND operation_amount_bs IS NOT NULL AND exchange_rate IS NOT NULL AND operation_amount_bs > 0 AND exchange_rate > 0 AND exchange_rate_source IS NOT NULL AND monetary_basis IS NOT NULL AND exchange_rate_source IN ('explicit', 'calculated') AND monetary_basis IN ('usd_bs', 'usd_rate'))", 'broker_movement_sign': "(broker_member_id IS NULL AND balance_delta = 0) OR (broker_member_id IS NOT NULL AND ((kind = 'in' AND (balance_delta > 0 OR (balance_delta = 0 AND related_payment_id IS NOT NULL AND portfolio_broker_member_id IS NULL))) OR (kind = 'operation' AND (operation_type = 'buy' OR balance_delta >= 0))))", 'broker_movement_operation_fields': "(kind <> 'operation' AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND settlement_status IS NULL AND related_receivable_id IS NULL) OR (kind = 'operation' AND operation_type IN ('buy', 'sell') AND settlement_status IN ('settled', 'partial', 'pending', 'account') AND related_payment_id IS NULL AND operation_amount > 0 AND ((operation_type = 'buy' AND excess = 0 AND ((settlement_status = 'settled' AND delivered_amount = operation_amount AND related_receivable_id IS NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE commission END) OR (related_receivable_id IS NOT NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 WHEN portfolio_broker_member_id IS NULL THEN commission ELSE commission - operation_amount END AND ((settlement_status = 'pending' AND delivered_amount = 0) OR (settlement_status = 'partial' AND delivered_amount > 0 AND delivered_amount < operation_amount) OR (settlement_status = 'settled' AND delivered_amount = operation_amount))) OR (settlement_status = 'account' AND delivered_amount = 0 AND broker_member_id IS NOT NULL AND related_receivable_id IS NULL AND balance_delta = commission - operation_amount))) OR (operation_type = 'sell' AND delivered_amount > 0 AND delivered_amount <= operation_amount AND excess = operation_amount - delivered_amount AND settlement_status = 'settled' AND related_receivable_id IS NULL AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE excess + commission END)) AND commission IS NOT NULL AND commission_rate IS NOT NULL )"}
