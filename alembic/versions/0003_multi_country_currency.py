"""Generalización multi-país: moneda local en vez de VES, tasa oficial en vez de BCV, métodos de pago neutros

Revision ID: 0003
Revises: 0002

Motivo: preparar el sistema para Latinoamérica (COP, ARS, …) sin implementar otras monedas todavía.
Toda la lógica específica de país vive ahora en app/domain/countries.py; el esquema es neutral.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AMOUNT_TABLES = ["expenses", "purchases", "receivables", "customer_payments"]
RENAMES: dict[str, list[tuple[str, str]]] = {
    **{t: [("amount_ves", "amount_local"), ("amount_usd_bcv", "amount_usd_official"), ("rate_bcv", "rate_official")]
        for t in AMOUNT_TABLES},
    "sales": [("total_ves", "total_local"), ("total_usd_bcv", "total_usd_official"), ("rate_bcv", "rate_official")],
    "sale_items": [("line_ves", "line_local"), ("line_usd_bcv", "line_usd_official"), ("cost_ves", "cost_local"),
                   ("cost_usd_bcv", "cost_usd_official")],
    "purchase_items": [("line_ves", "line_local"), ("line_usd_bcv", "line_usd_official")],
}
PAYMENT_METHOD_MAP = {"cash_ves": "cash_local", "pago_movil": "mobile_payment", "binance": "crypto"}
VIEW_MAP = {"ves": "local", "usd_bcv": "usd_official"}

VIEW = """
CREATE VIEW money_movements AS
SELECT 'sale'::text AS kind, s.id, s.business_id, s.business_date, s.occurred_at,
       coalesce(nullif(s.notes, ''), (SELECT string_agg(si.quantity::float8::text || ' x ' || si.description, ', ' ORDER BY si.position) FROM sale_items si WHERE si.sale_id = s.id)) AS description,
       s.customer_id, c.name AS customer_name, s.payment_method, s.payment_status, NULL::text AS category,
       1 AS direction,
       s.total_amount AS amount, s.total_currency AS currency,
       s.total_local AS amount_local, s.total_usd_official AS amount_usd_official, s.total_usd_market AS amount_usd_market,
       CASE WHEN s.total_amount > 0 THEN s.total_local * s.paid_amount / s.total_amount ELSE 0 END AS cash_local,
       CASE WHEN s.total_amount > 0 THEN s.total_usd_official * s.paid_amount / s.total_amount ELSE 0 END AS cash_usd_official,
       CASE WHEN s.total_amount > 0 THEN s.total_usd_market * s.paid_amount / s.total_amount ELSE 0 END AS cash_usd_market,
       s.rate_official, s.rate_market, s.rate_market_source, s.version, s.created_at
FROM sales s LEFT JOIN customers c ON c.id = s.customer_id
WHERE s.deleted_at IS NULL
UNION ALL
SELECT 'expense', e.id, e.business_id, e.business_date, e.occurred_at, e.description,
       NULL, NULL, e.payment_method, NULL, e.category, -1,
       e.amount, e.currency, e.amount_local, e.amount_usd_official, e.amount_usd_market,
       e.amount_local, e.amount_usd_official, e.amount_usd_market,
       e.rate_official, e.rate_market, e.rate_market_source, e.version, e.created_at
FROM expenses e WHERE e.deleted_at IS NULL
UNION ALL
SELECT 'purchase', p.id, p.business_id, p.business_date, p.occurred_at,
       p.description || coalesce(' (' || p.supplier_name || ')', ''),
       NULL, NULL, p.payment_method, NULL, NULL, -1,
       p.amount, p.currency, p.amount_local, p.amount_usd_official, p.amount_usd_market,
       p.amount_local, p.amount_usd_official, p.amount_usd_market,
       p.rate_official, p.rate_market, p.rate_market_source, p.version, p.created_at
FROM purchases p WHERE p.deleted_at IS NULL
UNION ALL
SELECT 'payment', cp.id, cp.business_id, cp.business_date, cp.occurred_at, 'Abono de ' || c.name,
       cp.customer_id, c.name, cp.payment_method, NULL, NULL, 1,
       cp.amount, cp.currency, cp.amount_local, cp.amount_usd_official, cp.amount_usd_market,
       cp.amount_local, cp.amount_usd_official, cp.amount_usd_market,
       cp.rate_official, cp.rate_market, cp.rate_market_source, cp.version, cp.created_at
