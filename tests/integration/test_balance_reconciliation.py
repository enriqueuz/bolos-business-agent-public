"""Financial acceptance: target balances, permissions, concurrency and real-money projections."""
import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agent.receipts import render, render_preview
from app.application.bus import load_command
from app.application.commands.brokerage import (
    DeleteBrokerMovement,
    EditBrokerMovement,
    RecordBrokerAccountPayment,
    RecordBrokerAccountPurchase,
    RecordBrokerReceivablePayment,
    RecordPendingBrokerPurchase,
)
from app.application.commands.customers import DeleteCustomer, MergeCustomers, UpsertCustomer
from app.application.commands.reconciliation import DeleteBalanceAdjustment, ReconcileAccountBalance
from app.application.customer_accounts import account_balances
from app.application.exports.brokerage import BrokerageWorkbook
from app.application.queries.brokerage import BrokerageQueries
from app.db.models import (
    AuditLog,
    BrokerDraftExecution,
    BrokerMovement,
    Customer,
    CustomerBalanceAdjustment,
    CustomerPayment,
    InboundMessage,
    Receivable,
)
from app.domain.errors import Conflict, DomainError, NeedsChoice, ValidationFailed
from tests.integration.test_brokerage import _brokerage, _ctx
from tests.integration.test_brokerage_workbook import WorkbookView

NOW = datetime(2026, 11, 4, 16, tzinfo=UTC)
CUTOFF = date(2026, 10, 31)


@pytest.fixture
async def reconciliation_env(session_factory):
    business, owner, broker, other = await _brokerage(session_factory)
    async with session_factory() as db, db.begin():
        inbound = InboundMessage(business_id=business, provider="console", type="text", body="ajustar saldo")
        db.add(inbound)
        await db.flush()
        origin = inbound.id
    ctx = replace(_ctx(business, owner), now=NOW, timezone="America/Caracas", source_message_id=origin,
                  reconciliation_operation_id=uuid4())
    return business, owner, broker, other, ctx


def reconciliation_command(ctx, name="Samu", balance="-900", **changes):
    return ReconcileAccountBalance(holder_name=name, declared_balance=balance, business_date=CUTOFF,
        draft_operation_id=ctx.reconciliation_operation_id, origin_message_id=ctx.source_message_id, **changes)


async def confirm(bus, ctx, command):
    preview = await bus.dispatch(command, ctx)
    if preview.needs_confirmation:
        result = await bus.dispatch(load_command(command.kind, preview.command_payload), ctx, confirmed=True)
        assert not result.needs_confirmation
        return result
    return preview


async def create_customer(bus, ctx, name="Cliente", personal=None):
    result = await bus.dispatch(UpsertCustomer(name=name, customer_classification="personal" if personal else "global",
        owner_broker_member_id=personal), ctx)
    return result.affected[0].entity_id


@pytest.mark.parametrize("account,baseline,declared,expected", [
    ("broker", "100.00", "-900", "-1000.00"),
    ("customer", "300", "-700", "-1000.00"),
    ("customer", "150", "500", "350.00"),
])
async def test_acceptance_examples(bus, session_factory, reconciliation_env, account, baseline, declared, expected):
    business, owner, broker, _, ctx = reconciliation_env
    if account == "broker":
        await bus.dispatch(RecordBrokerAccountPurchase(broker_id=broker, amount="101.01", exchange_rate="800"), ctx)
        name = "Samu"
    else:
        name = "Pedro" if declared == "-700" else "María"
        await bus.dispatch(RecordPendingBrokerPurchase(customer_name=name, customer_classification="global",
            amount=baseline, exchange_rate="800"), ctx)
    before = await BrokerageQueries(session_factory).summary(business, owner)
    preview = await bus.dispatch(reconciliation_command(ctx, name, declared, account_type=account), ctx)
    assert preview.needs_confirmation
    assert Decimal(preview.data["current_balance"]) == -Decimal(baseline)
    assert preview.data["new_balance"] == expected
    text = render_preview(preview)
    assert "Saldo actual (04/11)" in text and "Nuevo saldo actual (04/11)" in text
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(CustomerBalanceAdjustment)) == 0
        assert await db.scalar(select(func.count()).select_from(BrokerMovement).where(BrokerMovement.kind == "adjustment")) == 0
    result = await bus.dispatch(load_command(preview.kind, preview.command_payload), ctx, confirmed=True)
    assert result.data["new_balance"] == expected
    assert "Ajuste de saldo registrado" in render(result)
    after = await BrokerageQueries(session_factory).summary(business, owner)
    for key in ("payments_to_accounts", "commission", "excess", "bought", "sold"):
        if key in before["totals"]:
            assert before["totals"][key] == after["totals"][key]
    async with session_factory() as db:
        if account == "broker":
            movement = await db.get(BrokerMovement, result.affected[0].entity_id)
            assert movement.balance_delta == Decimal(declared)
            assert all(getattr(movement, field) is None for field in (
                "commission", "commission_rate", "operation_type", "operation_amount", "customer_id",
                "related_receivable_id", "related_payment_id", "operation_amount_bs", "received_amount"))
        else:
            assert await db.scalar(select(func.count()).select_from(Receivable)) == 1
        assert await db.scalar(select(func.count()).select_from(CustomerPayment)) == 0


