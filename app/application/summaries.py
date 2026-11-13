"""Resúmenes cortos y legibles de cada entidad (para handles, búsquedas y recibos)."""

from __future__ import annotations

from decimal import Decimal

from app.db.models import CurrencyExchange, CustomerPayment, Expense, Purchase, Receivable, Sale
from app.domain.countries import display_profile
from app.domain.currencies import USD
from app.domain.dates import fmt_short_date
from app.domain.enums import (
    EXPENSE_CATEGORY_LABELS,
    PAYMENT_METHOD_LABELS,
    ExpenseCategory,
    PaymentMethod,
    RateAppliedSource,
)
from app.domain.money import fmt_amount


def _qty(value: Decimal) -> str:
    from app.domain.money import fmt_qty

    return fmt_qty(value)


def method_label(method: str | None) -> str | None:
    if not method:
        return None
    profile = display_profile()
    return profile.payment_label(method) if profile else PAYMENT_METHOD_LABELS.get(PaymentMethod(method), method)


def sale_items_text(sale: Sale) -> str:
    # La búsqueda de movimientos alimenta correcciones y alertas de precio. Incluir
    # el precio unitario evita mensajes vagos como «hay un par de salidas raras»:
    # cada handle identifica producto, cantidad y precio realmente aplicado.
    return ", ".join(
        f"{_qty(i.quantity)}x {i.description} a {fmt_amount(i.unit_price_amount, i.unit_price_currency)}"
        for i in sale.items
    ) or "venta"


def sale_summary(sale: Sale) -> str:
    parts = [f"venta {fmt_short_date(sale.business_date)}", sale_items_text(sale), fmt_amount(sale.total_amount, sale.total_currency)]
    if sale.customer is not None:
        parts.append(f"cliente {sale.customer.display_name}")
    status = {"paid": "pagada", "credit": "fiada", "partial": "quedó debiendo"}[sale.payment_status]
    parts.append(status)
    if sale.discount_amount:
        parts.append(f"descuento {fmt_amount(sale.discount_amount, sale.total_currency)}")
    if sale.paid_currency and sale.paid_currency != sale.total_currency and sale.paid_amount:
        parts.append(f"cobró {fmt_amount(sale.paid_amount, sale.paid_currency)}")
    if m := method_label(sale.payment_method):
        parts.append(m)
    if USD != sale.total_currency or (sale.paid_currency and sale.paid_currency != USD):  # la tasa importó
        parts.append(f"tasa {_rate(sale.rate_applied_source)} {_qty(sale.rate_applied)}")
    return " | ".join(parts)


def _rate(source: str) -> str:
    profile = display_profile()
    if profile is None:
        return source
    return profile.applied_rate_label(RateAppliedSource(source))


def expense_summary(e: Expense) -> str:
    cat = EXPENSE_CATEGORY_LABELS.get(ExpenseCategory(e.category), e.category)
    real = f" | costo real {fmt_amount(e.amount_usd_real, USD)}" if e.amount_usd_real is not None and e.currency != USD else ""
    return f"gasto {fmt_short_date(e.business_date)} | {e.description} | {fmt_amount(e.amount, e.currency)} | {cat}{real}"


def purchase_summary(p: Purchase) -> str:
    sup = f" | proveedor {p.supplier_name}" if p.supplier_name else ""
    return f"compra {fmt_short_date(p.business_date)} | {p.description} | {fmt_amount(p.amount, p.currency)}{sup}"


def receivable_summary(r: Receivable) -> str:
    desc = f" | {r.description}" if r.description else ""
    origin = " | de una venta" if r.sale_id else ""
    return f"fiado {fmt_short_date(r.business_date)} | {r.customer.display_name} | {fmt_amount(r.amount, r.currency)}{desc}{origin}"


def payment_summary(p: CustomerPayment) -> str:
    m = f" | {method_label(p.payment_method)}" if p.payment_method else ""
    return f"abono {fmt_short_date(p.business_date)} | {p.customer.display_name} | {fmt_amount(p.amount, p.currency)}{m}"


def exchange_summary(x: CurrencyExchange) -> str:
    out_m = f" ({method_label(x.out_method)})" if x.out_method else ""
    in_m = f" ({method_label(x.in_method)})" if x.in_method else ""
    return (f"cambio {fmt_short_date(x.business_date)} | salieron {fmt_amount(x.out_amount, x.out_currency)}{out_m} | "
            f"entraron {fmt_amount(x.in_amount, x.in_currency)}{in_m} | tasa {_qty(x.rate)}")
