"""Escrituras del ledger de corredores."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, ClassVar, Literal
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.access import (
    BrokerageAccountScope,
    actor_member,
    effective_brokerage_account,
    existing_brokerage_account,
    lock_brokerage_account,
    resolve_brokerage_account,
)
from app.application.attachments import associate_attachment, retire_movement_attachment
from app.application.brokerage_money import BrokerDecimal
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.commands.receivables import allocate_payment, reallocate_customer_payments
from app.application.common import bump, check_version, customer_balance, receivable_paid
from app.application.context import EntityRef, ExecContext
from app.application.identity import normalize_phone
from app.application.resolvers import resolve_customer
from app.application.summaries import payment_summary, receivable_summary
from app.db.models import (
    BrokerAttachment,
    BrokerMovement,
    Business,
    BusinessMember,
    Customer,
    CustomerPayment,
    PaymentAllocation,
    Receivable,
)
from app.domain.brokerage import (
    DEFAULT_COMMISSION_RATE,
    account_purchase_effect,
    default_broker_abbreviation,
    normalize_broker_abbreviation,
    operation_credit,
    operation_money,
    purchase_excess,
    q_money,
)
from app.domain.dates import occurred_at_for, validate_business_date
from app.domain.enums import AuditAction, EntityType, MemberRole
from app.domain.errors import (
    Conflict,
    MissingBrokerBolivars,
    MissingBrokerPurchaseCustomer,
    NeedsChoice,
    NotFound,
    ValidationFailed,
)
from app.domain.money import fmt_amount


class BrokerCommand(Command):
    # Trusted conversation context, injected by the tool adapter and preserved in frozen actions.
    # Never part of a model-visible schema or a public panel request body.
    origin_message_id: UUID | None = None
    # Trusted adapter evidence, frozen across choices/confirmations; never model-visible inputs.
    money_evidence: dict[str, dict] = Field(default_factory=dict)
    draft_operation_id: UUID | None = None
    # Set only by server-generated settlement choices, never a model-visible tool argument.
    purchase_settlement_confirmed: bool = False
    # Settlement choices may change the command type, but not the original draft's execution identity.
    draft_origin_command_kind: Literal["record_broker_purchase", "record_pending_broker_purchase", "record_broker_operation"] | None = None


async def _lock_customer_write(session: AsyncSession, ctx: ExecContext) -> None:
    # One deterministic lock order for the pilot: business -> customer/account -> movement.
    # It also prevents two simultaneous first-use classifications creating duplicate identities.
    await session.execute(select(Business.id).where(Business.id == ctx.business_id).with_for_update())


def _portfolio_broker(customer: Customer | None) -> UUID | None:
    return customer.owner_broker_member_id if customer and customer.classification == "personal" else None


async def _classified_customer(session: AsyncSession, ctx: ExecContext, account: BrokerageAccountScope,
                               cmd: Command, *, create: bool = True) -> tuple[Customer, bool]:
    actor = await actor_member(session, ctx)
    classification = getattr(cmd, "customer_classification", None)
    payload = cmd.model_dump(mode="json", exclude_unset=True)
    if classification == "personal" and account.is_house:
        brokers = (await session.execute(select(BusinessMember).where(
            BusinessMember.business_id == ctx.business_id, BusinessMember.role == MemberRole.BROKER.value
        ).order_by(BusinessMember.name))).scalars().all()
        raise NeedsChoice("¿A qué corredor pertenece el cliente personal?", [
            {"title": broker.name or "Corredor", "action": "command", "command_kind": cmd.kind,
             "command_payload": {**payload, "broker_id": str(broker.id)}} for broker in brokers])
    try:
        customer, created = await resolve_customer(
            session, ctx.business_id, cmd.customer_name, customer_id=cmd.customer_id,
            create_if_missing=create, force_new=getattr(cmd, "force_new_customer", False),
            brokerage=True, visible_broker_id=actor.id if actor.role == MemberRole.BROKER.value else None,
            classification=classification, owner_broker_member_id=account.broker_member_id,
        )
    except NotFound as exc:
        if not create or not exc.details.get("classification_required"):
            raise
        raise NeedsChoice(f"No existe ningún cliente {cmd.customer_name} visible. ¿Qué tipo de cliente es?", [
            {"title": label, "action": "command", "command_kind": cmd.kind,
             "command_payload": {**payload, "customer_classification": scope}}
            for label, scope in [("Global", "global"), ("Personal", "personal")]
        ]) from exc
    if customer.classification == "personal" and customer.owner_broker_member_id != account.broker_member_id:
        raise ValidationFailed("Selecciona el corredor propietario para operar este cliente personal.")
    if created:
        audit.record(session, ctx, entity_type=EntityType.CUSTOMER, entity_id=customer.id,
                     action=AuditAction.CREATE, before=None, after=audit.snapshot(customer), command=cmd.kind)
    return customer, created


def _monetary_fields(movement: BrokerMovement) -> dict:
    status = "complete" if movement.operation_amount_bs is not None else "incomplete"
    return {"operation_amount_bs": str(movement.operation_amount_bs) if movement.operation_amount_bs is not None else None,
            "exchange_rate": str(movement.exchange_rate) if movement.exchange_rate is not None else None,
            "multicurrency_status": status if movement.kind == "operation" else None,
            "received": fmt_amount(movement.received_amount, "USD") if movement.received_amount is not None else None,
            "excess": fmt_amount(movement.excess or Decimal("0"), "USD"),
            "balance_effect": fmt_amount(movement.balance_delta, "USD"),
            "benefit": fmt_amount((movement.excess or Decimal("0")) + (movement.commission or Decimal("0")), "USD")}


def _require_operation_money(cmd: Command) -> None:
    usd = getattr(cmd, "operation_amount", None) or cmd.amount
    if operation_money(usd, cmd.operation_amount_bs, cmd.exchange_rate).amount_bs is None:
        raise MissingBrokerBolivars()


def _set_operation_money(movement: BrokerMovement, cmd: Command, ctx: ExecContext, *, editing: bool = False) -> None:
    changed = cmd.model_fields_set
    bs, rate = cmd.operation_amount_bs, cmd.exchange_rate
    if editing:
        if not ({"amount", "delivered_amount", "operation_amount_bs", "exchange_rate"} & changed):
            return
        if "operation_amount_bs" in changed:
            rate = cmd.exchange_rate if "exchange_rate" in changed else movement.exchange_rate if bs is None else None
        elif "exchange_rate" in changed:
            bs = movement.operation_amount_bs if rate is None else None
        elif movement.monetary_basis == "usd_rate":
            rate, bs = movement.exchange_rate, None
        else:
            bs, rate = movement.operation_amount_bs, None
    money = operation_money(movement.operation_amount, bs, rate)
    if money.amount_bs is None and (not editing or movement.operation_amount_bs is not None):
        raise MissingBrokerBolivars()
    movement.operation_amount_bs, movement.exchange_rate = money.amount_bs, money.exchange_rate
    movement.monetary_basis, movement.exchange_rate_source = money.basis, money.rate_source
    provenance = dict(movement.monetary_provenance or {})
    origin = {"message_id": str(cmd.origin_message_id or ctx.source_message_id) if cmd.origin_message_id or ctx.source_message_id else None,
              "actor_member_id": str(ctx.actor.member_id), "channel": ctx.actor.type.value}
    from_attachment = getattr(cmd, "monetary_from_attachment", False)
    if from_attachment:
        if not cmd.attachment_upload_id:
            raise ValidationFailed("El origen comprobante requiere una carga válida.")
        origin["attachment_id"] = str(cmd.attachment_upload_id)
    for field, value, derived in [("operation_amount_bs", money.amount_bs, money.basis == "usd_rate"),
                                   ("exchange_rate", money.exchange_rate, money.rate_source == "calculated")]:
        if not editing or field in changed or derived:
            provenance[field] = {**origin, **cmd.money_evidence.get(field, {}), "source": "unknown" if value is None else "calculated" if derived else cmd.money_evidence.get(field, {}).get("source", "attachment" if from_attachment else "explicit"),
                                 "value": str(value) if value is not None else None}
    if not editing or "amount" in changed:
        provenance["operation_amount"] = {**origin, **cmd.money_evidence.get("operation_amount", {}), "source": "explicit", "value": str(movement.operation_amount)}
    movement.monetary_provenance = provenance


def _commission_rate(settings: dict) -> Decimal:
    try:
        value = Decimal(str(settings.get("broker_commission_rate", DEFAULT_COMMISSION_RATE)))
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailed("La tasa de comisión del negocio no es válida.") from exc
    if value < 0 or value > Decimal("1"):
        raise ValidationFailed("La tasa de comisión debe estar entre 0% y 100%.")
    return value


async def _balance(session: AsyncSession, business_id, broker_id) -> Decimal:
    value = (await session.execute(
        select(func.coalesce(func.sum(BrokerMovement.balance_delta), 0)).where(
            BrokerMovement.business_id == business_id,
            BrokerMovement.broker_member_id == broker_id,
            BrokerMovement.deleted_at.is_(None),
        )
    )).scalar_one()
    return q_money(Decimal(value))


def _summary(
    movement: BrokerMovement, account: BrokerageAccountScope, *, amount: Decimal | None = None
) -> str:
    label = "Ajuste de saldo" if movement.kind == "adjustment" else "salida de cuenta" if movement.kind == "out" else "pago entregado" if movement.kind == "customer_payout" else "abono de cliente" if movement.related_payment_id else "abono a cuenta"
    if movement.kind == "operation":
        label = "compra" if movement.operation_type == "buy" else "venta"
    display_amount = amount if amount is not None else (
        movement.operation_amount if movement.kind == "operation" else movement.customer_payout_amount if movement.kind == "customer_payout" else movement.balance_delta
    )
    return f"{label} | {account.label} | {fmt_amount(display_amount, 'USD')}"


async def _account(
    session: AsyncSession,
    ctx: ExecContext,
    broker_id: UUID | None,
    broker_name: str | None,
    *,
    allow_house: bool = True,
) -> tuple[BusinessMember, BrokerageAccountScope]:
    if broker_id is not None:
        return await effective_brokerage_account(session, ctx, broker_id, allow_house=allow_house)
    return await resolve_brokerage_account(session, ctx, broker_name, allow_house=allow_house)


async def _account_balance(session: AsyncSession, account: BrokerageAccountScope) -> Decimal | None:
    if account.is_house:
        return None
    return await _balance(session, account.business.id, account.broker_member_id)


def _balance_fields(
    balance: Decimal | None, account: BrokerageAccountScope, ctx: ExecContext,
) -> dict[str, Any]:
    return {
        "balance": fmt_amount(balance, "USD") if balance is not None else None,
        "balance_raw": str(balance) if balance is not None else None,
        "balance_is_own": account.broker_member_id is not None and account.broker_member_id == ctx.actor.member_id,
    }


async def _sync_purchase_settlements(
    session: AsyncSession,
    ctx: ExecContext,
    *,
    broker_id: UUID | None,
    customer_id: UUID,
    command: str,
) -> None:
    """Sincroniza lo recibido en compras a crédito con los abonos vigentes del cliente."""
    rows = (await session.execute(
        select(BrokerMovement, Receivable)
        .join(Receivable, Receivable.id == BrokerMovement.related_receivable_id)
        .where(
            BrokerMovement.business_id == ctx.business_id,
            BrokerMovement.portfolio_broker_member_id.is_not_distinct_from(broker_id),
            BrokerMovement.operation_type == "buy",
            BrokerMovement.related_receivable_id.is_not(None),
            BrokerMovement.deleted_at.is_(None),
            Receivable.customer_id == customer_id,
            Receivable.deleted_at.is_(None),
        )
    )).all()
    for movement, receivable in rows:
        paid = min(q_money(await receivable_paid(session, receivable.id)), movement.operation_amount)
        status = (
            "pending" if paid <= Decimal("0.005")
            else "settled" if paid >= movement.operation_amount - Decimal("0.005")
            else "partial"
        )
        if movement.delivered_amount == paid and movement.settlement_status == status:
            continue
        before = audit.snapshot(movement)
        movement.delivered_amount = paid
        movement.settlement_status = status
        bump(movement)
        await session.flush()
        audit.record(
            session,
            ctx,
            entity_type=EntityType.BROKER_MOVEMENT,
            entity_id=movement.id,
            action=AuditAction.UPDATE,
            before=before,
            after=audit.snapshot(movement),
            command=command,
        )


async def _abbreviation_taken(
    session: AsyncSession, business_id: UUID, abbreviation: str, *, excluding: UUID | None = None
) -> bool:
    stmt = select(BusinessMember.id).where(
        BusinessMember.business_id == business_id,
        BusinessMember.abbreviation.is_not(None),
        func.lower(BusinessMember.abbreviation) == abbreviation.casefold(),
    )
    if excluding:
        stmt = stmt.where(BusinessMember.id != excluding)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None


class CreateBrokerMember(Command):
    kind: ClassVar[str] = "create_broker_member"

    name: str = Field(min_length=2, max_length=160)
    phone: str = Field(min_length=8, max_length=20)
    abbreviation: str | None = Field(default=None, max_length=12)


@command_handler(CreateBrokerMember)
async def create_broker_member(
    cmd: CreateBrokerMember, ctx: ExecContext, session: AsyncSession, deps: CommandDeps
) -> CommandResult:
    del deps
    actor = await actor_member(session, ctx)
    if actor.role != "owner":
        raise ValidationFailed("Solo el dueño puede agregar corredores.")
    phone = normalize_phone(cmd.phone)
    if not 8 <= len(phone) <= 15:
        raise ValidationFailed("Indica un número de WhatsApp válido.")
    # Comparte la exclusión con alta de sistema y WhatsApp para evitar números duplicados.
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:phone, 0))"), {"phone": phone})
    await session.execute(select(Business.id).where(Business.id == ctx.business_id).with_for_update())
    if (await session.execute(select(BusinessMember.id).where(BusinessMember.phone == phone))).scalar_one_or_none():
        raise Conflict("Ese número ya pertenece a un miembro.")
    abbreviation = normalize_broker_abbreviation(
        cmd.abbreviation or default_broker_abbreviation(cmd.name)
    )
    if await _abbreviation_taken(session, ctx.business_id, abbreviation):
        raise Conflict("Esa abreviación ya pertenece a otro corredor.")
    member = BusinessMember(
        business_id=ctx.business_id,
        phone=phone,
        name=cmd.name.strip(),
        role="broker",
        abbreviation=abbreviation,
    )
    session.add(member)
    await session.flush()
    return CommandResult(
        kind=cmd.kind,
        data={
            "id": str(member.id),
            "name": member.name,
            "phone": member.phone,
            "role": member.role,
            "abbreviation": member.abbreviation,
            "is_active": member.is_active,
        },
    )


class UpdateStaffMember(Command):
    """Perfil del equipo; teléfono y rol no son editables."""

    kind: ClassVar[str] = "update_staff_member"

    member_id: UUID
    name: str | None = Field(default=None, min_length=2, max_length=160)
    abbreviation: str | None = Field(default=None, min_length=1, max_length=12)


class UpdateBrokerMember(UpdateStaffMember):
    kind: ClassVar[str] = "update_broker_member"


@command_handler(UpdateStaffMember)
@command_handler(UpdateBrokerMember)
async def update_broker_member(
    cmd: UpdateStaffMember, ctx: ExecContext, session: AsyncSession, deps: CommandDeps
) -> CommandResult:
    del deps
    actor = await actor_member(session, ctx)
    if type(cmd) is UpdateStaffMember and actor.role != "owner":
        raise ValidationFailed("Solo el dueño puede editar el equipo.")
    if actor.role != "owner" and actor.id != cmd.member_id:
        raise ValidationFailed("Solo puedes cambiar tu propia abreviación.")
    # Serializa la unicidad de abreviaciones también con las ediciones del administrador.
    await session.execute(select(Business.id).where(Business.id == ctx.business_id).with_for_update())
    member = (
        await session.execute(
            select(BusinessMember)
            .where(BusinessMember.id == cmd.member_id)
            .with_for_update(of=BusinessMember)
        )
    ).scalar_one_or_none()
    allowed_roles = {"broker"} if type(cmd) is UpdateBrokerMember else {"owner", "broker"}
    if member is None or member.business_id != ctx.business_id or member.role not in allowed_roles:
        raise NotFound("Miembro no encontrado.")
    changed = cmd.model_fields_set
    if actor.role != "owner" and changed != {"member_id", "abbreviation"}:
        raise ValidationFailed("Solo puedes cambiar tu propia abreviación.")
    await apply_staff_profile(session, ctx.business_id, member, cmd)
    await session.flush()
    return CommandResult(
        kind=cmd.kind,
        data={
            "id": str(member.id),
            "name": member.name,
            "phone": member.phone,
            "role": member.role,
            "abbreviation": member.abbreviation,
            "is_active": member.is_active,
        },
    )


async def apply_staff_profile(
    session: AsyncSession, business_id: UUID, member: BusinessMember, cmd: UpdateStaffMember
) -> None:
    """Reglas de perfil compartidas por Equipo y administración del sistema."""
    changed = cmd.model_fields_set
    if "name" in changed:
        if cmd.name is None:
            raise ValidationFailed("El nombre no puede quedar vacío.")
        member.name = cmd.name.strip()
    if "abbreviation" in changed:
        if member.role != "broker":
            raise ValidationFailed("La abreviación solo aplica a corredores.")
        if cmd.abbreviation is None:
            raise ValidationFailed("La abreviación no puede quedar vacía.")
        abbreviation = normalize_broker_abbreviation(cmd.abbreviation)
        if await _abbreviation_taken(session, business_id, abbreviation, excluding=member.id):
            raise Conflict("Esa abreviación ya pertenece a otro corredor.")
        member.abbreviation = abbreviation


class SetBrokerMemberActive(Command):
    kind: ClassVar[str] = "set_broker_member_active"

    member_id: UUID
    is_active: bool


@command_handler(SetBrokerMemberActive)
async def set_broker_member_active(
    cmd: SetBrokerMemberActive, ctx: ExecContext, session: AsyncSession, deps: CommandDeps
) -> CommandResult:
    del deps
    actor = await actor_member(session, ctx)
    if actor.role != "owner":
        raise ValidationFailed("Solo el dueño puede cambiar el acceso de un corredor.")
    member = (
        await session.execute(
            select(BusinessMember)
            .where(BusinessMember.id == cmd.member_id)
            .with_for_update(of=BusinessMember)
        )
    ).scalar_one_or_none()
    if member is None or member.business_id != ctx.business_id or member.role != "broker":
        raise NotFound("Corredor no encontrado.")
    guard = await apply_broker_access(session, ctx.business_id, member, cmd.is_active)
    return CommandResult(
        kind=cmd.kind,
        data={"id": str(member.id), "is_active": member.is_active},
        guard_reason=guard,
    )


async def apply_broker_access(
    session: AsyncSession, business_id: UUID, member: BusinessMember, is_active: bool
) -> str | None:
    """Conserva cuenta/historial y exige confirmar saldo o cartera antes de revocar acceso."""
    balance = await _balance(session, business_id, member.id)
    paid = (
        select(func.coalesce(func.sum(PaymentAllocation.applied_amount), 0))
        .join(CustomerPayment, CustomerPayment.id == PaymentAllocation.payment_id)
        .where(
            PaymentAllocation.receivable_id == Receivable.id,
            CustomerPayment.deleted_at.is_(None),
        )
        .correlate(Receivable)
        .scalar_subquery()
    )
    debt = Decimal(
        (
            await session.execute(
                select(func.coalesce(func.sum(Receivable.amount - paid), 0)).where(
                    Receivable.business_id == business_id,
                    Receivable.broker_member_id == member.id,
                    Receivable.deleted_at.is_(None),
                    Receivable.amount - paid > Decimal("0.005"),
                )
            )
        ).scalar_one()
    )
    member.is_active = is_active
    await session.flush()
    guard = None
    if not is_active and (abs(balance) > Decimal("0.005") or debt > Decimal("0.005")):
        guard = (
            f"{member.name or 'El corredor'} conserva un saldo de {fmt_amount(balance, 'USD')} "
            f"y cartera abierta de {fmt_amount(debt, 'USD')}. Sus datos se conservarán."
        )
    return guard


class RecordBrokerAccountPayment(BrokerCommand):
    kind: ClassVar[str] = "record_broker_account_payment"

    amount: BrokerDecimal = Field(gt=0, le=Decimal("99999999999999.99"))
    description: str | None = Field(default=None, max_length=300)
    broker_id: UUID | None = None
    broker_name: str | None = Field(default=None, max_length=160)
    business_date: date | None = None
    attachment_upload_id: UUID | None = None


class RecordBrokerAccountWithdrawal(RecordBrokerAccountPayment):
    """Salida directa: monto positivo, delta negativo, sin operación comercial."""

    kind: ClassVar[str] = "record_broker_account_withdrawal"


@command_handler(RecordBrokerAccountWithdrawal)
@command_handler(RecordBrokerAccountPayment)
async def record_broker_account_payment(
    cmd: RecordBrokerAccountPayment | RecordBrokerAccountWithdrawal,
    ctx: ExecContext, session: AsyncSession, deps: CommandDeps,
) -> CommandResult:
    del deps
    actor, account = await _account(
        session, ctx, cmd.broker_id, cmd.broker_name, allow_house=False
    )
    await lock_brokerage_account(session, account)
    business = account.business
    bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    amount = q_money(cmd.amount)
    if amount <= 0:
        raise ValidationFailed("El monto debe ser de al menos $0,01.")
    is_withdrawal = cmd.kind == RecordBrokerAccountWithdrawal.kind
    balance_delta = -amount if is_withdrawal else amount
    movement = BrokerMovement(
        business_id=ctx.business_id,
        broker_member_id=account.broker_member_id,
        recorded_by_member_id=actor.id,
        kind="out" if is_withdrawal else "in",
        balance_delta=balance_delta,
        currency="USD",
        description=cmd.description,
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        source_message_id=cmd.origin_message_id or ctx.source_message_id,
    )
    session.add(movement)
    await session.flush()
    attachment, attachment_guard = await associate_attachment(
        session, ctx, cmd.attachment_upload_id, movement, command=cmd.kind
    )
    balance = await _account_balance(session, account)
    audit.record(session, ctx, entity_type=EntityType.BROKER_MOVEMENT, entity_id=movement.id,
                 action=AuditAction.CREATE, before=None, after=audit.snapshot(movement), command=cmd.kind)
    return CommandResult(
        kind=cmd.kind,
        guard_reason=attachment_guard,
        data={"movement": {"amount": str(amount), "balance_delta": fmt_amount(balance_delta, "USD"),
                            "description": cmd.description, "business_date": bdate.isoformat()},
              "broker": account.label, **_balance_fields(balance, account, ctx),
              "attachment": attachment is not None},
        affected=[EntityRef(EntityType.BROKER_MOVEMENT, movement.id, version=movement.version,
                            summary=_summary(movement, account))],
    )


class RecordBrokerOperation(BrokerCommand):
    kind: ClassVar[str] = "record_broker_operation"

    operation_type: Literal["buy", "sell"]
    settlement: Literal["settled", "pending"] = "settled"
    operation_amount: BrokerDecimal = Field(gt=0)
    delivered_amount: BrokerDecimal | None = Field(default=None, gt=0)
    customer_name: str | None = Field(default=None, max_length=160)
    # Solo lo inyectan el panel o una opción congelada; el modelo nunca recibe ids.
    customer_id: UUID | None = None
    force_new_customer: bool = False
    description: str | None = Field(default=None, max_length=300)
    broker_id: UUID | None = None
    broker_name: str | None = Field(default=None, max_length=160)
    business_date: date | None = None
    attachment_upload_id: UUID | None = None

    received_amount: BrokerDecimal | None = Field(default=None, gt=0)
    operation_amount_bs: BrokerDecimal | None = Field(default=None, gt=0)
    exchange_rate: BrokerDecimal | None = Field(default=None, gt=0)
    monetary_from_attachment: bool = False

    customer_classification: Literal["global", "personal"] | None = None


class RecordBrokerPurchase(BrokerCommand):
    """Freeze a commercial purchase until the user chooses its settlement."""

    kind: ClassVar[str] = "record_broker_purchase"
    amount: BrokerDecimal = Field(gt=0)
    customer_name: str | None = Field(default=None, max_length=160)
    customer_id: UUID | None = None
    force_new_customer: bool = False
    broker_id: UUID | None = None
    delivered_amount: BrokerDecimal | None = Field(default=None, gt=0)
    broker_name: str | None = None
    business_date: date | None = None
    description: str | None = None
    received_amount: BrokerDecimal | None = Field(default=None, gt=0)
    operation_amount_bs: BrokerDecimal | None = Field(default=None, gt=0)
    exchange_rate: BrokerDecimal | None = Field(default=None, gt=0)
    monetary_from_attachment: bool = False
    attachment_upload_id: UUID | None = None
    settlement: Literal["settled", "pending"] | None = None
    customer_classification: Literal["global", "personal"] | None = None


def _require_chat_purchase_settlement(cmd: BrokerCommand, ctx: ExecContext) -> None:
    """A proposed status (or USD received) is not the user's answer to our question.

    All chat purchase entrypoints converge on the same frozen command. IDs, verified
    money, origin date and draft identity survive settlement and customer choices.
    Panel calls and account purchases do not enable this conversational requirement.
    """
    if not ctx.require_purchase_settlement_choice or cmd.purchase_settlement_confirmed:
        return
    payload = cmd.model_dump(mode="json")
    payload.pop("operation_type", None)
    payload.pop("settlement", None)
    if "operation_amount" in payload:
        payload["amount"] = payload.pop("operation_amount")
    payload.update(purchase_settlement_confirmed=True,
                   draft_origin_command_kind=cmd.draft_origin_command_kind or cmd.kind,
                   draft_operation_id=str(cmd.draft_operation_id or uuid4()))
    raise NeedsChoice("¿La compra está pendiente o liquidada?", [
        {"title": title, "action": "command", "command_kind": RecordBrokerPurchase.kind,
         "command_payload": {**payload, "settlement": settlement}}
        for title, settlement in [("Pendiente", "pending"), ("Liquidada", "settled")]
    ])


@command_handler(RecordBrokerPurchase)
async def record_broker_purchase(
    cmd: RecordBrokerPurchase, ctx: ExecContext, session: AsyncSession, deps: CommandDeps,
) -> CommandResult:
    _require_operation_money(cmd)
    _require_chat_purchase_settlement(cmd, ctx)
    payload = cmd.model_dump(mode="json", exclude={"settlement"})
    if cmd.settlement is None and cmd.received_amount is not None:
        cmd = cmd.model_copy(update={"settlement": "settled"})
    if cmd.settlement is None:
        raise NeedsChoice("¿La compra fue inmediata o quedó pendiente?", [
            {"title": label, "action": "command", "command_kind": cmd.kind,
             "command_payload": {**payload, "settlement": state}}
            for label, state in [("Inmediata", "settled"), ("Pendiente", "pending")]])
    if cmd.settlement == "pending":
        if not (cmd.customer_name or cmd.customer_id):
            if ctx.require_purchase_settlement_choice:
                raise MissingBrokerPurchaseCustomer()
            raise ValidationFailed("Indica el cliente que quedó debiendo la compra.")
        payload.pop("delivered_amount", None)
        return await record_pending_broker_purchase(RecordPendingBrokerPurchase(**payload), ctx, session, deps)
    amount = payload.pop("amount")
    return await record_broker_operation(RecordBrokerOperation(operation_type="buy", operation_amount=amount, **payload),
                                        ctx, session, deps)


@command_handler(RecordBrokerOperation)
async def record_broker_operation(
    cmd: RecordBrokerOperation, ctx: ExecContext, session: AsyncSession, deps: CommandDeps
) -> CommandResult:
    if cmd.operation_type == "buy" and ctx.require_purchase_settlement_choice:
        _require_operation_money(cmd)
        _require_chat_purchase_settlement(cmd, ctx)
    if cmd.settlement == "pending" and cmd.operation_type != "sell":
        raise ValidationFailed("Para una compra pendiente usa el registro de compras pendientes.")
    if cmd.settlement == "pending" and not (cmd.customer_name or cmd.customer_id):
        raise ValidationFailed("Indica el cliente de la venta pendiente.")
    _require_operation_money(cmd)
    await _lock_customer_write(session, ctx)
    del deps
    actor, account = await _account(session, ctx, cmd.broker_id, cmd.broker_name)
    await lock_brokerage_account(session, account)
    business = account.business
    customer: Customer | None = None
    created = False
    if cmd.customer_name or cmd.customer_id:
        customer, created = await _classified_customer(session, ctx, account, cmd)
    rate = Decimal("0") if account.is_house else _commission_rate(business.settings or {})
    credit = operation_credit(
        cmd.operation_amount, cmd.delivered_amount, rate, operation_type=cmd.operation_type, received_amount=cmd.received_amount
    )
    delivered = q_money(cmd.delivered_amount if cmd.delivered_amount is not None else cmd.operation_amount)
    bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    movement = BrokerMovement(
        business_id=ctx.business_id,
        broker_member_id=account.broker_member_id,
        portfolio_broker_member_id=_portfolio_broker(customer),
        recorded_by_member_id=actor.id,
        kind="operation",
        balance_delta=Decimal("0.00") if account.is_house else credit.credit,
        sales_balance_rule="commission_only" if cmd.operation_type == "sell" else None,
        currency="USD",
        description=cmd.description,
        operation_type=cmd.operation_type,
        settlement_status=cmd.settlement,
        customer_id=customer.id if customer else None,
        customer=customer,
        operation_amount=q_money(cmd.operation_amount),
        delivered_amount=Decimal("0.00") if cmd.settlement == "pending" else delivered,
        received_amount=q_money(cmd.received_amount) if cmd.received_amount is not None else None,
        excess=Decimal("0.00") if account.is_house else credit.excess,
        commission=credit.commission,
        commission_rate=rate,
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        source_message_id=cmd.origin_message_id or ctx.source_message_id,
    )
    _set_operation_money(movement, cmd, ctx)
    session.add(movement)
    await session.flush()
    attachment, attachment_guard = await associate_attachment(
        session, ctx, cmd.attachment_upload_id, movement, command=cmd.kind
    )
    balance = await _account_balance(session, account)
    audit.record(session, ctx, entity_type=EntityType.BROKER_MOVEMENT, entity_id=movement.id,
                 action=AuditAction.CREATE, before=None, after=audit.snapshot(movement), command=cmd.kind)
    return CommandResult(
        kind=cmd.kind,
        guard_reason=attachment_guard,
        data={**_monetary_fields(movement),
            "broker": account.label,
            "account_kind": account.kind,
            "operation_type": cmd.operation_type,
            "settlement": cmd.settlement,
            "customer_balance": await customer_balance(session, ctx.business_id, customer.id) if customer else None,
            "customer_classification": customer.classification if customer else None,
            "portfolio_scope": ("global" if customer.classification == "global" else "personal") if customer else None,
            "customer": customer.display_name if customer else None,
            "customer_created": created,
            "operation": fmt_amount(movement.operation_amount, "USD"),
            "delivered": fmt_amount(movement.delivered_amount, "USD"),
            "excess": fmt_amount(movement.excess, "USD"),
            "commission": fmt_amount(credit.commission, "USD"),
            "commission_rate": str(rate),
            "credit": fmt_amount(Decimal("0") if account.is_house else credit.credit, "USD"),
            **_balance_fields(balance, account, ctx),
            "business_date": bdate.isoformat(),
            "description": cmd.description,
            "attachment": attachment is not None,
        },
        affected=[EntityRef(EntityType.BROKER_MOVEMENT, movement.id, version=movement.version,
                            summary=_summary(movement, account))],
    )


class RecordBrokerAccountPurchase(BrokerCommand):
    """Compra propia cuyo principal queda reflejado únicamente en el balance del corredor."""

    kind: ClassVar[str] = "record_broker_account_purchase"

    amount: BrokerDecimal = Field(gt=0)
    broker_id: UUID | None = None
    broker_name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=300)
    business_date: date | None = None
    attachment_upload_id: UUID | None = None

    received_amount: BrokerDecimal | None = Field(default=None, gt=0)
    operation_amount_bs: BrokerDecimal | None = Field(default=None, gt=0)
    exchange_rate: BrokerDecimal | None = Field(default=None, gt=0)
    monetary_from_attachment: bool = False


@command_handler(RecordBrokerAccountPurchase)
async def record_broker_account_purchase(
    cmd: RecordBrokerAccountPurchase, ctx: ExecContext, session: AsyncSession, deps: CommandDeps
) -> CommandResult:
    _require_operation_money(cmd)
    del deps
    actor, account = await _account(
        session, ctx, cmd.broker_id, cmd.broker_name, allow_house=False
    )
    business = account.business
    await lock_brokerage_account(session, account)
    amount = q_money(cmd.amount)
    rate = _commission_rate(business.settings or {})
    effect = account_purchase_effect(amount, rate)
    excess = purchase_excess(amount, cmd.received_amount)
    bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    movement = BrokerMovement(
        business_id=ctx.business_id,
        broker_member_id=account.broker_member_id,
        recorded_by_member_id=actor.id,
        kind="operation",
        balance_delta=q_money(effect.balance_delta + excess),
        currency="USD",
        description=cmd.description,
        operation_type="buy",
        settlement_status="account",
        customer_id=None,
        operation_amount=amount,
        delivered_amount=Decimal("0.00"),
        received_amount=q_money(cmd.received_amount) if cmd.received_amount is not None else None,
        excess=excess,
        commission=effect.commission,
        commission_rate=rate,
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        source_message_id=cmd.origin_message_id or ctx.source_message_id,
    )
    _set_operation_money(movement, cmd, ctx)
    session.add(movement)
    await session.flush()
    attachment, attachment_guard = await associate_attachment(
        session, ctx, cmd.attachment_upload_id, movement, command=cmd.kind
    )
    balance = await _account_balance(session, account)
    audit.record(session, ctx, entity_type=EntityType.BROKER_MOVEMENT, entity_id=movement.id,
                 action=AuditAction.CREATE, before=None, after=audit.snapshot(movement), command=cmd.kind)
    return CommandResult(
        kind=cmd.kind,
        guard_reason=attachment_guard,
        data={**_monetary_fields(movement),
            "broker": account.label,
            "operation": fmt_amount(amount, "USD"),
            "commission": fmt_amount(effect.commission, "USD"),
            "balance_effect": fmt_amount(movement.balance_delta, "USD"),
            **_balance_fields(balance, account, ctx),
            "business_date": bdate.isoformat(),
            "description": cmd.description,
            "attachment": attachment is not None,
        },
        affected=[EntityRef(EntityType.BROKER_MOVEMENT, movement.id, version=movement.version,
                            summary=_summary(movement, account))],
    )


class EditBrokerMovement(BrokerCommand):
    """Corrige un movimiento conservando tipo, corredor y tasas históricas."""

    kind: ClassVar[str] = "edit_broker_movement"

    movement_id: UUID
    expected_version: int
    amount: BrokerDecimal | None = Field(default=None, gt=0)
    delivered_amount: BrokerDecimal | None = Field(default=None, gt=0)
    customer_name: str | None = Field(default=None, max_length=160)
    customer_id: UUID | None = None
    force_new_customer: bool = False
    description: str | None = Field(default=None, max_length=300)
    business_date: date | None = None

    received_amount: BrokerDecimal | None = Field(default=None, gt=0)
    operation_amount_bs: BrokerDecimal | None = Field(default=None, gt=0)
    exchange_rate: BrokerDecimal | None = Field(default=None, gt=0)

    customer_classification: Literal["global", "personal"] | None = None


@command_handler(EditBrokerMovement)
async def edit_broker_movement(
    cmd: EditBrokerMovement, ctx: ExecContext, session: AsyncSession, deps: CommandDeps
) -> CommandResult:
    await _lock_customer_write(session, ctx)
    del deps
    movement = await session.get(BrokerMovement, cmd.movement_id)
    if movement is None or movement.business_id != ctx.business_id or movement.deleted_at is not None:
        raise NotFound("Ese movimiento ya no existe.")
    _actor, account = await existing_brokerage_account(session, ctx, movement.broker_member_id)
    await lock_brokerage_account(session, account)
    if movement.kind == "adjustment":
        raise ValidationFailed("Usa ajustar saldo para corregir o eliminar un ajuste histórico.")
    check_version(movement, cmd.expected_version, "movimiento")
    before = audit.snapshot(movement)
    changed = cmd.model_fields_set
    business = account.business

    if "amount" in changed and cmd.amount is None:
        raise ValidationFailed("El monto no puede quedar vacío.")
    if "delivered_amount" in changed and cmd.delivered_amount is None:
        raise ValidationFailed("El monto entregado no puede quedar vacío.")

    if "business_date" in changed:
        bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
        movement.business_date = bdate
        movement.occurred_at = occurred_at_for(bdate, business.timezone, ctx.now)
    if "description" in changed:
        movement.description = cmd.description

    previous_customer_id = movement.customer_id
    customer_changed = bool({"customer_name", "customer_id"} & changed)
    receivable = None
    payment = None
    related_before = None
    if movement.related_receivable_id:
        receivable = await session.get(Receivable, movement.related_receivable_id)
        if receivable is None or receivable.deleted_at is not None:
            raise ValidationFailed("La deuda vinculada ya no está disponible.")
        related_before = audit.snapshot(receivable)
        if customer_changed:
            if not cmd.customer_name and cmd.customer_id is None:
                raise ValidationFailed("Indica el cliente de la compra pendiente.")
            customer, _created = await _classified_customer(session, ctx, account, cmd)
            if _portfolio_broker(customer) != receivable.broker_member_id:
                raise ValidationFailed("No puedes cambiar la cartera de una compra mediante edición.")
            receivable.customer_id = customer.id
            receivable.customer = customer
            movement.customer_id = customer.id
            movement.customer = customer
        if "amount" in changed:
            amount = q_money(cmd.amount)
            receivable.set_native_usd(amount)
            commission = q_money(amount * movement.commission_rate)
            movement.operation_amount = amount
            movement.commission = commission
            movement.balance_delta = Decimal("0.00") if account.is_house else commission if movement.portfolio_broker_member_id is None else q_money(commission - amount)
        if "business_date" in changed:
            receivable.business_date = movement.business_date
            receivable.occurred_at = movement.occurred_at
        if "description" in changed:
            receivable.description = cmd.description or "Compra de divisas pendiente"
        bump(receivable)
    elif movement.related_payment_id:
        payment = await session.get(CustomerPayment, movement.related_payment_id)
        if payment is None or payment.deleted_at is not None:
            raise ValidationFailed("El abono de cliente vinculado ya no está disponible.")
        related_before = audit.snapshot(payment)
        if customer_changed:
            raise ValidationFailed("El cliente de un abono de cliente no se puede cambiar.")
        if "amount" in changed:
            amount = q_money(cmd.amount)
            payment.set_native_usd(amount)
            movement.balance_delta = Decimal("0.00") if account.is_house or movement.portfolio_broker_member_id is None else amount
        if "business_date" in changed:
            payment.business_date = movement.business_date
            payment.occurred_at = movement.occurred_at
        bump(payment)
        await session.flush()
        await reallocate_customer_payments(session, ctx, payment.customer_id)
        await _sync_purchase_settlements(
            session,
            ctx,
            broker_id=payment.broker_member_id,
            customer_id=payment.customer_id,
            command=cmd.kind,
        )
    elif movement.kind == "customer_payout":
        if customer_changed:
            raise ValidationFailed("El cliente de un pago entregado no se puede cambiar.")
        if "delivered_amount" in changed:
            raise ValidationFailed("Ese campo no aplica a un pago entregado.")
        if "amount" in changed:
            movement.customer_payout_amount = q_money(cmd.amount)
    elif movement.kind in {"in", "out"}:
        if "amount" in changed:
            if cmd.amount > Decimal("99999999999999.99"):
                raise ValidationFailed("El monto USD excede el límite admitido.")
            amount = q_money(cmd.amount)
            if amount <= 0:
                raise ValidationFailed("El monto debe ser de al menos $0,01.")
            movement.balance_delta = -amount if movement.kind == "out" else amount
        if {"delivered_amount", "customer_name", "customer_id"} & changed:
            raise ValidationFailed("Ese campo no aplica a un movimiento directo de cuenta.")
    elif movement.settlement_status == "account":
        if "amount" in changed:
            amount = q_money(cmd.amount)
            effect = account_purchase_effect(amount, movement.commission_rate)
            movement.operation_amount = amount
            movement.commission = effect.commission
            movement.balance_delta = effect.balance_delta
        if {"delivered_amount", "customer_name", "customer_id"} & changed:
            raise ValidationFailed("En una compra propia solo se corrigen monto, fecha y descripción.")
    else:
        if movement.operation_type == "buy" and "delivered_amount" in changed:
            raise ValidationFailed("El monto entregado de una compra es derivado y no se edita.")
        amount = q_money(cmd.amount) if "amount" in changed else movement.operation_amount
        delivered = (
            q_money(cmd.delivered_amount)
            if "delivered_amount" in changed
            else movement.delivered_amount
        )
        if movement.operation_type == "buy" or "delivered_amount" not in changed:
            delivered = amount
        credit = operation_credit(
            amount,
            delivered,
            movement.commission_rate,
            operation_type=movement.operation_type,
            received_amount=(cmd.received_amount if "received_amount" in changed else movement.received_amount),
        )
        movement.operation_amount = amount
        movement.delivered_amount = Decimal("0.00") if movement.operation_type == "sell" and movement.settlement_status == "pending" else delivered
        movement.commission = credit.commission
        movement.excess = Decimal("0.00") if account.is_house else credit.excess
        if movement.operation_type != "sell":
            movement.balance_delta = Decimal("0.00") if account.is_house else credit.credit
        if customer_changed:
            if not cmd.customer_name and cmd.customer_id is None:
                movement.customer_id = None
                movement.customer = None
            else:
                customer, _created = await _classified_customer(session, ctx, account, cmd)
                if movement.settlement_status == "pending" and _portfolio_broker(customer) != movement.portfolio_broker_member_id:
                    raise ValidationFailed("No puedes cambiar la cartera de una venta pendiente mediante edición.")
                movement.customer_id = customer.id
                movement.customer = customer
                movement.portfolio_broker_member_id = _portfolio_broker(customer)

    if movement.operation_type == "sell" and movement.settlement_status == "pending":
        if movement.customer_id is None:
            raise ValidationFailed("La venta pendiente requiere un cliente.")
    if movement.kind == "operation":
        if movement.operation_type == "sell":
            # An explicit nominal correction adopts ADR 0034, using the frozen rate.
            # Metadata/FX/receipt edits must not silently restate a legacy balance.
            if "amount" in changed:
                movement.sales_balance_rule = "commission_only"
            if movement.sales_balance_rule == "commission_only":
                movement.balance_delta = Decimal("0.00") if account.is_house else movement.commission
            if "received_amount" in changed and cmd.received_amount is not None:
                raise ValidationFailed("USD recibidos solo aplica a compras.")
        else:
            received = cmd.received_amount if "received_amount" in changed else movement.received_amount
            excess = purchase_excess(movement.operation_amount, received)
            movement.received_amount = q_money(received) if received is not None else None
            movement.excess = Decimal("0.00") if account.is_house else excess
            principal = (movement.operation_amount if movement.settlement_status == "account" or
                         movement.related_receivable_id and movement.portfolio_broker_member_id is not None else Decimal("0"))
            movement.balance_delta = Decimal("0.00") if account.is_house else q_money(movement.commission + excess - principal)
        _set_operation_money(movement, cmd, ctx, editing=True)
    elif {"operation_amount_bs", "exchange_rate", "received_amount"} & changed:
        raise ValidationFailed("Bs y tasa solo aplican a compras o ventas.")
    if receivable is not None and "amount" in changed:
        movement.delivered_amount = Decimal("0.00")
        movement.settlement_status = "pending"
    if receivable is not None:
        await session.flush()
        customer_ids = list({previous_customer_id, movement.customer_id} - {None})
        await reallocate_customer_payments(session, ctx, *customer_ids)
        for cid in customer_ids:
            await _sync_purchase_settlements(session, ctx, broker_id=movement.portfolio_broker_member_id,
                                             customer_id=cid, command=cmd.kind)

    bump(movement)
    await session.flush()
    audit.record(
        session,
        ctx,
        entity_type=EntityType.BROKER_MOVEMENT,
        entity_id=movement.id,
        action=AuditAction.UPDATE,
        before=before,
        after=audit.snapshot(movement),
        command=cmd.kind,
    )
    if receivable is not None:
        audit.record(
            session,
            ctx,
            entity_type=EntityType.RECEIVABLE,
            entity_id=receivable.id,
            action=AuditAction.UPDATE,
            before=related_before,
            after=audit.snapshot(receivable),
            command=cmd.kind,
        )
    if payment is not None:
        audit.record(
            session,
            ctx,
            entity_type=EntityType.PAYMENT,
            entity_id=payment.id,
            action=AuditAction.UPDATE,
            before=related_before,
            after=audit.snapshot(payment),
            command=cmd.kind,
        )
    balance = await _account_balance(session, account)
    amount = movement.operation_amount if movement.kind == "operation" else (
        payment.amount if payment is not None else movement.customer_payout_amount if movement.kind == "customer_payout" else movement.balance_delta
    )
    return CommandResult(
        kind=cmd.kind,
        data={**_monetary_fields(movement),
            "movement_id": str(movement.id),
            "settlement": movement.settlement_status,
            "portfolio_scope": movement.customer.classification if movement.customer else None,
            "summary": _summary(movement, account, amount=amount),
            "broker": account.label,
            "account_kind": account.kind,
            "logical_type": (
                "customer_payout" if movement.kind == "customer_payout" else
                "pending" if movement.related_receivable_id else
                "receivable_payment" if movement.related_payment_id else
                "account_withdrawal" if movement.kind == "out" else
                "account_payment" if movement.kind == "in" else
                "account" if movement.settlement_status == "account" else
                movement.operation_type
            ),
            "amount": fmt_amount(amount, "USD"),
            "commission": fmt_amount(movement.commission or Decimal("0"), "USD"),
            **_balance_fields(balance, account, ctx),
            "customer": movement.customer.display_name if movement.customer is not None else None,
            "receivable_amount": fmt_amount(receivable.amount, "USD") if receivable is not None else None,
            "customer_balance": await customer_balance(session, ctx.business_id, movement.customer_id) if movement.customer_id else None,
        },
        affected=[
            EntityRef(
                EntityType.BROKER_MOVEMENT,
                movement.id,
                version=movement.version,
                summary=_summary(movement, account, amount=amount),
            )
        ],
    )


class RecordPendingBrokerPurchase(BrokerCommand):
    """Compra a crédito: el negocio pasó los fondos y el cliente todavía debe las divisas."""

    kind: ClassVar[str] = "record_pending_broker_purchase"

    customer_name: str | None = Field(default=None, min_length=1, max_length=160)
    customer_id: UUID | None = None
    amount: BrokerDecimal = Field(gt=0)
    description: str | None = Field(default=None, max_length=300)
    broker_id: UUID | None = None
    broker_name: str | None = Field(default=None, max_length=160)
    business_date: date | None = None
    force_new_customer: bool = False
    attachment_upload_id: UUID | None = None

    received_amount: BrokerDecimal | None = Field(default=None, gt=0)
    operation_amount_bs: BrokerDecimal | None = Field(default=None, gt=0)
    exchange_rate: BrokerDecimal | None = Field(default=None, gt=0)
    monetary_from_attachment: bool = False

    customer_classification: Literal["global", "personal"] | None = None


@command_handler(RecordPendingBrokerPurchase)
async def record_pending_broker_purchase(
    cmd: RecordPendingBrokerPurchase, ctx: ExecContext, session: AsyncSession, deps: CommandDeps
) -> CommandResult:
    _require_operation_money(cmd)
    _require_chat_purchase_settlement(cmd, ctx)
    if not (cmd.customer_name or cmd.customer_id):
        raise ValidationFailed("Indica el cliente que quedó debiendo la compra.")
    await _lock_customer_write(session, ctx)
    actor, account = await _account(session, ctx, cmd.broker_id, cmd.broker_name)
    await lock_brokerage_account(session, account)
    business = account.business
    bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    amount = q_money(cmd.amount)
    if amount <= 0:
        raise ValidationFailed("El monto debe ser de al menos $0,01.")
    rate = Decimal("0") if account.is_house else _commission_rate(business.settings or {})
    credit = operation_credit(amount, None, rate, operation_type="buy", received_amount=cmd.received_amount)
    customer, created = await _classified_customer(session, ctx, account, cmd)
    receivable = Receivable(
        business_id=ctx.business_id,
        customer_id=customer.id,
        broker_member_id=_portfolio_broker(customer),
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        description=cmd.description or "Compra de divisas pendiente",
        source_message_id=cmd.origin_message_id or ctx.source_message_id,
    )
    receivable.set_native_usd(amount)
    receivable.customer = customer
    session.add(receivable)
    await session.flush()

    movement = BrokerMovement(
        business_id=ctx.business_id,
        broker_member_id=account.broker_member_id,
        portfolio_broker_member_id=_portfolio_broker(customer),
        recorded_by_member_id=actor.id,
        kind="operation",
        balance_delta=(
            Decimal("0.00") if account.is_house else credit.credit if customer.classification == "global" else q_money(credit.credit - amount)
        ),
        currency="USD",
        description=cmd.description or f"Compra pendiente a {customer.display_name}",
        operation_type="buy",
        settlement_status="pending",
        customer_id=customer.id,
        customer=customer,
        operation_amount=amount,
        delivered_amount=Decimal("0.00"),
        received_amount=q_money(cmd.received_amount) if cmd.received_amount is not None else None,
        excess=Decimal("0.00") if account.is_house else credit.excess,
        commission=credit.commission,
        commission_rate=rate,
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        source_message_id=cmd.origin_message_id or ctx.source_message_id,
        related_receivable_id=receivable.id,
    )
    _set_operation_money(movement, cmd, ctx)
    session.add(movement)
    await session.flush()
    attachment, attachment_guard = await associate_attachment(
        session, ctx, cmd.attachment_upload_id, movement, command=cmd.kind
    )
    await reallocate_customer_payments(session, ctx, customer.id)
    await _sync_purchase_settlements(session, ctx, broker_id=_portfolio_broker(customer),
                                     customer_id=customer.id, command=cmd.kind)
    balance = await _account_balance(session, account)
    audit.record(
        session,
        ctx,
        entity_type=EntityType.RECEIVABLE,
        entity_id=receivable.id,
        action=AuditAction.CREATE,
        before=None,
        after=audit.snapshot(receivable),
        command=cmd.kind,
    )
    audit.record(
        session,
        ctx,
        entity_type=EntityType.BROKER_MOVEMENT,
        entity_id=movement.id,
        action=AuditAction.CREATE,
        before=None,
        after=audit.snapshot(movement),
        command=cmd.kind,
    )
    return CommandResult(
        kind=cmd.kind,
        guard_reason=attachment_guard,
        data={**_monetary_fields(movement),
            "broker": account.label,
            "account_kind": account.kind,
            "customer_classification": customer.classification,
            "portfolio_scope": "global" if customer.classification == "global" else "personal",
            "customer": customer.display_name,
            "customer_created": created,
            "customer_balance": await customer_balance(session, ctx.business_id, customer.id),
            "amount": fmt_amount(amount, "USD"),
            "commission": fmt_amount(credit.commission, "USD"),
            "commission_rate": str(rate),
            "balance_effect": fmt_amount(movement.balance_delta, "USD"),
            **_balance_fields(balance, account, ctx),
            "business_date": bdate.isoformat(),
            "description": cmd.description,
            "attachment": attachment is not None,
        },
        affected=[
            EntityRef(
                EntityType.BROKER_MOVEMENT,
                movement.id,
                version=movement.version,
                summary=_summary(movement, account),
            ),
            EntityRef(
                EntityType.RECEIVABLE,
                receivable.id,
                version=receivable.version,
                summary=receivable_summary(receivable),
            ),
        ],
    )


class RecordBrokerReceivablePayment(BrokerCommand):
    """Abono USD de un cliente a la cartera del corredor que también acredita su saldo."""

    kind: ClassVar[str] = "record_broker_receivable_payment"

    customer_name: str = Field(min_length=1, max_length=160)
    customer_id: UUID | None = None
    amount: BrokerDecimal = Field(gt=0)
    description: str | None = Field(default=None, max_length=300)
    broker_id: UUID | None = None
    broker_name: str | None = Field(default=None, max_length=160)
    business_date: date | None = None
    attachment_upload_id: UUID | None = None


def _broker_payment_risk(
    cmd: RecordBrokerReceivablePayment, ctx: ExecContext, res: CommandResult
) -> str | None:
    del cmd, ctx
    if res.data.get("unapplied"):
        return (
            f"El abono supera lo que debe {res.data['customer']}: sobran {res.data['unapplied']}, "
            "que quedarían como saldo a favor."
        )
    return None


@command_handler(RecordBrokerReceivablePayment, risk=_broker_payment_risk)
async def record_broker_receivable_payment(
    cmd: RecordBrokerReceivablePayment, ctx: ExecContext, session: AsyncSession, deps: CommandDeps
) -> CommandResult:
    await _lock_customer_write(session, ctx)
    actor, account = await _account(session, ctx, cmd.broker_id, cmd.broker_name)
    await lock_brokerage_account(session, account)
    business = account.business
    bdate = validate_business_date(cmd.business_date, business.timezone, ctx.now)
    amount = q_money(cmd.amount)
    if amount <= 0:
        raise ValidationFailed("El monto debe ser de al menos $0,01.")
    customer, _ = await _classified_customer(session, ctx, account, cmd, create=False)
    previous_balance = await customer_balance(session, ctx.business_id, customer.id)
    payment = CustomerPayment(
        business_id=ctx.business_id,
        customer_id=customer.id,
        broker_member_id=_portfolio_broker(customer),
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        source_message_id=cmd.origin_message_id or ctx.source_message_id,
        unapplied_amount=Decimal("0.00"),
        allocations=[],
    )
    payment.set_native_usd(amount)
    payment.customer = customer
    session.add(payment)
    await session.flush()
    applied = await allocate_payment(session, payment)
    await session.flush()

    movement = BrokerMovement(
        business_id=ctx.business_id,
        broker_member_id=account.broker_member_id,
        portfolio_broker_member_id=_portfolio_broker(customer),
        recorded_by_member_id=actor.id,
        kind="in",
        balance_delta=Decimal("0.00") if account.is_house or customer.classification == "global" else amount,
        currency="USD",
        description=cmd.description or f"Abono de {customer.display_name}",
        customer_id=customer.id,
        customer=customer,
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, business.timezone, ctx.now),
        source_message_id=cmd.origin_message_id or ctx.source_message_id,
        related_payment_id=payment.id,
    )
    session.add(movement)
    await session.flush()
    attachment, attachment_guard = await associate_attachment(
        session, ctx, cmd.attachment_upload_id, movement, command=cmd.kind
    )
    await _sync_purchase_settlements(
        session,
        ctx,
        broker_id=_portfolio_broker(customer),
        customer_id=customer.id,
        command=cmd.kind,
    )
    balance = await _account_balance(session, account)
    debt = await customer_balance(session, ctx.business_id, customer.id, _portfolio_broker(customer))
    audit.record(
        session,
        ctx,
        entity_type=EntityType.PAYMENT,
        entity_id=payment.id,
        action=AuditAction.CREATE,
        before=None,
        after=audit.snapshot(payment),
        command=cmd.kind,
    )
    audit.record(
        session,
        ctx,
        entity_type=EntityType.BROKER_MOVEMENT,
        entity_id=movement.id,
        action=AuditAction.CREATE,
        before=None,
        after=audit.snapshot(movement),
        command=cmd.kind,
    )
    excess = max(Decimal("0"), amount - max(-Decimal(previous_balance["balance_usd"]), Decimal("0")))
    unapplied = fmt_amount(excess, "USD") if excess > 0 else None
    payment_guard = (
        f"El abono supera lo que debe {customer.display_name}: sobran {unapplied}. ¿Quieres registrarlo de todos modos?"
        if unapplied else None
    )
    return CommandResult(
        kind=cmd.kind,
        guard_reason=" ".join(filter(None, [attachment_guard, payment_guard])) or None,
        data={
            "broker": account.label,
            "account_kind": account.kind,
            "customer_classification": customer.classification,
            "portfolio_scope": "global" if customer.classification == "global" else "personal",
            "customer": customer.display_name,
            "amount": fmt_amount(amount, "USD"),
            "applied_to": applied,
            "unapplied": unapplied,
            "customer_balance": debt,
            **_balance_fields(balance, account, ctx),
            "business_date": bdate.isoformat(),
            "description": cmd.description,
            "attachment": attachment is not None,
        },
        affected=[
            EntityRef(
                EntityType.BROKER_MOVEMENT,
                movement.id,
                version=movement.version,
                summary=_summary(movement, account, amount=amount),
            ),
            EntityRef(
                EntityType.PAYMENT,
                payment.id,
                version=payment.version,
                summary=payment_summary(payment),
            ),
        ],
    )


class AttachBrokerAttachment(BrokerCommand):
    """Asocia una carga ya validada a un movimiento existente."""

    kind: ClassVar[str] = "attach_broker_attachment"

    movement_id: UUID
    attachment_upload_id: UUID
    expected_attachment_id: UUID | None = None

    expected_version: int | None = None
    operation_amount_bs: BrokerDecimal | None = Field(default=None, gt=0)
    exchange_rate: BrokerDecimal | None = Field(default=None, gt=0)
    monetary_from_attachment: bool = True
    extracted_data: dict | None = None
    extraction_ambiguous: bool = False


class CompleteBrokerReceipt(AttachBrokerAttachment):
    kind: ClassVar[str] = "complete_broker_receipt"
    movement_id: UUID | None = None


@command_handler(CompleteBrokerReceipt)
async def complete_broker_receipt(
    cmd: CompleteBrokerReceipt, ctx: ExecContext, session: AsyncSession, deps: CommandDeps,
) -> CommandResult:
    await _lock_customer_write(session, ctx)
    await _account(session, ctx, None, None)
    payload = cmd.model_dump(mode="json", exclude_unset=True)
    if cmd.movement_id is None:
        window = int((await session.get(Business, ctx.business_id)).settings.get("broker_receipt_window_minutes", 10))
        window = min(max(window, 1), 60)
        rows = (await session.execute(select(BrokerMovement).where(
            BrokerMovement.business_id == ctx.business_id,
            BrokerMovement.recorded_by_member_id == ctx.actor.member_id,
            BrokerMovement.kind == "operation", BrokerMovement.deleted_at.is_(None),
            BrokerMovement.operation_amount_bs.is_(None),
            BrokerMovement.created_at.between(ctx.now - timedelta(minutes=window), ctx.now),
        ).order_by(BrokerMovement.created_at.desc()).limit(10))).scalars().all()
        if not rows:
            raise ValidationFailed("No identifiqué una operación reciente compatible. Responde al recibo o indica cuál operación respalda la imagen.")
        if len(rows) > 1:
            raise NeedsChoice("¿A qué operación corresponde el comprobante?", [
                {"title": f"{'Compra' if row.operation_type == 'buy' else 'Venta'} ${row.operation_amount}",
                 "description": f"{row.customer.display_name if row.customer else 'Sin cliente'} · {row.business_date}",
                 "action": "command", "command_kind": cmd.kind,
                 "command_payload": {**payload, "movement_id": str(row.id), "expected_version": row.version}}
                for row in rows])
        row = rows[0]
        payload.update(movement_id=str(row.id), expected_version=row.version)
    else:
        row = await session.get(BrokerMovement, cmd.movement_id)
        if row is None or row.business_id != ctx.business_id or row.deleted_at is not None:
            raise NotFound("Movimiento no encontrado.")
    return await attach_broker_attachment(AttachBrokerAttachment(**payload), ctx, session, deps)


@command_handler(AttachBrokerAttachment)
async def attach_broker_attachment(
    cmd: AttachBrokerAttachment, ctx: ExecContext, session: AsyncSession, deps: CommandDeps
) -> CommandResult:
    await _lock_customer_write(session, ctx)
    del deps
    movement = (
        await session.execute(
            select(BrokerMovement).where(BrokerMovement.id == cmd.movement_id).with_for_update(of=BrokerMovement)
        )
    ).scalar_one_or_none()
    if movement is None or movement.business_id != ctx.business_id or movement.deleted_at is not None:
        raise NotFound("Movimiento no encontrado.")
    _, account = await existing_brokerage_account(session, ctx, movement.broker_member_id)
    candidate = await session.get(BrokerAttachment, cmd.attachment_upload_id)
    if candidate is None or candidate.business_id != ctx.business_id or candidate.uploaded_by_member_id != ctx.actor.member_id:
        raise NotFound("La carga del comprobante no está disponible para esta sesión.")
    already_applied = candidate.broker_movement_id == movement.id and candidate.attached_at is not None
    enrich = cmd.operation_amount_bs is not None or cmd.exchange_rate is not None
    if enrich and not already_applied:
        if movement.kind != "operation":
            raise ValidationFailed("Bs y tasa solo completan compras o ventas.")
        if cmd.expected_version is None:
            raise ValidationFailed("Completar datos monetarios requiere la versión del movimiento.")
        check_version(movement, cmd.expected_version, "movimiento")
    before = audit.snapshot(movement)
    attachment, guard = await associate_attachment(
        session,
        ctx,
        cmd.attachment_upload_id,
        movement,
        expected_attachment_id=cmd.expected_attachment_id,
        command=cmd.kind,
    )
    assert attachment is not None
    # A replay or another upload of the same checksum must not reinterpret its financial amount.
    duplicate = attachment.id != candidate.id
    if enrich and not already_applied and not duplicate:
        old_bs = movement.operation_amount_bs
        _set_operation_money(movement, cmd, ctx, editing=True)
        if old_bs is not None and old_bs != movement.operation_amount_bs:
            guard = f"El comprobante cambia Bs {old_bs} por Bs {movement.operation_amount_bs}. ¿Confirmas?"
        if cmd.extraction_ambiguous:
            guard = f"La lectura del monto es ambigua: Bs {movement.operation_amount_bs}. ¿Confirmas este monto?"
        bump(movement)
        audit.record(session, ctx, entity_type=EntityType.BROKER_MOVEMENT, entity_id=movement.id,
                     action=AuditAction.UPDATE, before=before, after=audit.snapshot(movement), command=cmd.kind)
    if cmd.extracted_data is not None and not already_applied and not duplicate:
        attachment.extracted_data = cmd.extracted_data
    confidence = (cmd.extracted_data or {}).get("confidence")
    warning = None
    if confidence == "unreadable":
        warning = "No pude leer el monto del comprobante. Indica el importe en Bs o envía una imagen más clara; conservé los datos financieros anteriores."
    elif confidence == "ambiguous" or cmd.extraction_ambiguous:
        warning = "No pude identificar con seguridad el monto transferido entre los importes de la imagen. Aclara cuál fue el importe en Bs o envía un comprobante más claro."
    await session.flush()
    return CommandResult(
        kind=cmd.kind,
        guard_reason=guard,
        data={**_monetary_fields(movement), "broker": account.label, "movement": _summary(movement, account), "attachment": True,
              "extraction_warning": warning},
        affected=[
            EntityRef(EntityType.BROKER_MOVEMENT, movement.id, version=movement.version,
                      summary=_summary(movement, account)),
            EntityRef(EntityType.BROKER_ATTACHMENT, attachment.id, version=attachment.version,
                      summary="comprobante de " + _summary(movement, account)),
        ],
    )


class DeleteBrokerMovement(Command):
    kind: ClassVar[str] = "delete_broker_movement"

    movement_id: UUID
    expected_version: int | None = None


def _delete_risk(cmd: DeleteBrokerMovement, ctx: ExecContext, res: CommandResult) -> str | None:
    del cmd, ctx
    debt = f" y su deuda vinculada: {res.data['deleted_receivable']}" if res.data.get("deleted_receivable") else ""
    payment = f" y su abono de cliente vinculado: {res.data['deleted_payment']}" if res.data.get("deleted_payment") else ""
    return f"Voy a borrar este movimiento: {res.data['deleted']}{debt}{payment}"


@command_handler(DeleteBrokerMovement, risk=_delete_risk)
async def delete_broker_movement(
    cmd: DeleteBrokerMovement, ctx: ExecContext, session: AsyncSession, deps: CommandDeps
) -> CommandResult:
    await _lock_customer_write(session, ctx)
    del deps
    movement = await session.get(BrokerMovement, cmd.movement_id)
    if movement is None or movement.business_id != ctx.business_id or movement.deleted_at is not None:
        raise NotFound("Ese movimiento ya no existe.")
    _, account = await existing_brokerage_account(session, ctx, movement.broker_member_id)
    if movement.kind == "adjustment":
        raise ValidationFailed("Usa ajustar saldo para corregir o eliminar un ajuste histórico.")
    check_version(movement, cmd.expected_version, "movimiento")
    await lock_brokerage_account(session, account)
    before = audit.snapshot(movement)
    receivable = None
    receivable_before = None
    deleted_receivable = None
    payment = None
    payment_before = None
    deleted_payment = None
    if movement.related_receivable_id is not None:
        receivable = await session.get(Receivable, movement.related_receivable_id)
        if receivable is not None and receivable.deleted_at is None:
            receivable_before = audit.snapshot(receivable)
            deleted_receivable = receivable_summary(receivable)
            receivable.deleted_at = ctx.now
            bump(receivable)
    if movement.related_payment_id is not None:
        payment = await session.get(CustomerPayment, movement.related_payment_id)
        if payment is not None and payment.deleted_at is None:
            payment_before = audit.snapshot(payment)
            deleted_payment = payment_summary(payment)
            payment.deleted_at = ctx.now
            bump(payment)
            await session.flush()
            await reallocate_customer_payments(session, ctx, payment.customer_id)
            await _sync_purchase_settlements(
                session,
                ctx,
                broker_id=payment.broker_member_id,
                customer_id=payment.customer_id,
                command=cmd.kind,
            )
    movement.deleted_at = ctx.now
    if receivable is not None:
        await session.flush()
        await reallocate_customer_payments(session, ctx, receivable.customer_id)
        await _sync_purchase_settlements(session, ctx, broker_id=receivable.broker_member_id,
                                         customer_id=receivable.customer_id, command=cmd.kind)
    summary = _summary(movement, account, amount=payment.amount if payment is not None else None)
    bump(movement)
    await retire_movement_attachment(session, ctx, movement.id, command=cmd.kind)
    await session.flush()
    balance = await _account_balance(session, account)
    audit.record(
        session,
        ctx,
        entity_type=EntityType.BROKER_MOVEMENT,
        entity_id=movement.id,
        action=AuditAction.DELETE,
        before=before,
        after=None,
        command=cmd.kind,
    )
    if receivable is not None and receivable_before is not None:
        audit.record(
            session,
            ctx,
            entity_type=EntityType.RECEIVABLE,
            entity_id=receivable.id,
            action=AuditAction.DELETE,
            before=receivable_before,
            after=None,
            command=cmd.kind,
        )
    if payment is not None and payment_before is not None:
        audit.record(
            session,
            ctx,
            entity_type=EntityType.PAYMENT,
            entity_id=payment.id,
            action=AuditAction.DELETE,
            before=payment_before,
            after=None,
            command=cmd.kind,
        )
    return CommandResult(
        kind=cmd.kind,
        guard_reason=(
            f"Voy a borrar este movimiento: {summary}"
            f"{' y su deuda vinculada: ' + deleted_receivable if deleted_receivable else ''}"
            f"{' y su abono de cliente vinculado: ' + deleted_payment if deleted_payment else ''}"
        ),
        data={
            "deleted": summary,
            "broker": account.label,
            "account_kind": account.kind,
            **_balance_fields(balance, account, ctx),
            "deleted_receivable": deleted_receivable,
            "deleted_payment": deleted_payment,
            "customer_balance": await customer_balance(session, ctx.business_id, movement.customer_id) if movement.customer_id else None,
            "customer": movement.customer.display_name if movement.customer else None,
        },
        affected=[EntityRef(EntityType.BROKER_MOVEMENT, movement.id, version=movement.version)],
    )


class RecordBrokerCustomerPayout(RecordBrokerReceivablePayment):
    """Real USD delivered to a customer, with no second charge to the broker."""

    kind: ClassVar[str] = "record_broker_customer_payout"


@command_handler(RecordBrokerCustomerPayout)
async def record_broker_customer_payout(
    cmd: RecordBrokerCustomerPayout, ctx: ExecContext, session: AsyncSession, deps: CommandDeps
) -> CommandResult:
    del deps
    await _lock_customer_write(session, ctx)
    actor, account = await _account(session, ctx, cmd.broker_id, cmd.broker_name)
    await lock_brokerage_account(session, account)
    customer, _ = await _classified_customer(session, ctx, account, cmd, create=False)
    amount = q_money(cmd.amount)
    if amount <= 0:
        raise ValidationFailed("El monto debe ser de al menos $0,01.")
    bdate = validate_business_date(cmd.business_date, account.business.timezone, ctx.now)
    movement = BrokerMovement(
        business_id=ctx.business_id,
        broker_member_id=account.broker_member_id,
        portfolio_broker_member_id=_portfolio_broker(customer),
        recorded_by_member_id=actor.id,
        kind="customer_payout",
        customer_payout_amount=amount,
        balance_delta=Decimal("0.00"),
        currency="USD",
        customer_id=customer.id,
        customer=customer,
        description=cmd.description or f"Pago entregado a {customer.display_name}",
        business_date=bdate,
        occurred_at=occurred_at_for(bdate, account.business.timezone, ctx.now),
        source_message_id=cmd.origin_message_id or ctx.source_message_id,
    )
    session.add(movement)
    await session.flush()
    attachment, guard = await associate_attachment(session, ctx, cmd.attachment_upload_id, movement, command=cmd.kind)
    audit.record(session, ctx, entity_type=EntityType.BROKER_MOVEMENT, entity_id=movement.id,
                 action=AuditAction.CREATE, before=None, after=audit.snapshot(movement), command=cmd.kind)
    return CommandResult(kind=cmd.kind, guard_reason=guard, data={
        "customer": customer.display_name, "amount": fmt_amount(amount, "USD"),
        "broker": account.label, "account_kind": account.kind,
        "customer_balance": await customer_balance(session, ctx.business_id, customer.id),
        "business_date": bdate.isoformat(), "description": cmd.description, "attachment": attachment is not None,
        **_balance_fields(await _account_balance(session, account), account, ctx),
    }, affected=[EntityRef(EntityType.BROKER_MOVEMENT, movement.id, version=movement.version,
                           summary=_summary(movement, account))])
