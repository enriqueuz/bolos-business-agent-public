"""Tasas del negocio.

- Tasa para cobrar (`charge_rate_mode`): oficial, mercado o la propia del dueño (CustomRate purpose=charge). Es la que se
  congela como `rate_applied` en cada movimiento nuevo. Cambiarla nunca toca los movimientos anteriores.
- Tasa real de los bolívares (CustomRate purpose=funds): a cuánto obtuvo el negocio sus Bs. La fija el dueño o sale de
  un cambio de moneda registrado; solo sirve para el costo real de lo que paga en moneda local, nunca para cobrar.

Una tasa propia persiste hasta que el dueño la cambie (nunca expira; se muestra su edad y se confirma una vez al día).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.common import bump, command_option, lock_business, profile_of
from app.application.context import EntityRef, ExecContext
from app.application.rates import RateService
from app.db.models import Business, CustomRate
from app.domain.dates import local_today
from app.domain.enums import AuditAction, ChargeRateMode, CustomRateSource, EntityType, RatePurpose, RateScope
from app.domain.errors import NeedsChoice, ValidationFailed
from app.domain.money import fmt_rate, unusual_rate_reason


async def replace_custom_rate(session: AsyncSession, ctx: ExecContext, business_id, purpose: RatePurpose, rate: Decimal,
                              label: str | None, source: CustomRateSource = CustomRateSource.MANUAL,
                              exchange_id=None) -> tuple[CustomRate, dict[str, Any] | None]:
    """Deja `rate` como la tasa vigente de ese propósito (la anterior queda en el historial, con `cleared_at`)."""
    current = await RateService.current_custom(session, business_id, purpose)
    previous = None
    if current:
        previous = {"rate": str(current.rate), "label": current.label, "source": current.source}
        current.cleared_at = ctx.now
    new = CustomRate(business_id=business_id, purpose=purpose.value, source=source.value, exchange_id=exchange_id,
                     rate=rate, label=label, set_at=ctx.now)
    session.add(new)
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.CUSTOM_RATE, entity_id=new.id, action=AuditAction.CREATE,
                 before=previous, after=audit.snapshot(new), command=f"set_{purpose.value}_rate")
    return new, previous


async def apply_charge_mode(session: AsyncSession, ctx: ExecContext, business: Business, mode: ChargeRateMode) -> bool:
    """Cambia la tasa para cobrar. A 'custom' solo si ya hay una tasa propia; al dejarla, la tasa propia se retira.

    Devuelve si cambió el negocio (quien llama sube la versión).
    """
    profile = profile_of(business)
    if mode == ChargeRateMode.MARKET and not profile.has_market_rate:
        raise ValidationFailed(f"En {profile.name} no hay tasa de mercado; usa la {profile.official_rate_label} o una tasa propia.")
    current = await RateService.current_custom(session, business.id, RatePurpose.CHARGE)
    if mode == ChargeRateMode.CUSTOM and current is None:
        raise ValidationFailed("Para cobrar con tu tasa propia, dime qué tasa usas.", missing="rate")
    if mode != ChargeRateMode.CUSTOM and current is not None:
        current.cleared_at = ctx.now
        audit.record(session, ctx, entity_type=EntityType.CUSTOM_RATE, entity_id=current.id, action=AuditAction.DELETE,
                     before=audit.snapshot(current), after=None, command="set_charge_rate_mode")
    if business.charge_rate_mode == mode.value:
        return False
    business.charge_rate_mode = mode.value
    return True


class SetRate(Command):
    """El dueño da una tasa. `purpose`: charge (para cobrar), funds (tasa real de sus Bs) o both.

    Sin `purpose` no se guarda nada: se le pregunta para qué es (una tasa suelta puede ser cualquiera de las dos).
    """

    kind: ClassVar[str] = "set_rate"

    rate: Decimal = Field(gt=0, description="Unidades de moneda local por 1 dólar")
    purpose: RateScope | None = None
    label: str | None = Field(default=None, max_length=60)


@command_handler(SetRate)
async def set_rate(cmd: SetRate, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    business = await lock_business(session, ctx.business_id)
    profile = profile_of(business)
    local = business.local_currency
    shown = fmt_rate(cmd.rate, local)
    unit = f"{profile.local.symbol}/$"
    if cmd.purpose is None:
        raise NeedsChoice(
            f"¿Para qué quieres usar la tasa de *{shown} {unit}*?\n"
            f"• *Para cobrar*: calcular lo que cobras a tus clientes\n"
            f"• *Tasa real*: calcular cuánto te costaron realmente tus {profile.local.plural}",
            [command_option("Para cobrar", cmd, purpose=RateScope.CHARGE),
             command_option(f"Tasa real de mis {profile.local.symbol}", cmd, purpose=RateScope.FUNDS),
             command_option("Para ambas cosas", cmd, purpose=RateScope.BOTH)],
        )
    snap = await deps.rates.snapshot(ctx.business_id, session)
    data: dict[str, Any] = {"rate": shown, "unit": unit, "label": cmd.label, "purpose": cmd.purpose.value,
                            "official": fmt_rate(snap.official, local) if snap.official else None,
                            "official_label": profile.official_rate_label}
    affected: list[EntityRef] = []
    if cmd.purpose in (RateScope.CHARGE, RateScope.BOTH):
        new, previous = await replace_custom_rate(session, ctx, business.id, RatePurpose.CHARGE, cmd.rate, cmd.label)
        if business.charge_rate_mode != ChargeRateMode.CUSTOM.value:
            data["previous_mode"] = business.charge_rate_mode
            business.charge_rate_mode = ChargeRateMode.CUSTOM.value
            bump(business)
        # Fijarla hoy es confirmarla por hoy: no se vuelve a preguntar si se mantiene.
        business.charge_rate_confirmed_on = local_today(business.timezone, ctx.now)
        data["previous_charge"] = fmt_rate(Decimal(previous["rate"]), local) if previous else None
        affected.append(EntityRef(EntityType.CUSTOM_RATE, new.id))
    if cmd.purpose in (RateScope.FUNDS, RateScope.BOTH):
        new, previous = await replace_custom_rate(session, ctx, business.id, RatePurpose.FUNDS, cmd.rate, cmd.label)
        data["previous_funds"] = fmt_rate(Decimal(previous["rate"]), local) if previous else None
        affected.append(EntityRef(EntityType.CUSTOM_RATE, new.id))
    return CommandResult(kind=cmd.kind, data=data, affected=affected,
                         guard_reason=unusual_rate_reason(cmd.rate, snap.official, local, profile.official_rate_label))


class SetChargeRateMode(Command):
    """Con qué tasa cobra el negocio: oficial, mercado o su tasa propia (que debe existir). No toca movimientos pasados."""

    kind: ClassVar[str] = "set_charge_rate_mode"

    mode: ChargeRateMode


@command_handler(SetChargeRateMode)
async def set_charge_rate_mode(cmd: SetChargeRateMode, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    business = await lock_business(session, ctx.business_id)
    previous = business.charge_rate_mode
    if await apply_charge_mode(session, ctx, business, cmd.mode):
        bump(business)
    await session.flush()
    rates = await deps.rates.effective(session, ctx.business_id)
    profile = profile_of(business)
    return CommandResult(kind=cmd.kind, data={
        "mode": cmd.mode.value, "previous_mode": previous, "label": profile.applied_rate_label(rates.applied_source),
        "rate": fmt_rate(rates.applied, business.local_currency) if rates.applied else None,
        "unit": f"{profile.local.symbol}/$"})
