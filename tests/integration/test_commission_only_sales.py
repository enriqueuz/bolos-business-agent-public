"""ADR 0034: sales volume, broker commission and customer obligations stay independent."""
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.agent.receipts import render, render_preview
from app.application.commands.brokerage import (
    DeleteBrokerMovement,
    EditBrokerMovement,
    RecordBrokerCustomerPayout,
    RecordBrokerOperation,
)
from app.application.exports.brokerage import BrokerageWorkbook
from app.application.queries.brokerage import BrokerageQueries
from app.db.models import BrokerMovement, Business
from app.domain.enums import ActorType
from tests.integration.test_brokerage import _brokerage, _ctx, _summary_on
from tests.integration.test_migration_0023 import alembic
from tests.workbook_helpers import WorkbookView


@pytest.mark.parametrize("channel", [ActorType.AGENT, ActorType.PANEL])
@pytest.mark.parametrize("scope,settlement", [
    (None, "settled"), ("personal", "settled"), ("global", "settled"),
    ("personal", "pending"), ("global", "pending"),
])
@pytest.mark.parametrize("rate,commission", [("0.01", "10"), ("0.025", "25"), ("0", "0")])
async def test_sales_matrix_and_all_read_models(bus, session_factory, channel, scope, settlement, rate, commission):
    business, owner, broker, _ = await _brokerage(session_factory)
    async with session_factory() as db, db.begin():
        row = await db.get(Business, business)
        row.settings = {**(row.settings or {}), "broker_commission_rate": rate}
    ctx = _ctx(business, broker)
    ctx = replace(ctx, actor=replace(ctx.actor, type=channel))
    # A real opening balance of -500, established independently of this sale.
    async with session_factory() as db, db.begin():
        db.add(BrokerMovement(business_id=business, broker_member_id=broker,
            recorded_by_member_id=owner, kind="operation", operation_type="buy", settlement_status="account",
            operation_amount=Decimal("500"), delivered_amount=0, commission=0, commission_rate=0,
            excess=0, balance_delta=-500, business_date=ctx.now.date(), occurred_at=ctx.now))
    sale = await bus.dispatch(RecordBrokerOperation(operation_type="sell", settlement=settlement,
        operation_amount="1000", operation_amount_bs="1100000",
        customer_name="Carlos" if scope else None, customer_classification=scope), ctx)
    mid = sale.affected[0].entity_id
    query = BrokerageQueries(session_factory)
    detail = await query.movement(business, broker, mid)
    assert Decimal(detail["balance_delta"]) == Decimal(commission)
    assert detail["sales_balance_rule"] == "commission_only"
    assert Decimal(detail["operation_amount"]) == 1000 and Decimal(detail["exchange_rate"]) == 1100
    assert Decimal(sale.data["balance_raw"]) == Decimal("-500") + Decimal(commission)
    assert Decimal(detail["customer_balance_delta"]) == (1000 if settlement == "pending" else 0)
    receipt = render(sale)
    assert "Salida por venta" not in receipt + render_preview(sale)
    assert "Variación del balance:" in receipt and "Bolívares:" in receipt
    assert "Comisión" in receipt
    summary = await _summary_on(query, business, owner)
    assert summary["totals"]["sold"] == "1000.00"
    assert Decimal(summary["totals"]["commission"]) == Decimal(commission)
    book = WorkbookView(await BrokerageWorkbook(session_factory).build(
        business, date_from=date(2026, 10, 1), date_to=date(2026, 10, 1)))
    exported = book.records("Ventas")[0]
    assert exported["Venta USD"] == 1000 and exported["Venta Bs"] == 1100000
    assert exported["Efecto en balance USD"] == exported["Comisión USD"] == Decimal(commission)

    # Business configuration changes do not replace the frozen rate on correction.
    async with session_factory() as db, db.begin():
        row = await db.get(Business, business)
        row.settings = {"broker_commission_rate": "0.07"}
    await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=1, amount="900"), ctx)
    corrected = await query.movement(business, broker, mid)
    expected = Decimal(commission) * Decimal("0.9")
    assert Decimal(corrected["balance_delta"]) == Decimal(corrected["commission"]) == expected
    if settlement == "pending":
        payment = await bus.dispatch(RecordBrokerCustomerPayout(customer_name="Carlos", amount="600"), ctx)
        assert payment.data["customer_balance"]["balance_usd"] == "300.00"
        payment = await bus.dispatch(RecordBrokerCustomerPayout(customer_name="Carlos", amount="300"), ctx)
        assert payment.data["customer_balance"]["balance_usd"] == "0.00"
        assert Decimal(payment.data["balance_raw"]) == Decimal("-500") + expected
    await bus.dispatch(DeleteBrokerMovement(movement_id=mid, expected_version=2), ctx, confirmed=True)
    assert (await _summary_on(query, business, broker))["totals"]["sold"] == "0.00"
    async with session_factory() as db:
        balance = await db.scalar(select(text("sum(balance_delta)")).select_from(BrokerMovement).where(
            BrokerMovement.business_id == business, BrokerMovement.deleted_at.is_(None)))
        assert balance == -500


