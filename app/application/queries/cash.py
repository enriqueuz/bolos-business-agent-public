"""Resumen de caja de un día (lo que se congela al cerrar).

Orden de lo que importa: primero el dinero que REALMENTE existe, por moneda y método, sin sumar dólares con bolívares;
después los cambios de moneda del día; y al final un equivalente para reporte, en una sola moneda, calculado con la
tasa para cobrar del negocio y etiquetado con ella (esa tasa exacta queda guardada en el cierre).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.common import get_business, profile_of
from app.application.rates import load_snapshot
from app.db.models import CurrencyExchange, Receivable
from app.db.views import money_movements as mm
from app.domain.currencies import USD
from app.domain.dates import fmt_date
from app.domain.enums import CurrencyView
from app.domain.money import convert_between, fmt_amount, fmt_rate, fmt_view, q


def _rate_name(label: str) -> str:
    """'BCV' → 'BCV'; 'mercado' → 'la tasa de mercado'; 'tu tasa propia' queda igual."""
    return label if label.isupper() or label.startswith("tu ") else f"la tasa de {label}"


def leg_col(table, view: CurrencyView, prefix: str = "amount"):
    return table.c[f"{prefix}_{view.value}"]


async def cash_summary(session: AsyncSession, business_id: UUID, day: date, view: CurrencyView) -> dict[str, Any]:
    business = await get_business(session, business_id)
    profile = profile_of(business)
    local = business.local_currency
    base = (mm.c.business_id == business_id) & (mm.c.business_date == day)

    kinds = (
        await session.execute(
            select(
                mm.c.kind,
                func.count(),
                func.coalesce(func.sum(leg_col(mm, view)), 0),
                func.coalesce(func.sum(leg_col(mm, view, "cashflow")), 0),
            ).where(base).group_by(mm.c.kind)
        )
    ).all()
    by_kind = {k: {"count": n, "total": q(Decimal(t)), "cash": q(Decimal(c))} for k, n, t, c in kinds}

    # Dinero real por método y moneda real (sin convertir): cashflow_amount ya está en cashflow_currency.
    # Los cambios de moneda van aparte (salió/entró); sí cuentan en el total real por moneda.
    methods = (
        await session.execute(
            select(
                func.coalesce(mm.c.payment_method, "unspecified"),
                mm.c.cashflow_currency,
                mm.c.kind,
                func.coalesce(func.sum(mm.c.cashflow_amount * mm.c.direction), 0),
            )
            .where(base, mm.c.direction != 0)
            .group_by(mm.c.payment_method, mm.c.cashflow_currency, mm.c.kind)
        )
    ).all()
    method_net: dict[tuple[str, str], Decimal] = {}
    by_currency: dict[str, Decimal] = {}
    for method, currency, kind, net in methods:
        net = q(Decimal(net))
        by_currency[currency] = by_currency.get(currency, Decimal(0)) + net
        if kind != "exchange":
            method_net[(method, currency)] = method_net.get((method, currency), Decimal(0)) + net
    by_method: dict[str, dict[str, str]] = {}
    for (method, currency), net in method_net.items():
        if abs(net) < Decimal("0.005"):
            continue
        label = profile.payment_label(method) if method != "unspecified" else "Sin método indicado"
        by_method.setdefault(label, {})[currency] = fmt_amount(net, currency)

    exchanges = []
    for x in (await session.execute(
        select(CurrencyExchange).where(CurrencyExchange.business_id == business_id, CurrencyExchange.business_date == day,
                                       CurrencyExchange.deleted_at.is_(None)).order_by(CurrencyExchange.occurred_at)
    )).scalars():
        via = profile.payment_label(x.out_method) if x.out_method and x.out_method == x.in_method else None
        exchanges.append({"text": (f"{via + ': ' if via else ''}salieron {fmt_amount(x.out_amount, x.out_currency)} y "
                                   f"entraron {fmt_amount(x.in_amount, x.in_currency)}"),
                          "rate": fmt_rate(x.rate, local)})

    # Equivalente para reporte: el dinero real de cada moneda llevado a una sola, con la tasa para cobrar de hoy.
    rates = (await load_snapshot(session, business_id)).effective()
    target = local if view == CurrencyView.LOCAL else USD
    equivalent = sum((convert_between(v, c, target, rates.applied) for c, v in by_currency.items() if rates.applied > 0),
                     Decimal(0))
    using = (f"{_rate_name(profile.applied_rate_label(rates.applied_source))} de {fmt_rate(rates.applied, local)} "
             f"{profile.local.symbol}/$")
    report = {"equivalent": fmt_amount(equivalent, target), "equivalent_raw": str(q(equivalent)), "currency": target,
              "mode": rates.applied_source.value, "rate": fmt_rate(rates.applied, local), "rate_raw": str(rates.applied),
              "using": using, "label": f"Equivalente para reporte usando {using}"}

    new_credit = (
        await session.execute(
            select(func.coalesce(func.sum(getattr(Receivable, f"amount_{view.value}")), 0)).where(
                Receivable.business_id == business_id, Receivable.business_date == day, Receivable.deleted_at.is_(None)
            )
        )
    ).scalar_one()

    def k(kind: str, field: str) -> Decimal:
        return by_kind.get(kind, {}).get(field, Decimal(0))

    inflow = k("sale", "cash") + k("payment", "cash")
    outflow = k("expense", "cash") + k("purchase", "cash")
    return {
        "business_date": day.isoformat(),
        "date_label": fmt_date(day),
        "currency_view": view.value,
        "view_title": profile.view_title(view),
        "sales": {"count": by_kind.get("sale", {}).get("count", 0), "total": fmt_view(k("sale", "total"), view, local),
                  "collected": fmt_view(k("sale", "cash"), view, local)},
        "payments_in": {"count": by_kind.get("payment", {}).get("count", 0), "total": fmt_view(k("payment", "total"), view, local)},
        "expenses": {"count": by_kind.get("expense", {}).get("count", 0), "total": fmt_view(k("expense", "total"), view, local)},
        "purchases": {"count": by_kind.get("purchase", {}).get("count", 0), "total": fmt_view(k("purchase", "total"), view, local)},
        "new_credit": fmt_view(q(Decimal(new_credit)), view, local),
        "cash_in": fmt_view(inflow, view, local),
        "cash_out": fmt_view(outflow, view, local),
        "net_cash": fmt_view(inflow - outflow, view, local),
        "by_payment_method_net": by_method,
        "by_currency_net": {c: fmt_amount(v, c) for c, v in sorted(by_currency.items()) if abs(v) >= Decimal("0.005")},
        "exchanges": exchanges,
        "report": report,
        "raw": {"cash_in": str(inflow), "cash_out": str(outflow), "net_cash": str(inflow - outflow),
                "sales_total": str(k("sale", "total")), "new_credit": str(q(Decimal(new_credit)))},
    }