async def test_target_correction_zero_and_post_cutoff_activity(bus, session_factory, reconciliation_env):
    business, _, broker, _, ctx = reconciliation_env
    await confirm(bus, ctx, reconciliation_command(ctx, balance="-700"))
    for amount in ("50", "20", "30"):
        await bus.dispatch(RecordBrokerAccountPayment(broker_id=broker, amount=amount), ctx)
    ctx = replace(ctx, reconciliation_operation_id=uuid4())
    command = reconciliation_command(ctx, balance="-700")
    unchanged = await bus.dispatch(command, ctx)
    assert unchanged.data["no_change"] and not unchanged.affected and not unchanged.needs_confirmation
    command = reconciliation_command(ctx, balance="-800")
    result = await confirm(bus, ctx, command)
    assert result.data["cutoff_balance"] == "-700.00"
    assert result.data["adjustment_delta"] == "-100.00"
    assert result.data["new_balance"] == "-700.00"
    ctx = replace(ctx, reconciliation_operation_id=uuid4())
    zero = await confirm(bus, ctx, reconciliation_command(ctx, balance="0"))
    assert zero.data["adjustment_delta"] == "800.00" and zero.data["new_balance"] == "100.00"
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement).where(BrokerMovement.kind == "adjustment")) == 3


async def test_prior_operations_count_in_cutoff(bus, reconciliation_env):
    _, _, broker, _, ctx = reconciliation_env
    await bus.dispatch(RecordBrokerAccountPayment(broker_id=broker, amount="200", business_date=CUTOFF-timedelta(days=1)), ctx)
    preview = await bus.dispatch(reconciliation_command(ctx, balance="-800"), ctx)
    assert preview.data["cutoff_balance"] == "200.00"
    assert preview.data["adjustment_delta"] == "-1000.00"


@pytest.mark.parametrize("actor,target,allowed", [
    ("owner", "self", True), ("owner", "other", True), ("owner", "global", True),
    ("owner", "personal", True), ("owner", "foreign_personal", True),
    ("broker", "self", True), ("broker", "other", True), ("broker", "global", True),
    ("broker", "personal", True), ("broker", "foreign_personal", False),
])
async def test_permissions(bus, session_factory, reconciliation_env, actor, target, allowed):
    business, owner, broker, other, ctx = reconciliation_env
    cid = None
    if target in {"global", "personal", "foreign_personal"}:
        cid = await create_customer(bus, ctx, personal=broker if target == "personal" else other if target == "foreign_personal" else None)
    ctx = replace(ctx, actor=replace(ctx.actor, member_id=owner if actor == "owner" else broker))
    changes = {"account_type": "customer", "customer_id": cid} if cid else {"account_type": "broker", "broker_id": broker if target == "self" else other}
    command = reconciliation_command(ctx, "Cliente" if cid else "Samu", **changes)
    if not allowed:
        with pytest.raises(DomainError):
            await bus.dispatch(command, ctx, confirmed=True)
        # Name lookup must neither reveal nor duplicate a hidden customer.
        with pytest.raises(DomainError):
            await bus.dispatch(command.model_copy(update={"customer_id": None}), ctx)
        async with session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(CustomerBalanceAdjustment)) == 0
        return
    result = await confirm(bus, ctx, command)
    assert result.affected
    if target == "other" and actor == "broker":
        with pytest.raises(ValidationFailed):
            await bus.dispatch(RecordBrokerAccountPayment(broker_id=other, amount="1"), ctx)


