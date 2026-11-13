"""Remove generic negative broker movements.

Revision ID: 0015
Revises: 0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    generic_outs = connection.execute(
        sa.text("SELECT count(*) FROM broker_movements WHERE kind = 'out'")
    ).scalar_one()
    if generic_outs:
        raise RuntimeError(
            "No se puede aplicar 0015: existen movimientos genéricos kind='out'. "
            "Revise ese entorno antes de continuar; la migración no elimina ni reinterpreta datos."
        )

    op.drop_constraint(op.f("ck_broker_movements_broker_movement_sign"), "broker_movements", type_="check")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_kind"), "broker_movements", type_="check")
    op.create_check_constraint(
        "broker_movement_kind",
        "broker_movements",
        "kind IN ('in', 'operation')",
    )
    op.create_check_constraint(
        "broker_movement_sign",
        "broker_movements",
        "(kind = 'in' AND balance_delta > 0) "
        "OR (kind = 'operation' AND (operation_type = 'buy' OR balance_delta >= 0))",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_sign"), "broker_movements", type_="check")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_kind"), "broker_movements", type_="check")
    op.create_check_constraint(
        "broker_movement_kind",
        "broker_movements",
        "kind IN ('out', 'in', 'operation')",
    )
    op.create_check_constraint(
        "broker_movement_sign",
        "broker_movements",
        "(kind = 'out' AND balance_delta < 0) OR (kind = 'in' AND balance_delta > 0) "
        "OR (kind = 'operation' AND (operation_type = 'buy' OR balance_delta >= 0))",
    )
