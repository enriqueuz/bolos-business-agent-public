from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import pairwise
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.application.commands.brokerage import (
    DeleteBrokerMovement,
    EditBrokerMovement,
    RecordBrokerAccountPayment,
    RecordBrokerAccountPurchase,
    RecordBrokerOperation,
    RecordBrokerReceivablePayment,
    RecordPendingBrokerPurchase,
    SetBrokerMemberActive,
)
from app.application.commands.customers import UpsertCustomer
from app.application.context import Actor, ExecContext
from app.application.queries.brokerage import BrokerageQueries
from app.db.models import BrokerMovement, Business, BusinessMember, CustomerPayment, Receivable
from app.domain.dates import PeriodName
from app.domain.enums import ActorType, BusinessVertical, MemberRole, OnboardingState
from app.domain.errors import Ambiguous, ValidationFailed


async def _brokerage(session_factory):
    async with session_factory() as session, session.begin():
        business = Business(name="Cambios Centro", vertical=BusinessVertical.BROKERAGE.value,
                            onboarding_state=OnboardingState.DONE.value)
        session.add(business)
        await session.flush()
        owner = BusinessMember(business_id=business.id, phone="584140000010", name="Enrique",
                               role=MemberRole.OWNER.value)
        samu = BusinessMember(business_id=business.id, phone="584140000011", name="Samu",
                              role=MemberRole.BROKER.value)
        pedro = BusinessMember(business_id=business.id, phone="584140000012", name="Pedro",
                               role=MemberRole.BROKER.value)
        session.add_all([owner, samu, pedro])
        await session.flush()
    return business.id, owner.id, samu.id, pedro.id


async def _summary_on(
    queries: BrokerageQueries,
    business_id,
    member_id,
    day: date = date(2026, 10, 1),
) -> dict:
    return await queries.summary(
        business_id,
        member_id,
        period=PeriodName.CUSTOM,
        date_from=day,
        date_to=day,
    )


def _ctx(business_id, member_id) -> ExecContext:
    return ExecContext(business_id=business_id, actor=Actor(ActorType.AGENT, member_id),
                       now=datetime(2026, 10, 1, 16, 0, tzinfo=UTC))


async def test_signed_ledger_and_operation_credit(bus, session_factory):
    business_id, owner_id, samu_id, _ = await _brokerage(session_factory)

    first = await bus.dispatch(RecordBrokerAccountPurchase(exchange_rate="800", amount=Decimal("300"), broker_name="Samu"),
                               _ctx(business_id, owner_id))
    operation = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", operation_type="sell", operation_amount=Decimal("1000"),
                                   delivered_amount=Decimal("1000")), _ctx(business_id, samu_id))

    assert first.data["balance_raw"] == "-297.00"
    assert operation.data["excess"] == "$0,00"
    assert operation.data["commission"] == "$10,00"
    assert operation.data["balance_raw"] == "-287.00"
    dashboard = await _summary_on(BrokerageQueries(session_factory), business_id, samu_id)
    assert dashboard["totals"]["payments_to_accounts"] == "0.00"


async def test_settled_buy_and_sale_without_excess_are_first_class_operations(bus, session_factory):
    business_id, _, samu_id, _ = await _brokerage(session_factory)

    buy = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", customer_classification="personal",
        operation_type="buy", operation_amount=Decimal("450"), customer_name="Lissy",
    ), _ctx(business_id, samu_id))
    sell = await bus.dispatch(RecordBrokerOperation(exchange_rate="800",
        operation_type="sell", operation_amount=Decimal("1000"),
    ), _ctx(business_id, samu_id))

    assert buy.data["commission"] == "$4,50"
    assert buy.data["excess"] == "$0,00"
    assert sell.data["commission"] == "$10,00"
    assert sell.data["delivered"] == "$1.000,00"
    async with session_factory() as session:
        buy_row = await session.get(BrokerMovement, buy.affected[0].entity_id)
        sell_row = await session.get(BrokerMovement, sell.affected[0].entity_id)
        assert buy_row.customer is not None and buy_row.customer.name == "Lissy"
        assert sell_row.customer_id is None
    queries = BrokerageQueries(session_factory)
    dashboard = await _summary_on(queries, business_id, samu_id)
    assert dashboard["totals"]["bought"] == "450.00"
    assert dashboard["totals"]["sold"] == "1000.00"
    assert dashboard["totals"]["payments_to_accounts"] == "0.00"