async def test_untrusted_context_and_other_business_rejected(bus, session_factory, reconciliation_env):
    _, _, _, _, ctx = reconciliation_env
    command = reconciliation_command(ctx)
    with pytest.raises(ValidationFailed):
        await bus.dispatch(command, replace(ctx, reconciliation_operation_id=None), confirmed=True)
    other_business, _, other_broker, _ = await _brokerage_unique(session_factory)
    assert other_business != ctx.business_id
    with pytest.raises(DomainError):
        await bus.dispatch(command.model_copy(update={"broker_id": other_broker}), ctx)


async def _brokerage_unique(factory):
    from app.db.models import Business, BusinessMember
    async with factory() as db, db.begin():
        business = Business(vertical="brokerage")
        db.add(business)
        await db.flush()
        member = BusinessMember(business_id=business.id, phone="584140009999", name="Otro", role="broker")
        db.add(member)
        await db.flush()
    return business.id, None, member.id, None


async def test_concurrent_confirmation_and_durable_replay(bus, session_factory, reconciliation_env):
    _, _, broker, _, ctx = reconciliation_env
    preview = await bus.dispatch(reconciliation_command(ctx), ctx)
    command = load_command(preview.kind, preview.command_payload)
    await bus.dispatch(RecordBrokerAccountPayment(broker_id=broker, amount="100"), ctx)
    refreshed = await bus.dispatch(command, ctx, confirmed=True)
    assert refreshed.needs_confirmation and refreshed.data["new_balance"] == "-800.00"
    command = load_command(refreshed.kind, refreshed.command_payload)
    first, second = await asyncio.gather(bus.dispatch(command, ctx, confirmed=True), bus.dispatch(command, ctx, confirmed=True))
    assert first.data == second.data
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement).where(BrokerMovement.kind == "adjustment")) == 1
        assert await db.scalar(select(func.count()).select_from(BrokerDraftExecution)) == 1
    with pytest.raises(ValidationFailed):
        await bus.dispatch(command, replace(ctx, reconciliation_operation_id=None), confirmed=True)


async def test_new_customer_is_created_only_after_confirmation_and_failure_is_atomic(bus, session_factory, reconciliation_env):
    _, _, _, _, ctx = reconciliation_env
    command = reconciliation_command(ctx, "Nuevo", account_type="customer", customer_classification="global")
    preview = await bus.dispatch(command, ctx)
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(Customer)) == 0
        assert await db.scalar(select(func.count()).select_from(AuditLog)) == 0
    invalid = load_command(command.kind, {**preview.command_payload, "origin_message_id": str(uuid4())})
    with pytest.raises(IntegrityError):
        await bus.dispatch(invalid, ctx, confirmed=True)
    async with session_factory() as db:
        for model in (Customer, CustomerBalanceAdjustment, AuditLog, BrokerDraftExecution):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
    assert (await bus.dispatch(load_command(command.kind, preview.command_payload), ctx, confirmed=True)).affected


async def test_customer_payment_fifo_net_history_export_and_merge(bus, session_factory, reconciliation_env):
    business, owner, _, _, ctx = reconciliation_env
    cid = await create_customer(bus, ctx)
    result = await confirm(bus, ctx, reconciliation_command(ctx, "Cliente", "-700", account_type="customer", customer_id=cid))
    await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Cliente", amount="700"), ctx, confirmed=True)
    query = BrokerageQueries(session_factory)
    history = await query.customer_account(business, owner, cid, include_balance_steps=True)
    assert history["balance_usd"] == "0.00" and history["total"] == 2
    assert history["items"][-1]["entry_type"] == "customer_balance_adjustment"
    assert history["items"][0]["customer_balance_after"] == "0.00"
    page = await query.customer_account(business, owner, cid, limit=1, offset=1)
    assert page["items"][0]["id"] == str(result.affected[0].entity_id)
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(Receivable)) == 0
        payment = (await db.scalars(select(CustomerPayment))).one()
        assert payment.unapplied_amount == Decimal("700") and not payment.allocations
        assert (await account_balances(db, business, cutoff=CUTOFF))[(cid, None)] == Decimal("-700")
    book = WorkbookView(await BrokerageWorkbook(session_factory).build(business,
        date_from=date(2026, 10, 1), date_to=date(2026, 11, 4)))
    row = book.records("Cartera")[0]
    assert row["Saldo neto USD"] == 0 and row["A favor USD"] == 0 and row["Ajustes de saldo USD"] == -700
    assert any(e["Tipo"] == "Ajuste de saldo" for e in book.records("MovimientosCliente"))
    with pytest.raises(Conflict):
        await bus.dispatch(DeleteCustomer(customer_id=cid), ctx)
    target = await create_customer(bus, ctx, "Destino")
    await bus.dispatch(MergeCustomers(source_customer_id=cid, target_customer_id=target), ctx)
    async with session_factory() as db:
        adjustment = await db.get(CustomerBalanceAdjustment, result.affected[0].entity_id)
        assert adjustment.customer_id == target


