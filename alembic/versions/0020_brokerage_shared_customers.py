"""Customer visibility and portfolio scope independent of executor."""

import sqlalchemy as sa

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = depends_on = None


LEGACY_OPERATION_FIELDS = (
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


def upgrade():
    op.add_column("customers", sa.Column("classification", sa.String(16)))
    op.add_column("customers", sa.Column("owner_broker_member_id", sa.UUID()))
    op.create_foreign_key(None, "customers", "business_members", ["owner_broker_member_id"], ["id"])
    op.create_index("ix_customers_owner_broker_member_id", "customers", ["owner_broker_member_id"])
    op.create_check_constraint("customer_classification", "customers",
        "(classification IS NULL AND owner_broker_member_id IS NULL) OR "
        "(classification IS NOT NULL AND classification = 'global' AND owner_broker_member_id IS NULL) OR "
        "(classification IS NOT NULL AND classification = 'personal' AND owner_broker_member_id IS NOT NULL)")
    op.add_column("broker_movements", sa.Column("portfolio_broker_member_id", sa.UUID()))
    op.create_foreign_key(None, "broker_movements", "business_members", ["portfolio_broker_member_id"], ["id"])
    op.execute("UPDATE broker_movements SET portfolio_broker_member_id = broker_member_id")
    # Keep historical rows intact and unclassified. Activation requires a verified pilot reset or explicit classification.
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_sign"), "broker_movements", type_="check")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_operation_fields"), "broker_movements", type_="check")
    op.create_check_constraint("broker_movement_sign", "broker_movements",
        "(broker_member_id IS NULL AND balance_delta = 0) OR "
        "(broker_member_id IS NOT NULL AND ((kind = 'in' AND (balance_delta > 0 OR "
        "(balance_delta = 0 AND related_payment_id IS NOT NULL AND portfolio_broker_member_id IS NULL))) "
        "OR (kind = 'operation' AND (operation_type = 'buy' OR balance_delta >= 0))))")
    op.create_check_constraint("broker_movement_operation_fields", "broker_movements", LEGACY_OPERATION_FIELDS.replace(
        "ELSE commission - operation_amount END", "WHEN portfolio_broker_member_id IS NULL THEN commission ELSE commission - operation_amount END"))


def downgrade():
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM customers WHERE classification IS NOT NULL)")).scalar():
        raise RuntimeError("Cannot discard customer classification; an explicit downgrade strategy is required.")
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM broker_movements WHERE "
                                  "(related_receivable_id IS NOT NULL OR related_payment_id IS NOT NULL) "
                                  "AND portfolio_broker_member_id IS DISTINCT FROM broker_member_id)")).scalar():
        raise RuntimeError("Cannot discard shared portfolio attribution.")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_sign"), "broker_movements", type_="check")
    op.drop_constraint(op.f("ck_broker_movements_broker_movement_operation_fields"), "broker_movements", type_="check")
    op.create_check_constraint("broker_movement_sign", "broker_movements",
        "(broker_member_id IS NULL AND balance_delta = 0) OR "
        "(broker_member_id IS NOT NULL AND ((kind = 'in' AND balance_delta > 0) "
        "OR (kind = 'operation' AND (operation_type = 'buy' OR balance_delta >= 0))))")
    op.create_check_constraint("broker_movement_operation_fields", "broker_movements", LEGACY_OPERATION_FIELDS)
    op.drop_constraint(op.f("fk_broker_movements_portfolio_broker_member_id_business_members"), "broker_movements", type_="foreignkey")
    op.drop_column("broker_movements", "portfolio_broker_member_id")
    op.drop_constraint(op.f("ck_customers_customer_classification"), "customers", type_="check")
    op.drop_index("ix_customers_owner_broker_member_id", table_name="customers")
    op.drop_constraint(op.f("fk_customers_owner_broker_member_id_business_members"), "customers", type_="foreignkey")
    op.drop_column("customers", "owner_broker_member_id")
    op.drop_column("customers", "classification")
