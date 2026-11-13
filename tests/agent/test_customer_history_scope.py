"""Regression: an owner's customer query must not inherit the Casa write scope."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.application.commands.brokerage import (
    CreateBrokerMember,
    RecordBrokerOperation,
    RecordBrokerReceivablePayment,
    RecordPendingBrokerPurchase,
)
from app.application.context import Actor, ExecContext
from app.db.models import BusinessMember, Customer
from app.domain.enums import ActorType, MemberRole
from tests.agent.test_brokerage_vertical import _set_brokerage
from tests.agent.test_pipeline import send


async def seed_shared_customer(container, session_factory):
    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    async with session_factory() as session:
        owner = (await session.scalars(select(BusinessMember))).one()
        context = ExecContext(business_id=owner.business_id, actor=Actor(ActorType.PANEL, owner.id))
    await container.bus.dispatch(CreateBrokerMember(name="Alex", phone="584140000099"), context)
    for command in (
        RecordPendingBrokerPurchase(customer_name="Carolina Example", customer_classification="global",
                                    amount="600", exchange_rate="950", broker_name="Alex"),
        RecordBrokerReceivablePayment(customer_name="Carolina Example", amount="600", broker_name="Alex"),
        RecordBrokerOperation(customer_name="Carolina Example", operation_type="buy", operation_amount="160", exchange_rate="950"),
        RecordPendingBrokerPurchase(customer_name="Carolina Example", amount="240", exchange_rate="950"),
        RecordBrokerReceivablePayment(customer_name="Carolina Example", amount="240"),
    ):
        await container.bus.dispatch(command, context)
    async with session_factory() as session, session.begin():
        customer = (await session.scalars(select(Customer))).one()
        customer.nickname = "CAP"
        session.add(Customer(business_id=owner.business_id, name="CAP", normalized_name="cap", classification="global"))


@pytest.mark.parametrize("tool", ["find_broker_movements", "find_broker_receivables"])
@pytest.mark.parametrize("scope,explicit,expected", [("house", False, 5), ("house", True, 3), ("default", False, 5)])
async def test_owner_customer_history_requires_explicit_account_filter(env, script, session_factory, business, tool, scope, explicit, expected):
    container, _ = env
    await seed_shared_customer(container, session_factory)
    args = {"customer_name": "CAP", "account_scope": scope, "account_filter_explicitly_requested": explicit}
    if tool == "find_broker_receivables":
        args["include_movements"] = True
    script.steps = [{"tool": tool, "args": args}, {"text": "Estas son las operaciones consultadas."}]
    await send(container, "Movimientos de CAP solo de Casa" if explicit else "Qué operaciones se han registrado de CAP?")
    result = script.seen_tool_returns[-1]
    assert result["ok"], result
    history = result if tool == "find_broker_movements" else result["customer_accounts"][0]
    assert history["history_total"] == expected
    assert history["history_complete"] is True
    assert len(history["movements"]) == expected
    assert history["history_scope"] == ("house" if explicit else "all")
    if tool == "find_broker_receivables":
        assert result["total"] == 1
        assert result["customer_accounts"][0]["customer"] == "Carolina Example"
        assert Decimal(result["customer_accounts"][0]["balance_usd"]) == 0


@pytest.mark.parametrize("tool", ["find_broker_movements", "find_broker_receivables"])
async def test_broker_shared_customer_history_includes_house_without_leaking_broker_data(env, script, session_factory, business, tool):
    container, _ = env
    await seed_shared_customer(container, session_factory)
    async with session_factory() as session, session.begin():
        viewer = (await session.scalars(select(BusinessMember).where(BusinessMember.phone == "584140000001"))).one()
        viewer.role = MemberRole.BROKER.value
    args = {"customer_name": "CAP"}
    if tool == "find_broker_receivables":
        args["include_movements"] = True
    script.steps = [{"tool": tool, "args": args}, {"text": "Historial de Carolina Example."}]
    await send(container, "Operaciones de CAP")
    result = script.seen_tool_returns[-1]
    assert result["ok"], result
    history = result if tool == "find_broker_movements" else result["customer_accounts"][0]
    assert history["history_total"] == 5
    forbidden = {"broker", "recorded_by", "commission", "commission_rate", "balance_delta", "balance_after", "member_id", "customer_id"}
    assert all(not forbidden.intersection(row) for row in history["movements"])


async def test_owner_explicit_broker_filter_and_truncated_customer_history(env, script, session_factory, business):
    container, _ = env
    await seed_shared_customer(container, session_factory)
    script.steps = [{"tool": "find_broker_movements", "args": {
        "customer_name": "CAP", "broker_name": "Alex", "account_filter_explicitly_requested": True, "limit": 1,
    }}, {"text": "Una de las dos operaciones de Alex para Carolina."}]
    await send(container, "Movimientos de CAP solo de Alex")
    result = script.seen_tool_returns[-1]
    assert result["ok"], result
    assert result["history_scope"] == "broker"
    assert result["history_total"] == 2
    assert len(result["movements"]) == 1
    assert result["history_complete"] is False
