"""money_movements: vista unificada de todo lo que mueve dinero

Revision ID: 0002
Revises: 0001
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# direction: +1 entra dinero, -1 sale dinero. "cash_*" = lo que realmente se movió en caja ese día
# (en una venta fiada o parcial, solo lo cobrado). amount_* = valor total del movimiento.
VIEW = """
CREATE VIEW money_movements AS
SELECT 'sale'::text AS kind, s.id, s.business_id, s.business_date, s.occurred_at,
       coalesce(nullif(s.notes, ''), (SELECT string_agg(si.quantity::float8::text || ' x ' || si.description, ', ' ORDER BY si.position) FROM sale_items si WHERE si.sale_id = s.id)) AS description,
       s.customer_id, c.name AS customer_name, s.payment_method, s.payment_status, NULL::text AS category,
       1 AS direction,
       s.total_amount AS amount, s.total_currency AS currency,
       s.total_ves AS amount_ves, s.total_usd_bcv AS amount_usd_bcv, s.total_usd_market AS amount_usd_market,
       CASE WHEN s.total_amount > 0 THEN s.total_ves * s.paid_amount / s.total_amount ELSE 0 END AS cash_ves,
       CASE WHEN s.total_amount > 0 THEN s.total_usd_bcv * s.paid_amount / s.total_amount ELSE 0 END AS cash_usd_bcv,
       CASE WHEN s.total_amount > 0 THEN s.total_usd_market * s.paid_amount / s.total_amount ELSE 0 END AS cash_usd_market,
       s.rate_bcv, s.rate_market, s.rate_market_source, s.version, s.created_at
FROM sales s LEFT JOIN customers c ON c.id = s.customer_id
WHERE s.deleted_at IS NULL
UNION ALL
SELECT 'expense', e.id, e.business_id, e.business_date, e.occurred_at, e.description,
       NULL, NULL, e.payment_method, NULL, e.category, -1,
       e.amount, e.currency, e.amount_ves, e.amount_usd_bcv, e.amount_usd_market,
       e.amount_ves, e.amount_usd_bcv, e.amount_usd_market,
       e.rate_bcv, e.rate_market, e.rate_market_source, e.version, e.created_at
FROM expenses e WHERE e.deleted_at IS NULL
UNION ALL
SELECT 'purchase', p.id, p.business_id, p.business_date, p.occurred_at,
       p.description || coalesce(' (' || p.supplier_name || ')', ''),
       NULL, NULL, p.payment_method, NULL, NULL, -1,
       p.amount, p.currency, p.amount_ves, p.amount_usd_bcv, p.amount_usd_market,
       p.amount_ves, p.amount_usd_bcv, p.amount_usd_market,
       p.rate_bcv, p.rate_market, p.rate_market_source, p.version, p.created_at
FROM purchases p WHERE p.deleted_at IS NULL
UNION ALL
SELECT 'payment', cp.id, cp.business_id, cp.business_date, cp.occurred_at, 'Abono de ' || c.name,
       cp.customer_id, c.name, cp.payment_method, NULL, NULL, 1,
       cp.amount, cp.currency, cp.amount_ves, cp.amount_usd_bcv, cp.amount_usd_market,
       cp.amount_ves, cp.amount_usd_bcv, cp.amount_usd_market,
       cp.rate_bcv, cp.rate_market, cp.rate_market_source, cp.version, cp.created_at
FROM customer_payments cp JOIN customers c ON c.id = cp.customer_id
WHERE cp.deleted_at IS NULL
UNION ALL
SELECT 'receivable', r.id, r.business_id, r.business_date, r.occurred_at,
       coalesce(r.description, 'Fiado a ' || c.name), r.customer_id, c.name, NULL, NULL, NULL, 0,
       r.amount, r.currency, r.amount_ves, r.amount_usd_bcv, r.amount_usd_market,
       0, 0, 0,
       r.rate_bcv, r.rate_market, r.rate_market_source, r.version, r.created_at
FROM receivables r JOIN customers c ON c.id = r.customer_id
WHERE r.deleted_at IS NULL AND r.sale_id IS NULL
"""

# Saldo vivo por fiado (solo asignaciones de abonos no borrados).
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


def upgrade() -> None:
    op.execute(VIEW)
    op.execute(RECEIVABLE_BALANCES)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS receivable_balances")
    op.execute("DROP VIEW IF EXISTS money_movements")
