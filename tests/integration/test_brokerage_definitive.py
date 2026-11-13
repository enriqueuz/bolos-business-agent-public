"""Financial acceptance: persisted nominal sales, independent receipts, and scopes."""
from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.application.attachments import AttachmentService
from app.application.bus import CommandBus, CommandDeps
from app.application.commands.brokerage import (
    CompleteBrokerReceipt,
    DeleteBrokerMovement,
    EditBrokerMovement,
    RecordBrokerAccountPurchase,
    RecordBrokerOperation,
    RecordBrokerPurchase,
    RecordBrokerReceivablePayment,
)
from app.application.queries.brokerage import BrokerageQueries
from app.db.models import BrokerMovement, Receivable
from app.domain.enums import ActorType
from app.domain.errors import MissingBrokerBolivars, ValidationFailed
from app.infra.storage.memory import MemoryAttachmentStore
from tests.integration.test_broker_attachments import _image
from tests.integration.test_brokerage import _brokerage, _ctx, _summary_on
from tests.integration.test_brokerage_v2 import NoRates


@pytest.mark.parametrize("house", [False, True])
@pytest.mark.parametrize("channel", [ActorType.AGENT, ActorType.PANEL])
@pytest.mark.parametrize("money", [{}, {"operation_amount_bs": "160000"}, {"exchange_rate": "800"}])
async def test_sales_persist_nominal_financial_effects(session_factory, house, channel, money):
    business, owner, broker, other = await _brokerage(session_factory)
    bus = CommandBus(session_factory, CommandDeps(rates=NoRates()))
    ctx = _ctx(business, owner if house else broker)
    ctx = replace(ctx, actor=replace(ctx.actor, type=channel))
    if not money:
        with pytest.raises(MissingBrokerBolivars):
            await bus.dispatch(RecordBrokerOperation(operation_type="sell", operation_amount="200"), ctx)
        return
    result = await bus.dispatch(RecordBrokerOperation(operation_type="sell", operation_amount="200", **money), ctx)
    async with session_factory() as session:
        row = await session.get(BrokerMovement, result.affected[0].entity_id)
        assert row.operation_amount == Decimal("200")
        assert row.excess == 0 and row.received_amount is None
        assert row.commission == (0 if house else Decimal("2"))
        assert row.balance_delta == (0 if house else Decimal("2"))
        assert row.broker_member_id == (None if house else broker)
        assert row.recorded_by_member_id == ctx.actor.member_id
        assert row.operation_amount_bs == (Decimal("160000") if money else None)
        assert row.exchange_rate == (Decimal("800") if money else None)
    queries = BrokerageQueries(session_factory)
    summary = await _summary_on(queries, business, owner)
    assert summary["totals"]["sold"] == "200.00"
    assert (await _summary_on(queries, business, other))["totals"]["sold"] == "0.00"
    if house:
        assert all(Decimal(item["balance"]) == 0 for item in summary["balances"])
    from app.agent.receipts import render
    receipt = render(result)
    assert ("No se registró el monto" in receipt) == (not money)
    assert "Salida por venta" not in receipt
    assert ("Sin comisión de corredor" in receipt) == house


@pytest.mark.parametrize("house", [False, True])
async def test_completion_replay_correction_and_delete_preserve_sales_effects(session_factory, house):
    business, owner, broker, _ = await _brokerage(session_factory)
    bus = CommandBus(session_factory, CommandDeps(rates=NoRates()))
    ctx = _ctx(business, owner if house else broker)
    sale = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", operation_type="sell", operation_amount="200"), ctx)
    mid = sale.affected[0].entity_id
    store = AttachmentService(session_factory, MemoryAttachmentStore())
    upload = await store.ingest(business_id=business, uploaded_by_member_id=ctx.actor.member_id, data=_image("red"))
    completion = CompleteBrokerReceipt(movement_id=mid, expected_version=1, attachment_upload_id=upload.id,
                                      operation_amount_bs="160000")
    await bus.dispatch(completion, ctx)
    await bus.dispatch(completion, ctx)
    await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=2, exchange_rate="850"), ctx)
    async with session_factory() as session:
        row = await session.get(BrokerMovement, mid)
        assert row.operation_amount == 200 and row.commission == (0 if house else 2)
        assert row.balance_delta == (0 if house else 2)
        assert row.operation_amount_bs == 170000 and row.exchange_rate == 850
        assert (await session.scalar(select(func.count()).select_from(BrokerMovement))) == 1
    await bus.dispatch(DeleteBrokerMovement(movement_id=mid, expected_version=3), ctx, confirmed=True)
    assert all(Decimal(b["balance"]) == 0 for b in (await _summary_on(BrokerageQueries(session_factory), business, owner))["balances"])


