"""tasas, precios y pagos: tasa para cobrar, tasa real de los bolívares, pago real de cada venta y cambios de moneda

Revision ID: 0008
Revises: 0007

Motivo (docs: plan "tasas, precios y pagos", Parte 1):
- Cada movimiento congela la tasa para cobrar que se usó (`rate_applied`) y su pata en $ (`*_usd_applied`), que es
  la de los reportes. `rate_official` y `rate_market` quedan como referencias automáticas puras; `rate_market_source`
  desaparece.
- `businesses.charge_rate_mode`: el negocio cobra a la oficial, a la de mercado o a su tasa propia.
- `custom_rates.purpose`: charge (tasa para cobrar) o funds (tasa real de los bolívares del negocio).
- Ventas: el pago real (`paid_amount` + `paid_currency` + patas), el precio antes del descuento y el descuento.
- Gastos y compras: `rate_funds` y `amount_usd_real` (costo real en $).
- Nueva tabla `currency_exchanges` (cambios de moneda: no son venta, gasto ni ingreso).
- `line_*` → `item_*` en sale_items y purchase_items; en la vista `cash_*` → `cashflow_*`.
- Método de pago: `cash_usd` y `cash_local` → `cash` (la moneda ya está en la fila).

Backfill: los reportes de cada negocio deben dar las mismas cifras que antes (ver comentarios en `upgrade`).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

AMOUNT = sa.Numeric(18, 4)
RATE = sa.Numeric(18, 6)

# tabla → prefijo de sus patas
MOVEMENTS = {"sales": "total", "expenses": "amount", "purchases": "amount", "receivables": "amount",
             "customer_payments": "amount"}
ITEM_RENAMES = {
    "sale_items": ["amount", "currency", "local", "usd_official", "usd_market"],
    "purchase_items": ["amount", "local", "usd_official", "usd_market"],
}
PAYMENT_TABLES = ["sales", "expenses", "purchases", "customer_payments"]
SALE_PAID_LEGS = ["local", "usd_official", "usd_market", "usd_applied"]
SALE_DISCOUNT_LEGS = ["amount", "local", "usd_official", "usd_market", "usd_applied"]

# Tasa de mercado automática vigente al crear la fila `x` (la última anterior; si no hay, la primera posterior; si no
# hay ninguna, la oficial de la fila). `b` es el negocio de la fila.
MARKET_AT_CREATION = """coalesce(
    (SELECT er.rate FROM exchange_rates er WHERE er.currency = b.local_currency AND er.kind = 'market'
       AND er.fetched_at <= x.created_at ORDER BY er.fetched_at DESC LIMIT 1),
    (SELECT er.rate FROM exchange_rates er WHERE er.currency = b.local_currency AND er.kind = 'market'
       AND er.fetched_at > x.created_at ORDER BY er.fetched_at ASC LIMIT 1),
    x.rate_official)"""


def _legs(prefix: str, currency: str, rate: str, rate_official: str, rate_market: str) -> str:
    """Patas local/usd_official/usd_market/usd_applied de un monto en la vista, calculadas con la tasa dada."""
    return f"""
       CASE WHEN {currency} = b.local_currency THEN {prefix} ELSE {prefix} * {rate} END,
       CASE WHEN {currency} = b.local_currency THEN {prefix} / {rate_official} ELSE {prefix} END,
       CASE WHEN {currency} = b.local_currency THEN {prefix} / {rate_market} ELSE {prefix} END,
       CASE WHEN {currency} = b.local_currency THEN {prefix} / {rate} ELSE {prefix} END"""


VIEW = f"""
CREATE VIEW money_movements AS
SELECT 'sale'::text AS kind, s.id, s.business_id, s.business_date, s.occurred_at,
       coalesce(nullif(s.notes, ''), (SELECT string_agg(si.quantity::float8::text || ' x ' || si.description, ', ' ORDER BY si.position) FROM sale_items si WHERE si.sale_id = s.id)) AS description,
       s.customer_id, c.name AS customer_name, s.payment_method, s.payment_status, NULL::text AS category,
       1 AS direction,
       s.total_amount AS amount, s.total_currency AS currency,
       s.total_local AS amount_local, s.total_usd_official AS amount_usd_official, s.total_usd_market AS amount_usd_market,
       s.total_usd_applied AS amount_usd_applied,
       s.paid_amount AS cashflow_amount, s.paid_currency AS cashflow_currency,
       s.paid_local AS cashflow_local, s.paid_usd_official AS cashflow_usd_official,
       s.paid_usd_market AS cashflow_usd_market, s.paid_usd_applied AS cashflow_usd_applied,
       s.rate_official, s.rate_market, s.rate_applied, s.rate_applied_source, s.version, s.created_at
