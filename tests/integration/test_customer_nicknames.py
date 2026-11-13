"""Persistent identifiers across existing commands, reads and customer scopes."""

import asyncio
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agent.receipts import render, render_preview
from app.application.commands.brokerage import RecordPendingBrokerPurchase
from app.application.commands.customers import EditCustomerProfile, UpsertCustomer
from app.application.queries.brokerage import BrokerageQueries
from app.application.queries.panel import PanelQueries
from app.application.resolvers import resolve_customer
from app.db.models import BrokerMovement, Customer, Receivable
from app.domain.errors import Ambiguous, Conflict, NotFound, ValidationFailed
from tests.integration.test_brokerage import _brokerage, _ctx


async def test_creation_resolution_search_and_profile_keep_identity(bus, session_factory):
    bid, owner, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(bid, owner)
    result = await bus.dispatch(UpsertCustomer(name="Julia Example", customer_classification="global"), ctx)
    cid = UUID(result.data["customer_id"])
    assert result.data["nickname"] == "JUL"
    receipt = render(result)
    assert "Cliente creado: Julia Example" in receipt and "Apodo: JUL" in receipt
    assert "Cambia el apodo" in receipt
    for reference in ["JUL", "jul", " Júl ", "Julia Example", "Julia"]:
        async with session_factory() as s:
            customer, created = await resolve_customer(s, bid, reference, create_if_missing=False, brokerage=True, visible_broker_id=broker)
            assert customer.id == cid and not created
    found = await BrokerageQueries(session_factory).customers(bid, broker, "jul")
    assert [c["id"] for c in found] == [str(cid)]
    assert found[0]["name"] == "Julia Example" and found[0]["nickname"] == "JUL"
    assert (await PanelQueries(session_factory).customers(bid, "Júl"))[0]["id"] == str(cid)
    operation = await bus.dispatch(RecordPendingBrokerPurchase(customer_name="jul", amount="100", exchange_rate="960"), _ctx(bid, broker))
    updated = await bus.dispatch(EditCustomerProfile(customer_name="jul", nickname=" JEX "), ctx)
    assert updated.data["previous_nickname"] == "JUL" and updated.data["nickname"] == "JEX"
    assert "Anterior: JUL" in render(updated) and "Nuevo: JEX" in render(updated)
    async with session_factory() as s:
        movement = await s.get(BrokerMovement, operation.affected[0].entity_id)
        assert movement.customer_id == cid and movement.operation_amount == Decimal("100")
        assert movement.balance_delta == Decimal("1")
        assert (await s.get(Receivable, movement.related_receivable_id)).customer_id == cid
        assert (await s.get(Customer, cid)).name == "Julia Example"


async def test_collision_does_not_block_creation_and_manual_duplicate_is_rejected(bus, session_factory):
    bid, owner, _, _ = await _brokerage(session_factory)
    ctx = _ctx(bid, owner)
    await bus.dispatch(UpsertCustomer(name="Julia Example", customer_classification="global"), ctx)
    second = await bus.dispatch(UpsertCustomer(name="Juliana Example", customer_classification="global"), ctx)
    assert second.data["nickname"] is None
    assert second.data["created_customers"][0]["nickname_conflict"] == "JUL"
    assert "JUL ya está ocupado" in render(second)
    assert "Apodo: JUL" not in render(second)
    with pytest.raises(Conflict):
        await bus.dispatch(EditCustomerProfile(customer_name="Juliana Example", nickname="júl"), ctx)
    with pytest.raises(Conflict):
        await bus.dispatch(UpsertCustomer(name="Otra persona", nickname="jul", customer_classification="global"), ctx)
    async with session_factory() as s:
        assert (await s.get(Customer, UUID(second.data["customer_id"]))).nickname is None


