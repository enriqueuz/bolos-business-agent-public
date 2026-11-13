"""abonos de cartera vinculados al saldo del corredor

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("broker_movements", sa.Column("related_payment_id", sa.UUID()))
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_operation_fields"), "broker_movements", type_="check")
    op.create_check_constraint(
        "broker_movement_operation_fields",
        "broker_movements",
        "(kind <> 'operation' AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL "
        "AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND settlement_status IS NULL "
        "AND related_receivable_id IS NULL) OR "
        "(kind = 'operation' AND operation_type IN ('buy', 'sell') "
        "AND settlement_status IN ('settled', 'partial', 'pending') AND related_payment_id IS NULL "
        "AND operation_amount > 0 "
        "AND ((operation_type = 'buy' AND excess = 0 "
        "AND ((settlement_status = 'settled' AND delivered_amount = operation_amount "
        "AND related_receivable_id IS NULL AND balance_delta = commission) "
        "OR (related_receivable_id IS NOT NULL AND balance_delta = commission - operation_amount "
        "AND ((settlement_status = 'pending' AND delivered_amount = 0) "
        "OR (settlement_status = 'partial' AND delivered_amount > 0 AND delivered_amount < operation_amount) "
        "OR (settlement_status = 'settled' AND delivered_amount = operation_amount))))) "
        "OR (operation_type = 'sell' AND delivered_amount > 0 AND delivered_amount <= operation_amount "
        "AND excess = operation_amount - delivered_amount AND settlement_status = 'settled' "
        "AND related_receivable_id IS NULL "
        "AND balance_delta = excess + commission)) "
        "AND commission IS NOT NULL AND commission_rate IS NOT NULL "
        ")",
    )
    op.create_check_constraint(
        "broker_movement_payment_link",
        "broker_movements",
        "related_payment_id IS NULL OR (kind = 'in' AND related_receivable_id IS NULL)",
    )
    op.create_foreign_key(
        op.f("fk_broker_movements_related_payment_id_customer_payments"),
        "broker_movements",
        "customer_payments",
        ["related_payment_id"],
        ["id"],
    )
    op.create_unique_constraint(
        op.f("uq_broker_movements_related_payment_id"), "broker_movements", ["related_payment_id"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_broker_movements_related_payment_id"), "broker_movements", type_="unique")
    op.drop_constraint(
        op.f("fk_broker_movements_related_payment_id_customer_payments"), "broker_movements", type_="foreignkey"
    )
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_payment_link"), "broker_movements", type_="check")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_operation_fields"), "broker_movements", type_="check")
    op.create_check_constraint(
        "broker_movement_operation_fields",
        "broker_movements",
        "(kind <> 'operation' AND operation_type IS NULL AND operation_amount IS NULL AND delivered_amount IS NULL "
        "AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL AND settlement_status IS NULL "
        "AND related_receivable_id IS NULL) OR "
        "(kind = 'operation' AND operation_type IN ('buy', 'sell') "
        "AND settlement_status IN ('settled', 'pending') "
        "AND operation_amount > 0 "
        "AND ((operation_type = 'buy' AND excess = 0 "
        "AND ((settlement_status = 'settled' AND delivered_amount = operation_amount "
        "AND related_receivable_id IS NULL AND balance_delta = commission) "
        "OR (settlement_status = 'pending' AND delivered_amount = 0 "
        "AND related_receivable_id IS NOT NULL AND balance_delta = commission - operation_amount))) "
        "OR (operation_type = 'sell' AND delivered_amount > 0 AND delivered_amount <= operation_amount "
        "AND excess = operation_amount - delivered_amount AND settlement_status = 'settled' "
        "AND related_receivable_id IS NULL "
        "AND balance_delta = excess + commission)) "
        "AND commission IS NOT NULL AND commission_rate IS NOT NULL "
        ")",
    )
    op.drop_column("broker_movements", "related_payment_id")
