"""Cierre de caja: calcular el resumen del día y congelarlo. Idempotente: cerrar dos veces actualiza el snapshot."""

from __future__ import annotations

from datetime import date
from typing import ClassVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.common import get_business
from app.application.context import EntityRef, ExecContext
from app.application.queries.cash import cash_summary
from app.db.models import Business, CashSession
from app.domain.dates import validate_business_date
from app.domain.enums import AuditAction, CurrencyView, EntityType


class CloseCash(Command):
    kind: ClassVar[str] = "close_cash"

    business_date: date | None = None


@command_handler(CloseCash)
async def close_cash(cmd: CloseCash, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    await session.execute(select(Business.id).where(Business.id == ctx.business_id).with_for_update())
    business = await get_business(session, ctx.business_id)
    day = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    summary = await cash_summary(session, ctx.business_id, day, CurrencyView(business.display_currency))
    existing = (
        await session.execute(select(CashSession).where(CashSession.business_id == ctx.business_id, CashSession.business_date == day))
    ).scalar_one_or_none()
    if existing:
        before = audit.snapshot(existing)
        existing.summary = summary
        existing.closed_at = ctx.now
        existing.closed_by_actor_type = ctx.actor.type.value
        existing.times_closed += 1
        session_row, action = existing, AuditAction.UPDATE
    else:
        before = None
        session_row = CashSession(business_id=ctx.business_id, business_date=day, closed_at=ctx.now,
                                  closed_by_actor_type=ctx.actor.type.value, summary=summary)
        session.add(session_row)
        action = AuditAction.CREATE
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.CASH_SESSION, entity_id=session_row.id, action=action,
                 before=before, after={"summary": summary}, command=cmd.kind)
    return CommandResult(
        kind=cmd.kind,
        data={"summary": summary, "already_closed_before": existing is not None},
        affected=[EntityRef(EntityType.CASH_SESSION, session_row.id)],
    )