async def test_owner_house_operations_have_no_commission_or_broker_exposure(bus, session_factory):
    business_id, owner_id, _, _ = await _brokerage(session_factory)

    buy = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", customer_classification="global",
        operation_type="buy", operation_amount=Decimal("450"), customer_name="Lissy",
    ), _ctx(business_id, owner_id))
    sell = await bus.dispatch(RecordBrokerOperation(exchange_rate="800",
        operation_type="sell", operation_amount=Decimal("1000"), delivered_amount=Decimal("1000"),
    ), _ctx(business_id, owner_id))

    assert buy.data["broker"] == "Casa"
    assert buy.data["commission"] == "$0,00"
    assert buy.data["balance"] is None
    assert sell.data["excess"] == "$0,00"
    assert sell.data["commission"] == "$0,00"
    async with session_factory() as session:
        rows = (await session.execute(
            select(BrokerMovement).where(BrokerMovement.id.in_([
                buy.affected[0].entity_id, sell.affected[0].entity_id,
            ]))
        )).scalars().all()
        assert all(row.broker_member_id is None for row in rows)
        assert all(row.balance_delta == Decimal("0.00") for row in rows)

    queries = BrokerageQueries(session_factory)
    consolidated = await _summary_on(queries, business_id, owner_id)
    house = await queries.summary(
        business_id, owner_id, period=PeriodName.CUSTOM,
        date_from=date(2026, 10, 1), date_to=date(2026, 10, 1), account_scope="house",
    )
    assert consolidated["totals"]["bought"] == "450.00"
    assert consolidated["totals"]["sold"] == "1000.00"
    assert consolidated["totals"]["commission"] == "0.00"
    assert consolidated["totals"]["excess"] == "0.00"
    assert house["balances"] == []
    assert house["exposure"]["owed_to_business"] == "0.00"


async def test_broker_cannot_edit_or_read_house_activity(bus, session_factory):
    business_id, owner_id, samu_id, _ = await _brokerage(session_factory)
    created = await bus.dispatch(
        RecordBrokerOperation(exchange_rate="800", customer_classification="global",
            operation_type="buy", operation_amount=Decimal("25"), customer_name="Cliente Casa"
        ),
        _ctx(business_id, owner_id),
    )
    movement_id = created.affected[0].entity_id

    with pytest.raises(ValidationFailed, match="propia cuenta"):
        await bus.dispatch(
            EditBrokerMovement(movement_id=movement_id, expected_version=1, description="intrusión"),
            _ctx(business_id, samu_id),
        )
    with pytest.raises(ValidationFailed, match="propia cuenta"):
        await BrokerageQueries(session_factory).movements(
            business_id, samu_id, account_scope="house"
        )


