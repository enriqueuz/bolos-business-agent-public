"""Capa semántica: la ÚNICA definición de qué es "ventas", "margen", "ticket promedio", etc.

Ninguna query, tool ni prompt define métricas por su cuenta. Agregar una métrica = agregar una entrada aquí;
su descripción aparece sola en el prompt del agente (ver agent/prompts).

Todos los fragmentos SQL son constantes de este archivo (nunca texto del usuario ni del modelo). `{leg}` se
sustituye por la pata elegida: local | usd_applied | usd_official | usd_market.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Dim(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    WEEKDAY = "weekday"
    PRODUCT = "product"
    CUSTOMER = "customer"
    PAYMENT_METHOD = "payment_method"
    PAYMENT_STATUS = "payment_status"
    EXPENSE_CATEGORY = "expense_category"
    SUPPLIER = "supplier"


TIME_DIMS = {Dim.DAY, Dim.WEEK, Dim.MONTH, Dim.WEEKDAY}


class Filter(StrEnum):
    CUSTOMER = "customer"
    PRODUCT = "product"
    PAYMENT_METHOD = "payment_method"
    PAYMENT_STATUS = "payment_status"
    EXPENSE_CATEGORY = "expense_category"
    SUPPLIER = "supplier"


class ValueKind(StrEnum):
    MONEY = "money"  # en la moneda de la vista del reporte
    MONEY_USD = "money_usd"  # siempre en dólares (costo real, diferencia cambiaria)
    COUNT = "count"
    QUANTITY = "quantity"
    PERCENT = "percent"


def _time_dims(date_col: str) -> dict[Dim, str]:
    return {
        Dim.DAY: f"{date_col}",
        Dim.WEEK: f"date_trunc('week', {date_col})::date",
        Dim.MONTH: f"to_char({date_col}, 'YYYY-MM')",
        Dim.WEEKDAY: f"extract(isodow from {date_col})::int",
    }


@dataclass(frozen=True)
class Source:
    name: str
    from_sql: str
    where_sql: str
    date_col: str
    dims: dict[Dim, str]
    # filtro → (expresión SQL, tipo): "name" compara normalizado con LIKE, "exact" compara igual
    filters: dict[Filter, tuple[str, str]] = field(default_factory=dict)


SOURCES: dict[str, Source] = {
    "sales": Source(
        name="sales",
        from_sql="sales s LEFT JOIN customers c ON c.id = s.customer_id",
        where_sql="s.deleted_at IS NULL",
        date_col="s.business_date",
        dims={**_time_dims("s.business_date"), Dim.CUSTOMER: "coalesce(c.name, c.nickname, '(sin cliente)')",
              Dim.PAYMENT_METHOD: "coalesce(s.payment_method, 'unspecified')", Dim.PAYMENT_STATUS: "s.payment_status"},
        filters={Filter.CUSTOMER: ("c.normalized_name", "name"), Filter.PAYMENT_METHOD: ("s.payment_method", "exact"),
                 Filter.PAYMENT_STATUS: ("s.payment_status", "exact"),
                 Filter.PRODUCT: ("EXISTS (SELECT 1 FROM sale_items fi JOIN products fp ON fp.id = fi.product_id "
                                  "WHERE fi.sale_id = s.id AND fp.normalized_name LIKE :{param})", "exists")},
    ),
    "sale_items": Source(
        name="sale_items",
        from_sql=("sale_items si JOIN sales s ON s.id = si.sale_id LEFT JOIN products p ON p.id = si.product_id "
                  "LEFT JOIN customers c ON c.id = s.customer_id"),
        where_sql="s.deleted_at IS NULL",
        date_col="s.business_date",
        dims={**_time_dims("s.business_date"), Dim.PRODUCT: "coalesce(p.name, si.description)",
              Dim.CUSTOMER: "coalesce(c.name, c.nickname, '(sin cliente)')", Dim.PAYMENT_METHOD: "coalesce(s.payment_method, 'unspecified')"},
        filters={Filter.CUSTOMER: ("c.normalized_name", "name"), Filter.PRODUCT: ("p.normalized_name", "name"),
                 Filter.PAYMENT_METHOD: ("s.payment_method", "exact")},
    ),
    "expenses": Source(
        name="expenses",
        from_sql="expenses e",
        where_sql="e.deleted_at IS NULL",
        date_col="e.business_date",
        dims={**_time_dims("e.business_date"), Dim.EXPENSE_CATEGORY: "e.category",
              Dim.PAYMENT_METHOD: "coalesce(e.payment_method, 'unspecified')"},
        filters={Filter.EXPENSE_CATEGORY: ("e.category", "exact"), Filter.PAYMENT_METHOD: ("e.payment_method", "exact")},
    ),
    "purchases": Source(
        name="purchases",
        from_sql="purchases pu",
        where_sql="pu.deleted_at IS NULL",
        date_col="pu.business_date",
        dims={**_time_dims("pu.business_date"), Dim.SUPPLIER: "coalesce(pu.supplier_name, '(sin proveedor)')",
              Dim.PAYMENT_METHOD: "coalesce(pu.payment_method, 'unspecified')"},
        filters={Filter.SUPPLIER: ("lower(pu.supplier_name)", "name"), Filter.PAYMENT_METHOD: ("pu.payment_method", "exact")},
    ),
    "purchase_items": Source(
        name="purchase_items",
        from_sql="purchase_items pi JOIN purchases pu ON pu.id = pi.purchase_id LEFT JOIN products p ON p.id = pi.product_id",
        where_sql="pu.deleted_at IS NULL",
        date_col="pu.business_date",
        dims={**_time_dims("pu.business_date"), Dim.PRODUCT: "coalesce(p.name, pi.description)",
              Dim.SUPPLIER: "coalesce(pu.supplier_name, '(sin proveedor)')"},
        filters={Filter.PRODUCT: ("p.normalized_name", "name"), Filter.SUPPLIER: ("lower(pu.supplier_name)", "name")},
    ),
    "payments": Source(
        name="payments",
        from_sql="customer_payments cp JOIN customers c ON c.id = cp.customer_id",
        where_sql="cp.deleted_at IS NULL",
        date_col="cp.business_date",
        dims={**_time_dims("cp.business_date"), Dim.CUSTOMER: "COALESCE(c.name, c.nickname)", Dim.PAYMENT_METHOD: "coalesce(cp.payment_method, 'unspecified')"},
        filters={Filter.CUSTOMER: ("c.normalized_name", "name"), Filter.PAYMENT_METHOD: ("cp.payment_method", "exact")},
    ),
    "receivables": Source(
        name="receivables",
        from_sql="receivables r JOIN customers c ON c.id = r.customer_id",
        where_sql="r.deleted_at IS NULL",
        date_col="r.business_date",
        dims={**_time_dims("r.business_date"), Dim.CUSTOMER: "COALESCE(c.name, c.nickname)"},
        filters={Filter.CUSTOMER: ("c.normalized_name", "name")},
    ),
}


@dataclass(frozen=True)
class Metric:
    name: str
    description: str
    kind: ValueKind
    source: str | None = None
    expr: str | None = None  # agregación SQL (puede usar {leg})
    # Métricas derivadas: fórmula sobre otras métricas (se calcula en Python con los resultados)
    derived_from: tuple[str, ...] = ()
    coverage_expr: str | None = None  # para margen: fracción del ingreso con costo conocido


# Ingreso neto de un ítem: su parte de la venta neta (el descuento de la venta se reparte en proporción a cada ítem).
NET_ITEM = "si.item_{leg} * s.total_amount / nullif(s.list_total_amount, 0)"
# Diferencia cambiaria de una salida en moneda local con tasa real conocida: costo real − referencia oficial.
FX_DIFF = "CASE WHEN {t}.currency <> 'USD' AND {t}.amount_usd_real IS NOT NULL THEN {t}.amount_usd_real - {t}.amount_usd_official END"

METRICS: dict[str, Metric] = {m.name: m for m in [
    Metric("sales_total", "Ventas netas: total vendido después de descuentos (cobrado o fiado)", ValueKind.MONEY, "sales",
           "sum(s.total_{leg})"),
    Metric("gross_sales", "Ventas antes de descuentos (a precio de lista)", ValueKind.MONEY, "sales",
           "sum(s.total_{leg} + s.discount_{leg})"),
    Metric("discounts_total", "Descuentos otorgados en ventas", ValueKind.MONEY, "sales", "sum(s.discount_{leg})"),
    Metric("sales_count", "Número de ventas", ValueKind.COUNT, "sales", "count(*)"),
    Metric("collected_total", "Dinero realmente cobrado al vender, en cualquier moneda (excluye lo fiado)", ValueKind.MONEY, "sales",
           "sum(s.paid_{leg})"),
    Metric("customers_count", "Clientes distintos que compraron (solo ventas con cliente)", ValueKind.COUNT, "sales",
           "count(DISTINCT s.customer_id)"),
    Metric("avg_ticket", "Ticket promedio por venta", ValueKind.MONEY, derived_from=("sales_total", "sales_count")),
    Metric("item_revenue", "Ingreso neto por ítem vendido (descuentos repartidos; para agrupar por producto)", ValueKind.MONEY,
           "sale_items", f"sum({NET_ITEM})"),
    Metric("units_sold", "Unidades vendidas", ValueKind.QUANTITY, "sale_items", "sum(si.quantity)"),
    Metric("gross_margin", "Ganancia bruta (precio de venta − costo congelado). Solo ítems con costo conocido", ValueKind.MONEY,
           "sale_items", f"sum(CASE WHEN si.cost_{{leg}} IS NOT NULL THEN {NET_ITEM} - si.cost_{{leg}} END)",
           coverage_expr="sum(CASE WHEN si.cost_{leg} IS NOT NULL THEN si.item_{leg} ELSE 0 END) / nullif(sum(si.item_{leg}), 0)"),
    Metric("margin_pct", "Margen bruto % sobre lo vendido con costo conocido", ValueKind.PERCENT, derived_from=("gross_margin",)),
    Metric("expenses_total", "Total de gastos operativos", ValueKind.MONEY, "expenses", "sum(e.amount_{leg})"),
    Metric("expenses_count", "Número de gastos", ValueKind.COUNT, "expenses", "count(*)"),
    Metric("purchases_total", "Total de compras de mercancía", ValueKind.MONEY, "purchases", "sum(pu.amount_{leg})"),
    Metric("units_purchased", "Unidades compradas (solo compras con ítems)", ValueKind.QUANTITY, "purchase_items", "sum(pi.quantity)"),
    Metric("payments_in_total", "Abonos recibidos de clientes a sus fiados", ValueKind.MONEY, "payments", "sum(cp.amount_{leg})"),
    Metric("credit_issued", "Fiado otorgado (nuevas deudas de clientes)", ValueKind.MONEY, "receivables", "sum(r.amount_{leg})"),
    Metric("net_cash_flow", "Flujo de caja: cobrado + abonos − gastos − compras", ValueKind.MONEY,
           derived_from=("collected_total", "payments_in_total", "expenses_total", "purchases_total")),
    Metric("expenses_real_usd", "Costo real en $ de los gastos (los pagados en Bs, a la tasa real de tus bolívares; "
           "sin tasa real conocida no cuentan)", ValueKind.MONEY_USD, "expenses", "sum(e.amount_usd_real)"),
    Metric("purchases_real_usd", "Costo real en $ de las compras (igual que expenses_real_usd)", ValueKind.MONEY_USD,
           "purchases", "sum(pu.amount_usd_real)"),
    Metric("fx_loss_expenses_usd", "Diferencia cambiaria de gastos pagados en Bs", ValueKind.MONEY_USD, "expenses",
           f"sum({FX_DIFF.format(t='e')})"),
    Metric("fx_loss_purchases_usd", "Diferencia cambiaria de compras pagadas en Bs", ValueKind.MONEY_USD, "purchases",
           f"sum({FX_DIFF.format(t='pu')})"),
    Metric("fx_loss_usd", "Diferencia cambiaria total en $: cuánto más te costaron los pagos en Bs (a la tasa real de tus "
           "bolívares) que a la tasa oficial. Positivo = perdiste; negativo = ganaste", ValueKind.MONEY_USD,
           derived_from=("fx_loss_expenses_usd", "fx_loss_purchases_usd")),
]}

WEEKDAY_NAMES = {1: "lunes", 2: "martes", 3: "miércoles", 4: "jueves", 5: "viernes", 6: "sábado", 7: "domingo"}


def base_metrics(metric: Metric) -> list[Metric]:
    if metric.derived_from:
        out: list[Metric] = []
        for name in metric.derived_from:
            out.extend(base_metrics(METRICS[name]))
        return out
    return [metric]


def supported_dims(metric: Metric) -> set[Dim]:
    sets = [set(SOURCES[m.source].dims) for m in base_metrics(metric)]  # type: ignore[index]
    return set.intersection(*sets) if sets else set()


def supported_filters(metric: Metric) -> set[Filter]:
    sets = [set(SOURCES[m.source].filters) for m in base_metrics(metric)]  # type: ignore[index]
    return set.intersection(*sets) if sets else set()


def catalog_text() -> str:
    """Descripción compacta para el prompt del agente."""
    lines = []
    for m in METRICS.values():
        dims = ", ".join(sorted(d.value for d in supported_dims(m)))
        lines.append(f"- {m.name}: {m.description}. Agrupable por: {dims}")
    return "\n".join(lines)