@pytest.mark.parametrize("kind", ["broker", "customer"])
async def test_delete_adjustment_permissions_confirmation_and_replay(bus, session_factory, reconciliation_env, kind):
    business, owner, broker, other, ctx = reconciliation_env
    if kind == "customer":
        await create_customer(bus, ctx)
    author_ctx = replace(ctx, actor=replace(ctx.actor, member_id=broker))
    result = await confirm(bus, author_ctx, reconciliation_command(author_ctx,
        "Samu" if kind == "broker" else "Cliente", account_type=kind))
    ref = result.affected[0]
    delete_ctx = replace(author_ctx, reconciliation_operation_id=uuid4())
    command = DeleteBalanceAdjustment(entity_type=ref.entity_type.value, entity_id=ref.entity_id,
        expected_version=ref.version, draft_operation_id=delete_ctx.reconciliation_operation_id,
        origin_message_id=ctx.source_message_id)
    forbidden = replace(delete_ctx, actor=replace(ctx.actor, member_id=other))
    with pytest.raises(ValidationFailed):
        await bus.dispatch(command, forbidden)
    preview = await bus.dispatch(command, delete_ctx)
    assert preview.needs_confirmation and preview.data["new_balance"] == "0.00"
    frozen = load_command(command.kind, preview.command_payload)
    deleted = await bus.dispatch(frozen, delete_ctx, confirmed=True)
    assert "eliminado" in render(deleted)
    assert (await bus.dispatch(frozen, delete_ctx, confirmed=True)).data == deleted.data
    async with session_factory() as db:
        model = BrokerMovement if kind == "broker" else CustomerBalanceAdjustment
        assert (await db.get(model, ref.entity_id)).deleted_at is not None
    if kind == "broker":
        fresh_ctx = replace(ctx, reconciliation_operation_id=uuid4())
        another = await confirm(bus, fresh_ctx, reconciliation_command(fresh_ctx))
        for normal_command in (DeleteBrokerMovement(movement_id=another.affected[0].entity_id),
                               EditBrokerMovement(movement_id=another.affected[0].entity_id, expected_version=1, amount="10")):
            with pytest.raises(ValidationFailed):
                await bus.dispatch(normal_command, ctx, confirmed=True)


async def test_ambiguous_customer_and_broker_never_writes(bus, session_factory, reconciliation_env):
    _, _, _, _, ctx = reconciliation_env
    await create_customer(bus, ctx, "Samu")
    with pytest.raises(NeedsChoice) as caught:
        await bus.dispatch(reconciliation_command(ctx), ctx)
    assert {option["command_payload"]["account_type"] for option in caught.value.options} == {"broker", "customer"}
    for option in caught.value.options:
        result = await bus.dispatch(load_command("reconcile_account_balance", option["command_payload"]), ctx, confirmed=True)
        assert result.needs_confirmation


async def test_migration_roundtrip_preserves_existing_movements(bus, engine, reconciliation_env):
    from sqlalchemy import text

    from tests.integration.test_migration_0023 import alembic
    _, _, broker, _, ctx = reconciliation_env
    existing = await bus.dispatch(RecordBrokerAccountPayment(broker_id=broker, amount="50"), ctx)
    try:
        alembic("downgrade", "0027")
        alembic("upgrade", "head")
        async with engine.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0029"
            assert await connection.scalar(text("SELECT balance_delta FROM broker_movements WHERE id=:id"),
                                           {"id": existing.affected[0].entity_id}) == Decimal("50")
    finally:
        alembic("upgrade", "head")


