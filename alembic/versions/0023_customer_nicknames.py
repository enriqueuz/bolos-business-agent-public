"""Optional customer nickname, canonical unique identity within each business."""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.add_column("customers", sa.Column("nickname", sa.String(160), nullable=True))
    op.alter_column("customers", "name", existing_type=sa.String(160), nullable=True)
    op.create_check_constraint("customer_identifier", "customers",
        "nullif(btrim(name), '') IS NOT NULL OR nullif(btrim(nickname), '') IS NOT NULL")
    op.create_check_constraint("customer_nickname_canonical", "customers",
        "nickname IS NULL OR (nickname <> '' AND nickname = upper(regexp_replace(btrim(unaccent(nickname)), '[[:space:]]+', ' ', 'g')))")
    op.create_index("uq_customers_business_nickname", "customers", ["business_id", "nickname"],
                    unique=True, postgresql_where=sa.text("deleted_at IS NULL AND nickname IS NOT NULL"))
    # Keep the existing view contract, including display labels for nickname-only customers.
    view = op.get_bind().execute(sa.text("SELECT pg_get_viewdef('money_movements'::regclass, true)")).scalar_one()
    op.execute("CREATE OR REPLACE VIEW money_movements AS " + view.replace("c.name", "COALESCE(c.name, c.nickname)"))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM customers WHERE name IS NULL OR nickname IS NOT NULL)")).scalar():
        raise RuntimeError("Cannot discard customer nicknames or nickname-only identities. Export and resolve them explicitly first.")
    view = op.get_bind().execute(sa.text("SELECT pg_get_viewdef('money_movements'::regclass, true)")).scalar_one()
    op.execute("CREATE OR REPLACE VIEW money_movements AS " + view.replace("COALESCE(c.name, c.nickname)", "c.name"))
    op.drop_index("uq_customers_business_nickname", table_name="customers")
    op.drop_constraint(op.f("ck_customers_customer_nickname_canonical"), "customers", type_="check")
    op.drop_constraint(op.f("ck_customers_customer_identifier"), "customers", type_="check")
    op.alter_column("customers", "name", existing_type=sa.String(160), nullable=False)
    op.drop_column("customers", "nickname")
