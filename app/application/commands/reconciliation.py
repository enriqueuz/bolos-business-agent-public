"""Exceptional historical balance writes, using the existing command/confirmation path."""
from datetime import date
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import select

from app.application import audit
from app.application.brokerage_money import BrokerDecimal
from app.application.bus import Command, CommandResult, command_handler
from app.application.common import bump, check_version
from app.application.context import EntityRef
from app.application.reconciliation import (
    ADJUSTMENT_DESCRIPTION,
    authorize_customer,
    authorize_reconciliation,
    reconciliation_balances,
)
from app.application.resolvers import resolve_customer
from app.db.models import BrokerMovement, Business, BusinessMember, Customer, CustomerBalanceAdjustment
from app.domain.brokerage import q_money
from app.domain.dates import occurred_at_for
from app.domain.enums import AuditAction, EntityType, MemberRole
from app.domain.errors import Ambiguous, NeedsChoice, NotFound, ValidationFailed
from app.domain.text import normalize_name


class ReconciliationCommand(Command):
    draft_operation_id: UUID
    origin_message_id: UUID
    # Server-generated preview, never exposed as a model-visible tool input.
    expected_snapshot: dict | None = None


class ReconcileAccountBalance(ReconciliationCommand):
    kind: ClassVar[str] = "reconcile_account_balance"
    holder_name: str = Field(min_length=1, max_length=160)
    account_type: Literal["auto", "broker", "customer"] = "auto"
    broker_id: UUID | None = None
    customer_id: UUID | None = None
    customer_classification: Literal["global", "personal"] | None = None
    owner_broker_id: UUID | None = None
    force_new_customer: bool = False
    business_date: date
    declared_balance: BrokerDecimal


class DeleteBalanceAdjustment(ReconciliationCommand):
    kind: ClassVar[str] = "delete_balance_adjustment"
    entity_type: Literal["broker_movement", "customer_balance_adjustment"]
    entity_id: UUID
    expected_version: int


def _choice(command, title, **changes):
    return {"title": title, "action": "command", "command_kind": command.kind,
            "command_payload": {**command.model_dump(mode="json"), "expected_snapshot": None, **changes}}


async def _broker_candidates(session, ctx, name):
    brokers = list((await session.scalars(select(BusinessMember).where(
        BusinessMember.business_id == ctx.business_id, BusinessMember.role == MemberRole.BROKER.value,
    ).order_by(BusinessMember.name, BusinessMember.id))).all())
    term = normalize_name(name)
    if not term:
        return brokers[:8]
    exact = [b for b in brokers if term in {normalize_name(b.name or ""), normalize_name(b.abbreviation or "")}]
    return exact or [b for b in brokers if term in normalize_name(b.name or "")][:8]


