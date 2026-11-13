"""One derived USD account per brokerage customer and portfolio.

FIFO allocations describe receipts, never the net obligation. In particular a
receipt is counted from CustomerPayment, not again from its BrokerMovement.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BrokerMovement, CustomerBalanceAdjustment, CustomerPayment, Receivable
from app.domain.brokerage import q_money
from app.domain.money import fmt_amount


def account_entries(business_id: UUID):
    sources = []
    for model, portfolio, amount, extra in (
        (Receivable, Receivable.broker_member_id, -Receivable.amount, []),
        (CustomerPayment, CustomerPayment.broker_member_id, CustomerPayment.amount, []),
        (CustomerBalanceAdjustment, CustomerBalanceAdjustment.portfolio_broker_member_id,
         CustomerBalanceAdjustment.amount_delta, []),
        (BrokerMovement, BrokerMovement.portfolio_broker_member_id, BrokerMovement.operation_amount,
         [BrokerMovement.kind == "operation", BrokerMovement.operation_type == "sell",
          BrokerMovement.settlement_status == "pending"]),
        (BrokerMovement, BrokerMovement.portfolio_broker_member_id, -BrokerMovement.customer_payout_amount,
         [BrokerMovement.kind == "customer_payout"]),
    ):
        sources.append(select(model.customer_id.label("customer_id"), portfolio.label("portfolio_id"),
                              model.business_date.label("business_date"), amount.label("delta"))
                       .where(model.business_id == business_id, model.deleted_at.is_(None), *extra))
    return union_all(*sources).subquery()


async def account_balances(session: AsyncSession, business_id: UUID, *,
                           customer_ids: list[UUID] | None = None, cutoff: date | None = None) -> dict:
    entries = account_entries(business_id)
    stmt = select(entries.c.customer_id, entries.c.portfolio_id, func.sum(entries.c.delta))
    if customer_ids is not None:
        if not customer_ids:
            return {}
        stmt = stmt.where(entries.c.customer_id.in_(customer_ids))
    if cutoff is not None:
        stmt = stmt.where(entries.c.business_date <= cutoff)
    rows = await session.execute(stmt.group_by(entries.c.customer_id, entries.c.portfolio_id))
    return {(customer, portfolio): q_money(Decimal(amount)) for customer, portfolio, amount in rows}


def balance_fields(amount: Decimal) -> dict:
    amount = q_money(amount)
    return {
        "balance_usd": str(amount), "currency": "USD",
        "status": "customer_owes" if amount < 0 else "house_owes" if amount > 0 else "settled",
        "owed": {"USD": str(-amount)} if amount < 0 else {},
        "credit_in_favor": {"USD": str(amount)} if amount > 0 else {},
        "display": fmt_amount(abs(amount), "USD"),
    }


async def customer_account_balance(session: AsyncSession, business_id: UUID, customer_id: UUID,
                                   portfolio_id: UUID | None) -> dict:
    amounts = await account_balances(session, business_id, customer_ids=[customer_id])
    return balance_fields(amounts.get((customer_id, portfolio_id), Decimal("0")))
