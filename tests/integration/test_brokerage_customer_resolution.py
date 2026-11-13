from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.application.commands.brokerage import RecordBrokerReceivablePayment, RecordPendingBrokerPurchase
from app.application.queries.brokerage import BrokerageQueries
from app.application.resolvers import resolve_customer
from app.db.models import BrokerMovement, Business, Customer, CustomerPayment, Receivable
from app.domain.errors import Ambiguous, NeedsChoice, NotFound
from app.domain.text import normalize_name
from tests.integration.test_brokerage import _brokerage, _ctx


async def seed_customer(factory, business, name, classification="global", owner=None):
    async with factory() as session, session.begin():
        customer = Customer(business_id=business, name=name, normalized_name=normalize_name(name),
                            classification=classification, owner_broker_member_id=owner)
        session.add(customer)
        await session.flush()
        return customer.id


async def test_fuzzy_customer_never_silently_receives_brokerage_debt(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    existing = await seed_customer(session_factory, business, "QA V2 Global Pendiente")
    with pytest.raises(Ambiguous) as error:
        await bus.dispatch(RecordPendingBrokerPurchase(customer_name="QA V2 Global Samu", amount="1000",
                           operation_amount_bs="800000"), _ctx(business, broker))
    assert error.value.details["candidate_ids"] == [str(existing)]
    async with session_factory() as session:
        assert not (await session.execute(select(BrokerMovement))).scalars().all()
        assert not (await session.execute(select(Receivable))).scalars().all()
    # Explicit ID selection preserves the monetary payload and authorizes this precise identity.
    result = await bus.dispatch(RecordPendingBrokerPurchase(customer_name="QA V2 Global Samu", customer_id=existing,
                                amount="1000", operation_amount_bs="800000"), _ctx(business, broker))
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, result.affected[0].entity_id)
        assert movement.customer_id == existing
        assert movement.operation_amount_bs == Decimal("800000")
        assert movement.balance_delta == Decimal("10")


async def test_exact_normalized_unique_reuses_and_homonyms_require_id(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    first = await seed_customer(session_factory, business, "Cárlos Pérez")
    async with session_factory() as session:
        customer, created = await resolve_customer(session, business, "  CARLOS  PEREZ ",
                                                  create_if_missing=False, brokerage=True)
        assert customer.id == first and not created
    second = await seed_customer(session_factory, business, "Carlos Perez")
    with pytest.raises(Ambiguous) as error:
        await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_name="Carlos Perez", amount="100"), _ctx(business, broker))
    assert set(error.value.details["candidate_ids"]) == {str(first), str(second)}
    for cid in (first, second):
        result = await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_name="Carlos Perez", customer_id=cid,
                                   amount="100"), _ctx(business, broker))
        async with session_factory() as session:
            assert (await session.get(BrokerMovement, result.affected[0].entity_id)).customer_id == cid


async def test_explicit_new_customer_bypasses_fuzzy_match_atomically(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    original = await seed_customer(session_factory, business, "QA V2 Global Pendiente")
    command = RecordPendingBrokerPurchase(customer_name="QA V2 Global Samu", force_new_customer=True,
                                          amount="1000", exchange_rate="800")
    with pytest.raises(NeedsChoice) as error:
        await bus.dispatch(command, _ctx(business, broker))
    payload = error.value.options[0]["command_payload"]
    assert payload["force_new_customer"] and payload["exchange_rate"] == "800"
    result = await bus.dispatch(RecordPendingBrokerPurchase(**payload), _ctx(business, broker))
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, result.affected[0].entity_id)
        assert movement.customer_id != original
        assert (await session.get(Customer, movement.customer_id)).name == "QA V2 Global Samu"
        assert (await session.execute(select(func.count()).select_from(Customer))).scalar_one() == 2
        assert (await session.execute(select(func.count()).select_from(Receivable))).scalar_one() == 1
    await bus.dispatch(RecordBrokerReceivablePayment(customer_name="QA V2 Global Samu", amount="400"), _ctx(business, broker))
    async with session_factory() as session:
        payment = (await session.execute(select(CustomerPayment))).scalar_one()
        assert payment.customer_id == movement.customer_id
    summary = await BrokerageQueries(session_factory).summary(business, broker)
    assert summary["balances"][0]["balance"] == "10.00"


async def test_brokerage_search_and_id_resolution_share_visibility(session_factory):
    business, owner, one, two = await _brokerage(session_factory)
    shared = await seed_customer(session_factory, business, "Carlos Global")
    own = await seed_customer(session_factory, business, "Carlos Personal", "personal", one)
    hidden = await seed_customer(session_factory, business, "Carlos Privado", "personal", two)
    legacy = await seed_customer(session_factory, business, "Carlos Histórico", None)
    async with session_factory() as session, session.begin():
        other = Business(id=uuid4(), name="Otro negocio", vertical="brokerage")
        session.add(other)
        await session.flush()
        other_id = other.id
    foreign = await seed_customer(session_factory, other_id, "Carlos Ajeno")
    queries = BrokerageQueries(session_factory)
    assert {r["id"] for r in await queries.customers(business, one, "Carl")} == {str(shared), str(own)}
    assert {r["id"] for r in await queries.customers(business, owner, "Carloz")} == {str(shared), str(own), str(hidden)}
    assert {r["id"] for r in await queries.customers(business, two)} == {str(shared), str(hidden)}
    for cid in (hidden, legacy, foreign):
        async with session_factory() as session:
            with pytest.raises(NotFound):
                await resolve_customer(session, business, None, customer_id=cid, brokerage=True,
                                       visible_broker_id=one, create_if_missing=False)
    async with session_factory() as session:
        with pytest.raises(Ambiguous) as error:
            await resolve_customer(session, business, "Carlos", brokerage=True, visible_broker_id=one, create_if_missing=False)
        assert str(hidden) not in error.value.details["candidate_ids"]
        assert "Privado" not in str(error.value.details)


async def test_retail_keeps_strong_fuzzy_customer_resolution(session_factory):
    business, _, _, _ = await _brokerage(session_factory)
    customer_id = await seed_customer(session_factory, business, "QA V2 Global Pendiente")
    async with session_factory() as session:
        customer, created = await resolve_customer(session, business, "QA V2 Global Samu", create_if_missing=False)
        assert customer.id == customer_id and not created
