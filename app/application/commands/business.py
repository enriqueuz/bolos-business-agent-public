"""Datos del negocio (onboarding y panel)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.commands.rates import apply_charge_mode
from app.application.common import bump, check_version, get_business
from app.application.context import EntityRef, ExecContext
from app.application.rates import RateService
from app.db.models import Business
from app.domain.countries import country_profile
from app.domain.enums import (
    AuditAction,
    ChargeRateMode,
    CurrencyView,
    EntityType,
    OnboardingState,
    PaymentMethod,
    RatePurpose,
)
from app.domain.errors import ValidationFailed


class UpdateBusiness(Command):
    kind: ClassVar[str] = "update_business"

    expected_version: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=160)
    what_sells: str | None = Field(default=None, max_length=500)
    display_currency: CurrencyView | None = None
    charge_rate_mode: ChargeRateMode | None = None
    payment_methods: list[PaymentMethod] | None = None
    onboarding_state: OnboardingState | None = None
    welcomed: bool | None = None
    onboarding_draft: dict[str, Any] | None = None
    # Solo durante el onboarding (antes del primer registro): cambiar el país después rompería los montos congelados.
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    declared_country: str | None = Field(default=None, max_length=80)


async def _apply_display_currency(session: AsyncSession, business: Business, view: CurrencyView) -> None:
    """Transición hasta la Parte 3 (pregunta de la tasa para cobrar): el onboarding y el panel siguen ofreciendo
    "Dólares oficial / Dólares mercado / moneda local". Elegir dólares a una tasa fija también la tasa para cobrar
    (misma regla que el backfill de la migración 0008); los reportes en dólares siempre usan la pata aplicada."""
    if view == CurrencyView.USD_OFFICIAL:
        business.charge_rate_mode = ChargeRateMode.OFFICIAL.value
    elif view == CurrencyView.USD_MARKET:
        has_custom = await RateService.current_custom(session, business.id, RatePurpose.CHARGE) is not None
        business.charge_rate_mode = (ChargeRateMode.CUSTOM if has_custom else ChargeRateMode.MARKET).value
    business.display_currency = (CurrencyView.LOCAL if view == CurrencyView.LOCAL else CurrencyView.USD_APPLIED).value


@command_handler(UpdateBusiness)
async def update_business(cmd: UpdateBusiness, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    business = await get_business(session, ctx.business_id)
    check_version(business, cmd.expected_version, "negocio")
    before = audit.snapshot(business)
    if cmd.country_code is not None:
        if business.onboarding_state == OnboardingState.DONE.value:
            raise ValidationFailed("El país del negocio no se puede cambiar después de configurarlo.")
        profile = country_profile(cmd.country_code)
        business.country_code, business.local_currency, business.timezone = profile.code, profile.local_currency, profile.timezone
    if cmd.declared_country is not None:
        business.declared_country = cmd.declared_country.strip()
    for field in ("name", "what_sells"):
        if (value := getattr(cmd, field)) is not None:
            setattr(business, field, value.strip())
    if cmd.display_currency is not None:
        await _apply_display_currency(session, business, cmd.display_currency)
    if cmd.charge_rate_mode is not None:
        await apply_charge_mode(session, ctx, business, cmd.charge_rate_mode)
    if cmd.payment_methods is not None:
        business.payment_methods = [m.value for m in cmd.payment_methods]
    if cmd.onboarding_state is not None:
        business.onboarding_state = cmd.onboarding_state.value
    if cmd.welcomed is not None:
        business.welcomed = cmd.welcomed
    if cmd.onboarding_draft is not None:
        business.onboarding_draft = cmd.onboarding_draft
    bump(business)
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.BUSINESS, entity_id=business.id, action=AuditAction.UPDATE,
                 before=before, after=audit.snapshot(business), command=cmd.kind)
    return CommandResult(kind=cmd.kind, data={"version": business.version},
                         affected=[EntityRef(EntityType.BUSINESS, business.id, version=business.version)])
