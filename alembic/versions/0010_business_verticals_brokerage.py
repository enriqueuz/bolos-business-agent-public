"""vertical principal, roles y ledger de corredores

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column("vertical", sa.String(32), nullable=False, server_default="retail"),
    )
    op.create_table(
        "broker_movements",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("business_id", sa.UUID(), nullable=False),
        sa.Column("broker_member_id", sa.UUID(), nullable=False),
        sa.Column("recorded_by_member_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("balance_delta", sa.Numeric(18, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("description", sa.String(300)),
        sa.Column("operation_amount", sa.Numeric(18, 4)),
        sa.Column("delivered_amount", sa.Numeric(18, 4)),
        sa.Column("excess", sa.Numeric(18, 4)),
        sa.Column("commission", sa.Numeric(18, 4)),
        sa.Column("commission_rate", sa.Numeric(18, 6)),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_message_id", sa.UUID()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("kind IN ('out', 'in', 'operation')", name=op.f("ck_broker_movements_broker_movement_kind")),
        sa.CheckConstraint(
            "(kind = 'out' AND balance_delta < 0) OR (kind IN ('in', 'operation') AND balance_delta > 0)",
            name=op.f("ck_broker_movements_broker_movement_sign"),
        ),
        sa.CheckConstraint(
            "(kind <> 'operation' AND operation_amount IS NULL AND delivered_amount IS NULL "
            "AND excess IS NULL AND commission IS NULL AND commission_rate IS NULL) OR "
            "(kind = 'operation' AND operation_amount > 0 AND delivered_amount > 0 "
            "AND delivered_amount <= operation_amount AND excess IS NOT NULL "
            "AND commission IS NOT NULL AND commission_rate IS NOT NULL "
            "AND balance_delta = excess + commission)",
            name=op.f("ck_broker_movements_broker_movement_operation_fields"),
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["broker_member_id"], ["business_members.id"]),
        sa.ForeignKeyConstraint(["recorded_by_member_id"], ["business_members.id"]),
        sa.ForeignKeyConstraint(["source_message_id"], ["inbound_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_broker_movements_account_date",
        "broker_movements",
        ["business_id", "broker_member_id", "business_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_broker_movements_account_date", table_name="broker_movements")
    op.drop_table("broker_movements")
    op.drop_column("businesses", "vertical")
