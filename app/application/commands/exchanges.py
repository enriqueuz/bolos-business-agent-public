"""Cambios de moneda: salen dólares y entran bolívares (o al revés). No es venta, gasto ni ingreso.

Cuando el negocio OBTIENE moneda local con un cambio, la tasa del cambio pasa a ser la tasa real de sus bolívares
(CustomRate purpose=funds, source=exchange): con ella se calcula cuánto le cuestan realmente sus pagos en moneda local.
La tasa para cobrar no cambia.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import ClassVar

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.commands.rates import replace_custom_rate
from app.application.common import get_business, profile_of
from app.application.context import EntityRef, ExecContext
from app.application.rates import RateService
from app.application.summaries import exchange_summary, method_label
from app.db.models import CurrencyExchange, CustomRate
from app.domain.currencies import USD
from app.domain.dates import occurred_at_for, validate_business_date
from app.domain.enums import AuditAction, Currency, CustomRateSource, EntityType, PaymentMethod, RatePurpose
from app.domain.errors import ValidationFailed
from app.domain.money import ensure_supported, fmt_amount, fmt_rate, q, unusual_rate_reason

RATE_Q = Decimal("0.000001")


class RecordExchange(Command):
    kind: ClassVar[str] = "record_exchange"

    out_amount: Decimal = Field(gt=0, description="Lo que salió")
    out_currency: Currency
    out_method: PaymentMethod | None = None
    in_amount: Decimal = Field(gt=0, description="Lo que entró")
    in_currency: Currency
    in_method: PaymentMethod | None = None
    business_date: date | None = None
    description: str | None = Field(default=None, max_length=300)


async def _newer_funds_from_exchange(session: AsyncSession, business_id, business_date: date) -> bool:
    """¿La tasa real vigente sale de un cambio con fecha posterior? Entonces un cambio atrasado no la reemplaza."""
    current = await RateService.current_custom(session, business_id, RatePurpose.FUNDS)
    if current is None or current.exchange_id is None:
        return False
    source = await session.get(CurrencyExchange, current.exchange_id)
    return source is not None and source.business_date > business_date


@command_handler(RecordExchange)
async def record_exchange(cmd: RecordExchange, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    business = await get_business(session, ctx.business_id)
    profile = profile_of(business)
    local = business.local_currency
    for currency in (cmd.out_currency, cmd.in_currency):
        ensure_supported(str(currency), local)
    if str(cmd.out_currency) == str(cmd.in_currency):
        raise ValidationFailed(f"Un cambio es entre dólares y {profile.local.plural}: indica qué salió y qué entró en cada moneda.")
    bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    usd, local_amount = (cmd.out_amount, cmd.in_amount) if str(cmd.out_currency) == USD else (cmd.in_amount, cmd.out_amount)
    rate = (local_amount / usd).quantize(RATE_Q)
    snap = await deps.rates.snapshot(ctx.business_id, session)
    exchange = CurrencyExchange(
        business_id=ctx.business_id, business_date=bdate, occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        out_amount=q(cmd.out_amount), out_currency=str(cmd.out_currency), out_method=cmd.out_method.value if cmd.out_method else None,
        in_amount=q(cmd.in_amount), in_currency=str(cmd.in_currency), in_method=cmd.in_method.value if cmd.in_method else None,
        rate=rate, rate_official=snap.official or rate, rate_market=snap.market or snap.official or rate,
        description=cmd.description, source_message_id=ctx.source_message_id,
    )
    session.add(exchange)
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.EXCHANGE, entity_id=exchange.id, action=AuditAction.CREATE,
                 before=None, after=audit.snapshot(exchange), command=cmd.kind)

    # Solo cuando el negocio obtiene moneda local: esa es la tasa a la que consiguió sus bolívares.
    obtained_local = str(cmd.in_currency) == local
    funds_set = obtained_local and not await _newer_funds_from_exchange(session, ctx.business_id, bdate)
    previous_funds = None
    if funds_set:
        _, previous = await replace_custom_rate(session, ctx, ctx.business_id, RatePurpose.FUNDS, rate, None,
                                                source=CustomRateSource.EXCHANGE, exchange_id=exchange.id)
        previous_funds = fmt_rate(Decimal(previous["rate"]), local) if previous else None
    return CommandResult(
        kind=cmd.kind,
        guard_reason=unusual_rate_reason(rate, snap.official, local, profile.official_rate_label),
        data={
            "exchange": {
                "out": fmt_amount(exchange.out_amount, exchange.out_currency), "out_method": method_label(exchange.out_method),
                "in": fmt_amount(exchange.in_amount, exchange.in_currency), "in_method": method_label(exchange.in_method),
                "rate": fmt_rate(rate, local), "unit": f"{profile.local.symbol}/$", "business_date": bdate.isoformat(),
            },
            "obtained_local": obtained_local,
            "funds_rate_set": funds_set,
            "previous_funds_rate": previous_funds,
            "note": "Un cambio no es venta, gasto ni ingreso: solo cambia la forma del dinero.",
        },
        affected=[EntityRef(EntityType.EXCHANGE, exchange.id, version=exchange.version, summary=exchange_summary(exchange))],
    )


async def restore_funds_after_exchange_removed(session: AsyncSession, ctx: ExecContext, exchange: CurrencyExchange) -> str | None:
    """Al borrar un cambio: si su tasa era la tasa real vigente, vuelve a valer la anterior (manual o de otro cambio vivo).

    Devuelve un aviso para el dueño, o None si la tasa real vigente no dependía de este cambio.
    """
    current = await RateService.current_custom(session, exchange.business_id, RatePurpose.FUNDS)
    if current is None or current.exchange_id != exchange.id:
        return None
    current.cleared_at = ctx.now
    candidates = (await session.execute(
        select(CustomRate).where(CustomRate.business_id == exchange.business_id, CustomRate.purpose == RatePurpose.FUNDS.value,
                                 CustomRate.id != current.id, CustomRate.set_at <= current.set_at)
        .order_by(CustomRate.set_at.desc())
    )).scalars().all()
    business = await get_business(session, exchange.business_id)
    for candidate in candidates:
        if candidate.exchange_id is not None:
            source = await session.get(CurrencyExchange, candidate.exchange_id)
            if source is None or source.deleted_at is not None:
                continue
        restored, _ = await replace_custom_rate(session, ctx, exchange.business_id, RatePurpose.FUNDS, candidate.rate,
                                                candidate.label, source=CustomRateSource(candidate.source),
                                                exchange_id=candidate.exchange_id)
        return f"Tu tasa real de los {profile_of(business).local.plural} vuelve a ser {fmt_rate(restored.rate, business.local_currency)}."
    return f"Ya no tengo una tasa real de tus {profile_of(business).local.plural}; dímela o registra un cambio."
