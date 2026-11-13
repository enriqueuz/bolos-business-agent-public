"""Direct movements stay separate from commercial and customer accounting (ADR 0052)."""

import asyncio
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.application.commands.brokerage import (
    DeleteBrokerMovement,
    EditBrokerMovement,
    RecordBrokerAccountPayment,
    RecordBrokerAccountWithdrawal,
)
from app.application.exports.brokerage import BrokerageWorkbook
from app.application.queries.brokerage import BrokerageQueries
from app.db.models import AuditLog, BrokerMovement, BusinessMember, Customer, CustomerPayment, Receivable
from app.domain.errors import NotFound, Stale, ValidationFailed
from tests.integration.test_brokerage import _brokerage, _ctx, _summary_on
from tests.workbook_helpers import WorkbookView


async def test_direct_movements_balance_and_customer_isolation(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    await bus.dispatch(RecordBrokerAccountPayment(amount="500"), ctx)
    withdrawal = await bus.dispatch(RecordBrokerAccountWithdrawal(amount="1240", description="Efectivo"), ctx)
    assert withdrawal.data["balance_raw"] == "-740.00"
    assert withdrawal.data["movement"]["balance_delta"] == "-$1.240,00"
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, withdrawal.affected[0].entity_id)
        assert movement.kind == "out" and movement.balance_delta == Decimal("-1240")
        assert movement.commission is movement.operation_amount is movement.customer_id is None
        assert movement.exchange_rate is movement.operation_amount_bs is None
        for model in (Customer, CustomerPayment, Receivable):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        assert await session.scalar(select(func.count()).select_from(AuditLog).where(
            AuditLog.entity_id == movement.id)) == 1
    summary = await _summary_on(BrokerageQueries(session_factory), business, broker)
    assert summary["totals"]["withdrawals_from_accounts"] == "1240.00"
    assert summary["totals"]["payments_to_accounts"] == "500.00"
    for key in ("bought", "sold", "commission", "excess"):
        assert summary["totals"][key] == "0.00"
    history = await BrokerageQueries(session_factory).movements(business, broker, logical_type="account_withdrawal")
    assert history["total"] == 1
    assert history["items"][0]["amount"] == "1240.00"
    assert history["items"][0]["balance_delta"] == "-1240.0000"


async def test_withdrawal_correction_delete_and_replay(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    command = RecordBrokerAccountWithdrawal(amount="500", draft_operation_id=uuid4())
    created = await bus.dispatch(command, ctx)
    replay = await bus.dispatch(command, ctx)
    assert replay.affected[0].entity_id == created.affected[0].entity_id
    assert replay.data["balance_raw"] == "-500.00"
    movement_id = created.affected[0].entity_id
    corrected = await bus.dispatch(EditBrokerMovement(movement_id=movement_id, expected_version=1, amount="250"), ctx)
    assert corrected.data["balance_raw"] == "-250.00"
    assert corrected.data["logical_type"] == "account_withdrawal"
    with pytest.raises(Stale):
        await bus.dispatch(EditBrokerMovement(movement_id=movement_id, expected_version=1, amount="100"), ctx)
    deleted = await bus.dispatch(DeleteBrokerMovement(movement_id=movement_id, expected_version=2), ctx, confirmed=True)
    assert deleted.data["balance_raw"] == "0.00"


async def test_withdrawal_permissions(bus, session_factory):
    business, owner, broker, other = await _brokerage(session_factory)
    with pytest.raises(ValidationFailed):
        await bus.dispatch(RecordBrokerAccountWithdrawal(amount="5"), _ctx(business, owner))
    with pytest.raises(ValidationFailed):
        await bus.dispatch(RecordBrokerAccountWithdrawal(amount="5", broker_id=other), _ctx(business, broker))
    result = await bus.dispatch(RecordBrokerAccountWithdrawal(amount="5", broker_id=broker), _ctx(business, owner))
    assert result.data["balance_raw"] == "-5.00"
    foreign_business, _, foreign_broker, _ = await _brokerage(session_factory)
    with pytest.raises(NotFound):
        await bus.dispatch(RecordBrokerAccountWithdrawal(amount="5", broker_id=foreign_broker), _ctx(business, owner))
    with pytest.raises(ValidationFailed):
        await bus.dispatch(RecordBrokerAccountWithdrawal(amount="5"), _ctx(foreign_business, broker))


async def test_concurrent_withdrawals_and_duplicate_replays_are_serialized(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    command = RecordBrokerAccountWithdrawal(amount="500", draft_operation_id=uuid4())
    results = await asyncio.gather(bus.dispatch(command, ctx), bus.dispatch(command, ctx))
    assert results[0].affected[0].entity_id == results[1].affected[0].entity_id
    results = await asyncio.gather(*(
        bus.dispatch(RecordBrokerAccountWithdrawal(amount="5", draft_operation_id=uuid4()), ctx) for _ in range(3)))
    assert sorted(Decimal(result.data["balance_raw"]) for result in results) == [Decimal("-515"), Decimal("-510"), Decimal("-505")]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 4


async def test_withdrawal_replay_revalidates_active_membership(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    command = RecordBrokerAccountWithdrawal(amount="5", draft_operation_id=uuid4())
    await bus.dispatch(command, ctx)
    async with session_factory() as session, session.begin():
        member = await session.get(BusinessMember, broker)
        member.is_active = False
    with pytest.raises(ValidationFailed):
        await bus.dispatch(command, ctx)


@pytest.mark.parametrize("mutation", [
    "balance_delta=0", "balance_delta=10", "currency='VES'", "commission=1", "exchange_rate=950",
    "customer_id=recorded_by_member_id", "portfolio_broker_member_id=broker_member_id", "broker_member_id=NULL",
    "operation_amount=500", "received_amount=500", "description='Reconciliación de saldo', kind='invalid'",
])
async def test_postgres_rejects_invalid_withdrawals(bus, session_factory, mutation):
    business, _, broker, _ = await _brokerage(session_factory)
    result = await bus.dispatch(RecordBrokerAccountWithdrawal(amount="500"), _ctx(business, broker))
    with pytest.raises(IntegrityError):
        async with session_factory() as session, session.begin():
            # Mutations are closed test fixtures, never user SQL.
            await session.execute(text(f"UPDATE broker_movements SET {mutation} WHERE id=:id"),
                                  {"id": result.affected[0].entity_id})


async def test_withdrawals_cutoffs_history_and_excel(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    await bus.dispatch(RecordBrokerAccountPayment(amount="1000", business_date=date(2026, 9, 30)), ctx)
    await bus.dispatch(RecordBrokerAccountWithdrawal(amount="500"), ctx)
    queries = BrokerageQueries(session_factory)
    history = await queries.movements(business, broker, date_from=date(2026, 10, 1), date_to=date(2026, 10, 1))
    assert history["opening_balance"] == "1000.00"
    assert history["closing_balance"] == "500.00"
    assert history["items"][0]["balance_after"] == "500.00"
    book = WorkbookView(await BrokerageWorkbook(session_factory).build(
        business, date_from=date(2026, 10, 1), date_to=date(2026, 10, 1)))
    account = next(row for row in book.records("Cuentas") if row["Corredor"] == "Samu")
    assert account["Balance inicial USD"] == 1000 and account["Balance final USD"] == 500
    assert account["Salidas de cuenta USD"] == 500
    row = book.records("Diario")[0]
    assert row["Tipo"] == "Salida de cuenta" and row["Monto USD"] == 500 and row["Variación USD"] == -500
    assert row["Comisión USD"] == 0
    assert not any(row["ID movimiento"] for row in book.records("Compras"))