async def test_account_purchase_uses_balance_without_creating_receivable(bus, session_factory):
    business_id, owner_id, samu_id, _ = await _brokerage(session_factory)

    purchase = await bus.dispatch(
        RecordBrokerAccountPurchase(exchange_rate="800", amount=Decimal("40"), description="Farmatodo"),
        _ctx(business_id, samu_id),
    )

    assert purchase.data["commission"] == "$0,40"
    assert purchase.data["balance_effect"] == "-$39,60"
    assert purchase.data["balance_raw"] == "-39.60"
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, purchase.affected[0].entity_id)
        assert movement.kind == "operation"
        assert movement.operation_type == "buy"
        assert movement.settlement_status == "account"
        assert movement.customer_id is None
        assert movement.description == "Farmatodo"
        assert movement.operation_amount == Decimal("40.00")
        assert movement.delivered_amount == Decimal("0.00")
        assert movement.commission == Decimal("0.40")
        assert movement.balance_delta == Decimal("-39.60")
        assert movement.related_receivable_id is None
        assert (await session.execute(
            select(func.count()).select_from(Receivable).where(Receivable.business_id == business_id)
        )).scalar_one() == 0

    queries = BrokerageQueries(session_factory)
    dashboard = await _summary_on(queries, business_id, samu_id)
    assert dashboard["totals"]["bought"] == "40.00"
    assert "out" not in dashboard["totals"]
    assert dashboard["totals"]["commission"] == "0.40"

    repayment = await bus.dispatch(
        RecordBrokerAccountPayment(amount=Decimal("20")),
        _ctx(business_id, samu_id),
    )
    assert repayment.data["balance_raw"] == "-19.60"
    movements = await queries.movements(business_id, samu_id)
    repayment_row = next(row for row in movements["items"] if row["id"] == str(repayment.affected[0].entity_id))
    assert repayment_row["logical_type"] == "account_payment"

    with pytest.raises(ValidationFailed, match="corredor"):
        await bus.dispatch(
            RecordBrokerAccountPurchase(exchange_rate="800", amount=Decimal("40")),
            _ctx(business_id, owner_id),
        )


async def test_pending_purchase_creates_purchase_commission_debt_and_undoes_atomically(bus, session_factory):
    business_id, _, samu_id, _ = await _brokerage(session_factory)
    created = await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal",
        customer_name="Orlando", amount=Decimal("197.77"), force_new_customer=True,
    ), _ctx(business_id, samu_id), confirmed=True)

    assert created.data["commission"] == "$1,98"
    assert created.data["balance_effect"] == "-$195,79"
    assert created.data["balance_raw"] == "-195.79"
    movement_ref, receivable_ref = created.affected
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, movement_ref.entity_id)
        receivable = await session.get(Receivable, receivable_ref.entity_id)
        assert movement.kind == "operation"
        assert movement.operation_type == "buy"
        assert movement.settlement_status == "pending"
        assert movement.customer is not None and movement.customer.name == "Orlando"
        assert movement.operation_amount == Decimal("197.77")
        assert movement.delivered_amount == Decimal("0.00")
        assert movement.commission == Decimal("1.98")
        assert movement.balance_delta == Decimal("-195.79")
        assert movement.related_receivable_id == receivable.id
        assert receivable.broker_member_id == samu_id

    queries = BrokerageQueries(session_factory)
    dashboard = await _summary_on(queries, business_id, samu_id)
    assert dashboard["totals"]["bought"] == "197.77"
    assert "out" not in dashboard["totals"]
    assert dashboard["totals"]["commission"] == "1.98"
    assert dashboard["recent"][0]["settlement"] == "pending"

    await bus.dispatch(DeleteBrokerMovement(movement_id=movement_ref.entity_id),
                       _ctx(business_id, samu_id), confirmed=True)
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, movement_ref.entity_id)
        receivable = await session.get(Receivable, receivable_ref.entity_id)
        assert movement.deleted_at is not None
        assert receivable.deleted_at is not None

    dashboard = await _summary_on(queries, business_id, samu_id)
    assert dashboard["balances"][0]["balance"] == "0.00"
    assert dashboard["totals"]["bought"] == "0.00"
    assert "out" not in dashboard["totals"]
    assert dashboard["totals"]["commission"] == "0.00"


