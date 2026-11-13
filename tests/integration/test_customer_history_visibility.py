"""Customer identity ranking must never bypass tenant or portfolio visibility."""

from datetime import date

import pytest
from sqlalchemy import select

from app.application.commands.brokerage import RecordPendingBrokerPurchase
from app.application.queries.brokerage import BrokerageQueries
from app.db.models import Customer
from app.domain.customers import normalize_nickname
from app.domain.errors import NotFound
from app.domain.text import normalize_name
from tests.integration.test_brokerage import _brokerage, _ctx


@pytest.mark.parametrize("nickname", ["ABC", "cliente-42", "ÁREA 7"])
async def test_identity_resolution_is_independent_of_customer_names_and_nicknames(session_factory, nickname):
    business, owner, _, _ = await _brokerage(session_factory)
    async with session_factory() as session, session.begin():
        historical = Customer(business_id=business, name=nickname,
                              normalized_name=normalize_name(nickname), classification="global")
        intended = Customer(business_id=business, name="Cliente Compartido",
                            normalized_name="cliente compartido", nickname=normalize_nickname(nickname),
                            classification="global")
        session.add_all([historical, intended])
        await session.flush()
        intended_id = str(intended.id)
    queries = BrokerageQueries(session_factory)
    for reference in (nickname, nickname.lower(), f"  {nickname}  ", "Cliente Compartido"):
        result = await queries.customer_accounts(business, owner, customer_name=reference, status="all")
        assert result["total"] == 1
        assert result["items"][0]["customer_id"] == intended_id


async def test_duplicate_exact_names_remain_ambiguous(session_factory):
    business, owner, _, _ = await _brokerage(session_factory)
    async with session_factory() as session, session.begin():
        session.add_all([
            Customer(business_id=business, name="Cliente Homónimo", normalized_name="cliente homonimo",
                     nickname=nickname, classification="global")
            for nickname in ("PRIMERO", "SEGUNDO")
        ])
    result = await BrokerageQueries(session_factory).customer_accounts(
        business, owner, customer_name="Cliente Homónimo", status="all")
    assert result["total"] == 2


async def test_exact_nickname_priority_is_applied_only_to_visible_customers(bus, session_factory):
    business, owner, broker, other = await _brokerage(session_factory)
    foreign, foreign_owner, _, _ = await _brokerage(session_factory)
    async with session_factory() as session, session.begin():
        visible = Customer(business_id=business, name="CAP", normalized_name="cap", classification="global")
        hidden = Customer(business_id=business, name="Carolina Example", normalized_name="carolina parra",
                          nickname="CAP", classification="personal", owner_broker_member_id=broker)
        foreign_customer = Customer(business_id=foreign, name="Foreign CAP", normalized_name="foreign cap",
                                    nickname="CAP", classification="global")
        session.add_all([visible, hidden, foreign_customer])
        await session.flush()
        visible_id, hidden_id = visible.id, hidden.id
    queries = BrokerageQueries(session_factory)
    for viewer, expected in ((owner, hidden_id), (broker, hidden_id), (other, visible_id)):
        result = await queries.customer_accounts(business, viewer, customer_name="CAP", status="all")
        assert result["total"] == 1
        assert result["items"][0]["customer_id"] == str(expected)
    with pytest.raises(NotFound):
        await queries.customer_account(business, other, hidden_id, account_scope="all")
    with pytest.raises(NotFound):
        await queries.customer_account(foreign, foreign_owner, hidden_id)


async def test_customer_history_date_range_remains_separate_from_current_balance(bus, session_factory):
    business, owner, broker, _ = await _brokerage(session_factory)
    await bus.dispatch(RecordPendingBrokerPurchase(amount="100", exchange_rate="950", customer_name="Carolina Example",
                       customer_classification="global"), _ctx(business, broker))
    async with session_factory() as session:
        customer_id = (await session.scalars(select(Customer).where(Customer.business_id == business))).one().id
    history = await BrokerageQueries(session_factory).customer_account(
        business, owner, customer_id, date_from=date(2026, 11, 1))
    assert history["items"] == [] and history["total"] == 0
    assert history["balance_usd"] == "-100.00"
