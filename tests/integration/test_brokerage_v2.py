import asyncio
from decimal import Decimal

import pytest

from app.application.bus import CommandBus, CommandDeps
from app.application.commands.brokerage import (
    DeleteBrokerMovement,
    EditBrokerMovement,
    RecordBrokerOperation,
    RecordBrokerReceivablePayment,
    RecordPendingBrokerPurchase,
)
from app.application.queries.brokerage import BrokerageQueries
from app.db.models import BrokerMovement, CustomerPayment, Receivable
from app.domain.errors import NeedsChoice, NotFound, ValidationFailed
from tests.integration.test_brokerage import _brokerage, _ctx, _summary_on
from tests.integration.test_brokerage_required_bs import seed_legacy_incomplete


class NoRates:
    async def effective(self, *args):
        raise AssertionError("Brokerage must not request reference rates")


async def test_real_money_and_corrections_preserve_input_pair(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    result = await bus.dispatch(RecordBrokerOperation(operation_type="buy", operation_amount="100",
                               exchange_rate="800"), ctx)
    mid = result.affected[0].entity_id
    await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=1, amount="200"), ctx)
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, mid)
        assert movement.operation_amount_bs == Decimal("160000")
        assert movement.exchange_rate == Decimal("800")
        assert movement.commission == Decimal("2")
    await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=2, operation_amount_bs="150000"), ctx)
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, mid)
        assert movement.exchange_rate == Decimal("750")
        assert movement.monetary_basis == "usd_bs"


async def test_pending_purchase_and_payment_need_no_reference_rates(session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    bus = CommandBus(session_factory, CommandDeps(rates=NoRates()))
    ctx = _ctx(business, broker)
    result = await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal", customer_name="Juan", amount="1000"), ctx)
    payment = await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Juan", amount="400"), ctx)
    async with session_factory() as session:
        debt = await session.get(Receivable, result.affected[1].entity_id)
        pay = await session.get(CustomerPayment, payment.affected[1].entity_id)
        assert debt.rate_applied is None and debt.amount_local is None
        assert pay.rate_applied is None and pay.amount_local is None
    # Bs can be completed after payments without changing principal/commission.
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, result.affected[0].entity_id)
        version = movement.version
    await bus.dispatch(EditBrokerMovement(movement_id=movement.id, expected_version=version,
                                         operation_amount_bs="800000"), ctx)


async def test_new_customer_choice_preserves_operation_and_creates_once(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    with pytest.raises(NeedsChoice) as choice:
        await bus.dispatch(RecordPendingBrokerPurchase(customer_name="Juan", amount="1000", exchange_rate="800"), ctx)
    from app.application.bus import load_command
    option = choice.value.options[0]
    result = await bus.dispatch(load_command(option["command_kind"], option["command_payload"]), ctx)
    assert result.data["operation_amount_bs"] == "800000.00"
    assert result.data["balance_raw"] == "10.00"


async def test_global_purchase_shared_payment_and_undo(bus, session_factory):
    business, owner, samanta, samuel = await _brokerage(session_factory)
    purchase = await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_name="Juan", amount="1000",
                                 customer_classification="global"), _ctx(business, samanta))
    assert purchase.data["balance_raw"] == "10.00"
    payment = await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Juan", amount="400"), _ctx(business, samuel))
    assert payment.data["balance_raw"] == "0.00"
    assert payment.data["customer_balance"]["display"] == "$600,00"
    queries = BrokerageQueries(session_factory)
    shared = await queries.receivable(business, samuel, purchase.affected[1].entity_id)
    assert shared["remaining"] == "600.00"
    original = await queries.movement(business, samuel, purchase.affected[0].entity_id)
    assert original["editable"] is False
    assert original["settlement"] == "partial"
    with pytest.raises(ValidationFailed):
        await bus.dispatch(EditBrokerMovement(movement_id=purchase.affected[0].entity_id,
                                             expected_version=original["version"], description="ajeno"), _ctx(business, samuel))
    await bus.dispatch(DeleteBrokerMovement(movement_id=payment.affected[0].entity_id), _ctx(business, samuel), confirmed=True)
    restored = await queries.receivable(business, owner, purchase.affected[1].entity_id)
    assert restored["remaining"] == "1000.00"
    assert (await queries.movement(business, samanta, purchase.affected[0].entity_id))["balance_delta"] == "10.0000"


async def test_personal_identity_is_hidden_from_other_broker(bus, session_factory):
    business, _, one, two = await _brokerage(session_factory)
    purchase = await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_name="Privado", amount="1000",
                                 customer_classification="personal"), _ctx(business, one))
    with pytest.raises(NotFound):
        await BrokerageQueries(session_factory).receivable(business, two, purchase.affected[1].entity_id)
    with pytest.raises(NotFound):
        await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Privado", amount="400"), _ctx(business, two))
    from app.application.queries.service import QueryService
    from app.domain.enums import EntityType
    queries = QueryService(session_factory, NoRates())
    refs = [(EntityType.BROKER_MOVEMENT, purchase.affected[0].entity_id),
            (EntityType.RECEIVABLE, purchase.affected[1].entity_id)]
    assert await queries.load_refs(business, two, refs) == []


async def test_concurrent_global_payments_keep_fifo_and_balances(bus, session_factory):
    business, owner, one, two = await _brokerage(session_factory)
    purchase = await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_name="Global", amount="1000",
                                 customer_classification="global"), _ctx(business, one))
    await asyncio.gather(*(bus.dispatch(RecordBrokerReceivablePayment(customer_name="Global", amount="400"),
                                       _ctx(business, broker)) for broker in (one, two)))
    debt = await BrokerageQueries(session_factory).receivable(business, owner, purchase.affected[1].entity_id)
    assert debt["remaining"] == "200.00"


async def test_ambiguous_purchase_choice_is_atomic_and_preserves_money(bus, session_factory):
    from sqlalchemy import func, select

    from app.application.bus import load_command
    from app.application.commands.brokerage import RecordBrokerPurchase
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    with pytest.raises(NeedsChoice) as choice:
        await bus.dispatch(RecordBrokerPurchase(amount="100", operation_amount_bs="80000"), ctx)
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(BrokerMovement))).scalar() == 0
    option = choice.value.options[0]
    result = await bus.dispatch(load_command(option["command_kind"], option["command_payload"]), ctx)
    assert result.kind == "record_broker_operation"
    assert result.data["exchange_rate"] == "800.00"


async def test_summary_weights_complete_volume_and_reports_coverage(bus, session_factory):
    business, owner, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    for amount, bs in [("100", "75000"), ("300", "240000"), ("200", None)]:
        created = await bus.dispatch(RecordBrokerOperation(operation_type="buy", operation_amount=amount, operation_amount_bs=bs,
                                    exchange_rate="800" if bs is None else None), ctx)
        if bs is None:
            await seed_legacy_incomplete(session_factory, created.affected[0].entity_id)
    sale = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", operation_type="sell", operation_amount="1000"), ctx)
    await seed_legacy_incomplete(session_factory, sale.affected[0].entity_id)
    summary = await _summary_on(BrokerageQueries(session_factory), business, owner)
    buy = summary["multicurrency"]["buy"]
    assert summary["totals"]["bought"] == "600.00"
    assert buy["complete_usd"] == "400.00"
    assert buy["amount_bs"] == "315000.00"
    assert buy["weighted_rate"] == "787.50"
    assert buy["incomplete_count"] == 1 and buy["missing_usd"] == "200.00"
    sell = summary["multicurrency"]["sell"]
    assert sell["incomplete_count"] == 1 and sell["weighted_rate"] is None