async def test_customer_payment_reduces_broker_debt_and_credits_same_account(bus, session_factory):
    business_id, _, samu_id, _ = await _brokerage(session_factory)
    purchase = await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal",
        customer_name="Carlos", amount=Decimal("100"), force_new_customer=True,
    ), _ctx(business_id, samu_id), confirmed=True)
    purchase_movement_id = purchase.affected[0].entity_id
    receivable_id = purchase.affected[1].entity_id

    first = await bus.dispatch(RecordBrokerReceivablePayment(
        customer_name="Carlos", amount=Decimal("50"),
    ), _ctx(business_id, samu_id))
    first_movement_id = first.affected[0].entity_id
    first_payment_id = first.affected[1].entity_id

    assert first.data["customer_balance"]["display"] == "$50,00"
    assert first.data["balance_raw"] == "-49.00"
    async with session_factory() as session:
        purchase_movement = await session.get(BrokerMovement, purchase_movement_id)
        payment_movement = await session.get(BrokerMovement, first_movement_id)
        payment = await session.get(CustomerPayment, first_payment_id)
        assert purchase_movement.settlement_status == "partial"
        assert purchase_movement.delivered_amount == Decimal("50.00")
        assert payment_movement.kind == "in"
        assert payment_movement.balance_delta == Decimal("50.00")
        assert payment_movement.related_payment_id == payment.id
        assert payment.broker_member_id == samu_id

    second = await bus.dispatch(RecordBrokerReceivablePayment(
        customer_name="Carlos", amount=Decimal("50"),
    ), _ctx(business_id, samu_id))
    assert second.data["customer_balance"]["display"] == "$0,00"
    assert second.data["balance_raw"] == "1.00"
    async with session_factory() as session:
        purchase_movement = await session.get(BrokerMovement, purchase_movement_id)
        assert purchase_movement.settlement_status == "settled"
        assert purchase_movement.delivered_amount == Decimal("100.00")

    queries = BrokerageQueries(session_factory)
    movements = await queries.movements(business_id, samu_id)
    payment_row = next(row for row in movements["items"] if row["id"] == str(second.affected[0].entity_id))
    assert payment_row["logical_type"] == "receivable_payment"
    assert movements["items"][0]["balance_after"] == "1.00"
    for newer, older in pairwise(movements["items"]):
        assert Decimal(older["balance_after"]) == (
            Decimal(newer["balance_after"]) - Decimal(newer["balance_delta"])
        )
    dashboard = await _summary_on(queries, business_id, samu_id)
    assert dashboard["totals"]["payments_to_accounts"] == "100.00"
    assert "account_payments" not in dashboard["totals"]
    assert "net" not in dashboard["totals"]
    assert "out" not in dashboard["totals"]
    assert dashboard["totals"]["bought"] == "100.00"
    assert dashboard["totals"]["commission"] == "1.00"
    assert (await queries.receivables(business_id, samu_id))["items"] == []

    await bus.dispatch(DeleteBrokerMovement(movement_id=second.affected[0].entity_id),
                       _ctx(business_id, samu_id), confirmed=True)
    async with session_factory() as session:
        purchase_movement = await session.get(BrokerMovement, purchase_movement_id)
        assert purchase_movement.settlement_status == "partial"
        assert purchase_movement.delivered_amount == Decimal("50.00")
    dashboard = await _summary_on(queries, business_id, samu_id)
    assert dashboard["balances"][0]["balance"] == "-49.00"
    assert (await queries.receivables(business_id, samu_id))["items"][0]["remaining"] == "50.00"

    await bus.dispatch(DeleteBrokerMovement(movement_id=first_movement_id),
                       _ctx(business_id, samu_id), confirmed=True)
    async with session_factory() as session:
        purchase_movement = await session.get(BrokerMovement, purchase_movement_id)
        receivable = await session.get(Receivable, receivable_id)
        assert purchase_movement.settlement_status == "pending"
        assert purchase_movement.delivered_amount == Decimal("0.00")
        assert receivable.deleted_at is None
    dashboard = await _summary_on(queries, business_id, samu_id)
    assert dashboard["balances"][0]["balance"] == "-99.00"
    assert (await queries.receivables(business_id, samu_id))["items"][0]["remaining"] == "100.00"


async def test_pending_purchase_deletion_preserves_its_payments(bus, session_factory):
    business_id, _, samu_id, _ = await _brokerage(session_factory)
    purchase = await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal",
        customer_name="Carlos", amount=Decimal("100"), force_new_customer=True,
    ), _ctx(business_id, samu_id), confirmed=True)
    await bus.dispatch(RecordBrokerReceivablePayment(
        customer_name="Carlos", amount=Decimal("50"),
    ), _ctx(business_id, samu_id))

    await bus.dispatch(DeleteBrokerMovement(movement_id=purchase.affected[0].entity_id),
                       _ctx(business_id, samu_id), confirmed=True)
    accounts = await BrokerageQueries(session_factory).customer_accounts(business_id, samu_id)
    assert accounts["items"][0]["balance_usd"] == "50.00"


