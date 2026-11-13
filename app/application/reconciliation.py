"""Trusted reconciliation boundary and shared deterministic account calculations."""
from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.access import actor_member, require_vertical
from app.application.context import ExecContext
from app.application.customer_accounts import account_balances
from app.db.models import BrokerMovement, Customer
from app.domain.brokerage import q_money
from app.domain.enums import BusinessVertical, MemberRole
from app.domain.errors import NotFound, ValidationFailed

RECONCILIATION_KINDS = frozenset({"reconcile_account_balance", "delete_balance_adjustment"})
ADJUSTMENT_DESCRIPTION = "Reconciliación de saldo"


async def authorize_reconciliation(session: AsyncSession, ctx: ExecContext, operation_id: UUID):
    if ctx.reconciliation_operation_id is None or ctx.reconciliation_operation_id != operation_id:
        raise ValidationFailed("Esta acción requiere una solicitud de reconciliación activa.")
    business = await require_vertical(session, ctx.business_id, BusinessVertical.BROKERAGE)
    actor = await actor_member(session, ctx)
    if actor.role not in {MemberRole.OWNER.value, MemberRole.BROKER.value}:
        raise ValidationFailed("No tienes permiso para reconciliar saldos.")
    return business, actor


def authorize_customer(customer: Customer, ctx: ExecContext, actor) -> None:
    if customer.business_id != ctx.business_id or customer.deleted_at is not None or customer.classification is None:
        raise NotFound("Cliente no encontrado.")
    if (actor.role == MemberRole.BROKER.value and customer.classification == "personal"
            and customer.owner_broker_member_id != actor.id):
        raise NotFound("Cliente no encontrado.")


def historical_date(day: int, month: int, year: int | None, today: date) -> date:
    try:
        cutoff = date(year or today.year, month, day)
    except ValueError as exc:
        raise ValidationFailed("La fecha histórica no es válida.") from exc
    if cutoff > today:
        message = "Indica el año de la fecha histórica." if year is None else "La fecha histórica no puede ser futura."
        raise ValidationFailed(message)
    return cutoff


async def reconciliation_balances(session: AsyncSession, ctx: ExecContext, *, account_type: str,
                                  account_id: UUID | None, portfolio_id: UUID | None, cutoff: date):
    if account_id is None:
        return Decimal("0.00"), Decimal("0.00")
    if account_type == "customer":
        at_cutoff = await account_balances(session, ctx.business_id, customer_ids=[account_id], cutoff=cutoff)
        current = await account_balances(session, ctx.business_id, customer_ids=[account_id], cutoff=ctx.today)
        return at_cutoff.get((account_id, portfolio_id), Decimal("0.00")), current.get((account_id, portfolio_id), Decimal("0.00"))
    values = []
    for boundary in (cutoff, ctx.today):
        value = await session.scalar(select(func.coalesce(func.sum(BrokerMovement.balance_delta), 0)).where(
            BrokerMovement.business_id == ctx.business_id, BrokerMovement.broker_member_id == account_id,
            BrokerMovement.deleted_at.is_(None), BrokerMovement.business_date <= boundary))
        values.append(q_money(Decimal(value)))
    return tuple(values)
