from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.application.commands.brokerage import (
    EditBrokerMovement,
    RecordBrokerAccountPurchase,
    RecordBrokerOperation,
    RecordBrokerPurchase,
    RecordPendingBrokerPurchase,
)
from app.db.models import BrokerMovement, Customer, Receivable
from app.domain.enums import ActorType
from app.domain.errors import MissingBrokerBolivars
from tests.integration.test_brokerage import _brokerage, _ctx


@pytest.mark.parametrize("channel", [ActorType.AGENT, ActorType.PANEL])
@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("command", [
    RecordBrokerOperation(operation_type="buy", operation_amount="100", customer_name="Nuevo"),
    RecordBrokerOperation(operation_type="sell", operation_amount="100"),
    RecordBrokerPurchase(amount="100", customer_name="Nuevo"),
    RecordPendingBrokerPurchase(amount="100", customer_name="Nuevo", customer_classification="personal"),
    RecordBrokerAccountPurchase(amount="100"),
])
async def test_missing_bs_rejects_before_any_business_write(bus, session_factory, channel, confirmed, command):
    business, owner, broker, _ = await _brokerage(session_factory)
    for member in (owner, broker):
        ctx = _ctx(business, member)
        ctx = replace(ctx, actor=replace(ctx.actor, type=channel))
        with pytest.raises(MissingBrokerBolivars, match="Dime la cantidad de bolívares"):
            await bus.dispatch(command, ctx, confirmed=confirmed)
    async with session_factory() as session:
        for table in (BrokerMovement, Receivable, Customer):
            assert await session.scalar(select(func.count()).select_from(table)) == 0


async def seed_legacy_incomplete(session_factory, movement_id):
    """Historical rows remain supported, but cannot be created through today's commands."""
    async with session_factory() as session, session.begin():
        row = await session.get(BrokerMovement, movement_id)
        row.operation_amount_bs = row.exchange_rate = None
        row.monetary_basis = row.exchange_rate_source = None
        row.monetary_provenance = {}


async def test_historical_incomplete_can_be_corrected_without_fx(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    result = await bus.dispatch(RecordBrokerOperation(operation_type="buy", operation_amount="100", exchange_rate="800"), ctx)
    mid = result.affected[0].entity_id
    await seed_legacy_incomplete(session_factory, mid)
    await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=1, description="Histórico", amount="200"), ctx)
    async with session_factory() as session:
        row = await session.get(BrokerMovement, mid)
        assert row.description == "Histórico" and row.operation_amount == 200
        assert row.operation_amount_bs is None and row.exchange_rate is None
    await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=2, exchange_rate="800"), ctx)
    async with session_factory() as session:
        row = await session.get(BrokerMovement, mid)
        assert row.operation_amount_bs == 160000 and row.commission == 2


async def test_complete_operation_cannot_lose_both_money_fields(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    result = await bus.dispatch(RecordBrokerOperation(operation_type="sell", operation_amount="100", exchange_rate="800"), ctx)
    mid = result.affected[0].entity_id
    with pytest.raises(MissingBrokerBolivars):
        await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=1,
                                             operation_amount_bs=None, exchange_rate=None), ctx)
    async with session_factory() as session:
        row = await session.get(BrokerMovement, mid)
        assert row.operation_amount_bs == 80000 and row.exchange_rate == 800 and row.version == 1
    # Removing a single supplied field keeps the other usable input and derives its counterpart.
    await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=1, operation_amount_bs=None), ctx)
    async with session_factory() as session:
        row = await session.get(BrokerMovement, mid)
        assert row.operation_amount_bs == Decimal("80000") and row.exchange_rate == Decimal("800")
