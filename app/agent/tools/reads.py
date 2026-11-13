"""Tools de lectura. Resultados chicos, agregados en SQL, sin ids (los registros salen como handles R1, R2…)."""

from datetime import date
from typing import Any

from pydantic_ai import RunContext

from app.agent.deps import TurnDeps
from app.agent.receipts import render_cash_summary
from app.agent.tools.registry import execute_query, tool
from app.application.metrics.compiler import MetricsQuery
from app.application.metrics.definitions import Dim, Filter
from app.application.queries.service import QueryResult
from app.domain.dates import CompareTo, PeriodName
from app.domain.enums import CurrencyView, EntityType


@tool("Consultar las tasas: la que el negocio usa para cobrar, la tasa real de sus bolívares y las de referencia "
      "(oficial y mercado), con su antigüedad", kind="read")
async def get_rates(ctx: RunContext[TurnDeps]) -> dict[str, Any]:
    """Devuelve las tasas vigentes: `applied` (tasa para cobrar), `funds` (tasa real de los bolívares, si la hay),
    `official` y `market_auto` (referencias). Úsala antes de hablar de tasas o conversiones."""
    async def run() -> QueryResult:
        return QueryResult(await ctx.deps.queries.rates_info(ctx.deps.business_id, ctx.deps.now), [])
    return await execute_query(ctx, "get_rates", {}, run)


@tool("Consultar el precio (y costo) de un producto del catálogo", kind="read")
async def get_product_price(ctx: RunContext[TurnDeps], product: str) -> dict[str, Any]:
    """Busca productos parecidos en el catálogo y devuelve su precio y costo.

    Args:
        product: Nombre o parte del nombre del producto.
    """
    return await execute_query(ctx, "get_product_price", {"product": product},
                               lambda: ctx.deps.queries.product_prices(ctx.deps.business_id, product))


@tool("Consultar cuánto debe un cliente y el detalle de sus fiados abiertos", kind="read")
async def get_customer_balance(ctx: RunContext[TurnDeps], customer_name: str) -> dict[str, Any]:
    """Saldo de un cliente, sus fiados abiertos (con referencias R… para corregirlos) y su último abono.

    Args:
        customer_name: Cliente.
    """
    return await execute_query(ctx, "get_customer_balance", {"customer_name": customer_name},
                               lambda: ctx.deps.queries.customer_balance(ctx.deps.business_id, customer_name))


@tool("Listar los clientes que deben (cuentas por cobrar), de mayor a menor", kind="read")
async def list_receivables(ctx: RunContext[TurnDeps], limit: int = 10) -> dict[str, Any]:
    """Quiénes deben, cuánto y desde cuándo, más el total por cobrar.

    Args:
        limit: Máximo de clientes a listar (1-30).
    """
    return await execute_query(ctx, "list_receivables", {"limit": limit},
                               lambda: ctx.deps.queries.list_receivables(ctx.deps.business_id, max(1, min(limit, 30))))