FROM customer_payments cp JOIN customers c ON c.id = cp.customer_id
WHERE cp.deleted_at IS NULL
UNION ALL
SELECT 'receivable', r.id, r.business_id, r.business_date, r.occurred_at,
       coalesce(r.description, 'Fiado a ' || c.name), r.customer_id, c.name, NULL, NULL, NULL, 0,
       r.amount, r.currency, r.amount_local, r.amount_usd_official, r.amount_usd_market,
       0, 0, 0,
       r.rate_official, r.rate_market, r.rate_market_source, r.version, r.created_at
FROM receivables r JOIN customers c ON c.id = r.customer_id
WHERE r.deleted_at IS NULL AND r.sale_id IS NULL
"""

RECEIVABLE_BALANCES = """
CREATE VIEW receivable_balances AS
SELECT r.id AS receivable_id, r.business_id, r.customer_id, r.sale_id, r.business_date, r.currency,
       r.amount AS original_amount,
       coalesce(sum(pa.applied_amount) FILTER (WHERE cp.deleted_at IS NULL), 0) AS paid_amount,
       r.amount - coalesce(sum(pa.applied_amount) FILTER (WHERE cp.deleted_at IS NULL), 0) AS remaining_amount
FROM receivables r
LEFT JOIN payment_allocations pa ON pa.receivable_id = r.id
LEFT JOIN customer_payments cp ON cp.id = pa.payment_id
WHERE r.deleted_at IS NULL
GROUP BY r.id
"""


def _map_values(table: str, column: str, mapping: dict[str, str]) -> None:
    for old, new in mapping.items():
        op.execute(sa.text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old").bindparams(new=new, old=old))


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS receivable_balances")
    op.execute("DROP VIEW IF EXISTS money_movements")

    for table, pairs in RENAMES.items():
        for old, new in pairs:
            op.alter_column(table, old, new_column_name=new)

    # negocio: país y moneda local
    op.add_column("businesses", sa.Column("country_code", sa.String(2), nullable=False, server_default="VE"))
    op.add_column("businesses", sa.Column("local_currency", sa.String(3), nullable=False, server_default="VES"))
    _map_values("businesses", "display_currency", VIEW_MAP)
    op.alter_column("businesses", "display_currency", server_default="usd_official")
    for old, new in PAYMENT_METHOD_MAP.items():
        op.execute(sa.text(
            "UPDATE businesses SET payment_methods = (SELECT coalesce(jsonb_agg(CASE WHEN m = to_jsonb(CAST(:old AS text)) "
            "THEN to_jsonb(CAST(:new AS text)) ELSE m END), '[]'::jsonb) FROM jsonb_array_elements(payment_methods) m)"
        ).bindparams(old=old, new=new))

    # métodos de pago neutros
    for table in ["sales", "expenses", "purchases", "customer_payments"]:
        _map_values(table, "payment_method", PAYMENT_METHOD_MAP)

    # tasas: por moneda y tipo (oficial/mercado) + proveedor
    op.drop_index("ix_exchange_rates_source_fetched", table_name="exchange_rates")
    op.alter_column("exchange_rates", "source", new_column_name="kind")
    _map_values("exchange_rates", "kind", {"bcv": "official", "market_auto": "market"})
    op.add_column("exchange_rates", sa.Column("currency", sa.String(3), nullable=False, server_default="VES"))
    op.alter_column("exchange_rates", "currency", server_default=None)
    op.add_column("exchange_rates", sa.Column("provider", sa.String(40), nullable=True))
    op.execute("UPDATE exchange_rates SET provider = 'dolarapi_ve'")
    op.create_index("ix_exchange_rates_currency_kind_fetched", "exchange_rates", ["currency", "kind", "fetched_at"])

    # snapshots de cierres de caja ya guardados
    for old, new in VIEW_MAP.items():
        op.execute(sa.text(
            "UPDATE cash_sessions SET summary = jsonb_set(summary, '{currency_view}', to_jsonb(CAST(:new AS text))) "
            "WHERE summary->>'currency_view' = :old"
        ).bindparams(old=old, new=new))

    op.execute(VIEW)
    op.execute(RECEIVABLE_BALANCES)


def downgrade() -> None:
    raise NotImplementedError("Migración de generalización multi-país: no se revierte (restaurar backup si hace falta).")
