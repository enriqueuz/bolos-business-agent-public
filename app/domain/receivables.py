"""Reglas puras de fiado: asignación FIFO de abonos."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.domain.enums import Currency
from app.domain.money import convert_between, q

EPS = Decimal("0.005")


@dataclass(frozen=True, slots=True)
class OpenReceivable:
    id: UUID
    business_date: date
    currency: Currency
    remaining: Decimal  # en la moneda del fiado


@dataclass(frozen=True, slots=True)
class Allocation:
    receivable_id: UUID
    applied_amount: Decimal  # en la moneda del fiado
    applied_in_payment_currency: Decimal


@dataclass(frozen=True, slots=True)
class AllocationPlan:
    allocations: list[Allocation]
    unapplied_in_payment_currency: Decimal  # sobrante (abono mayor a la deuda)


def allocate_fifo(
    payment_amount: Decimal,
    payment_currency: Currency,
    rate: Decimal | None,
    open_items: list[OpenReceivable],
) -> AllocationPlan:
    """Aplica un abono a los fiados abiertos del más viejo al más nuevo.

    Si el fiado está en otra moneda que el abono, se convierte con la tasa aplicada del abono.
    """
    remaining_payment = q(payment_amount)
    allocations: list[Allocation] = []
    # sort estable: a igual fecha respeta el orden de llegada (created_at) que trae la consulta
    for item in sorted(open_items, key=lambda r: r.business_date):
        if remaining_payment <= EPS:
            break
        if item.remaining <= EPS:
            continue
        available_in_item_ccy = remaining_payment if item.currency == payment_currency else convert_between(remaining_payment, payment_currency, item.currency, rate)
        applied = min(available_in_item_ccy, item.remaining)
        applied_in_payment_ccy = (
            applied if item.currency == payment_currency else convert_between(applied, item.currency, payment_currency, rate)
        )
        applied_in_payment_ccy = min(applied_in_payment_ccy, remaining_payment)
        allocations.append(Allocation(item.id, q(applied), q(applied_in_payment_ccy)))
        remaining_payment = q(remaining_payment - applied_in_payment_ccy)
    return AllocationPlan(allocations, remaining_payment if remaining_payment > EPS else Decimal("0"))