FROM sales s LEFT JOIN customers c ON c.id = s.customer_id
WHERE s.deleted_at IS NULL
UNION ALL
SELECT 'expense', e.id, e.business_id, e.business_date, e.occurred_at, e.description,
       NULL, NULL, e.payment_method, NULL, e.category, -1,
       e.amount, e.currency, e.amount_local, e.amount_usd_official, e.amount_usd_market, e.amount_usd_applied,
       e.amount, e.currency, e.amount_local, e.amount_usd_official, e.amount_usd_market, e.amount_usd_applied,
       e.rate_official, e.rate_market, e.rate_applied, e.rate_applied_source, e.version, e.created_at
FROM expenses e WHERE e.deleted_at IS NULL
UNION ALL
SELECT 'purchase', p.id, p.business_id, p.business_date, p.occurred_at,
       p.description || coalesce(' (' || p.supplier_name || ')', ''),
       NULL, NULL, p.payment_method, NULL, NULL, -1,
       p.amount, p.currency, p.amount_local, p.amount_usd_official, p.amount_usd_market, p.amount_usd_applied,
       p.amount, p.currency, p.amount_local, p.amount_usd_official, p.amount_usd_market, p.amount_usd_applied,
       p.rate_official, p.rate_market, p.rate_applied, p.rate_applied_source, p.version, p.created_at
FROM purchases p WHERE p.deleted_at IS NULL
UNION ALL
SELECT 'payment', cp.id, cp.business_id, cp.business_date, cp.occurred_at, 'Abono de ' || c.name,
       cp.customer_id, c.name, cp.payment_method, NULL, NULL, 1,
       cp.amount, cp.currency, cp.amount_local, cp.amount_usd_official, cp.amount_usd_market, cp.amount_usd_applied,
       cp.amount, cp.currency, cp.amount_local, cp.amount_usd_official, cp.amount_usd_market, cp.amount_usd_applied,
       cp.rate_official, cp.rate_market, cp.rate_applied, cp.rate_applied_source, cp.version, cp.created_at
FROM customer_payments cp JOIN customers c ON c.id = cp.customer_id
WHERE cp.deleted_at IS NULL
UNION ALL
SELECT 'receivable', r.id, r.business_id, r.business_date, r.occurred_at,
       coalesce(r.description, 'Fiado a ' || c.name), r.customer_id, c.name, NULL, NULL, NULL, 0,
       r.amount, r.currency, r.amount_local, r.amount_usd_official, r.amount_usd_market, r.amount_usd_applied,
       0, r.currency, 0, 0, 0, 0,
       r.rate_official, r.rate_market, r.rate_applied, r.rate_applied_source, r.version, r.created_at
FROM receivables r JOIN customers c ON c.id = r.customer_id
WHERE r.deleted_at IS NULL AND r.sale_id IS NULL
UNION ALL
SELECT 'exchange', x.id, x.business_id, x.business_date, x.occurred_at, coalesce(x.description, 'Cambio de moneda'),
       NULL, NULL, x.out_method, NULL, NULL, -1,
       x.out_amount, x.out_currency, {_legs("x.out_amount", "x.out_currency", "x.rate", "x.rate_official", "x.rate_market")},
       x.out_amount, x.out_currency, {_legs("x.out_amount", "x.out_currency", "x.rate", "x.rate_official", "x.rate_market")},
       x.rate_official, x.rate_market, x.rate, 'exchange', x.version, x.created_at