async def _resolve_target(session, ctx, actor, command):
    brokers = []
    if command.account_type != "customer":
        if command.broker_id:
            broker = await session.get(BusinessMember, command.broker_id)
            if broker is None or broker.business_id != ctx.business_id or broker.role != MemberRole.BROKER.value:
                raise NotFound("Corredor no encontrado.")
            brokers = [broker]
        else:
            brokers = await _broker_candidates(session, ctx, command.holder_name)
    customer, candidates = None, []
    if command.account_type != "broker":
        try:
            customer, _ = await resolve_customer(session, ctx.business_id, command.holder_name,
                customer_id=command.customer_id, create_if_missing=False, brokerage=True,
                visible_broker_id=actor.id if actor.role == MemberRole.BROKER.value else None)
        except Ambiguous as exc:
            candidates = list(zip(exc.details["candidates"], exc.details["candidate_ids"], strict=True))
        except NotFound:
            if command.customer_id:
                raise
        if customer:
            authorize_customer(customer, ctx, actor)
            candidates = [(customer.display_name, str(customer.id))]
    options = [_choice(command, (b.name or "Corredor") + " · Corredor", account_type="broker", broker_id=str(b.id))
               for b in brokers]
    options += [_choice(command, name + " · Cliente", account_type="customer", customer_id=str(identifier))
                for name, identifier in candidates]
    if len(options) > 1:
        raise NeedsChoice("¿Qué cuenta deseas reconciliar?", options[:10])
    if brokers:
        broker = brokers[0]
        await session.execute(select(BusinessMember.id).where(BusinessMember.id == broker.id).with_for_update())
        return "broker", broker, None
    if candidates and customer is None:
        customer = await session.get(Customer, UUID(candidates[0][1]))
        authorize_customer(customer, ctx, actor)
    if customer:
        await session.execute(select(Customer.id).where(Customer.id == customer.id).with_for_update())
        return "customer", customer, customer.owner_broker_member_id
    if command.account_type == "broker":
        raise NotFound("Corredor no encontrado.")
    # Do not silently create a second identity for a hidden personal customer.
    hidden = await session.scalar(select(Customer.id).where(Customer.business_id == ctx.business_id,
        Customer.deleted_at.is_(None), Customer.normalized_name == normalize_name(command.holder_name)).limit(1))
    if hidden and not command.force_new_customer:
        raise ValidationFailed("No puedo crear ese cliente con estos datos. Solicita la revisión del dueño.")
    if command.customer_classification is None:
        raise NeedsChoice("¿El nuevo cliente es global o personal?", [
            _choice(command, title, account_type="customer", customer_classification=classification)
            for title, classification in [("Global", "global"), ("Personal", "personal")]])
    portfolio = None
    if command.customer_classification == "personal":
        portfolio = command.owner_broker_id
        if actor.role == MemberRole.BROKER.value:
            if portfolio is not None and portfolio != actor.id:
                raise ValidationFailed("Solo puedes crear clientes personales propios.")
            portfolio = actor.id
        if portfolio is None:
            all_brokers = await _broker_candidates(session, ctx, "")
            if not all_brokers:
                raise ValidationFailed("Primero registra el corredor propietario.")
            raise NeedsChoice("¿A qué corredor pertenece el nuevo cliente?", [
                _choice(command, b.name or "Corredor", owner_broker_id=str(b.id)) for b in all_brokers])
        owner = await session.get(BusinessMember, portfolio)
        if owner is None or owner.business_id != ctx.business_id or owner.role != MemberRole.BROKER.value:
            raise NotFound("Corredor no encontrado.")
    return "customer", None, portfolio


def _preview(command, data, snapshot):
    return CommandResult(kind=command.kind, data=data, status="needs_confirmation",
        confirmation_reason="Revisa el saldo actual y el nuevo saldo antes de confirmar.",
        command_payload={**command.model_dump(mode="json"), "expected_snapshot": snapshot})


def _snapshot(ctx, account_type, account_id, name, portfolio, cutoff, declared, at_cutoff, current, delta, **extra):
    return {"account_type": account_type, "account_id": str(account_id) if account_id else None,
            "holder": name, "portfolio_id": str(portfolio) if portfolio else None,
            "business_date": cutoff.isoformat(), "today": ctx.today.isoformat(),
            "declared_balance": str(declared), "cutoff_balance": str(at_cutoff),
            "post_cutoff_delta": str(q_money(current - at_cutoff)),
            "current_balance": str(current), "adjustment_delta": str(delta),
            "new_balance": str(q_money(current + delta)), **extra}


