"""operaciones de divisas y cartera acotada por corredor

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("broker_movements", sa.Column("operation_type", sa.String(16)))
    op.add_column("broker_movements", sa.Column("settlement_status", sa.String(16)))
    op.add_column("broker_movements", sa.Column("counterparty_name", sa.String(160)))
    op.add_column("broker_movements", sa.Column("related_receivable_id", sa.UUID()))
    op.add_column("receivables", sa.Column("broker_member_id", sa.UUID()))
    op.add_column("customer_payments", sa.Column("broker_member_id", sa.UUID()))

    # Las filas históricas eran ventas: su matemática ya era monto - entregado.
    op.execute(
        "UPDATE broker_movements SET operation_type = 'sell', settlement_status = 'settled' "
        "WHERE kind = 'operation'"
    )

    op.drop_constraint(op.f("ck_broker_movements_broker_movement_sign"), "broker_movements", type_="check")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_operation_fields"), "broker_movements", type_="check")
    op.create_check_constraint(
        "broker_movement_sign",
        "broker_movements",
        "(kind = 'out' AND balance_delta < 0) OR (kind = 'in' AND balance_delta > 0) "
        "OR (kind = 'operation' AND (operation_type = 'buy' OR balance_delta >= 0))",
    )
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
    op.create_foreign_key(
        op.f("fk_broker_movements_related_receivable_id_receivables"),
        "broker_movements",
        "receivables",
        ["related_receivable_id"],
        ["id"],
    )
    op.create_unique_constraint(
        op.f("uq_broker_movements_related_receivable_id"), "broker_movements", ["related_receivable_id"]
    )
    op.create_foreign_key(
        op.f("fk_receivables_broker_member_id_business_members"),
        "receivables",
        "business_members",
        ["broker_member_id"],
        ["id"],
    )
    op.create_index(op.f("ix_receivables_broker_member_id"), "receivables", ["broker_member_id"])
    op.create_foreign_key(
        op.f("fk_customer_payments_broker_member_id_business_members"),
        "customer_payments",
        "business_members",
        ["broker_member_id"],
        ["id"],
    )
    op.create_index(op.f("ix_customer_payments_broker_member_id"), "customer_payments", ["broker_member_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_customer_payments_broker_member_id"), table_name="customer_payments")
    op.drop_constraint(
        op.f("fk_customer_payments_broker_member_id_business_members"), "customer_payments", type_="foreignkey"
    )
    op.drop_index(op.f("ix_receivables_broker_member_id"), table_name="receivables")
    op.drop_constraint(op.f("fk_receivables_broker_member_id_business_members"), "receivables", type_="foreignkey")
    op.drop_constraint(op.f("uq_broker_movements_related_receivable_id"), "broker_movements", type_="unique")
    op.drop_constraint(
        op.f("fk_broker_movements_related_receivable_id_receivables"), "broker_movements", type_="foreignkey"
    )
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_operation_fields"), "broker_movements", type_="check")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_sign"), "broker_movements", type_="check")
    op.create_check_constraint(
        "broker_movement_sign",
        "broker_movements",
        "(kind = 'out' AND balance_delta < 0) OR (kind IN ('in', 'operation') AND balance_delta > 0)",
    )
    op.create_check_constraint(
        "broker_movement_operation_fields",
        "broker_movements",
        "(kind <> 'operation' AND operation_amount IS NULL AND delivered_amount IS NULL "
        "AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL) OR "
        "(kind = 'operation' AND operation_amount > 0 AND delivered_amount > 0 "
        "AND delivered_amount <= operation_amount AND excess IS NOT NULL "
        "AND commission IS NOT NULL AND commission_rate IS NOT NULL "
        "AND balance_delta = excess + commission)",
    )
    op.drop_column("customer_payments", "broker_member_id")
    op.drop_column("receivables", "broker_member_id")
    op.drop_column("broker_movements", "related_receivable_id")
    op.drop_column("broker_movements", "counterparty_name")
    op.drop_column("broker_movements", "settlement_status")
    op.drop_column("broker_movements", "operation_type")