async def test_nickname_only_can_receive_name_without_new_identity(bus, session_factory):
    bid, owner, broker, _ = await _brokerage(session_factory)
    result = await bus.dispatch(RecordPendingBrokerPurchase(customer_name="JUL", customer_classification="personal",
                        amount="100", exchange_rate="960"), _ctx(bid, broker))
    assert result.data["customer_created"]
    assert "Apodo: JUL" in render(result)
    assert "Cliente creado" not in render_preview(result)
    async with session_factory() as s:
        movement = await s.get(BrokerMovement, result.affected[0].entity_id)
        cid = movement.customer_id
        assert (await s.get(Customer, cid)).name is None
    updated = await bus.dispatch(EditCustomerProfile(customer_name="jul", name="Julia Example"), _ctx(bid, broker))
    assert updated.data["nickname"] == "JUL" and updated.data["customer_id"] == str(cid)
    again = await bus.dispatch(RecordPendingBrokerPurchase(customer_name="Julia Example", amount="23.40", exchange_rate="960"), _ctx(bid, broker))
    assert not again.data["customer_created"] and "Cambia el apodo" not in render(again)
    async with session_factory() as s:
        assert (await s.get(BrokerMovement, again.affected[0].entity_id)).customer_id == cid
        assert (await s.execute(select(func.count()).select_from(Customer))).scalar_one() == 1


async def test_permissions_and_ambiguity_remain_scoped(bus, session_factory):
    bid, owner, one, two = await _brokerage(session_factory)
    ctx = _ctx(bid, owner)
    own = await bus.dispatch(UpsertCustomer(name="Carlos Pérez", nickname="CP", customer_classification="personal", owner_broker_member_id=one), ctx)
    await bus.dispatch(UpsertCustomer(name="Carlos Rojas", nickname="CR", customer_classification="personal", owner_broker_member_id=one), ctx)
    hidden = await bus.dispatch(UpsertCustomer(name="Carlos Privado", nickname="SECRET", customer_classification="personal", owner_broker_member_id=two), ctx)
    await bus.dispatch(UpsertCustomer(name="Cliente global", nickname="GLOBAL", customer_classification="global"), ctx)
    with pytest.raises(Ambiguous) as error:
        await bus.dispatch(EditCustomerProfile(customer_name="Carlos", nickname="NEW"), _ctx(bid, one))
    assert hidden.data["customer_id"] not in error.value.details["candidate_ids"]
    assert own.data["customer_id"] in error.value.details["candidate_ids"]
    for reference in ["SECRET"]:
        with pytest.raises(NotFound):
            await bus.dispatch(EditCustomerProfile(customer_name=reference, nickname="NEW"), _ctx(bid, one))
        with pytest.raises(NotFound):
            await bus.dispatch(EditCustomerProfile(customer_name=reference, name="Otra identidad"), _ctx(bid, one))
    with pytest.raises(NotFound):
        await bus.dispatch(UpsertCustomer(customer_id=UUID(hidden.data["customer_id"]), name="Otra identidad"), _ctx(bid, one))
    with pytest.raises(NotFound):
        await bus.dispatch(EditCustomerProfile(customer_name="desconocido", nickname="NEW"), ctx)


async def test_concurrent_creation_assigns_generated_nickname_once(bus, session_factory):
    bid, owner, _, _ = await _brokerage(session_factory)
    results = await asyncio.gather(*[
        bus.dispatch(UpsertCustomer(name=name, customer_classification="global"), _ctx(bid, owner))
        for name in ["María Fernanda", "Mario Rodríguez"]
    ])
    assert sorted(r.data["nickname"] or "" for r in results) == ["", "MAR"]
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Customer))).scalar_one() == 2
    # Explicit competing assignments are serialized too: only one succeeds.
    results = await asyncio.gather(*[
        bus.dispatch(EditCustomerProfile(customer_name=name, nickname="CUSTOM"), _ctx(bid, owner))
        for name in ["María Fernanda", "Mario Rodríguez"]
    ], return_exceptions=True)
    assert sum(isinstance(r, Conflict) for r in results) == 1


async def test_database_rejects_duplicate_noncanonical_and_empty_identifiers(bus, session_factory):
    bid, owner, _, _ = await _brokerage(session_factory)
    await bus.dispatch(UpsertCustomer(name="Julia Example", customer_classification="global"), _ctx(bid, owner))
    for name, nickname in [("Duplicado", "JUL"), ("Otro", " júl "), (None, None)]:
        with pytest.raises(IntegrityError):
            async with session_factory() as s, s.begin():
                s.add(Customer(id=uuid4(), business_id=bid, name=name, normalized_name="", nickname=nickname))
                await s.flush()