FROM currency_exchanges x JOIN businesses b ON b.id = x.business_id
WHERE x.deleted_at IS NULL
UNION ALL
SELECT 'exchange', x.id, x.business_id, x.business_date, x.occurred_at, coalesce(x.description, 'Cambio de moneda'),
       NULL, NULL, x.in_method, NULL, NULL, 1,
       x.in_amount, x.in_currency, {_legs("x.in_amount", "x.in_currency", "x.rate", "x.rate_official", "x.rate_market")},
       x.in_amount, x.in_currency, {_legs("x.in_amount", "x.in_currency", "x.rate", "x.rate_official", "x.rate_market")},
       x.rate_official, x.rate_market, x.rate, 'exchange', x.version, x.created_at
FROM currency_exchanges x JOIN businesses b ON b.id = x.business_id
WHERE x.deleted_at IS NULL
"""

# Vista anterior (0003), para el downgrade.
OLD_VIEW = """
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


def _map_methods_json(column: str, mapping_sql: str) -> str:
    """Reescribe una lista JSONB de métodos de pago con `mapping_sql` (sobre `e`), sin duplicados y en el mismo orden."""
    return (
        f"(SELECT coalesce(jsonb_agg(to_jsonb(m) ORDER BY first_ord), '[]'::jsonb) FROM "
        f"(SELECT m, min(ord) AS first_ord FROM "
        f"(SELECT {mapping_sql} AS m, ord FROM jsonb_array_elements_text({column}) WITH ORDINALITY t(e, ord)) s "
        f"GROUP BY m) u)"
    )