@tool("Buscar registros (ventas, gastos, compras, fiados, abonos) para verlos o corregirlos", kind="read")
async def find_records(
    ctx: RunContext[TurnDeps],
    kind: EntityType | None = None,
    customer_name: str | None = None,
    product: str | None = None,
    text: str | None = None,
    period: PeriodName | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    amount: float | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Busca registros concretos y devuelve referencias (R1, R2…) que puedes usar en edit_record/delete_record.
    Sin período busca en los últimos 30 días.

    Args:
        kind: sale, expense, purchase, receivable, payment o exchange (cambio de moneda).
        customer_name: Cliente.
        product: Producto vendido.
        text: Texto en la descripción (gastos/compras).
        period: today, yesterday, this_week, last_week, this_month, last_month, last_7_days, last_30_days, this_year o custom.
        date_from: Inicio si period=custom (YYYY-MM-DD).
        date_to: Fin si period=custom (YYYY-MM-DD).
        amount: Monto aproximado del registro.
        limit: Máximo de resultados (1-15).
    """
    from decimal import Decimal

    args = {k: v for k, v in dict(kind=kind, customer_name=customer_name, product=product, text=text, period=period,
                                  date_from=date_from, date_to=date_to, amount=amount, limit=limit).items() if v is not None}
    if kind is not None and kind not in {EntityType.SALE, EntityType.EXPENSE, EntityType.PURCHASE, EntityType.RECEIVABLE, EntityType.PAYMENT,
                                         EntityType.EXCHANGE}:
        kind = None
    return await execute_query(ctx, "find_records", args, lambda: ctx.deps.queries.find_records(
        ctx.deps.business_id, kind=kind, customer_name=customer_name, product=product, text_query=text, period=period,
        date_from=date_from, date_to=date_to, amount=Decimal(str(amount)) if amount else None, limit=limit))


@tool("Ver cómo va la caja de un día sin cerrarla", kind="read")
async def get_cash_status(ctx: RunContext[TurnDeps], business_date: date | None = None) -> dict[str, Any]:
    """Resumen de caja en vivo (y si ya se cerró).

    Args:
        business_date: Día (YYYY-MM-DD); por defecto hoy.
    """
    out = await execute_query(ctx, "get_cash_status", {"business_date": business_date},
                              lambda: ctx.deps.queries.cash_status(ctx.deps.business_id, business_date, ctx.deps.now))
    # Vista candidata: una consulta aislada usa el renderer; al combinar lecturas responde el agente.
    # Ver la caja nunca ejecuta close_cash ni produce un recibo de cierre.
    if out.get("ok") and out.get("live_summary"):
        ctx.deps.renders.append(render_cash_summary(out["live_summary"], closed=bool(out.get("closed"))))
    return out


@tool("Responder cualquier pregunta analítica del negocio: ventas, ganancias, márgenes, productos top, gastos, "
      "comparaciones entre períodos, días de la semana, métodos de pago, clientes", kind="read")
async def query_metrics(
    ctx: RunContext[TurnDeps],
    metrics: list[str],
    period: PeriodName = PeriodName.THIS_MONTH,
    group_by: list[Dim] | None = None,
    filters: dict[Filter, str] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    compare_to: CompareTo | None = None,
    order_by: str | None = None,
    order_desc: bool = True,
    limit: int = 10,
    currency_view: CurrencyView | None = None,
) -> dict[str, Any]:
    """Consulta métricas del negocio con la capa semántica (ver catálogo de métricas en las instrucciones).
    Puedes pedir varias métricas compatibles, agrupar hasta por 2 dimensiones, filtrar y comparar con el período anterior.
    Si una combinación no es válida, el resultado dice qué sí se puede.

    Args:
        metrics: Nombres de métricas del catálogo, ej. ["sales_total", "sales_count"].
        period: today, yesterday, this_week, last_week, this_month, last_month, last_7_days, last_30_days, this_year, custom.
        group_by: Dimensiones: day, week, month, weekday, product, customer, payment_method, payment_status, expense_category, supplier.
        filters: Filtros por nombre: customer, product, payment_method, payment_status, expense_category, supplier.
        date_from: Inicio si period=custom (YYYY-MM-DD).
        date_to: Fin si period=custom (YYYY-MM-DD).
        compare_to: previous_period o same_period_last_year.
        order_by: Métrica por la cual ordenar las filas.
        order_desc: Orden descendente (true) o ascendente.
        limit: Máximo de filas (1-50).
        currency_view: local, usd_applied (a la tasa para cobrar), usd_official o usd_market (referencias); por defecto la preferida del negocio.
    """
    args = {k: v for k, v in dict(metrics=metrics, period=period, group_by=group_by, filters=filters, date_from=date_from,
                                  date_to=date_to, compare_to=compare_to, order_by=order_by, limit=limit,
                                  currency_view=currency_view).items() if v is not None}

    async def run() -> QueryResult:
        query = MetricsQuery(metrics=metrics, period=period, group_by=group_by or [], filters=filters or {}, date_from=date_from,
                             date_to=date_to, compare_to=compare_to, order_by=order_by, order_desc=order_desc,
                             limit=max(1, min(limit, 50)), currency_view=currency_view)
        return await ctx.deps.queries.metrics(ctx.deps.business_id, query, ctx.deps.now)

    return await execute_query(ctx, "query_metrics", args, run)


@tool("Buscar en la memoria del negocio lo que el dueño ha contado (notas sobre clientes, proveedores, reglas)", kind="read")
async def search_notes(ctx: RunContext[TurnDeps], query: str | None = None, subject_name: str | None = None) -> dict[str, Any]:
    """Busca notas guardadas.

    Args:
        query: Palabras a buscar.
        subject_name: Cliente, proveedor o producto.
    """
    return await execute_query(ctx, "search_notes", {"query": query, "subject_name": subject_name},
                               lambda: ctx.deps.queries.search_notes(ctx.deps.business_id, query, subject_name))


@tool("Buscar en mensajes anteriores del dueño (\"¿qué te dije del proveedor?\")", kind="read")
async def search_messages(ctx: RunContext[TurnDeps], query: str, period: PeriodName | None = None) -> dict[str, Any]:
    """Búsqueda de texto en los mensajes que el dueño ha enviado.

    Args:
        query: Palabras a buscar.
        period: Período; por defecto últimos 30 días.
    """
    return await execute_query(ctx, "search_messages", {"query": query, "period": period},
                               lambda: ctx.deps.queries.search_messages(ctx.deps.business_id, query, period))