async def test_customer_payment_only_reduces_authenticated_broker_portfolio(bus, session_factory):
    business_id, owner_id, samu_id, pedro_id = await _brokerage(session_factory)
    await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal",
        customer_name="Carlos", amount=Decimal("100"), force_new_customer=True,
    ), _ctx(business_id, samu_id), confirmed=True)
    await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal",
        customer_name="Carlos", amount=Decimal("80"),
    ), _ctx(business_id, pedro_id))

    await bus.dispatch(RecordBrokerReceivablePayment(
        customer_name="Carlos", amount=Decimal("50"),
    ), _ctx(business_id, samu_id))

    queries = BrokerageQueries(session_factory)
    samu_debt = await queries.receivables(business_id, owner_id, broker_id=samu_id)
    pedro_debt = await queries.receivables(business_id, owner_id, broker_id=pedro_id)
    assert samu_debt["items"][0]["remaining"] == "50.00"
    assert pedro_debt["items"][0]["remaining"] == "80.00"

    with pytest.raises(Ambiguous):
        await bus.dispatch(RecordBrokerReceivablePayment(
            customer_name="Carlos", amount=Decimal("10"),
        ), _ctx(business_id, owner_id))


async def test_pending_purchase_does_not_guess_an_ambiguous_customer(bus, session_factory):
    business_id, _, samu_id, _ = await _brokerage(session_factory)
    ctx = _ctx(business_id, samu_id)
    await bus.dispatch(UpsertCustomer(customer_classification="personal", name="Orlando Pérez"), ctx)
    await bus.dispatch(UpsertCustomer(customer_classification="personal", name="Orlando Gómez"), ctx)

    with pytest.raises(Ambiguous):
        await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal",
            customer_name="Orlando", amount=Decimal("100"),
        ), ctx)

    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(BrokerMovement))).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(Receivable))).scalar_one() == 0


async def test_pending_purchase_accepts_selected_exact_customer_homonym(bus, session_factory):
    business_id, _, samu_id, _ = await _brokerage(session_factory)
    ctx = _ctx(business_id, samu_id)
    first = await bus.dispatch(UpsertCustomer(customer_classification="personal", name="Orlando", phone="04120000001"), ctx)
    second = await bus.dispatch(UpsertCustomer(customer_classification="personal", name="Orlando", phone="04120000002"), ctx)

    with pytest.raises(Ambiguous) as exc:
        await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal", customer_name="Orlando", amount=Decimal("100")), ctx)
    assert set(exc.value.details["candidate_ids"]) == {first.data["customer_id"], second.data["customer_id"]}

    selected = await bus.dispatch(
        RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal",
            customer_name="Orlando",
            customer_id=UUID(second.data["customer_id"]),
            amount=Decimal("100"),
        ),
        ctx,
    )
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, selected.affected[0].entity_id)
        assert movement.customer_id == UUID(second.data["customer_id"])


async def test_owner_defaults_pending_purchase_and_payment_to_house(bus, session_factory):
    business_id, owner_id, _, _ = await _brokerage(session_factory)

    purchase = await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="global",
        customer_name="Orlando", amount=Decimal("100"), force_new_customer=True,
    ), _ctx(business_id, owner_id), confirmed=True)
    payment = await bus.dispatch(RecordBrokerReceivablePayment(
        customer_name="Orlando", amount=Decimal("50"),
    ), _ctx(business_id, owner_id))

    assert purchase.data["broker"] == "Casa"
    assert purchase.data["commission"] == "$0,00"
    assert purchase.data["balance"] is None
    assert payment.data["broker"] == "Casa"
    assert payment.data["balance"] is None
    async with session_factory() as session:
        purchase_movement = await session.get(BrokerMovement, purchase.affected[0].entity_id)
        payment_movement = await session.get(BrokerMovement, payment.affected[0].entity_id)
        receivable = await session.get(Receivable, purchase.affected[1].entity_id)
        customer_payment = await session.get(CustomerPayment, payment.affected[1].entity_id)
        assert purchase_movement.broker_member_id is None
        assert purchase_movement.balance_delta == Decimal("0.00")
        assert purchase_movement.commission_rate == Decimal("0")
        assert payment_movement.broker_member_id is None
        assert payment_movement.balance_delta == Decimal("0.00")
        assert receivable.broker_member_id is None
        assert customer_payment.broker_member_id is None

    queries = BrokerageQueries(session_factory)
    house = await queries.receivables(
        business_id, owner_id, account_scope="house", status="all"
    )
    assert house["items"][0]["remaining"] == "50.00"
    assert house["items"][0]["account_kind"] == "house"


