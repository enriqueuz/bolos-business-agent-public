"""Compila una consulta de métricas a SQL parametrizado y la ejecuta.

Seguridad: el modelo solo elige NOMBRES (métricas, dimensiones, filtros, período) validados contra
`definitions.py`. Los valores de filtro viajan como parámetros enlazados. `business_id` lo inyecta el
llamador. Hay timeout de consulta y límite de filas.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.metrics.definitions import (
    METRICS,
    NET_ITEM,
    SOURCES,
    TIME_DIMS,
    WEEKDAY_NAMES,
    Dim,
    Filter,
    Metric,
    ValueKind,
    base_metrics,
    supported_dims,
    supported_filters,
)
from app.application.resolvers import resolve_customer
from app.domain.countries import CountryProfile
from app.domain.currencies import USD
from app.domain.dates import (
    CompareTo,
    DateRange,
    PeriodName,
    comparison_range,
    fmt_short_date,
    resolve_period,
)
from app.domain.enums import (
    EXPENSE_CATEGORY_LABELS,
    CurrencyView,
    ExpenseCategory,
    PaymentMethod,
)
from app.domain.errors import ValidationFailed
from app.domain.money import fmt_amount, fmt_qty, fmt_view, q
from app.domain.text import normalize_name

MAX_LIMIT = 50
STATEMENT_TIMEOUT_MS = 5000


class MetricsQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics: list[str] = Field(min_length=1, max_length=6)
    group_by: list[Dim] = Field(default_factory=list, max_length=2)
    filters: dict[Filter, str] = Field(default_factory=dict)
    period: PeriodName = PeriodName.THIS_MONTH
    date_from: date | None = None
    date_to: date | None = None
    compare_to: CompareTo | None = None
    order_by: str | None = None
    order_desc: bool = True
    limit: int = Field(default=10, ge=1, le=MAX_LIMIT)
    currency_view: CurrencyView | None = None


@dataclass
class _Result:
    rows: dict[tuple, dict[str, Decimal | None]] = field(default_factory=lambda: defaultdict(dict))
    coverage: dict[tuple, Decimal | None] = field(default_factory=dict)


def validate(query: MetricsQuery) -> list[Metric]:
    unknown = [m for m in query.metrics if m not in METRICS]
    if unknown:
        raise ValidationFailed(f"Métricas desconocidas: {', '.join(unknown)}", available=sorted(METRICS))
    metrics = [METRICS[m] for m in query.metrics]
    for m in metrics:
        bad_dims = set(query.group_by) - supported_dims(m)
        if bad_dims:
            raise ValidationFailed(
                f"La métrica {m.name} no se puede agrupar por {', '.join(sorted(d.value for d in bad_dims))}.",
                supported_group_by=sorted(d.value for d in supported_dims(m)),
                hint="Haz consultas separadas por métrica si necesitan agrupaciones distintas.",
            )
        bad_filters = set(query.filters) - supported_filters(m)
        if bad_filters:
            raise ValidationFailed(
                f"La métrica {m.name} no admite el filtro {', '.join(sorted(f.value for f in bad_filters))}.",
                supported_filters=sorted(f.value for f in supported_filters(m)),
            )
    if query.order_by and query.order_by not in query.metrics:
        raise ValidationFailed("order_by debe ser una de las métricas pedidas.")
    for f, value in query.filters.items():
        if f == Filter.PAYMENT_METHOD and value not in {p.value for p in PaymentMethod}:
            raise ValidationFailed("payment_method inválido.", allowed=[p.value for p in PaymentMethod])
        if f == Filter.EXPENSE_CATEGORY and value not in {c.value for c in ExpenseCategory}:
            raise ValidationFailed("expense_category inválida.", allowed=[c.value for c in ExpenseCategory])
    return metrics


async def _run_source(
    session: AsyncSession, business_id: UUID, source_name: str, metrics: list[Metric], query: MetricsQuery,
    rng: DateRange, leg: str, result: _Result,
) -> None:
    src = SOURCES[source_name]
    params: dict[str, Any] = {"business_id": business_id, "start": rng.start, "end": rng.end}
    where = [src.where_sql, _business_filter(src), f"{src.date_col} BETWEEN :start AND :end"]
    for i, (f, value) in enumerate(query.filters.items()):
        expr, mode = src.filters[f]
        pname = f"f{i}"
        if f == Filter.CUSTOMER:
            customer, _ = await resolve_customer(session, business_id, value, create_if_missing=False)
            params[pname] = customer.id
            where.append(f"c.id = :{pname}")
        elif mode == "name":
            params[pname] = f"%{normalize_name(value)}%"
            where.append(f"{expr} LIKE :{pname}")
        elif mode == "exists":
            params[pname] = f"%{normalize_name(value)}%"
            where.append(expr.format(param=pname))
        else:
            params[pname] = value
            where.append(f"{expr} = :{pname}")
    dim_exprs = [src.dims[d] for d in query.group_by]
    select_parts = [f"{e} AS d{i}" for i, e in enumerate(dim_exprs)]
    for m in metrics:
        select_parts.append(f"{m.expr.format(leg=leg)} AS m_{m.name}")  # type: ignore[union-attr]
        if m.coverage_expr:
            select_parts.append(f"{m.coverage_expr.format(leg=leg)} AS cov_{m.name}")
    sql = f"SELECT {', '.join(select_parts)} FROM {src.from_sql} WHERE {' AND '.join(where)}"
    if dim_exprs:
        sql += " GROUP BY " + ", ".join(str(i + 1) for i in range(len(dim_exprs)))
    sql += " LIMIT 2000"
    rows = (await session.execute(text(sql), params)).mappings().all()
    for row in rows:
        key = tuple(row[f"d{i}"] for i in range(len(dim_exprs)))
        for m in metrics:
            value = row[f"m_{m.name}"]
            result.rows[key][m.name] = Decimal(value) if value is not None else None
            if m.coverage_expr:
                cov = row[f"cov_{m.name}"]
                result.coverage[key] = Decimal(cov) if cov is not None else None


def _business_filter(src) -> str:
    # la tabla que tiene business_id: en fuentes de ítems es la cabecera (sales / purchases)
    alias = {"si": "s", "pi": "pu"}.get(src.from_sql.split()[1], src.from_sql.split()[1])
    return f"{alias}.business_id = :business_id"


async def _execute(session: AsyncSession, business_id: UUID, metrics: list[Metric], query: MetricsQuery, rng: DateRange,
                   leg: str) -> _Result:
    result = _Result()
    by_source: dict[str, dict[str, Metric]] = defaultdict(dict)
    for m in metrics:
        for base in base_metrics(m):
            by_source[base.source][base.name] = base  # type: ignore[index]
    for source_name, source_metrics in by_source.items():
        await _run_source(session, business_id, source_name, list(source_metrics.values()), query, rng, leg, result)
    # derivadas
    for _key, values in result.rows.items():
        for m in metrics:
            if m.name == "avg_ticket":
                total, count = values.get("sales_total"), values.get("sales_count")
                values["avg_ticket"] = (total / count) if total is not None and count else None
            elif m.name == "net_cash_flow":
                parts = [values.get(n) or Decimal(0) for n in m.derived_from]
                values["net_cash_flow"] = parts[0] + parts[1] - parts[2] - parts[3]
            elif m.name == "fx_loss_usd":
                parts = [values.get(n) for n in m.derived_from]
                values["fx_loss_usd"] = None if all(p is None for p in parts) else sum((p or Decimal(0)) for p in parts)
    return result


def _label(dim: Dim, value: Any, profile: CountryProfile) -> str:
    if value is None:
        return "(vacío)"
    if dim == Dim.WEEKDAY:
        return WEEKDAY_NAMES.get(int(value), str(value))
    if dim == Dim.DAY and isinstance(value, date):
        return fmt_short_date(value)
    if dim == Dim.WEEK and isinstance(value, date):
        return f"semana del {fmt_short_date(value)}"
    if dim == Dim.PAYMENT_METHOD:
        return "sin método" if value == "unspecified" else (profile.payment_label(value) or value)
    if dim == Dim.EXPENSE_CATEGORY:
        return EXPENSE_CATEGORY_LABELS.get(ExpenseCategory(value), value)
    if dim == Dim.PAYMENT_STATUS:
        return {"paid": "pagada", "credit": "fiada", "partial": "parcial"}.get(value, value)
    return str(value)


def _fmt(metric: Metric, value: Decimal | None, view: CurrencyView, local_currency: str) -> dict[str, Any]:
    if value is None:
        return {"value": None, "display": "sin datos"}
    if metric.kind == ValueKind.MONEY:
        return {"value": str(q(value)), "display": fmt_view(value, view, local_currency)}
    if metric.kind == ValueKind.MONEY_USD:
        return {"value": str(q(value)), "display": fmt_amount(value, USD)}
    if metric.kind == ValueKind.PERCENT:
        return {"value": str(value.quantize(Decimal("0.1"))), "display": f"{value.quantize(Decimal('0.1'))}%"}
    if metric.kind == ValueKind.COUNT:
        return {"value": int(value), "display": str(int(value))}
    return {"value": fmt_qty(value), "display": fmt_qty(value)}


def _pct_change(current: Decimal | None, previous: Decimal | None) -> str | None:
    if current is None or previous is None or previous == 0:
        return None
    return f"{((current - previous) / abs(previous) * 100).quantize(Decimal('0.1'))}%"


async def run_metrics_query(session: AsyncSession, business_id: UUID, query: MetricsQuery, today: date,
                            default_view: CurrencyView, profile: CountryProfile) -> dict[str, Any]:
    local_currency = profile.local_currency
    metrics = validate(query)
    view = query.currency_view or default_view
    leg = view.value
    rng = resolve_period(query.period, today, query.date_from, query.date_to)
    await session.execute(text(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}"))

    needs_covered_revenue = any(m.name == "margin_pct" for m in metrics) and not any(m.name == "gross_margin" for m in metrics)
    exec_metrics = metrics + ([METRICS["gross_margin"]] if needs_covered_revenue else [])

    async def compute(r: DateRange, grouped: bool) -> _Result:
        q_ = query if grouped else query.model_copy(update={"group_by": []})
        res = await _execute(session, business_id, exec_metrics, q_, r, leg)
        if any(m.name == "margin_pct" for m in metrics):
            await _add_covered_revenue(session, business_id, q_, r, leg, res)
        return res

    current = await compute(rng, grouped=True)
    totals = await compute(rng, grouped=False) if query.group_by else current

    rows = []
    for key, values in current.rows.items():
        row: dict[str, Any] = {d.value: _label(d, key[i], profile) for i, d in enumerate(query.group_by)}
        for m in metrics:
            row[m.name] = _fmt(m, values.get(m.name), view, local_currency)
        if any(m.coverage_expr for m in metrics) and current.coverage.get(key) is not None:
            row["cost_coverage"] = f"{(current.coverage[key] * 100).quantize(Decimal('1'))}%"
        row["_sort"] = values.get(query.order_by or metrics[0].name)
        row["_key"] = key
        rows.append(row)

    if query.group_by and all(d in TIME_DIMS for d in query.group_by) and not query.order_by:
        rows.sort(key=lambda r: tuple(str(k) if not isinstance(k, int) else f"{k:03d}" for k in r["_key"]))
    else:
        rows.sort(key=lambda r: (r["_sort"] is None, -(r["_sort"] or 0) if query.order_by is None or query.order_desc else (r["_sort"] or 0)))
    truncated = len(rows) > query.limit
    rows = rows[: query.limit]

    total_values = next(iter(totals.rows.values()), {})
    out: dict[str, Any] = {
        "period": rng.to_dict(),
        "currency_view": view.value,
        "view_title": profile.view_title(view),
        "totals": {m.name: _fmt(m, total_values.get(m.name), view, local_currency) for m in metrics},
        "rows": [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows] if query.group_by else [],
        "truncated": truncated,
    }
    if any(m.coverage_expr or m.name == "margin_pct" for m in metrics):
        cov = next(iter(totals.coverage.values()), None)
        out["cost_coverage"] = f"{(cov * 100).quantize(Decimal('1'))}% de lo vendido tiene costo conocido" if cov is not None else "sin costos registrados"
    if query.compare_to:
        prev_rng = comparison_range(rng, query.compare_to)
        previous = await compute(prev_rng, grouped=False)
        prev_values = next(iter(previous.rows.values()), {})
        out["comparison"] = {
            "period": prev_rng.to_dict(),
            "totals": {m.name: _fmt(m, prev_values.get(m.name), view, local_currency) for m in metrics},
            "change": {m.name: _pct_change(total_values.get(m.name), prev_values.get(m.name)) for m in metrics},
        }
    return out


async def _add_covered_revenue(session: AsyncSession, business_id: UUID, query: MetricsQuery, rng: DateRange, leg: str,
                               res: _Result) -> None:
    """Ingreso con costo conocido por fila, para margin_pct."""
    src = SOURCES["sale_items"]
    metric = Metric("_covered_revenue", "", ValueKind.MONEY, "sale_items",
                    f"sum(CASE WHEN si.cost_{{leg}} IS NOT NULL THEN {NET_ITEM} END)")
    tmp = _Result()
    await _run_source(session, business_id, src.name, [metric], query, rng, leg, tmp)
    for key, values in tmp.rows.items():
        res.rows[key]["_covered_revenue"] = values.get("_covered_revenue")
        margin, revenue = res.rows[key].get("gross_margin"), values.get("_covered_revenue")
        res.rows[key]["margin_pct"] = (margin / revenue * 100) if margin is not None and revenue else None
