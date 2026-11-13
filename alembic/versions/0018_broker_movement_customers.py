"""Unify brokerage movement customers with the shared customer entity.

Revision ID: 0018
Revises: 0017
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CUSTOMER_SCOPE = (
    "((settlement_status = 'account' OR (kind = 'in' AND related_payment_id IS NULL)) "
    "AND customer_id IS NULL) OR "
    "((related_receivable_id IS NOT NULL OR related_payment_id IS NOT NULL) AND customer_id IS NOT NULL) OR "
    "(kind = 'operation' AND settlement_status <> 'account' AND related_receivable_id IS NULL)"
)


def upgrade() -> None:
    op.add_column("broker_movements", sa.Column("customer_id", sa.UUID()))
    op.create_foreign_key(
        op.f("fk_broker_movements_customer_id_customers"),
        "broker_movements", "customers", ["customer_id"], ["id"],
    )
    op.create_index("ix_broker_movements_business_customer", "broker_movements", ["business_id", "customer_id"])

    # Deuda y pago son las fuentes autoritativas: no se intenta interpretar el texto legado.
    op.execute("""
        UPDATE broker_movements movement
        SET customer_id = receivable.customer_id
        FROM receivables receivable
        WHERE movement.related_receivable_id = receivable.id
    """)
    op.execute("""
        UPDATE broker_movements movement
        SET customer_id = payment.customer_id
        FROM customer_payments payment
        WHERE movement.related_payment_id = payment.id
    """)

    # Solo se asocia una operación liquidada cuando el texto coincide exactamente (sin distinguir mayúsculas)
    # con UN cliente activo del negocio. Nunca se usan similitudes en una migración histórica.
    op.execute("""
        UPDATE broker_movements movement
        SET customer_id = candidate.id
        FROM customers candidate
        WHERE movement.customer_id IS NULL
          AND movement.kind = 'operation'
          AND movement.settlement_status <> 'account'
          AND movement.counterparty_name IS NOT NULL
          AND candidate.business_id = movement.business_id
          AND candidate.deleted_at IS NULL
          AND lower(candidate.name) = lower(btrim(movement.counterparty_name))
          AND 1 = (
              SELECT count(*) FROM customers only_candidate
              WHERE only_candidate.business_id = movement.business_id
                AND only_candidate.deleted_at IS NULL
                AND lower(only_candidate.name) = lower(btrim(movement.counterparty_name))
          )
    """)

    # No perder el texto que no se pudo asociar con seguridad. Desde este punto es observación histórica,
    # no una segunda identidad: búsquedas y UI usan exclusivamente customer_id.
    op.execute("""
        UPDATE broker_movements
        SET description = concat_ws(' · ', description, 'Contraparte histórica: ' || counterparty_name)
        WHERE customer_id IS NULL
          AND kind = 'operation'
          AND settlement_status <> 'account'
          AND counterparty_name IS NOT NULL
          AND btrim(counterparty_name) <> ''
    """)

    op.create_check_constraint("broker_movement_customer_scope", "broker_movements", _CUSTOMER_SCOPE)
    op.drop_column("broker_movements", "counterparty_name")


def downgrade() -> None:
    op.add_column("broker_movements", sa.Column("counterparty_name", sa.String(160)))
    op.execute("""
        UPDATE broker_movements movement
        SET counterparty_name = customer.name
        FROM customers customer
        WHERE movement.customer_id = customer.id
    """)
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_customer_scope"), "broker_movements", type_="check")
    op.drop_index("ix_broker_movements_business_customer", table_name="broker_movements")
    op.drop_constraint(op.f("fk_broker_movements_customer_id_customers"), "broker_movements", type_="foreignkey")
    op.drop_column("broker_movements", "customer_id")
