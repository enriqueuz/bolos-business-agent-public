"""Clientes: altas/ediciones desde el panel y fusión de duplicados."""

from __future__ import annotations

from typing import ClassVar, Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.access import actor_member, effective_broker
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.commands.receivables import reallocate_customer_payments
from app.application.common import bump, check_version, lock_customers
from app.application.context import EntityRef, ExecContext
from app.application.customer_identity import assign_nickname, create_customer, lock_customer_identity
from app.application.resolvers import resolve_customer
from app.db.models import (
    BrokerMovement,
    Business,
    Customer,
    CustomerBalanceAdjustment,
    CustomerPayment,
    Receivable,
    Sale,
)
from app.domain.enums import AuditAction, BusinessVertical, EntityType, MemberRole
from app.domain.errors import Conflict, NeedsChoice, NotFound, ValidationFailed
from app.domain.text import clean_display_name, normalize_name


async def _lock_brokerage_business(session: AsyncSession, ctx: ExecContext) -> None:
    business = await session.get(Business, ctx.business_id)
    if business.vertical == BusinessVertical.BROKERAGE.value:
        await session.execute(select(Business.id).where(Business.id == ctx.business_id).with_for_update())


async def _get(session: AsyncSession, ctx: ExecContext, customer_id: UUID, *, allow_global_profile: bool = False) -> Customer:
    c = await session.get(Customer, customer_id)
    if c is None or c.business_id != ctx.business_id or c.deleted_at is not None:
        raise NotFound("Cliente no encontrado.")
    business = await session.get(Business, ctx.business_id)
    if business.vertical == BusinessVertical.BROKERAGE.value:
        actor = await actor_member(session, ctx)
        if actor.role == MemberRole.BROKER.value:
            own = c.classification == "personal" and c.owner_broker_member_id == actor.id
            global_profile = allow_global_profile and c.classification == "global"
            if not (own or global_profile):
                raise NotFound("Cliente no encontrado.")
    return c


class UpsertCustomer(Command):
    kind: ClassVar[str] = "upsert_customer"

    customer_id: UUID | None = None
    expected_version: int | None = None
    name: str | None = Field(default=None, max_length=160)
    nickname: str | None = Field(default=None, max_length=160)
    phone: str | None = Field(default=None, max_length=32)
    notes: str | None = None
    customer_classification: Literal["global", "personal"] | None = None
    owner_broker_member_id: UUID | None = None


@command_handler(UpsertCustomer)
async def upsert_customer(cmd: UpsertCustomer, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    business = await session.get(Business, ctx.business_id)
    await lock_customer_identity(session, ctx.business_id)
    if cmd.customer_id:
        profile_only = bool(cmd.model_fields_set & {"name", "nickname"}) and cmd.model_fields_set <= {
            "customer_id", "expected_version", "name", "nickname",
        }
        customer = await _get(session, ctx, cmd.customer_id, allow_global_profile=profile_only)
        check_version(customer, cmd.expected_version, "cliente")
        if ((cmd.customer_classification is not None and cmd.customer_classification != customer.classification)
            or (cmd.owner_broker_member_id is not None and cmd.owner_broker_member_id != customer.owner_broker_member_id)):
            raise ValidationFailed("La clasificación y propietario son inmutables.")
        before, action = audit.snapshot(customer), AuditAction.UPDATE
        if "name" in cmd.model_fields_set:
            customer.name = clean_display_name(cmd.name) if cmd.name else None
            customer.normalized_name = normalize_name(customer.name or "")
        if "nickname" in cmd.model_fields_set:
            await assign_nickname(session, customer, cmd.nickname)
        if not customer.name and not customer.nickname:
            raise ValidationFailed("Indica al menos un nombre completo o un apodo.")
        bump(customer)
    else:
        classification, owner = None, None
        if business.vertical == BusinessVertical.BROKERAGE.value:
            if cmd.customer_classification is None:
                payload = cmd.model_dump(mode="json", exclude_unset=True)
                raise NeedsChoice("¿Qué tipo de cliente es?", [
                    {"title": label, "action": "command", "command_kind": cmd.kind,
                     "command_payload": {**payload, "customer_classification": scope}}
                    for label, scope in [("Global", "global"), ("Personal", "personal")]])
            classification = cmd.customer_classification
            if classification == "personal":
                _, broker = await effective_broker(session, ctx, cmd.owner_broker_member_id)
                owner = broker.id
        customer = await create_customer(session, ctx.business_id, name=cmd.name, nickname=cmd.nickname,
                                         automatic="nickname" not in cmd.model_fields_set,
                                         classification=classification, owner_broker_member_id=owner)
        before, action = None, AuditAction.CREATE
    if "phone" in cmd.model_fields_set:
        customer.phone = cmd.phone
    if "notes" in cmd.model_fields_set:
        customer.notes = cmd.notes
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.CUSTOMER, entity_id=customer.id, action=action, before=before,
                 after=audit.snapshot(customer), command=cmd.kind)
    return CommandResult(kind=cmd.kind, data={"customer_id": str(customer.id), "version": customer.version, "customer": customer.display_name,
                               "name": customer.name, "nickname": customer.nickname},
                         affected=[EntityRef(EntityType.CUSTOMER, customer.id, version=customer.version)])