async def test_short_names_manual_alias_and_edit_do_not_regenerate(bus, session_factory):
    bid, owner, _, _ = await _brokerage(session_factory)
    ctx = _ctx(bid, owner)
    short = await bus.dispatch(UpsertCustomer(name="Al", customer_classification="global"), ctx)
    assert short.data["nickname"] is None
    manual = await bus.dispatch(UpsertCustomer(name="José González", nickname="Josecito", customer_classification="global"), ctx)
    assert manual.data["nickname"] == "JOSECITO"
    updated = await bus.dispatch(UpsertCustomer(customer_id=UUID(manual.data["customer_id"]), name="Alejandro Pérez"), ctx)
    assert updated.data["nickname"] == "JOSECITO"
    with pytest.raises(ValidationFailed):
        await bus.dispatch(UpsertCustomer(name=None, nickname=None, customer_classification="global"), ctx)


async def test_retail_nickname_only_queries_and_receipts(bus, session_factory, panel_ctx, business):
    result = await bus.dispatch(UpsertCustomer(nickname="CAR"), panel_ctx)
    cid = UUID(result.data["customer_id"])
    async with session_factory() as s:
        customer, created = await resolve_customer(s, business.id, "car", create_if_missing=False)
        assert customer.id == cid and customer.name is None and not created
    rows = await PanelQueries(session_factory).customers(business.id, "car")
    assert rows[0]["name"] is None and rows[0]["display_name"] == "CAR"


async def test_alias_priority_and_concurrent_first_reference_reuse(bus, session_factory):
    bid, owner, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(bid, owner)
    first = await bus.dispatch(UpsertCustomer(name="Julia Example", nickname="CUSTOM", customer_classification="global"), ctx)
    await bus.dispatch(UpsertCustomer(name="CUSTOM", nickname=None, customer_classification="global"), ctx)
    async with session_factory() as s:
        selected, _ = await resolve_customer(s, bid, "custom", create_if_missing=False, brokerage=True)
        assert str(selected.id) == first.data["customer_id"]
    operations = await asyncio.gather(*[
        bus.dispatch(RecordPendingBrokerPurchase(customer_name="Alejandra Pérez", customer_classification="personal",
                     amount="100", exchange_rate="960"), _ctx(bid, broker)) for _ in range(2)
    ])
    async with session_factory() as s:
        movements = [await s.get(BrokerMovement, result.affected[0].entity_id) for result in operations]
        assert movements[0].customer_id == movements[1].customer_id
        assert sum(result.data["customer_created"] for result in operations) == 1


async def test_database_unique_index_prevents_concurrent_duplicate_writes(session_factory):
    bid, _, _, _ = await _brokerage(session_factory)
    async def insert(name):
        async with session_factory() as s, s.begin():
            s.add(Customer(business_id=bid, name=name, normalized_name=name.lower(), nickname="SAME"))
            await s.flush()
    outcomes = await asyncio.gather(insert("Primero"), insert("Segundo"), return_exceptions=True)
    assert sum(isinstance(outcome, IntegrityError) for outcome in outcomes) == 1
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Customer).where(Customer.nickname == "SAME"))).scalar_one() == 1


async def test_generated_alias_collision_does_not_block_original_operation(bus, session_factory):
    bid, owner, _, _ = await _brokerage(session_factory)
    ctx = _ctx(bid, owner)
    await bus.dispatch(UpsertCustomer(name="Julia Example", customer_classification="global"), ctx)
    result = await bus.dispatch(RecordPendingBrokerPurchase(customer_name="Julieta Example", force_new_customer=True,
        customer_classification="global", amount="100", exchange_rate="960"), ctx)
    assert result.data["customer_created"] and "JUL ya está ocupado" in render(result)
    async with session_factory() as s:
        movement = await s.get(BrokerMovement, result.affected[0].entity_id)
        customer = await s.get(Customer, movement.customer_id)
        assert customer.name == "Julieta Example" and customer.nickname is None
        assert movement.operation_amount == Decimal("100") and movement.operation_amount_bs == Decimal("96000")