async def test_edit_account_purchase_keeps_original_commission_rate(bus, session_factory):
    business_id, owner_id, samu_id, _ = await _brokerage(session_factory)
    created = await bus.dispatch(
        RecordBrokerAccountPurchase(exchange_rate="800", amount=Decimal("40"), broker_id=samu_id),
        _ctx(business_id, owner_id),
    )
    movement_id = created.affected[0].entity_id
    async with session_factory() as session, session.begin():
        business = await session.get(Business, business_id)
        business.settings = {"broker_commission_rate": "0.20"}

    edited = await bus.dispatch(
        EditBrokerMovement(movement_id=movement_id, expected_version=1, amount=Decimal("50")),
        _ctx(business_id, owner_id),
    )
    assert edited.data["balance"] == "-$49,50"
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, movement_id)
        assert movement.operation_amount == Decimal("50.00")
        assert movement.commission_rate == Decimal("0.010000")
        assert movement.commission == Decimal("0.50")
        assert movement.balance_delta == Decimal("-49.50")


async def test_edit_pending_purchase_updates_receivable_commission_and_balance_atomically(
    bus, session_factory,
):
    business_id, owner_id, samu_id, _ = await _brokerage(session_factory)
    created = await bus.dispatch(
        RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal",
            customer_name="Pedro", amount=Decimal("1200"), broker_id=samu_id, force_new_customer=True,
        ),
        _ctx(business_id, owner_id),
        confirmed=True,
    )

    edited = await bus.dispatch(
        EditBrokerMovement(
            movement_id=created.affected[0].entity_id,
            expected_version=1,
            amount=Decimal("1000"),
        ),
        _ctx(business_id, owner_id),
    )

    assert edited.data["amount"] == "$1.000,00"
    assert edited.data["receivable_amount"] == "$1.000,00"
    assert edited.data["commission"] == "$10,00"
    assert edited.data["balance"] == "-$990,00"
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, created.affected[0].entity_id)
        receivable = await session.get(Receivable, movement.related_receivable_id)
        assert movement.operation_amount == Decimal("1000.00")
        assert movement.commission == Decimal("10.00")
        assert movement.balance_delta == Decimal("-990.00")
        assert receivable.amount == Decimal("1000.00")
        assert movement.deleted_at is None and receivable.deleted_at is None


async def test_pending_purchase_with_payment_recalculates_principal_and_deactivation_keeps_history(
    bus, session_factory,
):
    business_id, owner_id, samu_id, _ = await _brokerage(session_factory)
    purchase = await bus.dispatch(
        RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal",
            customer_name="Carlos", amount=Decimal("100"), force_new_customer=True,
        ),
        _ctx(business_id, samu_id),
        confirmed=True,
    )
    await bus.dispatch(
        RecordBrokerReceivablePayment(customer_name="Carlos", amount=Decimal("20")),
        _ctx(business_id, samu_id),
    )
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, purchase.affected[0].entity_id)
        version = movement.version
    await bus.dispatch(EditBrokerMovement(movement_id=purchase.affected[0].entity_id,
                                         expected_version=version, amount=Decimal("90")),
                       _ctx(business_id, owner_id))
    account = await BrokerageQueries(session_factory).customer_accounts(business_id, owner_id)
    assert account["items"][0]["balance_usd"] == "-70.00"

    preview = await bus.dispatch(
        SetBrokerMemberActive(member_id=samu_id, is_active=False),
        _ctx(business_id, owner_id),
    )
    assert preview.needs_confirmation
    async with session_factory() as session:
        assert (await session.get(BusinessMember, samu_id)).is_active is True

    await bus.dispatch(
        SetBrokerMemberActive(member_id=samu_id, is_active=False),
        _ctx(business_id, owner_id),
        confirmed=True,
    )
    async with session_factory() as session:
        assert (await session.get(BusinessMember, samu_id)).is_active is False
        assert await session.get(BrokerMovement, purchase.affected[0].entity_id) is not None
    with pytest.raises(ValidationFailed, match="permiso"):
        await bus.dispatch(
            RecordBrokerAccountPayment(amount=Decimal("1")),
            _ctx(business_id, samu_id),
        )


