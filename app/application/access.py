"""Autorización por miembro para acciones y datos de verticales."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.context import ExecContext
from app.db.models import Business, BusinessMember
from app.domain.enums import BusinessVertical, MemberRole
from app.domain.errors import Ambiguous, NotFound, ValidationFailed


@dataclass(frozen=True, slots=True)
class BrokerageAccountScope:
    """Cuenta operativa autorizada; Casa es el negocio, no un miembro ficticio."""

    business: Business
    broker: BusinessMember | None

    @property
    def kind(self) -> str:
        return "house" if self.broker is None else "broker"

    @property
    def broker_member_id(self) -> UUID | None:
        return self.broker.id if self.broker else None

    @property
    def label(self) -> str:
        return "Casa" if self.broker is None else (self.broker.name or "Corredor")

    @property
    def is_house(self) -> bool:
        return self.broker is None


async def actor_member(session: AsyncSession, ctx: ExecContext) -> BusinessMember:
    if ctx.actor.member_id is None:
        raise ValidationFailed("Esta acción necesita un miembro autenticado.")
    member = await session.get(BusinessMember, ctx.actor.member_id)
    if member is None or member.business_id != ctx.business_id or not member.is_active:
        raise ValidationFailed("No tienes permiso para este negocio.")
    return member


async def require_vertical(session: AsyncSession, business_id: UUID, vertical: BusinessVertical) -> Business:
    business = await session.get(Business, business_id)
    if business is None:
        raise NotFound("Negocio no encontrado.")
    if business.vertical != vertical.value:
        raise ValidationFailed("Esta función no está habilitada para este negocio.")
    return business


async def effective_broker(
    session: AsyncSession,
    ctx: ExecContext,
    requested_broker_id: UUID | None,
) -> tuple[BusinessMember, BusinessMember]:
    """Devuelve (actor, cuenta). Un corredor nunca puede seleccionar otra cuenta."""
    await require_vertical(session, ctx.business_id, BusinessVertical.BROKERAGE)
    actor = await actor_member(session, ctx)
    role = MemberRole(actor.role)
    if role == MemberRole.BROKER:
        if requested_broker_id is not None and requested_broker_id != actor.id:
            raise ValidationFailed("Solo puedes operar y consultar tu propia cuenta.")
        return actor, actor
    if role != MemberRole.OWNER:
        raise ValidationFailed("No tienes permiso para operar cuentas de corredores.")
    if requested_broker_id is None:
        raise ValidationFailed("Indica el corredor.")
    broker = await session.get(BusinessMember, requested_broker_id)
    if broker is None or broker.business_id != ctx.business_id or broker.role != MemberRole.BROKER.value:
        raise NotFound("Corredor no encontrado.")
    return actor, broker


async def effective_brokerage_account(
    session: AsyncSession,
    ctx: ExecContext,
    requested_broker_id: UUID | None,
    *,
    allow_house: bool = True,
) -> tuple[BusinessMember, BrokerageAccountScope]:
    """Resuelve una cuenta por id: el owner cae en Casa y el broker siempre en sí mismo."""
    business = await require_vertical(session, ctx.business_id, BusinessVertical.BROKERAGE)
    actor = await actor_member(session, ctx)
    role = MemberRole(actor.role)
    if role == MemberRole.BROKER:
        if requested_broker_id is not None and requested_broker_id != actor.id:
            raise ValidationFailed("Solo puedes operar y consultar tu propia cuenta.")
        return actor, BrokerageAccountScope(business, actor)
    if role != MemberRole.OWNER:
        raise ValidationFailed("No tienes permiso para operar cuentas de corredores.")
    if requested_broker_id is None:
        if not allow_house:
            raise ValidationFailed("Indica el corredor.")
        return actor, BrokerageAccountScope(business, None)
    broker = await session.get(BusinessMember, requested_broker_id)
    if broker is None or broker.business_id != ctx.business_id or broker.role != MemberRole.BROKER.value:
        raise NotFound("Corredor no encontrado.")
    return actor, BrokerageAccountScope(business, broker)


async def existing_brokerage_account(
    session: AsyncSession,
    ctx: ExecContext,
    broker_member_id: UUID | None,
) -> tuple[BusinessMember, BrokerageAccountScope]:
    """Autoriza el ámbito ya persistido sin confundir Casa con una selección omitida."""
    business = await require_vertical(session, ctx.business_id, BusinessVertical.BROKERAGE)
    actor = await actor_member(session, ctx)
    role = MemberRole(actor.role)
    if role == MemberRole.BROKER:
        if broker_member_id != actor.id:
            raise ValidationFailed("Solo puedes operar y consultar tu propia cuenta.")
        return actor, BrokerageAccountScope(business, actor)
    if role != MemberRole.OWNER:
        raise ValidationFailed("No tienes permiso para operar cuentas de corredores.")
    if broker_member_id is None:
        return actor, BrokerageAccountScope(business, None)
    broker = await session.get(BusinessMember, broker_member_id)
    if broker is None or broker.business_id != ctx.business_id or broker.role != MemberRole.BROKER.value:
        raise NotFound("Corredor no encontrado.")
    return actor, BrokerageAccountScope(business, broker)


async def resolve_broker_account(
    session: AsyncSession,
    ctx: ExecContext,
    broker_name: str | None,
) -> tuple[BusinessMember, BusinessMember]:
    """Como ``effective_broker``, pero el owner usa nombre y el corredor no puede suplantar a otro."""
    await require_vertical(session, ctx.business_id, BusinessVertical.BROKERAGE)
    actor = await actor_member(session, ctx)
    if actor.role == MemberRole.BROKER.value:
        if broker_name and actor.name and broker_name.casefold().strip() != actor.name.casefold().strip():
            raise ValidationFailed("Solo puedes operar y consultar tu propia cuenta.")
        return actor, actor
    if actor.role != MemberRole.OWNER.value:
        raise ValidationFailed("No tienes permiso para operar cuentas de corredores.")
    if not broker_name:
        raise ValidationFailed("Indica el corredor.")
    term = broker_name.strip().casefold()
    exact = (await session.execute(
        select(BusinessMember).where(
            BusinessMember.business_id == ctx.business_id,
            BusinessMember.role == MemberRole.BROKER.value,
            func.lower(BusinessMember.name) == term,
        )
    )).scalars().all()
    if len(exact) == 1:
        return actor, exact[0]
    candidates = (await session.execute(
        select(BusinessMember).where(
            BusinessMember.business_id == ctx.business_id,
            BusinessMember.role == MemberRole.BROKER.value,
            func.lower(BusinessMember.name).contains(term),
        ).order_by(BusinessMember.name).limit(6)
    )).scalars().all()
    if len(candidates) == 1:
        return actor, candidates[0]
    if candidates:
        raise Ambiguous("Hay varios corredores parecidos.", entity="corredor", candidates=[m.name for m in candidates])
    raise NotFound("Corredor no encontrado.")


async def resolve_brokerage_account(
    session: AsyncSession,
    ctx: ExecContext,
    broker_name: str | None,
    *,
    allow_house: bool = True,
) -> tuple[BusinessMember, BrokerageAccountScope]:
    """Resuelve por nombre; omitirlo selecciona Casa para el owner y la propia para el broker."""
    business = await require_vertical(session, ctx.business_id, BusinessVertical.BROKERAGE)
    actor = await actor_member(session, ctx)
    if actor.role == MemberRole.BROKER.value:
        if broker_name and actor.name and broker_name.casefold().strip() != actor.name.casefold().strip():
            raise ValidationFailed("Solo puedes operar y consultar tu propia cuenta.")
        return actor, BrokerageAccountScope(business, actor)
    if actor.role != MemberRole.OWNER.value:
        raise ValidationFailed("No tienes permiso para operar cuentas de corredores.")
    if not broker_name:
        if not allow_house:
            raise ValidationFailed("Indica el corredor.")
        return actor, BrokerageAccountScope(business, None)
    term = broker_name.strip().casefold()
    if term == "casa":
        if not allow_house:
            raise ValidationFailed("Esta acción solo aplica a corredores.")
        return actor, BrokerageAccountScope(business, None)
    exact = (await session.execute(
        select(BusinessMember).where(
            BusinessMember.business_id == ctx.business_id,
            BusinessMember.role == MemberRole.BROKER.value,
            func.lower(BusinessMember.name) == term,
        )
    )).scalars().all()
    if len(exact) == 1:
        return actor, BrokerageAccountScope(business, exact[0])
    candidates = (await session.execute(
        select(BusinessMember).where(
            BusinessMember.business_id == ctx.business_id,
            BusinessMember.role == MemberRole.BROKER.value,
            func.lower(BusinessMember.name).contains(term),
        ).order_by(BusinessMember.name).limit(6)
    )).scalars().all()
    if len(candidates) == 1:
        return actor, BrokerageAccountScope(business, candidates[0])
    if candidates:
        raise Ambiguous("Hay varios corredores parecidos.", entity="corredor", candidates=[m.name for m in candidates])
    raise NotFound("Corredor no encontrado.")


async def lock_brokerage_account(session: AsyncSession, account: BrokerageAccountScope) -> None:
    """Serializa escrituras por cuenta sin inventar un miembro para Casa."""
    if account.broker is None:
        await session.execute(select(Business.id).where(Business.id == account.business.id).with_for_update())
    else:
        await session.execute(
            select(BusinessMember.id).where(BusinessMember.id == account.broker.id).with_for_update()
        )
