"""compras propias en cuenta y abreviaciones de corredores

Revision ID: 0013
Revises: 0012
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _initials(name: str) -> str:
    value = "".join(next((char for char in part if char.isalnum()), "") for part in name.split()).upper()
    return value[:12] or "C"


def upgrade() -> None:
    op.add_column("business_members", sa.Column("abbreviation", sa.String(length=12)))
    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT id, business_id, name FROM business_members WHERE role = 'broker' ORDER BY created_at, id"
    )).mappings()
    used: dict[object, set[str]] = {}
    for row in rows:
        base = _initials(row["name"] or "Corredor")
        candidate = base
        suffix = 2
        business_used = used.setdefault(row["business_id"], set())
        while candidate.casefold() in business_used:
            ending = str(suffix)
            candidate = f"{base[:12 - len(ending)]}{ending}"
            suffix += 1
        business_used.add(candidate.casefold())
        connection.execute(
            sa.text("UPDATE business_members SET abbreviation = :value WHERE id = :member_id"),
            {"value": candidate, "member_id": row["id"]},
        )
    op.create_index(
        "uq_business_members_business_abbreviation_ci",
        "business_members",
        ["business_id", sa.text("lower(abbreviation)")],
        unique=True,
        postgresql_where=sa.text("abbreviation IS NOT NULL"),
    )

    op.drop_constraint(op.f("ck_broker_movements_broker_movement_operation_fields"), "broker_movements", type_="check")
    op.create_check_constraint(
        "broker_movement_operation_fields",
        "broker_movements",
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
        "AND commission IS NOT NULL AND commission_rate IS NOT NULL)",
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(sa.text(
        "UPDATE broker_movements SET kind = 'out', operation_type = NULL, settlement_status = NULL, "
        "counterparty_name = NULL, operation_amount = NULL, delivered_amount = NULL, excess = NULL, "
        "commission = NULL, commission_rate = NULL WHERE settlement_status = 'account'"
    ))
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
        "AND related_receivable_id IS NULL AND balance_delta = excess + commission)) "
        "AND commission IS NOT NULL AND commission_rate IS NOT NULL)",
    )
    op.drop_index("uq_business_members_business_abbreviation_ci", table_name="business_members")
    op.drop_column("business_members", "abbreviation")