@command_handler(ReconcileAccountBalance)
async def reconcile_account_balance(command, ctx, session, deps):
    del deps
    _, actor = await authorize_reconciliation(session, ctx, command.draft_operation_id)
    await session.execute(select(Business.id).where(Business.id == ctx.business_id).with_for_update())
    if command.business_date > ctx.today:
        raise ValidationFailed("La fecha histórica no puede ser futura.")
    declared = q_money(command.declared_balance)
    account_type, account, portfolio = await _resolve_target(session, ctx, actor, command)
    account_id = account.id if account else None
    name = (account.display_name if account_type == "customer" else account.name) if account else command.holder_name
    at_cutoff, current = await reconciliation_balances(session, ctx, account_type=account_type,
        account_id=account_id, portfolio_id=portfolio, cutoff=command.business_date)
    delta = q_money(declared - at_cutoff)
    snapshot = _snapshot(ctx, account_type, account_id, name, portfolio, command.business_date,
                         declared, at_cutoff, current, delta,
                         account_version=account.version if account_type == "customer" and account else None,
                         classification=account.classification if account_type == "customer" and account else command.customer_classification)
    if delta == 0:
        return CommandResult(kind=command.kind, data={**snapshot, "no_change": True})
    # Freeze resolved identity, not a name that may resolve differently at confirmation.
    resolved = command.model_copy(update={"account_type": account_type,
        "broker_id": account_id if account_type == "broker" else None,
        "customer_id": account_id if account_type == "customer" else None})
    if not ctx.command_confirmed or command.expected_snapshot != snapshot:
        return _preview(resolved, snapshot, snapshot)
    if account_type == "customer":
        if account is None:
            account, created = await resolve_customer(session, ctx.business_id, command.holder_name,
                create_if_missing=True, force_new=command.force_new_customer, brokerage=True,
                visible_broker_id=actor.id if actor.role == MemberRole.BROKER.value else None,
                classification=command.customer_classification, owner_broker_member_id=portfolio)
            if not created:
                raise ValidationFailed("La cuenta cambió. Vuelve a preparar la reconciliación.")
            audit.record(session, ctx, entity_type=EntityType.CUSTOMER, entity_id=account.id,
                action=AuditAction.CREATE, before=None, after=audit.snapshot(account), command=command.kind)
        movement = CustomerBalanceAdjustment(business_id=ctx.business_id, customer_id=account.id,
            portfolio_broker_member_id=portfolio, recorded_by_member_id=actor.id, amount_delta=delta)
        entity_type = EntityType.CUSTOMER_BALANCE_ADJUSTMENT
    else:
        movement = BrokerMovement(business_id=ctx.business_id, broker_member_id=account.id,
            recorded_by_member_id=actor.id, kind="adjustment", balance_delta=delta, currency="USD")
        entity_type = EntityType.BROKER_MOVEMENT
    movement.business_date = command.business_date
    movement.occurred_at = occurred_at_for(command.business_date, ctx.timezone, ctx.now)
    movement.description = ADJUSTMENT_DESCRIPTION
    movement.source_message_id = command.origin_message_id
    session.add(movement)
    await session.flush()
    audit.record(session, ctx, entity_type=entity_type, entity_id=movement.id, action=AuditAction.CREATE,
                 before=None, after={**audit.snapshot(movement), "reconciliation": snapshot}, command=command.kind)
    # Read the actual persisted effect instead of treating the preview as a receipt.
    _, resulting = await reconciliation_balances(session, ctx, account_type=account_type,
        account_id=account.id, portfolio_id=portfolio, cutoff=command.business_date)
    return CommandResult(kind=command.kind, data={**snapshot, "new_balance": str(resulting),
        "holder": account.display_name if account_type == "customer" else account.name},
        affected=[EntityRef(entity_type, movement.id, version=movement.version,
                           summary=f"Ajuste de saldo | {name} | USD {delta} | {command.business_date}")])


@command_handler(DeleteBalanceAdjustment)
async def delete_balance_adjustment(command, ctx, session, deps):
    del deps
    _, actor = await authorize_reconciliation(session, ctx, command.draft_operation_id)
    await session.execute(select(Business.id).where(Business.id == ctx.business_id).with_for_update())
    model = BrokerMovement if command.entity_type == "broker_movement" else CustomerBalanceAdjustment
    movement = await session.get(model, command.entity_id)
    if (movement is None or movement.business_id != ctx.business_id or movement.deleted_at is not None
            or isinstance(movement, BrokerMovement) and movement.kind != "adjustment"):
        raise NotFound("Ajuste de saldo no encontrado.")
    if actor.role != MemberRole.OWNER.value and movement.recorded_by_member_id != actor.id:
        raise ValidationFailed("Solo el dueño o el autor pueden eliminar este ajuste.")
    check_version(movement, command.expected_version, "ajuste")
    if isinstance(movement, CustomerBalanceAdjustment):
        account_type, account, portfolio = "customer", movement.customer, movement.portfolio_broker_member_id
        authorize_customer(account, ctx, actor)
        delta = movement.amount_delta
        name = account.display_name
    else:
        account_type, account, portfolio = "broker", movement.broker, None
        delta = movement.balance_delta
        name = account.name
        await session.execute(select(BusinessMember.id).where(BusinessMember.id == account.id).with_for_update())
    at_cutoff, current = await reconciliation_balances(session, ctx, account_type=account_type,
        account_id=account.id, portfolio_id=portfolio, cutoff=movement.business_date)
    snapshot = _snapshot(ctx, account_type, account.id, name, portfolio, movement.business_date,
        q_money(at_cutoff - delta), at_cutoff, current, q_money(-delta), version=movement.version, deleting=True)
    if not ctx.command_confirmed or command.expected_snapshot != snapshot:
        return _preview(command, snapshot, snapshot)
    before = audit.snapshot(movement)
    movement.deleted_at = ctx.now
    bump(movement)
    await session.flush()
    entity_type = EntityType(command.entity_type)
    audit.record(session, ctx, entity_type=entity_type, entity_id=movement.id, action=AuditAction.DELETE,
                 before=before, after=None, command=command.kind)
    return CommandResult(kind=command.kind, data=snapshot,
        affected=[EntityRef(entity_type, movement.id, version=movement.version)])