async def test_broker_cannot_operate_or_read_another_account(bus, session_factory):
    business_id, owner_id, samu_id, pedro_id = await _brokerage(session_factory)
    await bus.dispatch(RecordBrokerAccountPayment(amount=Decimal("50"), broker_name="Pedro"),
                       _ctx(business_id, owner_id))

    with pytest.raises(ValidationFailed):
        await bus.dispatch(RecordBrokerAccountPayment(amount=Decimal("5"), broker_name="Pedro"),
                           _ctx(business_id, samu_id))

    queries = BrokerageQueries(session_factory)
    own = await queries.summary(business_id, samu_id, period=PeriodName.TODAY)
    assert [row["member_id"] for row in own["balances"]] == [str(samu_id)]
    with pytest.raises(ValidationFailed):
        await queries.movements(business_id, samu_id, broker_id=pedro_id)


async def test_owner_can_select_broker_by_id(bus, session_factory):
    business_id, owner_id, samu_id, _ = await _brokerage(session_factory)

    result = await bus.dispatch(
        RecordBrokerAccountPayment(amount=Decimal("25"), broker_id=samu_id),
        _ctx(business_id, owner_id),
    )

    assert result.data["broker"] == "Samu"
    assert result.data["balance_raw"] == "25.00"


async def test_summary_separates_current_portfolio_and_period_customer_payments(bus, session_factory):
    business_id, owner_id, samu_id, _ = await _brokerage(session_factory)
    owner = _ctx(business_id, owner_id)
    broker = _ctx(business_id, samu_id)
    await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="global", customer_name="Ana", amount=Decimal("800")), owner)
    await bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal", customer_name="Luis", amount=Decimal("100")), broker)
    await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Ana", amount=Decimal("40")), owner)
    await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Luis", amount=Decimal("20")), broker)
    queries = BrokerageQueries(session_factory)
    all_data = await _summary_on(queries, business_id, owner_id)
    assert all_data["totals"]["bought"] == "900.00"
    assert all_data["totals"]["customer_payments"] == "60.00"
    assert all_data["totals"]["receivables_open"] == "840.00"
    assert all_data["totals"]["receivables_house"] == "760.00"
    assert all_data["totals"]["receivables_brokers"] == "80.00"
    house = await queries.summary(business_id, owner_id, period=PeriodName.CUSTOM,
                                  date_from=date(2026, 10, 1), date_to=date(2026, 10, 1), account_scope="house")
    assert house["balances"] == [] and house["totals"]["customer_payments"] == "40.00"
    assert house["totals"]["commission"] == "0.00"
    own = await _summary_on(queries, business_id, samu_id)
    assert own["totals"]["bought"] == "100.00"
    assert own["totals"]["customer_payments"] == "20.00"
    assert own["totals"]["receivables_house"] == "760.00"
    assert own["balances"][0]["balance"] == "-79.00"
    empty_period = await _summary_on(queries, business_id, owner_id, day=date(2026, 9, 30))
    assert empty_period["totals"]["bought"] == "0.00"
    assert empty_period["totals"]["customer_payments"] == "0.00"
    assert empty_period["totals"]["receivables_open"] == "840.00"
