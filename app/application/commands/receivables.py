"""Fiado: crear cuentas por cobrar y registrar abonos (asignación FIFO)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar
from uuid import UUID

from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.common import (
    assume_currency,
    customer_balance,
    get_business,
    lock_customers,
    receivable_paid,
    require_fresh_charge_rate,
)
from app.application.context import EntityRef, ExecContext
from app.application.resolvers import resolve_customer
from app.application.summaries import payment_summary, receivable_summary
from app.db.models import CustomerPayment, PaymentAllocation, Receivable
from app.domain.dates import occurred_at_for, validate_business_date
from app.domain.enums import AuditAction, Currency, EntityType, PaymentMethod
from app.domain.money import compute_legs, fmt_amount
from app.domain.receivables import OpenReceivable, allocate_fifo


class CreateReceivable(Command):
    kind: ClassVar[str] = "create_receivable"

    customer_name: str = Field(min_length=1, max_length=160)
    customer_id: UUID | None = None  # trusted choice callback; not a model argument
    amount: Decimal = Field(gt=0)
    currency: Currency | None = None
    description: str | None = Field(default=None, max_length=300)
    business_date: date | None = None
    force_new_customer: bool = False
    # Lo pone el botón "Mantener" de la pregunta diaria de la tasa propia (nunca el modelo ni el panel).
    charge_rate_confirmed: bool = False


def _receivable_risk(cmd: CreateReceivable, ctx: ExecContext, res: CommandResult) -> str | None:
    # Dar de alta al cliente es parte indivisible del fiado; la ambigüedad ya se
    # resolvió antes de llegar aquí y no requiere una segunda confirmación.
    del cmd, ctx, res
    return None


@command_handler(CreateReceivable, risk=_receivable_risk)
async def create_receivable(cmd: CreateReceivable, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    business = await get_business(session, ctx.business_id)
    bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    rates = await deps.rates.effective(session, ctx.business_id)
    customer, created = await resolve_customer(
        session, ctx.business_id, cmd.customer_name, create_if_missing=True, force_new=cmd.force_new_customer,
        customer_id=cmd.customer_id,
    )
    currency, assumed = assume_currency(cmd.amount, cmd.currency, business)
    legs = compute_legs(cmd.amount, currency, rates)
    await require_fresh_charge_rate(session, ctx, business, rates, cmd, involves_local=currency == business.local_currency)
    receivable = Receivable(
        business_id=ctx.business_id,
        customer_id=customer.id,
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        description=cmd.description,
        source_message_id=ctx.source_message_id,
    )
    receivable.set_amount_legs(legs)
    receivable.customer = customer
    session.add(receivable)
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.RECEIVABLE, entity_id=receivable.id, action=AuditAction.CREATE,
                 before=None, after=audit.snapshot(receivable), command=cmd.kind)
    credit_applied = await apply_credit_in_favor(session, ctx, customer.id)
    return CommandResult(
        kind=cmd.kind,
        data={
            "receivable": {"amount": fmt_amount(legs.amount, legs.currency), "description": cmd.description,
                           "business_date": bdate.isoformat(), "currency_assumed": assumed},
            "customer": customer.display_name,
            "customer_created": created,
            "credit_applied": credit_applied,
            "customer_balance": await customer_balance(session, ctx.business_id, customer.id),
        },
        affected=[EntityRef(EntityType.RECEIVABLE, receivable.id, version=receivable.version,
                            summary=receivable_summary(receivable))],
    )


class RecordPayment(Command):
    kind: ClassVar[str] = "record_payment"

    customer_name: str = Field(min_length=1, max_length=160)
    customer_id: UUID | None = None  # trusted choice callback; not a model argument
    amount: Decimal = Field(gt=0)
    currency: Currency | None = None
    payment_method: PaymentMethod | None = None
    business_date: date | None = None
    # Lo pone el botón "Mantener" de la pregunta diaria de la tasa propia (nunca el modelo ni el panel).
    charge_rate_confirmed: bool = False


def _payment_risk(cmd: RecordPayment, ctx: ExecContext, res: CommandResult) -> str | None:
    if res.data.get("unapplied"):
        return (f"El abono supera lo que debe {res.data['customer']}: sobran {res.data['unapplied']}, "
                "que quedarían como saldo a favor.")
    return None


async def open_receivables(
    session: AsyncSession, business_id, customer_id, broker_member_id: UUID | None = None
) -> list[OpenReceivable]:
    rows = (
        await session.execute(
            select(Receivable)
            .where(
                Receivable.business_id == business_id,
                Receivable.customer_id == customer_id,
                Receivable.broker_member_id.is_not_distinct_from(broker_member_id),
                Receivable.deleted_at.is_(None),
            )
            .order_by(Receivable.business_date, Receivable.created_at)
        )
    ).scalars().all()
    out = []
    for r in rows:
        remaining = r.amount - await receivable_paid(session, r.id)
        if remaining > Decimal("0.005"):
            out.append(OpenReceivable(r.id, r.business_date, r.currency, remaining))
    return out


async def allocate_payment(session: AsyncSession, payment: CustomerPayment) -> list[dict]:
    """(Re)asigna un abono a los fiados abiertos de su cliente. Usado al crear y al corregir abonos."""
    await lock_customers(session, payment.business_id, payment.customer_id)  # antes de leer qué fiados siguen abiertos
    for existing in list(await payment.awaitable_attrs.allocations):
        await session.delete(existing)
    payment.allocations.clear()
    await session.flush()
    items = await open_receivables(
        session, payment.business_id, payment.customer_id, payment.broker_member_id
    )
    plan = allocate_fifo(payment.amount, payment.currency, payment.rate_applied, items)
    by_id = {i.id: i for i in items}
    applied = []
    for alloc in plan.allocations:
        payment.allocations.append(PaymentAllocation(receivable_id=alloc.receivable_id, applied_amount=alloc.applied_amount))
        item = by_id[alloc.receivable_id]
        applied.append({
            "receivable_date": item.business_date.isoformat(),
            "currency": str(item.currency),
            "applied": fmt_amount(alloc.applied_amount, item.currency),
            "settled": alloc.applied_amount >= item.remaining - Decimal("0.005"),
        })
    payment.unapplied_amount = plan.unapplied_in_payment_currency
    return applied


async def reallocate_customer_payments(session: AsyncSession, ctx: ExecContext, *customer_ids: UUID) -> None:
    """Tras mover/cambiar/borrar un fiado o abono, recalcula las asignaciones FIFO de los abonos de esos clientes.

    Idempotente: borra todas las asignaciones de los abonos vigentes y las vuelve a calcular en orden cronológico.
    """
    if not customer_ids:
        return
    await lock_customers(session, ctx.business_id, *customer_ids)
    await session.flush()
    payments = (await session.execute(
        select(CustomerPayment).where(CustomerPayment.business_id == ctx.business_id, CustomerPayment.customer_id.in_(customer_ids),
                                      CustomerPayment.deleted_at.is_(None)).order_by(CustomerPayment.business_date, CustomerPayment.created_at)
    )).scalars().all()
    for p in payments:
        for a in list(await p.awaitable_attrs.allocations):
            await session.delete(a)
        p.allocations.clear()
    await session.flush()
    for p in payments:
        await allocate_payment(session, p)
        await session.flush()


async def apply_credit_in_favor(
    session: AsyncSession, ctx: ExecContext, customer_id: UUID, broker_member_id: UUID | None = None
) -> str | None:
    """Tras crear un fiado: si el cliente tenía saldo a favor (abonos sin aplicar), se descuenta de la deuda nueva.

    Devuelve lo descontado, formateado ("$5,00"), o None si no tenía saldo a favor.
    """
    await lock_customers(session, ctx.business_id, customer_id)  # antes de leer el saldo a favor: otro abono pudo cambiarlo
    before = await _unapplied_by_currency(session, ctx.business_id, customer_id, broker_member_id)
    if not before:
        return None
    await reallocate_customer_payments(session, ctx, customer_id)
    after = await _unapplied_by_currency(session, ctx.business_id, customer_id, broker_member_id)
    used = {c: v - after.get(c, Decimal(0)) for c, v in before.items()}
    text = " + ".join(fmt_amount(v, c) for c, v in sorted(used.items()) if v > Decimal("0.005"))
    return text or None


async def _unapplied_by_currency(
    session: AsyncSession, business_id: UUID, customer_id: UUID, broker_member_id: UUID | None = None
) -> dict[str, Decimal]:
    rows = (await session.execute(
        select(CustomerPayment.currency, func.sum(CustomerPayment.unapplied_amount))
        .where(CustomerPayment.business_id == business_id, CustomerPayment.customer_id == customer_id,
               CustomerPayment.broker_member_id.is_not_distinct_from(broker_member_id),
               CustomerPayment.deleted_at.is_(None), CustomerPayment.unapplied_amount > 0)
        .group_by(CustomerPayment.currency)
    )).all()
    return {str(c): Decimal(v) for c, v in rows if v}



@command_handler(RecordPayment, risk=_payment_risk)
async def record_payment(cmd: RecordPayment, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    business = await get_business(session, ctx.business_id)
    bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    rates = await deps.rates.effective(session, ctx.business_id)
    customer, _ = await resolve_customer(session, ctx.business_id, cmd.customer_name, create_if_missing=False,
                                         customer_id=cmd.customer_id)
    currency, assumed = assume_currency(cmd.amount, cmd.currency, business)
    legs = compute_legs(cmd.amount, currency, rates)
    payment = CustomerPayment(
        business_id=ctx.business_id,
        customer_id=customer.id,
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        payment_method=cmd.payment_method.value if cmd.payment_method else None,
        source_message_id=ctx.source_message_id,
        unapplied_amount=Decimal(0),
        allocations=[],
    )
    payment.set_amount_legs(legs)
    payment.customer = customer
    session.add(payment)
    await session.flush()
    applied = await allocate_payment(session, payment)
    await session.flush()
    # Toca la moneda local si el abono es en ella o si se aplicó a un fiado en otra moneda (hubo conversión).
    await require_fresh_charge_rate(session, ctx, business, rates, cmd, involves_local=currency == business.local_currency
                                    or any(a["currency"] != currency for a in applied))
    audit.record(session, ctx, entity_type=EntityType.PAYMENT, entity_id=payment.id, action=AuditAction.CREATE,
                 before=None, after=audit.snapshot(payment), command=cmd.kind)
    unapplied = fmt_amount(payment.unapplied_amount, currency) if payment.unapplied_amount > 0 else None
    return CommandResult(
        kind=cmd.kind,
        data={
            "payment": {"amount": fmt_amount(legs.amount, legs.currency), "payment_method": payment.payment_method,
                        "business_date": bdate.isoformat(), "currency_assumed": assumed},
            "customer": customer.display_name,
            "applied_to": applied,
            "unapplied": unapplied,
            "customer_balance": await customer_balance(session, ctx.business_id, customer.id),
        },
        affected=[EntityRef(EntityType.PAYMENT, payment.id, version=payment.version, summary=payment_summary(payment))],
    )
