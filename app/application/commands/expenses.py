"""Registrar gastos operativos (no mercancía para vender: eso es una compra)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.common import (
    assume_currency,
    get_business,
    legs_brief,
    real_cost_brief,
    require_fresh_charge_rate,
)
from app.application.context import EntityRef, ExecContext
from app.application.summaries import expense_summary
from app.db.models import Expense
from app.domain.dates import occurred_at_for, validate_business_date
from app.domain.enums import AuditAction, Currency, EntityType, ExpenseCategory, PaymentMethod
from app.domain.money import compute_legs


class RecordExpense(Command):
    kind: ClassVar[str] = "record_expense"

    description: str = Field(min_length=1, max_length=300)
    amount: Decimal = Field(gt=0)
    currency: Currency | None = None
    category: ExpenseCategory = ExpenseCategory.OTHER
    payment_method: PaymentMethod | None = None
    business_date: date | None = None
    # Lo pone el botón "Mantener" de la pregunta diaria de la tasa propia (nunca el modelo ni el panel).
    charge_rate_confirmed: bool = False


@command_handler(RecordExpense)
async def record_expense(cmd: RecordExpense, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    business = await get_business(session, ctx.business_id)
    bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    rates = await deps.rates.effective(session, ctx.business_id)
    currency, assumed = assume_currency(cmd.amount, cmd.currency, business)
    legs = compute_legs(cmd.amount, currency, rates)
    await require_fresh_charge_rate(session, ctx, business, rates, cmd, involves_local=currency == business.local_currency)
    expense = Expense(
        business_id=ctx.business_id,
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        category=cmd.category.value,
        description=cmd.description.strip(),
        payment_method=cmd.payment_method.value if cmd.payment_method else None,
        source_message_id=ctx.source_message_id,
    )
    expense.set_amount_legs(legs)
    expense.set_real_cost(legs, rates.funds)
    session.add(expense)
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.EXPENSE, entity_id=expense.id, action=AuditAction.CREATE,
                 before=None, after=audit.snapshot(expense), command=cmd.kind)
    return CommandResult(
        kind=cmd.kind,
        data={
            "expense": {
                "description": expense.description,
                "category": cmd.category.value,
                "business_date": bdate.isoformat(),
                "amount": legs_brief(legs),
                "payment_method": expense.payment_method,
                "currency_assumed": assumed,
                "real_cost": real_cost_brief(expense, legs, business),
            }
        },
        affected=[EntityRef(EntityType.EXPENSE, expense.id, version=expense.version, summary=expense_summary(expense))],
    )