@pytest.mark.parametrize("kind", ["broker", "customer"])
async def test_downgrade_preserves_even_deleted_adjustments(bus, reconciliation_env, kind):
    from tests.integration.test_migration_0023 import alembic
    _, _, _, _, ctx = reconciliation_env
    if kind == "customer":
        await create_customer(bus, ctx)
    result = await confirm(bus, ctx, reconciliation_command(ctx, "Samu" if kind == "broker" else "Cliente", account_type=kind))
    ref = result.affected[0]
    delete_ctx = replace(ctx, reconciliation_operation_id=uuid4())
    await confirm(bus, delete_ctx, DeleteBalanceAdjustment(entity_type=ref.entity_type.value, entity_id=ref.entity_id,
        expected_version=ref.version, draft_operation_id=delete_ctx.reconciliation_operation_id, origin_message_id=ctx.source_message_id))
    attempt = alembic("downgrade", "0027", check=False)
    assert attempt.returncode != 0 and "historical adjustments must be preserved" in attempt.stderr


async def test_database_rejects_commission_or_zero_adjustments(session_factory, reconciliation_env):
    business, owner, broker, _, _ = reconciliation_env
    async with session_factory() as db:
        db.add(BrokerMovement(business_id=business, broker_member_id=broker, recorded_by_member_id=owner,
            kind="adjustment", balance_delta=Decimal("-100"), commission=Decimal("0"), currency="USD",
            description="Reconciliación de saldo", business_date=CUTOFF, occurred_at=NOW))
        with pytest.raises(IntegrityError):
            await db.commit()


async def test_actor_deactivation_and_changed_cutoff_require_revalidation(bus, session_factory, reconciliation_env):
    from app.db.models import BusinessMember
    _, _, broker, _, ctx = reconciliation_env
    payment = await bus.dispatch(RecordBrokerAccountPayment(broker_id=broker, amount="100"), ctx)
    preview = await bus.dispatch(reconciliation_command(ctx), ctx)
    frozen = load_command(preview.kind, preview.command_payload)
    await bus.dispatch(EditBrokerMovement(movement_id=payment.affected[0].entity_id, expected_version=1,
        business_date=CUTOFF), ctx)
    refreshed = await bus.dispatch(frozen, ctx, confirmed=True)
    assert refreshed.needs_confirmation and refreshed.data["current_balance"] == preview.data["current_balance"]
    assert refreshed.data["adjustment_delta"] == "-1000.00"
    async with session_factory() as db, db.begin():
        actor = await db.get(BusinessMember, ctx.actor.member_id)
        actor.is_active = False
    with pytest.raises(ValidationFailed):
        await bus.dispatch(load_command(refreshed.kind, refreshed.command_payload), ctx, confirmed=True)


@pytest.mark.parametrize("name,principal,declared,november_balance,october_balance", [
    ("Laura", "90", "500", "410.00", "500.00"),
    ("Pepe", "500", "-500", "-1000.00", "-500.00"),
    ("Sujaila", "2000", "-800", "-2800.00", "-800.00"),
])
@pytest.mark.parametrize("purchase_date", [date(2026, 10, 31), date(2026, 11, 1)])
async def test_user_cases_distinguish_cutoff_from_later_operations(
        bus, reconciliation_env, name, principal, declared, november_balance, october_balance, purchase_date):
    _, _, _, _, ctx = reconciliation_env
    await bus.dispatch(RecordPendingBrokerPurchase(customer_name=name, customer_classification="global",
        amount=principal, exchange_rate="800", business_date=purchase_date), ctx)
    command = reconciliation_command(ctx, name, declared, account_type="customer")
    preview = await bus.dispatch(command, ctx)
    after_cutoff = purchase_date > CUTOFF
    assert Decimal(preview.data["cutoff_balance"]) == (0 if after_cutoff else -Decimal(principal))
    assert Decimal(preview.data["post_cutoff_delta"]) == (-Decimal(principal) if after_cutoff else 0)
    assert preview.data["new_balance"] == (november_balance if after_cutoff else october_balance)
    rendered = render(preview) if preview.data.get("no_change") else render_preview(preview)
    assert "Variación posterior al corte:" in rendered
    assert "Saldo actual (04/11):" in rendered
    if name == "Pepe":
        assert preview.data.get("no_change", False) is not after_cutoff
    if preview.needs_confirmation:
        saved = await bus.dispatch(load_command(command.kind, preview.command_payload), ctx, confirmed=True)
        assert saved.data["new_balance"] == (november_balance if after_cutoff else october_balance)
