"""Read-only operational cutoff for the editable workbook; no new accounting rules."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.customer_accounts import account_balances
from app.db.models import (
    BrokerMovement,
    Business,
    BusinessMember,
    CustomerBalanceAdjustment,
    CustomerPayment,
    PaymentAllocation,
    Receivable,
)
from app.domain.errors import NotFound, ValidationFailed

ZERO = Decimal("0.00")


def classification(value: str | None, has_customer: bool = True) -> str:
    return {"global": "Global", "personal": "Personal"}.get(value, "Sin clasificar" if has_customer else "")


def settlement(paid: Decimal, amount: Decimal) -> str:
    return "Liquidado" if paid >= amount else "Parcial" if paid > ZERO else "Pendiente"


def movement_type(movement: BrokerMovement) -> str:
    if movement.kind == "adjustment":
        return "Ajuste de saldo"
    if movement.kind == "customer_payout":
        return "Pago entregado"
    if movement.kind == "out":
        return "Salida de cuenta"
    if movement.related_payment_id:
        return "Abono de cliente"
    if movement.kind != "operation":
        return "Abono a cuenta"
    if movement.operation_type == "sell":
        return "Venta"
    if movement.settlement_status == "account":
        return "Compra propia"
    return "Compra pendiente" if movement.related_receivable_id else "Compra inmediata"


@dataclass(frozen=True)
class MovementRow:
    key: str
    day: date
    account_key: str
    account: str
    customer: str = ""
    classification: str = ""
    portfolio: str = ""
    type: str = ""
    operation: str = ""
    usd: Decimal = ZERO
    bs: Decimal | None = None
    rate: Decimal | None = None
    basis: str | None = None
    received: Decimal | None = None
    commission: Decimal = ZERO
    excess: Decimal = ZERO
    delta: Decimal = ZERO
    status: str = ""
    description: str = ""
    recorded_by: str = ""
    original_settlement: str = ""


@dataclass(frozen=True)
class DebtRow:
    key: str
    day: date
    customer: str
    classification: str
    portfolio: str
    principal: Decimal
    paid_before: Decimal = ZERO
    paid_period: Decimal = ZERO
    description: str = ""


@dataclass(frozen=True)
class PaymentRow:
    key: str
    debt_key: str
    day: date
    customer: str
    classification: str
    portfolio: str
    applied: Decimal = ZERO
    unapplied: Decimal = ZERO
    # A split payment shows its total once. Applied amounts remain additive per debt.
    total: Decimal | None = None
    account: str = ""


@dataclass(frozen=True)
class BrokerRow:
    key: str
    name: str
    active: bool = True
    opening: Decimal = ZERO


@dataclass(frozen=True)
class CustomerAccountRow:
    key: str
    customer: str
    classification: str
    portfolio: str
    opening: Decimal = ZERO
    closing: Decimal = ZERO


@dataclass(frozen=True)
class CustomerEventRow:
    key: str
    customer_key: str
    customer: str
    classification: str
    portfolio: str
    day: date
    type: str
    amount: Decimal
    delta: Decimal
    operation: str = ""


@dataclass(frozen=True)
class WorkbookData:
    business: str
    start: date
    end: date
    scope: str = "Consolidado del negocio"
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    movements: tuple[MovementRow, ...] = ()
    debts: tuple[DebtRow, ...] = ()
    payments: tuple[PaymentRow, ...] = ()
    brokers: tuple[BrokerRow, ...] = ()
    customer_accounts: tuple[CustomerAccountRow, ...] = ()
    customer_events: tuple[CustomerEventRow, ...] = ()


async def load_workbook_data(
    factory: async_sessionmaker[AsyncSession], business_id: UUID, *, start: date, end: date,
    broker_id: UUID | None = None, account_scope: str = "all",
) -> WorkbookData:
    """Restated cutoff: current active records and allocations, restricted by business_date.

    All queries share a repeatable-read snapshot. Current balances and current purchase
    settlement are deliberately not used for a previous cutoff. No audit replay or FIFO.
    """
    async with factory() as db, db.begin():
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        business = await db.get(Business, business_id)
        if business is None or business.vertical != "brokerage":
            raise NotFound("Casa de Cambio no encontrada.")
        if start > end or account_scope not in {"all", "house", "broker"}:
            raise ValidationFailed("Período o ámbito de exportación inválido.")
        members = (await db.scalars(select(BusinessMember).where(
            BusinessMember.business_id == business_id, BusinessMember.role == "broker",
        ).order_by(BusinessMember.name, BusinessMember.id))).all()
        member_by_id = {m.id: m for m in members}
        if broker_id is not None:
            if broker_id not in member_by_id:
                raise NotFound("Corredor no encontrado.")
            if account_scope == "house":
                raise ValidationFailed("No combines Casa con un corredor.")
            account_scope = "broker"
        if account_scope == "broker" and broker_id is None:
            raise ValidationFailed("Indica un corredor.")

        def in_scope(member_id):
            return account_scope == "all" or (member_id is None if account_scope == "house" else member_id == broker_id)

        def portfolio_name(member_id):
            if member_id is None:
                return "Global / Casa"
            member = member_by_id.get(member_id)
            return member.name or "Corredor" if member else "Sin identificar"

        movements = (await db.scalars(select(BrokerMovement).where(
            BrokerMovement.business_id == business_id, BrokerMovement.deleted_at.is_(None),
            BrokerMovement.business_date <= end,
        ).order_by(BrokerMovement.business_date, BrokerMovement.occurred_at, BrokerMovement.id))).unique().all()
        debts = (await db.scalars(select(Receivable).where(
            Receivable.business_id == business_id, Receivable.deleted_at.is_(None), Receivable.business_date <= end,
        ).order_by(Receivable.business_date, Receivable.occurred_at, Receivable.id))).unique().all()
        payments = (await db.scalars(select(CustomerPayment).where(
            CustomerPayment.business_id == business_id, CustomerPayment.deleted_at.is_(None),
            CustomerPayment.business_date <= end,
        ).order_by(CustomerPayment.business_date, CustomerPayment.occurred_at, CustomerPayment.id))).unique().all()
        adjustments = (await db.scalars(select(CustomerBalanceAdjustment).where(
            CustomerBalanceAdjustment.business_id == business_id, CustomerBalanceAdjustment.deleted_at.is_(None),
            CustomerBalanceAdjustment.business_date <= end,
        ).order_by(CustomerBalanceAdjustment.business_date, CustomerBalanceAdjustment.occurred_at,
                   CustomerBalanceAdjustment.id))).unique().all()
        debt_by_id = {d.id: d for d in debts}
        allocations = (await db.execute(select(PaymentAllocation).join(CustomerPayment).join(
            Receivable, PaymentAllocation.receivable_id == Receivable.id,
        ).where(
            CustomerPayment.business_id == business_id, Receivable.business_id == business_id,
            CustomerPayment.deleted_at.is_(None), Receivable.deleted_at.is_(None),
            CustomerPayment.business_date <= end, Receivable.business_date <= end,
        ).order_by(Receivable.business_date, Receivable.id, PaymentAllocation.id))).scalars().all()
        by_payment = defaultdict(list)
        for allocation in allocations:
            by_payment[allocation.payment_id].append(allocation)
        before, during = defaultdict(lambda: ZERO), defaultdict(lambda: ZERO)
        payment_rows = []
        account_by_payment = {m.related_payment_id: m.broker.name if m.broker else "Casa" for m in movements if m.related_payment_id}
        for payment in payments:
            lines = by_payment[payment.id]
            target = before if payment.business_date < start else during
            for allocation in lines:
                target[allocation.receivable_id] += allocation.applied_amount
            if payment.business_date < start or not in_scope(payment.broker_member_id):
                continue
            common = dict(key=str(payment.id), day=payment.business_date, customer=payment.customer.display_name,
                          classification=classification(payment.customer.classification),
                          portfolio=portfolio_name(payment.broker_member_id), account=account_by_payment.get(payment.id, ""))
            for i, allocation in enumerate(lines):
                payment_rows.append(PaymentRow(**common, debt_key=str(allocation.receivable_id),
                                               applied=allocation.applied_amount, total=payment.amount if i == 0 else None))
            # Derive at the same cutoff, instead of using today's unapplied_amount.
            unapplied = payment.amount - sum((a.applied_amount for a in lines), ZERO)
            if unapplied > ZERO:
                payment_rows.append(PaymentRow(**common, debt_key="", unapplied=unapplied,
                                               total=payment.amount if not lines else None))
        debt_rows = tuple(DebtRow(
            key=str(d.id), day=d.business_date, customer=d.customer.display_name,
            classification=classification(d.customer.classification), portfolio=portfolio_name(d.broker_member_id),
            principal=d.amount, paid_before=before[d.id], paid_period=during[d.id], description=d.description or "",
        ) for d in debts if in_scope(d.broker_member_id) and (
            d.business_date >= start or d.amount > before[d.id] or during[d.id] > ZERO
        ))
        openings = defaultdict(lambda: ZERO)
        rows = []
        for m in movements:
            if not in_scope(m.broker_member_id):
                continue
            if m.business_date < start:
                if m.broker_member_id:
                    openings[m.broker_member_id] += m.balance_delta
                continue
            status = {"settled": "Liquidado", "account": "Cuenta propia", "pending": "Pendiente", "partial": "Parcial"}.get(m.settlement_status, "")
            if m.related_receivable_id in debt_by_id:
                d = debt_by_id[m.related_receivable_id]
                status = settlement(before[d.id] + during[d.id], d.amount)
            rows.append(MovementRow(
                key=str(m.id), day=m.business_date, account_key=str(m.broker_member_id or ""),
                account=m.broker.name or "Corredor" if m.broker else "Casa", customer=m.customer.display_name if m.customer else "",
                classification=classification(m.customer.classification if m.customer else None, bool(m.customer)),
                portfolio=portfolio_name(m.portfolio_broker_member_id) if m.customer else "",
                type=movement_type(m), operation=m.operation_type or "", usd=m.operation_amount if m.kind == "operation" else m.customer_payout_amount if m.kind == "customer_payout" else m.related_payment.amount if m.related_payment else abs(m.balance_delta),
                bs=m.operation_amount_bs, rate=m.exchange_rate, basis=m.monetary_basis, received=m.received_amount,
                commission=m.commission or ZERO, excess=m.excess or ZERO, delta=m.balance_delta,
                status=status, description=m.description or "", recorded_by=m.recorded_by.name or "",
                original_settlement="" if m.kind == "adjustment" else "Pendiente" if m.related_receivable_id or m.settlement_status == "pending" else "Cuenta propia" if m.settlement_status == "account" else "Liquidada",
            ))
        # The account at either boundary uses exactly the same signed sources as chat/panel.
        opening_customer = await account_balances(db, business_id, cutoff=start - timedelta(days=1))
        closing_customer = await account_balances(db, business_id, cutoff=end)
        origins = {m.related_receivable_id: m for m in movements if m.related_receivable_id}
        payment_origins = {m.related_payment_id: m for m in movements if m.related_payment_id}
        customers, events = {}, []

        def add_event(customer, portfolio_id, day, type_, amount, delta, key, operation=""):
            if not in_scope(portfolio_id):
                return
            identity = (customer.id, portfolio_id)
            customers[identity] = customer
            if day >= start:
                events.append(CustomerEventRow(str(key), str(customer.id), customer.display_name,
                    classification(customer.classification), portfolio_name(portfolio_id), day,
                    type_, amount, delta, operation))

        for d in debts:
            origin = origins.get(d.id)
            add_event(d.customer, d.broker_member_id, d.business_date, "Compra pendiente", d.amount,
                      -d.amount, origin.id if origin else d.id, "buy" if origin else "")
        for adjustment in adjustments:
            add_event(adjustment.customer, adjustment.portfolio_broker_member_id, adjustment.business_date,
                      "Ajuste de saldo", adjustment.amount_delta, adjustment.amount_delta, adjustment.id)
        for payment in payments:
            origin = payment_origins.get(payment.id)
            add_event(payment.customer, payment.broker_member_id, payment.business_date,
                      "Pago recibido", payment.amount, payment.amount, origin.id if origin else payment.id)
        for m in movements:
            if m.operation_type == "sell" and m.settlement_status == "pending":
                add_event(m.customer, m.portfolio_broker_member_id, m.business_date,
                          "Venta pendiente", m.operation_amount, m.operation_amount, m.id, "sell")
            elif m.kind == "customer_payout":
                add_event(m.customer, m.portfolio_broker_member_id, m.business_date,
                          "Pago entregado", m.customer_payout_amount, -m.customer_payout_amount, m.id)
        customer_rows = tuple(CustomerAccountRow(str(c.id), c.display_name, classification(c.classification),
            portfolio_name(portfolio_id), opening_customer.get((cid, portfolio_id), ZERO),
            closing_customer.get((cid, portfolio_id), ZERO))
            for (cid, portfolio_id), c in sorted(customers.items(), key=lambda pair: (pair[1].display_name, str(pair[0]))))
        events.sort(key=lambda e: (e.day, e.key))
        active_accounts = {r.account_key for r in rows}
        brokers = tuple(BrokerRow(str(m.id), m.name or "Corredor", m.is_active, openings[m.id]) for m in members
                        if in_scope(m.id) and (m.is_active or openings[m.id] or str(m.id) in active_accounts))
        if account_scope == "all":
            scope = "Consolidado del negocio"
        elif account_scope == "house":
            scope = "Cuenta ejecutora: Casa · Cartera: Global / Casa"
        else:
            scope = f"Cuenta ejecutora y cartera personal: {portfolio_name(broker_id)}"
        return WorkbookData(business.name, start, end, scope, movements=tuple(rows), debts=debt_rows,
                            payments=tuple(payment_rows), brokers=brokers,
                            customer_accounts=customer_rows, customer_events=tuple(events))