async def test_broker_can_edit_global_identifiers_without_other_profile_permissions(bus, session_factory):
    from app.application.commands.customers import DeleteCustomer, MergeCustomers

    bid, owner, one, two = await _brokerage(session_factory)
    result = await bus.dispatch(UpsertCustomer(name="Laida García", nickname="LAI", customer_classification="global"), _ctx(bid, owner))
    cid = UUID(result.data["customer_id"])
    operation = await bus.dispatch(RecordPendingBrokerPurchase(customer_name="LAI", amount="85", exchange_rate="960"), _ctx(bid, one))
    updated = await bus.dispatch(EditCustomerProfile(customer_name="lai", nickname="LAIDA"), _ctx(bid, one))
    assert updated.data["customer_id"] == str(cid) and updated.data["previous_nickname"] == "LAI"
    assert updated.data["nickname"] == "LAIDA"
    assert "Nuevo: LAIDA" in render(updated)
    # Any other broker sees the same global identity and can change its nickname too.
    await bus.dispatch(EditCustomerProfile(customer_name="Laida García", nickname="LG"), _ctx(bid, two))
    await bus.dispatch(EditCustomerProfile(customer_name="LG", name="Laida González"), _ctx(bid, one))
    await bus.dispatch(EditCustomerProfile(customer_name="LG", name="Laida García", nickname="LG"), _ctx(bid, two))
    for changes in [{"phone": "123"}, {"nickname": "OTHER", "phone": "123"},
                    {"name": "Otra", "notes": "nota"}, {"nickname": "OTHER", "customer_classification": "personal"},
                    {"name": "Otra", "owner_broker_member_id": one}]:
        with pytest.raises(NotFound):
            await bus.dispatch(UpsertCustomer(customer_id=cid, **changes), _ctx(bid, one))
    with pytest.raises(NotFound):
        await bus.dispatch(DeleteCustomer(customer_id=cid), _ctx(bid, one))
    other = await bus.dispatch(UpsertCustomer(name="Otro global", customer_classification="global"), _ctx(bid, owner))
    with pytest.raises(NotFound):
        await bus.dispatch(MergeCustomers(source_customer_id=cid, target_customer_id=UUID(other.data["customer_id"])), _ctx(bid, one))
    with pytest.raises(Conflict):
        await bus.dispatch(EditCustomerProfile(customer_name="LG", nickname="OTR"), _ctx(bid, one))
    async with session_factory() as session:
        customer = await session.get(Customer, cid)
        movement = await session.get(BrokerMovement, operation.affected[0].entity_id)
        assert (customer.name, customer.nickname, customer.classification, customer.phone) == ("Laida García", "LG", "global", None)
        assert movement.customer_id == cid and movement.operation_amount == Decimal("85")
        assert movement.balance_delta == Decimal("0.85")


@pytest.mark.parametrize("with_nickname", [False, True])
async def test_broker_completes_global_alm_name_preserving_ledger_and_audit(bus, session_factory, with_nickname):
    from app.db.models import AuditLog

    bid, owner, broker, _ = await _brokerage(session_factory)
    created = await bus.dispatch(UpsertCustomer(nickname="ALM", customer_classification="global"), _ctx(bid, owner))
    cid = UUID(created.data["customer_id"])
    operation = await bus.dispatch(RecordPendingBrokerPurchase(customer_name="ALM", amount="200", exchange_rate="940"), _ctx(bid, broker))
    changes = {"name": "Ángel Leon Macas", **({"nickname": "ALM"} if with_nickname else {})}
    updated = await bus.dispatch(EditCustomerProfile(customer_name="alm", **changes), _ctx(bid, broker))
    assert updated.data["customer_id"] == str(cid)
    assert updated.data["name"] == "Ángel Leon Macas" and updated.data["nickname"] == "ALM"
    assert updated.data["version"] == 2
    async with session_factory() as session:
        customer = await session.get(Customer, cid)
        movement = await session.get(BrokerMovement, operation.affected[0].entity_id)
        debt = await session.get(Receivable, movement.related_receivable_id)
        assert customer.classification == "global" and customer.owner_broker_member_id is None
        assert movement.customer_id == debt.customer_id == cid
        assert movement.operation_amount == Decimal("200") and movement.balance_delta == Decimal("2")
        entries = (await session.scalars(select(AuditLog).where(AuditLog.entity_id == cid).order_by(AuditLog.id))).all()
        assert len(entries) == 2
        assert entries[-1].actor_id == broker
        assert entries[-1].before["name"] is None and entries[-1].after["name"] == "Ángel Leon Macas"