@pytest.mark.parametrize("modality,classification,delta", [
    ("settled", "personal", "22.87"), ("pending", "personal", "-964.13"),
    ("pending", "global", "22.87"), ("account", None, "-964.13"),
])
@pytest.mark.parametrize("channel", [ActorType.AGENT, ActorType.PANEL])
async def test_purchase_excess_is_independent_of_modality_and_money(bus, session_factory, modality, classification, delta, channel):
    business, owner, broker, other = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    ctx = replace(ctx, actor=replace(ctx.actor, type=channel))
    common = {"received_amount": "1000", "operation_amount_bs": "789600"}
    if modality == "account":
        command = RecordBrokerAccountPurchase(amount="987", **common)
    else:
        command = RecordBrokerPurchase(amount="987", customer_name="Gamal", settlement=modality,
                                      customer_classification=classification, **common)
    created = await bus.dispatch(command, ctx)
    mid = created.affected[0].entity_id
    async with session_factory() as session:
        row = await session.get(BrokerMovement, mid)
        assert row.operation_amount == 987 and row.received_amount == 1000
        assert row.excess == 13 and row.commission == Decimal("9.87")
        assert row.balance_delta == Decimal(delta) and row.exchange_rate == 800
        assert row.settlement_status == modality and row.broker_member_id == broker
        if modality == "pending":
            debt = await session.get(Receivable, row.related_receivable_id)
            assert debt.amount == 987 and debt.broker_member_id == (broker if classification == "personal" else None)
    # A corrected receipt changes only commercial FX; corrected received USD changes excess, never debt/commission.
    await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=1, exchange_rate="810"), ctx)
    await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=2, received_amount="1001"), ctx)
    async with session_factory() as session:
        row = await session.get(BrokerMovement, mid)
        assert row.excess == 14 and row.commission == Decimal("9.87")
        assert row.balance_delta == Decimal(delta) + 1
        assert row.operation_amount == 987 and row.operation_amount_bs == Decimal("799470")
    if modality == "pending":
        payer = other if classification == "global" else broker
        await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Gamal", amount="100"), _ctx(business, payer))
        async with session_factory() as session:
            row = await session.get(BrokerMovement, mid)
            assert row.excess == 14 and row.received_amount == 1001 and row.delivered_amount == 100
            assert row.balance_delta == Decimal(delta) + 1 and row.commission == Decimal("9.87")
        # Deleting the obligation preserves real received payments as customer credit.
        await bus.dispatch(DeleteBrokerMovement(movement_id=mid), ctx, confirmed=True)
        accounts = await BrokerageQueries(session_factory).customer_accounts(business, owner, customer_name="Gamal")
        assert accounts["items"][0]["balance_usd"] == "100.00"
    else:
        await bus.dispatch(DeleteBrokerMovement(movement_id=mid), ctx, confirmed=True)
        assert all(Decimal(b["balance"]) == 0 for b in (await _summary_on(BrokerageQueries(session_factory), business, owner))["balances"])


async def test_received_confirms_immediate_and_house_never_earns_excess(bus, session_factory):
    business, owner, broker, _ = await _brokerage(session_factory)
    for actor in (owner, broker):
        result = await bus.dispatch(RecordBrokerPurchase(exchange_rate="800", amount="987", received_amount="1000"), _ctx(business, actor))
        async with session_factory() as session:
            row = await session.get(BrokerMovement, result.affected[0].entity_id)
            assert row.settlement_status == "settled" and row.received_amount == 1000
            assert row.excess == (0 if actor == owner else 13)
            assert row.commission == (0 if actor == owner else Decimal("9.87"))
            assert row.balance_delta == (0 if actor == owner else Decimal("22.87"))


async def test_legacy_sale_and_short_purchase_fail_atomically(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    for command in (
        RecordBrokerOperation(exchange_rate="800", operation_type="sell", operation_amount="200", delivered_amount="195"),
        RecordBrokerPurchase(exchange_rate="800", amount="987", received_amount="980"),
    ):
        with pytest.raises(ValidationFailed, match="Aclara"):
            await bus.dispatch(command, _ctx(business, broker))
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 0


async def test_database_rejects_sale_excess_and_wrong_signed_delta(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    result = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", operation_type="sell", operation_amount="200"), _ctx(business, broker))
    for changes in ({"excess": Decimal("5")}, {"balance_delta": Decimal("-198")}):
        with pytest.raises(IntegrityError):
            async with session_factory() as session, session.begin():
                row = await session.get(BrokerMovement, result.affected[0].entity_id)
                for key, value in changes.items():
                    setattr(row, key, value)
                await session.flush()


@pytest.mark.parametrize("rate,commission,delta", [("0", "0", "0"), ("0.02", "4", "4"), ("1", "200", "200")])
async def test_sale_uses_configured_commission_even_when_owner_selects_broker(bus, session_factory, rate, commission, delta):
    from app.db.models import Business
    business, owner, broker, _ = await _brokerage(session_factory)
    async with session_factory() as session, session.begin():
        row = await session.get(Business, business)
        row.settings = {**(row.settings or {}), "broker_commission_rate": rate}
    result = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", operation_type="sell", operation_amount="200", broker_id=broker),
                                _ctx(business, owner))
    async with session_factory() as session:
        row = await session.get(BrokerMovement, result.affected[0].entity_id)
        assert row.broker_member_id == broker and row.recorded_by_member_id == owner
        assert row.commission == Decimal(commission) and row.balance_delta == Decimal(delta) and row.excess == 0


async def test_purchase_with_excess_receipt_completion_keeps_original_financial_effects(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    created = await bus.dispatch(RecordBrokerPurchase(exchange_rate="800", amount="987", received_amount="1000"), ctx)
    store = AttachmentService(session_factory, MemoryAttachmentStore())
    upload = await store.ingest(business_id=business, uploaded_by_member_id=broker, data=_image("blue"))
    mid = created.affected[0].entity_id
    command = CompleteBrokerReceipt(movement_id=mid, expected_version=1, attachment_upload_id=upload.id,
                                    operation_amount_bs="789600")
    await bus.dispatch(command, ctx)
    await bus.dispatch(command, ctx)
    async with session_factory() as session:
        row = await session.get(BrokerMovement, mid)
        assert row.operation_amount == 987 and row.received_amount == 1000 and row.exchange_rate == 800
        assert row.excess == 13 and row.commission == Decimal("9.87") and row.balance_delta == Decimal("22.87")