def upgrade() -> None:
    op.execute("DROP VIEW IF EXISTS money_movements")

    # ---------------------------------------------------------------- negocio: tasa para cobrar
    op.add_column("businesses", sa.Column("charge_rate_mode", sa.String(32), nullable=False, server_default="official"))
    op.add_column("businesses", sa.Column("charge_rate_confirmed_on", sa.Date(), nullable=True))

    # ---------------------------------------------------------------- cambios de moneda
    op.create_table(
        "currency_exchanges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("business_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("businesses.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("out_amount", AMOUNT, nullable=False),
        sa.Column("out_currency", sa.String(32), nullable=False),
        sa.Column("out_method", sa.String(32), nullable=True),
        sa.Column("in_amount", AMOUNT, nullable=False),
        sa.Column("in_currency", sa.String(32), nullable=False),
        sa.Column("in_method", sa.String(32), nullable=True),
        sa.Column("rate", RATE, nullable=False),
        sa.Column("rate_official", RATE, nullable=False),
        sa.Column("rate_market", RATE, nullable=False),
        sa.Column("description", sa.String(300), nullable=True),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("inbound_messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_index("ix_currency_exchanges_business_date", "currency_exchanges", ["business_id", "business_date"])

    # ---------------------------------------------------------------- tasas propias: para cobrar o tasa real de los Bs
    op.add_column("custom_rates", sa.Column("purpose", sa.String(32), nullable=False, server_default="charge"))
    op.add_column("custom_rates", sa.Column("source", sa.String(32), nullable=False, server_default="manual"))
    op.add_column("custom_rates", sa.Column(
        "exchange_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("currency_exchanges.id", ondelete="SET NULL",
                      name="fk_custom_rates_exchange_id_currency_exchanges"),
        nullable=True))
    op.create_index("ix_custom_rates_business_purpose_set", "custom_rates", ["business_id", "purpose", "set_at"])

    # ---------------------------------------------------------------- line_* → item_*
    for table, legs in ITEM_RENAMES.items():
        for leg in legs:
            op.alter_column(table, f"line_{leg}", new_column_name=f"item_{leg}")

    # ---------------------------------------------------------------- columnas nuevas (nullable hasta el backfill)
    for table, prefix in MOVEMENTS.items():
        op.add_column(table, sa.Column("rate_applied", RATE, nullable=True))
        op.add_column(table, sa.Column("rate_applied_source", sa.String(32), nullable=True))
        op.add_column(table, sa.Column(f"{prefix}_usd_applied", AMOUNT, nullable=True))
    op.add_column("sale_items", sa.Column("item_usd_applied", AMOUNT, nullable=True))
    op.add_column("sale_items", sa.Column("cost_usd_applied", AMOUNT, nullable=True))
    op.add_column("purchase_items", sa.Column("item_usd_applied", AMOUNT, nullable=True))

    op.add_column("sales", sa.Column("list_total_amount", AMOUNT, nullable=True))
    for leg in SALE_DISCOUNT_LEGS:
        op.add_column("sales", sa.Column(f"discount_{leg}", AMOUNT, nullable=False, server_default="0"))
    op.add_column("sales", sa.Column("paid_currency", sa.String(32), nullable=True))
    for leg in SALE_PAID_LEGS:
        op.add_column("sales", sa.Column(f"paid_{leg}", AMOUNT, nullable=True))
    op.add_column("sales", sa.Column("settlement", sa.String(32), nullable=False, server_default="exact"))

    for table in ("expenses", "purchases"):
        op.add_column(table, sa.Column("rate_funds", RATE, nullable=True))
        op.add_column(table, sa.Column("amount_usd_real", AMOUNT, nullable=True))

    # ---------------------------------------------------------------- backfill
    # 1) Modo de cada negocio, desde lo que veía en sus resúmenes y si tenía tasa propia activa.
    op.execute("""
        UPDATE businesses b SET charge_rate_mode = CASE
            WHEN b.display_currency = 'usd_official' THEN 'official'
            WHEN EXISTS (SELECT 1 FROM custom_rates cr WHERE cr.business_id = b.id AND cr.cleared_at IS NULL) THEN 'custom'
            WHEN b.display_currency = 'usd_market' THEN 'market'
            ELSE 'official' END
    """)

    # 2) Tasa aplicada de cada fila según el modo de su negocio: así la pata en $ que ve cada negocio no cambia.
    #    Modo official → la oficial; market/custom → la que estaba en rate_market (automática, propia o inline).
    for table, prefix in MOVEMENTS.items():
        op.execute(f"""
            UPDATE {table} x SET
                rate_applied = CASE WHEN b.charge_rate_mode = 'official' THEN x.rate_official ELSE x.rate_market END,
                rate_applied_source = CASE
                    WHEN b.charge_rate_mode = 'official' THEN 'official'
                    WHEN x.rate_market_source IN ('custom', 'inline') THEN x.rate_market_source
                    ELSE 'market' END,
                {prefix}_usd_applied = CASE WHEN b.charge_rate_mode = 'official'
                                            THEN x.{prefix}_usd_official ELSE x.{prefix}_usd_market END
            FROM businesses b WHERE b.id = x.business_id
        """)
    op.execute("""
        UPDATE sale_items si SET
            item_usd_applied = CASE WHEN b.charge_rate_mode = 'official' THEN si.item_usd_official ELSE si.item_usd_market END,
            cost_usd_applied = CASE WHEN b.charge_rate_mode = 'official' THEN si.cost_usd_official ELSE si.cost_usd_market END
        FROM sales s JOIN businesses b ON b.id = s.business_id WHERE s.id = si.sale_id
    """)
    op.execute("""
        UPDATE purchase_items pi SET
            item_usd_applied = CASE WHEN b.charge_rate_mode = 'official' THEN pi.item_usd_official ELSE pi.item_usd_market END
        FROM purchases p JOIN businesses b ON b.id = p.business_id WHERE p.id = pi.purchase_id
    """)

    # 3) rate_market vuelve a ser la referencia automática en filas que guardaban una tasa propia o inline, y se
    #    recalculan sus patas de mercado de montos en moneda local (las de montos en $ no dependen de la tasa).
    op.execute("CREATE TEMP TABLE _market_fix (tbl text, id uuid, rate numeric(18,6))")
    for table in MOVEMENTS:
        op.execute(f"""
            INSERT INTO _market_fix
            SELECT '{table}', x.id, {MARKET_AT_CREATION}
            FROM {table} x JOIN businesses b ON b.id = x.business_id
            WHERE x.rate_market_source IN ('custom', 'inline')
        """)
    for table, prefix in MOVEMENTS.items():
        currency = "total_currency" if table == "sales" else "currency"
        op.execute(f"""
            UPDATE {table} x SET rate_market = f.rate,
                {prefix}_usd_market = CASE WHEN x.{currency} = b.local_currency
                                           THEN round(x.{prefix}_local / f.rate, 4) ELSE x.{prefix}_usd_market END
            FROM _market_fix f, businesses b
            WHERE f.tbl = '{table}' AND f.id = x.id AND b.id = x.business_id
        """)
    op.execute("""
        UPDATE sale_items si SET
            item_usd_market = CASE WHEN si.item_currency = b.local_currency
                                   THEN round(si.item_local / f.rate, 4) ELSE si.item_usd_market END,
            cost_usd_market = CASE WHEN si.cost_local IS NOT NULL AND si.cost_usd_official <> si.cost_usd_market
                                   THEN round(si.cost_local / f.rate, 4) ELSE si.cost_usd_market END
        FROM _market_fix f, sales s, businesses b
        WHERE f.tbl = 'sales' AND f.id = si.sale_id AND s.id = si.sale_id AND b.id = s.business_id
    """)
    op.execute("""
        UPDATE purchase_items pi SET
            item_usd_market = CASE WHEN pi.unit_cost_currency = b.local_currency
                                   THEN round(pi.item_local / f.rate, 4) ELSE pi.item_usd_market END
        FROM _market_fix f, purchases p, businesses b
        WHERE f.tbl = 'purchases' AND f.id = pi.purchase_id AND p.id = pi.purchase_id AND b.id = p.business_id
    """)
    # Ventas con ítems en monedas mezcladas: su total en $ es la suma de los ítems.
    op.execute("""
        UPDATE sales s SET total_usd_market = sub.usd_market
        FROM (SELECT si.sale_id, sum(si.item_usd_market) AS usd_market FROM sale_items si GROUP BY si.sale_id) sub,
             _market_fix f, businesses b
        WHERE sub.sale_id = s.id AND f.tbl = 'sales' AND f.id = s.id AND b.id = s.business_id
          AND s.total_currency <> b.local_currency
    """)
    op.execute("DROP TABLE _market_fix")

    # 4) Ventas: lo cobrado estaba en la moneda del total; sus patas salen proporcionales (como las calculaba la vista).
    op.execute(f"""
        UPDATE sales SET
            paid_currency = total_currency,
            list_total_amount = total_amount,
            settlement = CASE WHEN payment_status = 'paid' THEN 'exact' ELSE 'debt' END,
            {", ".join(f"paid_{leg} = CASE WHEN total_amount > 0 THEN round(total_{leg} * paid_amount / total_amount, 4) ELSE 0 END"
                       for leg in SALE_PAID_LEGS)}
    """)

    # 5) Vista de resúmenes: los dólares pasan a ser los de la tasa para cobrar.
    op.execute("UPDATE businesses SET display_currency = 'usd_applied' WHERE display_currency IN ('usd_official', 'usd_market')")
    op.alter_column("businesses", "display_currency", server_default="usd_applied")

    # 6) Efectivo: un solo método; la moneda ya está en cada fila.
    for table in PAYMENT_TABLES:
        op.execute(f"UPDATE {table} SET payment_method = 'cash' WHERE payment_method IN ('cash_usd', 'cash_local')")
    cash_map = "CASE WHEN e IN ('cash_usd', 'cash_local') THEN 'cash' ELSE e END"
    op.execute(f"UPDATE businesses SET payment_methods = {_map_methods_json('payment_methods', cash_map)}")
    op.execute(f"""
        UPDATE businesses SET onboarding_draft = jsonb_set(onboarding_draft, '{{payment_methods}}',
            {_map_methods_json("onboarding_draft->'payment_methods'", cash_map)})
        WHERE jsonb_typeof(onboarding_draft->'payment_methods') = 'array'
    """)

    # ---------------------------------------------------------------- obligatorias y limpieza
    for table, prefix in MOVEMENTS.items():
        for column in ("rate_applied", "rate_applied_source", f"{prefix}_usd_applied"):
            op.alter_column(table, column, nullable=False)
        op.drop_column(table, "rate_market_source")
    op.alter_column("sale_items", "item_usd_applied", nullable=False)
    op.alter_column("purchase_items", "item_usd_applied", nullable=False)
    for column in ["list_total_amount", "paid_currency", *(f"paid_{leg}" for leg in SALE_PAID_LEGS)]:
        op.alter_column("sales", column, nullable=False)

    op.execute(VIEW)


def downgrade() -> None:
    """Vuelve al esquema 0007. Los `rate_market` reescritos por el backfill no se restauran (quedan las automáticas)."""
    op.execute("DROP VIEW IF EXISTS money_movements")

    for table in MOVEMENTS:
        op.add_column(table, sa.Column("rate_market_source", sa.String(32), nullable=True))
        op.execute(f"""
            UPDATE {table} SET rate_market_source = CASE
                WHEN rate_applied_source IN ('custom', 'inline') THEN rate_applied_source ELSE 'auto' END
        """)
        op.alter_column(table, "rate_market_source", nullable=False)

    # efectivo: se reconstruye desde la moneda de cada fila
    for table in PAYMENT_TABLES:
        currency = "paid_currency" if table == "sales" else "currency"
        op.execute(f"""
            UPDATE {table} x SET payment_method = CASE WHEN x.{currency} = 'USD' THEN 'cash_usd' ELSE 'cash_local' END
            WHERE x.payment_method = 'cash'
        """)
    op.execute("""
        UPDATE businesses SET payment_methods = (
            SELECT coalesce(jsonb_agg(m), '[]'::jsonb) FROM (
                SELECT jsonb_array_elements(CASE WHEN e = to_jsonb('cash'::text)
                    THEN '["cash_usd", "cash_local"]'::jsonb ELSE jsonb_build_array(e) END) AS m
                FROM jsonb_array_elements(payment_methods) e) s)
    """)
    op.execute("UPDATE businesses SET display_currency = CASE WHEN charge_rate_mode = 'official' THEN 'usd_official' "
               "ELSE 'usd_market' END WHERE display_currency = 'usd_applied'")
    op.alter_column("businesses", "display_currency", server_default="usd_official")

    for table in ("expenses", "purchases"):
        op.drop_column(table, "amount_usd_real")
        op.drop_column(table, "rate_funds")
    for column in ["settlement", "list_total_amount", "paid_currency", *(f"paid_{leg}" for leg in SALE_PAID_LEGS),
                   *(f"discount_{leg}" for leg in SALE_DISCOUNT_LEGS)]:
        op.drop_column("sales", column)
    op.drop_column("purchase_items", "item_usd_applied")
    op.drop_column("sale_items", "cost_usd_applied")
    op.drop_column("sale_items", "item_usd_applied")
    for table, prefix in MOVEMENTS.items():
        op.drop_column(table, f"{prefix}_usd_applied")
        op.drop_column(table, "rate_applied_source")
        op.drop_column(table, "rate_applied")

    for table, legs in ITEM_RENAMES.items():
        for leg in legs:
            op.alter_column(table, f"item_{leg}", new_column_name=f"line_{leg}")

    op.execute("DELETE FROM custom_rates WHERE purpose <> 'charge'")  # antes solo existían tasas para cobrar
    op.drop_index("ix_custom_rates_business_purpose_set", table_name="custom_rates")
    op.drop_column("custom_rates", "exchange_id")
    op.drop_column("custom_rates", "source")
    op.drop_column("custom_rates", "purpose")
    op.drop_index("ix_currency_exchanges_business_date", table_name="currency_exchanges")
    op.drop_table("currency_exchanges")
    op.drop_column("businesses", "charge_rate_confirmed_on")
    op.drop_column("businesses", "charge_rate_mode")

    op.execute(OLD_VIEW)
