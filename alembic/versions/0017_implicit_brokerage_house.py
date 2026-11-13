"""Add the implicit brokerage house account.

Revision ID: 0017
Revises: 0016
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OPERATION_FIELDS = (
    "(kind <> 'operation' AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL "
    "AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND settlement_status IS NULL "
    "AND related_receivable_id IS NULL) OR "
    "(kind = 'operation' AND operation_type IN ('buy', 'sell') "
    "AND settlement_status IN ('settled', 'partial', 'pending', 'account') AND related_payment_id IS NULL "
    "AND operation_amount > 0 "
    "AND ((operation_type = 'buy' AND excess = 0 "
    "AND ((settlement_status = 'settled' AND delivered_amount = operation_amount "
    "AND related_receivable_id IS NULL "
    "AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE commission END) "
    "OR (related_receivable_id IS NOT NULL "
    "AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE commission - operation_amount END "
    "AND ((settlement_status = 'pending' AND delivered_amount = 0) "
    "OR (settlement_status = 'partial' AND delivered_amount > 0 AND delivered_amount < operation_amount) "
    "OR (settlement_status = 'settled' AND delivered_amount = operation_amount))) "
    "OR (settlement_status = 'account' AND delivered_amount = 0 "
    "AND broker_member_id IS NOT NULL AND related_receivable_id IS NULL "
    "AND balance_delta = commission - operation_amount))) "
    "OR (operation_type = 'sell' AND delivered_amount > 0 AND delivered_amount <= operation_amount "
    "AND excess = operation_amount - delivered_amount AND settlement_status = 'settled' "
    "AND related_receivable_id IS NULL "
    "AND balance_delta = CASE WHEN broker_member_id IS NULL THEN 0 ELSE excess + commission END)) "
    "AND commission IS NOT NULL AND commission_rate IS NOT NULL)"
)

_LEGACY_OPERATION_FIELDS = (
    "(kind <> 'operation' AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL "
    "AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND settlement_status IS NULL "
    "AND related_receivable_id IS NULL) OR "
    "(kind = 'operation' AND operation_type IN ('buy', 'sell') "
    "AND settlement_status IN ('settled', 'partial', 'pending', 'account') AND related_payment_id IS NULL "
    "AND operation_amount > 0 "
    "AND ((operation_type = 'buy' AND excess = 0 "
    "AND ((settlement_status = 'settled' AND delivered_amount = operation_amount "
    "AND related_receivable_id IS NULL AND balance_delta = commission) "
    "OR (related_receivable_id IS NOT NULL AND balance_delta = commission - operation_amount "
    "AND ((settlement_status = 'pending' AND delivered_amount = 0) "
    "OR (settlement_status = 'partial' AND delivered_amount > 0 AND delivered_amount < operation_amount) "
    "OR (settlement_status = 'settled' AND delivered_amount = operation_amount))) "
    "OR (settlement_status = 'account' AND delivered_amount = 0 "
    "AND related_receivable_id IS NULL AND balance_delta = commission - operation_amount))) "
    "OR (operation_type = 'sell' AND delivered_amount > 0 AND delivered_amount <= operation_amount "
    "AND excess = operation_amount - delivered_amount AND settlement_status = 'settled' "
    "AND related_receivable_id IS NULL AND balance_delta = excess + commission)) "
    "AND commission IS NOT NULL AND commission_rate IS NOT NULL)"
)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_sign"), "broker_movements", type_="check")
    op.drop_constraint(
        op.f("ck_broker_movements_broker_movement_operation_fields"), "broker_movements", type_="check"
    )
    op.alter_column("broker_movements", "broker_member_id", existing_type=sa.UUID(), nullable=True)
    op.create_check_constraint(
        "broker_movement_sign",
        "broker_movements",
        "(broker_member_id IS NULL AND balance_delta = 0) OR "
        "(broker_member_id IS NOT NULL AND ((kind = 'in' AND balance_delta > 0) "
        "OR (kind = 'operation' AND (operation_type = 'buy' OR balance_delta >= 0))))",
    )
    op.create_check_constraint("broker_movement_operation_fields", "broker_movements", _OPERATION_FIELDS)
    op.create_check_constraint(
        "broker_movement_house_scope",
        "broker_movements",
        "broker_member_id IS NOT NULL OR "
        "((kind = 'operation' AND settlement_status <> 'account' AND commission = 0 AND commission_rate = 0) "
        "OR (kind = 'in' AND related_payment_id IS NOT NULL))",
    )


def downgrade() -> None:
    house_rows = op.get_bind().execute(
        sa.text("SELECT count(*) FROM broker_movements WHERE broker_member_id IS NULL")
    ).scalar_one()
    if house_rows:
        raise RuntimeError(
            "No se puede revertir 0017: existen movimientos de Casa. "
            "La migración no elimina ni reatribuye datos operativos."
        )
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_house_scope"), "broker_movements", type_="check")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_operation_fields"), "broker_movements", type_="check")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_sign"), "broker_movements", type_="check")
    op.alter_column("broker_movements", "broker_member_id", existing_type=sa.UUID(), nullable=False)
    op.create_check_constraint(
        "broker_movement_sign",
        "broker_movements",
        "(kind = 'in' AND balance_delta > 0) "
        "OR (kind = 'operation' AND (operation_type = 'buy' OR balance_delta >= 0))",
    )
    op.create_check_constraint("broker_movement_operation_fields", "broker_movements", _LEGACY_OPERATION_FIELDS)
