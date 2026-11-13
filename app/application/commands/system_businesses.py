"""Edición interna de casas de cambio; solo invocable desde contexto confiable de sistema."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.commands.brokerage import UpdateStaffMember, apply_broker_access, apply_staff_profile
from app.application.common import bump
from app.application.context import ExecContext
from app.application.identity import normalize_phone
from app.db.models import Business, BusinessMember
from app.domain.brokerage import normalize_broker_abbreviation
from app.domain.enums import ActorType, AuditAction, BusinessVertical, EntityType, MemberRole
from app.domain.errors import Conflict, NotFound, ValidationFailed


class SystemRenameBusiness(Command):
    kind: ClassVar[str] = "system_rename_business"
    name: str = Field(min_length=1, max_length=160)


class SystemCreateMember(Command):
    kind: ClassVar[str] = "system_create_member"
    name: str = Field(min_length=2, max_length=160)
    phone: str = Field(min_length=8, max_length=20)
    role: MemberRole
    abbreviation: str | None = Field(default=None, max_length=12)


class SystemUpdateMember(UpdateStaffMember):
    kind: ClassVar[str] = "system_update_member"
    is_active: bool | None = None


async def _business(session: AsyncSession, ctx: ExecContext) -> Business:
    if ctx.actor.type != ActorType.SYSTEM:
        raise ValidationFailed("Esta acción requiere administración del sistema.")
    business = (await session.execute(
        select(Business).where(Business.id == ctx.business_id).with_for_update()
    )).scalar_one_or_none()
    if business is None:
        raise NotFound("Negocio no encontrado.")
    if business.vertical != BusinessVertical.BROKERAGE.value:
        raise ValidationFailed("Esta edición solo está disponible para casas de cambio.")
    return business


def member_data(member: BusinessMember) -> dict:
    return {"id": str(member.id), "name": member.name, "phone": member.phone,
            "role": member.role, "abbreviation": member.abbreviation, "is_active": member.is_active}


def _audit(session: AsyncSession, ctx: ExecContext, command: str, before: dict | None, after: dict,
           *, creating: bool = False) -> None:
    # Los miembros son parte de la configuración del negocio; no se inventa un tipo de entidad contable.
    audit.record(session, ctx, entity_type=EntityType.BUSINESS, entity_id=ctx.business_id,
                 action=AuditAction.CREATE if creating else AuditAction.UPDATE,
                 before=before, after=after, command=command)


@command_handler(SystemRenameBusiness)
async def rename_business(cmd: SystemRenameBusiness, ctx: ExecContext, session: AsyncSession,
                          deps: CommandDeps) -> CommandResult:
    business = await _business(session, ctx)
    before = audit.snapshot(business)
    business.name = cmd.name
    bump(business)
    _audit(session, ctx, cmd.kind, before, audit.snapshot(business))
    return CommandResult(kind=cmd.kind, data={"id": str(business.id), "name": business.name})


@command_handler(SystemCreateMember)
async def create_member(cmd: SystemCreateMember, ctx: ExecContext, session: AsyncSession,
                        deps: CommandDeps) -> CommandResult:
    # Mismo orden que el bootstrap y el alta por WhatsApp: teléfono antes de bloquear negocio.
    if ctx.actor.type != ActorType.SYSTEM:
        raise ValidationFailed("Esta acción requiere administración del sistema.")
    phone = normalize_phone(cmd.phone)
    if not phone.isascii() or not 8 <= len(phone) <= 15 or phone.startswith("0"):
        raise ValidationFailed("Indica un número de WhatsApp válido con código de país.")
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:phone, 0))"), {"phone": phone})
    await _business(session, ctx)
    if (await session.execute(select(BusinessMember.id).where(BusinessMember.phone == phone).limit(1))).first():
        raise Conflict("Ese número ya pertenece a un negocio. Selecciona un número libre.")
    abbreviation = None
    if cmd.role == MemberRole.BROKER:
        if not cmd.abbreviation:
            raise ValidationFailed("Cada corredor necesita una abreviación.")
        abbreviation = normalize_broker_abbreviation(cmd.abbreviation)
        existing = await session.execute(select(BusinessMember.id).where(
            BusinessMember.business_id == ctx.business_id,
            func.lower(BusinessMember.abbreviation) == abbreviation.lower(),
        ).limit(1))
        if existing.first():
            raise Conflict("Esa abreviación ya pertenece a otro corredor.")
    elif cmd.abbreviation:
        raise ValidationFailed("La abreviación solo aplica a corredores.")
    member = BusinessMember(business_id=ctx.business_id, name=cmd.name, phone=phone,
                            role=cmd.role.value, abbreviation=abbreviation)
    session.add(member)
    await session.flush()
    data = member_data(member)
    _audit(session, ctx, cmd.kind, None, {"member": data}, creating=True)
    return CommandResult(kind=cmd.kind, data=data)


@command_handler(SystemUpdateMember)
async def update_member(cmd: SystemUpdateMember, ctx: ExecContext, session: AsyncSession,
                        deps: CommandDeps) -> CommandResult:
    await _business(session, ctx)
    member = (await session.execute(select(BusinessMember).where(
        BusinessMember.id == cmd.member_id, BusinessMember.business_id == ctx.business_id,
    ).with_for_update(of=BusinessMember))).scalar_one_or_none()
    if member is None:
        raise NotFound("Miembro no encontrado.")
    before = member_data(member)
    await apply_staff_profile(session, ctx.business_id, member, cmd)
    guard = None
    if "is_active" in cmd.model_fields_set:
        if member.role != MemberRole.BROKER.value:
            raise ValidationFailed("Solo se puede cambiar el acceso de corredores.")
        if cmd.is_active is None:
            raise ValidationFailed("Indica si el acceso está activo.")
        guard = await apply_broker_access(session, ctx.business_id, member, cmd.is_active)
    await session.flush()
    data = member_data(member)
    _audit(session, ctx, cmd.kind, {"member": before}, {"member": data})
    return CommandResult(kind=cmd.kind, data=data, guard_reason=guard)