class MergeCustomers(Command):
    """Mueve ventas, fiados y abonos de `source` a `target` y archiva `source`."""

    kind: ClassVar[str] = "merge_customers"

    source_customer_id: UUID
    target_customer_id: UUID


@command_handler(MergeCustomers)
async def merge_customers(cmd: MergeCustomers, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    await _lock_brokerage_business(session, ctx)
    if cmd.source_customer_id == cmd.target_customer_id:
        raise ValidationFailed("No puedes fusionar un cliente consigo mismo.")
    await lock_customers(session, ctx.business_id, cmd.source_customer_id, cmd.target_customer_id)
    source = await _get(session, ctx, cmd.source_customer_id)
    target = await _get(session, ctx, cmd.target_customer_id)
    if (source.classification, source.owner_broker_member_id) != (target.classification, target.owner_broker_member_id):
        raise ValidationFailed("Solo se fusionan clientes de la misma clasificación y propietario.")
    before = audit.snapshot(source)
    moved = {}
    for model in (Sale, Receivable, CustomerPayment, BrokerMovement, CustomerBalanceAdjustment):
        res = await session.execute(
            update(model).where(model.business_id == ctx.business_id, model.customer_id == source.id)
            .values(customer_id=target.id, version=model.version + 1)
        )
        moved[model.__tablename__] = res.rowcount
    source.merged_into_id = target.id
    source.deleted_at = ctx.now
    bump(source)
    await session.flush()
    await reallocate_customer_payments(session, ctx, target.id)
    audit.record(session, ctx, entity_type=EntityType.CUSTOMER, entity_id=source.id, action=AuditAction.DELETE,
                 before=before, after={"merged_into": str(target.id), "moved": moved}, command=cmd.kind)
    return CommandResult(kind=cmd.kind, data={"merged_into": target.display_name, "moved": moved},
                         affected=[EntityRef(EntityType.CUSTOMER, target.id)])


class DeleteCustomer(Command):
    kind: ClassVar[str] = "delete_customer"

    customer_id: UUID
    expected_version: int | None = None


@command_handler(DeleteCustomer)
async def delete_customer(cmd: DeleteCustomer, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    await _lock_brokerage_business(session, ctx)
    customer = await _get(session, ctx, cmd.customer_id)
    check_version(customer, cmd.expected_version, "cliente")
    for model in (Sale, Receivable, CustomerPayment, BrokerMovement, CustomerBalanceAdjustment):
        count = (await session.execute(
            select(func.count()).select_from(model).where(model.customer_id == customer.id, model.deleted_at.is_(None))
        )).scalar_one()
        if count:
            raise Conflict("Este cliente tiene movimientos; fusiónalo con otro en vez de borrarlo.")
    before = audit.snapshot(customer)
    customer.deleted_at = ctx.now
    bump(customer)
    audit.record(session, ctx, entity_type=EntityType.CUSTOMER, entity_id=customer.id, action=AuditAction.DELETE,
                 before=before, after=None, command=cmd.kind)
    return CommandResult(kind=cmd.kind, data={"deleted": customer.display_name})


class EditCustomerProfile(Command):
    """Resolve a customer reference and edit identifiers only, never create a customer."""
    kind: ClassVar[str] = "edit_customer_profile"
    customer_name: str = Field(min_length=1, max_length=160)
    customer_id: UUID | None = None
    name: str | None = Field(default=None, max_length=160)
    nickname: str | None = Field(default=None, max_length=160)


@command_handler(EditCustomerProfile)
async def edit_customer_profile(cmd: EditCustomerProfile, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    await lock_customer_identity(session, ctx.business_id)
    business = await session.get(Business, ctx.business_id)
    actor = await actor_member(session, ctx)
    if (actor.role not in {MemberRole.OWNER.value, MemberRole.BROKER.value}
        or business.vertical != "brokerage" and actor.role != MemberRole.OWNER.value):
        raise ValidationFailed("No tienes permiso para editar clientes.")
    customer, _ = await resolve_customer(session, ctx.business_id, cmd.customer_name,
        customer_id=cmd.customer_id, create_if_missing=False, brokerage=business.vertical == "brokerage",
        visible_broker_id=actor.id if actor.role == "broker" else None)
    changes = cmd.model_dump(include={"name", "nickname"}, exclude_unset=True)
    if not changes:
        raise ValidationFailed("Indica el nombre o apodo que quieres cambiar.")
    previous = customer.nickname
    result = await upsert_customer(UpsertCustomer(customer_id=customer.id, expected_version=customer.version, **changes), ctx, session, deps)
    result.kind = cmd.kind
    result.data.update(previous_nickname=previous, nickname_changed="nickname" in changes, name_changed="name" in changes)
    return result
