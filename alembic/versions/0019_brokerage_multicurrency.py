"""Real transaction money and reference-free USD portfolios."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = depends_on = None

VALUATION = (
    "(valuation_mode = 'reference' AND amount_local IS NOT NULL AND rate_official IS NOT NULL "
    "AND rate_market IS NOT NULL AND rate_applied IS NOT NULL AND rate_applied_source IS NOT NULL) OR "
    "(valuation_mode = 'brokerage_usd' AND currency = 'USD' AND amount_local IS NULL "
    "AND rate_official IS NULL AND rate_market IS NULL AND rate_applied IS NULL AND rate_applied_source IS NULL "
    "AND amount_usd_official = amount AND amount_usd_market = amount AND amount_usd_applied = amount)"
)
MONEY = (
    "(operation_amount_bs IS NULL AND exchange_rate IS NULL AND exchange_rate_source IS NULL AND monetary_basis IS NULL) OR "
    "(kind = 'operation' AND excess = 0 AND operation_amount_bs > 0 AND exchange_rate > 0 "
    "AND operation_amount_bs IS NOT NULL AND exchange_rate IS NOT NULL "
    "AND exchange_rate_source IS NOT NULL AND monetary_basis IS NOT NULL "
    "AND exchange_rate_source IN ('explicit', 'calculated') AND monetary_basis IN ('usd_bs', 'usd_rate'))"
)


def upgrade():
    for column in [sa.Column("operation_amount_bs", sa.Numeric(24, 2)),
                   sa.Column("exchange_rate", sa.Numeric(28, 2)),
                   sa.Column("exchange_rate_source", sa.String(16)),
                   sa.Column("monetary_basis", sa.String(16)),
                   sa.Column("monetary_provenance", postgresql.JSONB())]:
        op.add_column("broker_movements", column)
    op.create_check_constraint("broker_movement_real_money", "broker_movements", MONEY)
    for table, name in [("receivables", "receivable"), ("customer_payments", "payment")]:
        op.add_column(table, sa.Column("valuation_mode", sa.String(20), nullable=False, server_default="reference"))
        for column in ["amount_local", "rate_official", "rate_market", "rate_applied", "rate_applied_source"]:
            op.alter_column(table, column, nullable=True)
        op.create_check_constraint(name + "_valuation", table, VALUATION)


def downgrade():
    connection = op.get_bind()
    for table in ["receivables", "customer_payments"]:
        if connection.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE valuation_mode = 'brokerage_usd')")).scalar():
            raise RuntimeError("Cannot downgrade native USD portfolios without an explicit conversion strategy.")
    if connection.execute(sa.text("SELECT EXISTS (SELECT 1 FROM broker_movements WHERE operation_amount_bs IS NOT NULL)")).scalar():
        raise RuntimeError("Cannot discard real transaction money.")
    op.drop_constraint("broker_movement_real_money", "broker_movements", type_="check")
    for column in ["monetary_provenance", "monetary_basis", "exchange_rate_source", "exchange_rate", "operation_amount_bs"]:
        op.drop_column("broker_movements", column)
    for table, name in [("receivables", "receivable"), ("customer_payments", "payment")]:
        op.drop_constraint(name + "_valuation", table, type_="check")
        for column in ["amount_local", "rate_official", "rate_market", "rate_applied", "rate_applied_source"]:
            op.alter_column(table, column, nullable=False)
        op.drop_column(table, "valuation_mode")