@pytest.mark.parametrize("settlement", ["settled", "pending"])
async def test_owner_explicit_broker_and_house_use_same_command(bus, session_factory, settlement):
    business, owner, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, owner)
    for selected in (None, "Samu"):
        result = await bus.dispatch(RecordBrokerOperation(operation_type="sell", settlement=settlement,
            operation_amount="1000", exchange_rate="1100", broker_name=selected,
            customer_name="Carlos", customer_classification="global"), ctx)
        async with session_factory() as db:
            sale = await db.get(BrokerMovement, result.affected[0].entity_id)
            assert sale.broker_member_id == (broker if selected else None)
            assert sale.balance_delta == sale.commission == (10 if selected else 0)


@pytest.mark.parametrize("settlement,scope", [("settled", None), ("pending", "personal"), ("pending", "global")])
async def test_legacy_migration_preserves_balances_and_explicit_corrections_are_audited(bus, session_factory, settlement, scope):
    business, owner, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    result = await bus.dispatch(RecordBrokerOperation(operation_type="sell", settlement=settlement,
        operation_amount="1000", exchange_rate="1100", customer_name="Carlos" if scope else None,
        customer_classification=scope), ctx)
    mid = result.affected[0].entity_id
    legacy_delta = 10 if scope == "global" else -990
    async with session_factory() as db, db.begin():
        row = await db.get(BrokerMovement, mid)
        row.sales_balance_rule = None
        row.balance_delta = legacy_delta
    try:
        alembic("downgrade", "0024")
        alembic("upgrade", "head")
        query = BrokerageQueries(session_factory)
        old = await query.movement(business, broker, mid)
        assert old["sales_balance_rule"] == "legacy" and Decimal(old["balance_delta"]) == legacy_delta
        await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=1, description="Histórico", exchange_rate="1200"), ctx)
        detail = await query.movement(business, broker, mid)
        assert Decimal(detail["balance_delta"]) == legacy_delta and detail["sales_balance_rule"] == "legacy"
        await bus.dispatch(EditBrokerMovement(movement_id=mid, expected_version=2, amount="900"), ctx)
        detail = await query.movement(business, broker, mid)
        assert Decimal(detail["balance_delta"]) == 9 and detail["sales_balance_rule"] == "commission_only"
        assert len(detail["history"]) >= 3
        # New-rule rows must not accept the old nominal debit, even at zero commission.
        with pytest.raises(IntegrityError):
            async with session_factory() as db, db.begin():
                row = await db.get(BrokerMovement, mid)
                row.balance_delta = -891
                await db.flush()
        await bus.dispatch(DeleteBrokerMovement(movement_id=mid, expected_version=3), ctx, confirmed=True)
        attempt = alembic("downgrade", "0024", check=False)
        assert attempt.returncode != 0 and "Cannot discard commission-only sales" in attempt.stderr
    finally:
        alembic("upgrade", "head")
