"""Customer account acceptance: direction, netting, scopes, edits and real money."""
import asyncio
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.agent.receipts import render, render_preview
from app.application.commands.brokerage import (
    DeleteBrokerMovement,
    EditBrokerMovement,
    RecordBrokerCustomerPayout,
    RecordBrokerOperation,
    RecordBrokerReceivablePayment,
    RecordPendingBrokerPurchase,
)
from app.application.commands.customers import UpsertCustomer
from app.application.common import customer_balance
from app.application.customer_accounts import account_balances
from app.application.exports.brokerage import BrokerageWorkbook
from app.application.queries.brokerage import BrokerageQueries
from app.db.models import BrokerMovement, CustomerPayment, Receivable
from app.domain.errors import NotFound, ValidationFailed
from tests.integration.test_brokerage import _brokerage, _ctx, _summary_on
from tests.integration.test_brokerage_workbook import WorkbookView


async def sale(bus, ctx, amount="1000", classification="global", **changes):
    return await bus.dispatch(RecordBrokerOperation(operation_type="sell", settlement="pending",
        operation_amount=amount, exchange_rate="1000", customer_name="Hum",
        customer_classification=classification, **changes), ctx, confirmed=True)


async def purchase(bus, ctx, amount="200", classification="global", **changes):
    return await bus.dispatch(RecordPendingBrokerPurchase(amount=amount, exchange_rate="1000",
        customer_name="Hum", customer_classification=classification, **changes), ctx, confirmed=True)


async def pay(bus, ctx, amount, outgoing=True):
    command = RecordBrokerCustomerPayout if outgoing else RecordBrokerReceivablePayment
    return await bus.dispatch(command(customer_name="Hum", amount=amount), ctx, confirmed=True)


async def account(factory, business, viewer):
    data = await BrokerageQueries(factory).customer_accounts(business, viewer, status="all")
    assert len(data["items"]) == 1
    return data["items"][0]


@pytest.mark.parametrize("scope,expected", [("house", "0"), ("global", "10"), ("personal", "10")])
async def test_pending_sale_partial_full_payout_keeps_broker_effect(bus, session_factory, scope, expected):
    business, owner, broker, other = await _brokerage(session_factory)
    ctx = _ctx(business, owner if scope == "house" else broker)
    result = await sale(bus, ctx, classification="personal" if scope == "personal" else "global")
    assert result.data["customer_balance"]["balance_usd"] == "1000.00"
    assert "Venta pendiente registrada" in render(result)
    assert "a favor de HUM" in render(result)
    assert "Sin guardar" in render_preview(result)
    payer = _ctx(business, other) if scope == "global" else ctx
    partial = await pay(bus, payer, "600")
    assert partial.data["customer_balance"]["balance_usd"] == "400.00"
    full = await pay(bus, payer, "400")
    assert "Todo saldado" in render(full)
    assert (await account(session_factory, business, owner))["balance_usd"] == "0.00"
    async with session_factory() as db:
        movement = await db.get(BrokerMovement, result.affected[0].entity_id)
        assert movement.balance_delta == Decimal(expected)
        assert movement.settlement_status == "pending" and movement.delivered_amount == 0
        assert await db.scalar(select(func.count()).select_from(CustomerPayment)) == 0
        assert await db.scalar(select(func.count()).select_from(Receivable)) == 0
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 3
    detail = await BrokerageQueries(session_factory).movement(business, owner, result.affected[0].entity_id)
    assert detail["original_settlement"] == "pending"
    assert detail["customer_balance"]["balance_usd"] == "0.00"


@pytest.mark.parametrize("reverse", [False, True])
async def test_hum_netting_is_order_independent_and_export_matches(bus, session_factory, reverse):
    business, owner, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, owner)
    if reverse:
        await purchase(bus, ctx)
    await sale(bus, ctx, "1524")
    if not reverse:
        await purchase(bus, ctx)
    row = await account(session_factory, business, owner)
    assert row["balance_usd"] == "1324.00"
    await pay(bus, ctx, "1324")
    row = await account(session_factory, business, owner)
    assert row["balance_usd"] == "0.00"
    query = BrokerageQueries(session_factory)
    assert (await query.customer_accounts(business, owner))["total"] == 0
    summary = await _summary_on(query, business, owner)
    assert summary["totals"]["receivables_open"] == "0.00"
    assert summary["totals"]["customer_credit"] == "0.00"
    history = await query.customer_account(business, broker, row["customer_id"], date_from=date(2026, 9, 1))
    assert history["total"] == 3 and history["balance_usd"] == "0.00"
    book = WorkbookView(await BrokerageWorkbook(session_factory).build(business, date_from=date(2026, 10, 1), date_to=date(2026, 10, 1)))
    assert book.records("Cartera")[0]["Saldo neto USD"] == 0
    assert len(book.records("MovimientosCliente")) == 3
    assert sum(e["Efecto cliente USD"] for e in book.records("MovimientosCliente")) == 0
    assert book.records("Compras")[0]["Modalidad original"] == "Pendiente"
    assert book.records("Ventas")[0]["Modalidad original"] == "Pendiente"


async def test_existing_overpayment_credit_and_both_zero_crossings(bus, session_factory):
    business, owner, _, _ = await _brokerage(session_factory)
    ctx = _ctx(business, owner)
    await purchase(bus, ctx)
    received = await pay(bus, ctx, "300", outgoing=False)
    assert received.data["customer_balance"]["balance_usd"] == "100.00"
    await purchase(bus, ctx, "50")
    assert (await account(session_factory, business, owner))["balance_usd"] == "50.00"
    await sale(bus, ctx, "100")
    assert (await account(session_factory, business, owner))["balance_usd"] == "150.00"
    outgoing = await pay(bus, ctx, "250")
    assert outgoing.data["customer_balance"]["balance_usd"] == "-100.00"
    # FIFO says all old purchases have receipts, but the net now correctly says the customer owes.
    assert (await _summary_on(BrokerageQueries(session_factory), business, owner))["totals"]["receivables_open"] == "100.00"


async def test_edit_and_delete_purchase_preserve_received_payments(bus, session_factory):
    business, owner, _, _ = await _brokerage(session_factory)
    ctx = _ctx(business, owner)
    buy = await purchase(bus, ctx)
    received = await pay(bus, ctx, "150", outgoing=False)
    query = BrokerageQueries(session_factory)
    movement = await query.movement(business, owner, buy.affected[0].entity_id)
    await bus.dispatch(EditBrokerMovement(movement_id=buy.affected[0].entity_id,
        expected_version=movement["version"], amount="100"), ctx)
    assert (await account(session_factory, business, owner))["balance_usd"] == "50.00"
    await bus.dispatch(DeleteBrokerMovement(movement_id=buy.affected[0].entity_id), ctx, confirmed=True)
    assert (await account(session_factory, business, owner))["balance_usd"] == "150.00"
    payment = await query.movement(business, owner, received.affected[0].entity_id)
    assert payment["amount"] == "150.00"
    await bus.dispatch(EditBrokerMovement(movement_id=received.affected[0].entity_id,
        expected_version=payment["version"], amount="170"), ctx)
    assert (await account(session_factory, business, owner))["balance_usd"] == "170.00"
    await bus.dispatch(DeleteBrokerMovement(movement_id=received.affected[0].entity_id), ctx, confirmed=True)
    assert (await account(session_factory, business, owner))["balance_usd"] == "0.00"


async def test_sale_payout_edits_deletes_and_atomic_failure(bus, session_factory):
    business, owner, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    sell = await sale(bus, ctx)
    outgoing = await pay(bus, ctx, "600")
    query = BrokerageQueries(session_factory)
    await bus.dispatch(EditBrokerMovement(movement_id=sell.affected[0].entity_id,
        expected_version=1, amount="500"), ctx)
    assert (await account(session_factory, business, owner))["balance_usd"] == "-100.00"
    movement = await query.movement(business, broker, sell.affected[0].entity_id)
    assert movement["balance_delta"] == "5.0000"
    with pytest.raises(ValidationFailed):
        await bus.dispatch(EditBrokerMovement(movement_id=sell.affected[0].entity_id,
            expected_version=movement["version"], customer_name=""), ctx)
    assert (await account(session_factory, business, owner))["balance_usd"] == "-100.00"
    await bus.dispatch(EditBrokerMovement(movement_id=outgoing.affected[0].entity_id,
        expected_version=1, amount="400"), ctx)
    assert (await account(session_factory, business, owner))["balance_usd"] == "100.00"
    await bus.dispatch(DeleteBrokerMovement(movement_id=sell.affected[0].entity_id), ctx, confirmed=True)
    assert (await account(session_factory, business, owner))["balance_usd"] == "-400.00"
    await bus.dispatch(DeleteBrokerMovement(movement_id=outgoing.affected[0].entity_id), ctx, confirmed=True)
    assert (await account(session_factory, business, owner))["balance_usd"] == "0.00"


async def test_permissions_shared_global_and_private_personal(bus, session_factory):
    business, owner, broker, other = await _brokerage(session_factory)
    await sale(bus, _ctx(business, broker), classification="personal")
    row = await account(session_factory, business, owner)
    query = BrokerageQueries(session_factory)
    assert (await query.customer_accounts(business, other, status="all"))["total"] == 0
    with pytest.raises(NotFound):
        await query.customer_account(business, other, row["customer_id"])
    with pytest.raises(NotFound):
        await pay(bus, _ctx(business, other), "100")
    async with session_factory() as db:
        bal = await customer_balance(db, business, row["customer_id"])
        assert bal["balance_usd"] == "1000.00"  # derives the personal portfolio even through generic customer views
    await bus.dispatch(UpsertCustomer(name="Global", customer_classification="global"), _ctx(business, owner))
    foreign, foreign_owner, _, _ = await _brokerage(session_factory)
    with pytest.raises(NotFound):
        await query.customer_account(foreign, foreign_owner, row["customer_id"])


async def test_operational_cutoff_does_not_use_future_payments(bus, session_factory):
    business, owner, _, _ = await _brokerage(session_factory)
    early = replace(_ctx(business, owner), now=_ctx(business, owner).now.replace(day=3))
    await sale(bus, _ctx(business, owner))
    await pay(bus, early, "1000")
    async with session_factory() as db:
        at_sale = await account_balances(db, business, cutoff=date(2026, 10, 1))
        at_payment = await account_balances(db, business, cutoff=date(2026, 10, 3))
    assert sum(at_sale.values()) == 1000
    assert sum(at_payment.values()) == 0
    row = await account(session_factory, business, owner)
    filtered = await BrokerageQueries(session_factory).customer_account(business, owner, row["customer_id"], date_to=date(2026, 10, 1))
    assert filtered["total"] == 1 and filtered["balance_usd"] == "0.00"


async def test_concurrent_payouts_are_real_and_single_counted(bus, session_factory):
    business, owner, broker, other = await _brokerage(session_factory)
    await sale(bus, _ctx(business, owner))
    await asyncio.gather(pay(bus, _ctx(business, broker), "600"), pay(bus, _ctx(business, other), "600"))
    assert (await account(session_factory, business, owner))["balance_usd"] == "-200.00"


async def test_database_rejects_invalid_payout_and_global_pending_sale_charge(bus, session_factory):
    business, owner, broker, _ = await _brokerage(session_factory)
    pending = await sale(bus, _ctx(business, broker))
    with pytest.raises(IntegrityError):
        async with session_factory() as db, db.begin():
            row = await db.get(BrokerMovement, pending.affected[0].entity_id)
            row.balance_delta = Decimal("-990")
            await db.flush()
    payout = await pay(bus, _ctx(business, owner), "10")
    for changes in ({"customer_payout_amount": None}, {"customer_payout_amount": Decimal("-1")}, {"balance_delta": Decimal("-10")}):
        with pytest.raises(IntegrityError):
            async with session_factory() as db, db.begin():
                row = await db.get(BrokerMovement, payout.affected[0].entity_id)
                for key, value in changes.items():
                    setattr(row, key, value)
                await db.flush()


async def test_customer_accounts_api_and_payout_contract(redis, session_factory):
    import httpx

    from app.db.models import BusinessMember
    from app.main import create_app
    from tests.agent.harness import Script, build_test_container

    business, owner, _, _ = await _brokerage(session_factory)
    container, _ = build_test_container(Script())
    app = create_app(container.settings, container)
    app.state.container = container
    async with session_factory() as db:
        phone = (await db.get(BusinessMember, owner)).phone
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        prefix = "/api/panel/v1/brokerage"
        assert (await client.get(prefix + "/customer-accounts")).status_code == 401
        token = await container.links.mint(phone, business)
        assert (await client.post("/api/panel/v1/auth/link", json={"token": token})).status_code == 200
        created = await client.post(prefix + "/operations", json={"operation_type": "sell", "settlement": "pending",
            "operation_amount": "1524", "exchange_rate": "1000", "customer_name": "Hum", "customer_classification": "global"})
        assert created.status_code == 200, created.text
        rows = (await client.get(prefix + "/customer-accounts")).json()
        assert rows["items"][0]["balance_usd"] == "1524.00"
        customer_id = rows["items"][0]["customer_id"]
        payout = await client.post(prefix + "/customer-payouts", json={"amount": "1524", "customer_name": "Hum", "customer_id": customer_id})
        assert payout.status_code == 200, payout.text
        assert payout.json()["data"]["customer_balance"]["balance_usd"] == "0.00"
        history = (await client.get(prefix + f"/customer-accounts/{customer_id}")).json()
        assert history["total"] == 2 and history["balance_usd"] == "0.00"
        assert {m["logical_type"] for m in history["items"]} == {"sell", "customer_payout"}
        assert (await client.get(prefix + "/customer-accounts", params={"status": "settled"})).json()["total"] == 1
        duplicate = await client.post(prefix + "/customer-payouts", json={"amount": "1524", "customer_name": "Hum", "customer_id": customer_id})
        assert duplicate.status_code == 409
    await container.close()


async def test_settled_global_sale_credits_only_commission(bus, session_factory):
    business, owner, broker, _ = await _brokerage(session_factory)
    created = await bus.dispatch(RecordBrokerOperation(operation_type="sell", operation_amount="1000",
        exchange_rate="1000", customer_name="Hum", customer_classification="global"), _ctx(business, broker))
    async with session_factory() as db:
        row = await db.get(BrokerMovement, created.affected[0].entity_id)
        assert row.balance_delta == Decimal("10") and row.commission == Decimal("10")
        assert row.settlement_status == "settled" and row.delivered_amount == 1000
    account_row = await account(session_factory, business, owner)
    assert account_row["balance_usd"] == "0.00"


async def test_exposure_never_offsets_different_customers(bus, session_factory):
    business, owner, _, _ = await _brokerage(session_factory)
    ctx = _ctx(business, owner)
    await sale(bus, ctx, "500")
    await bus.dispatch(RecordPendingBrokerPurchase(amount="200", exchange_rate="1000",
        customer_name="Carlos", customer_classification="global"), ctx)
    query = BrokerageQueries(session_factory)
    accounts = await query.customer_accounts(business, owner, limit=1)
    assert accounts["total"] == 2
    assert accounts["total_to_collect"] == "200.00" and accounts["total_to_pay"] == "500.00"
    summary = await _summary_on(query, business, owner)
    assert summary["totals"]["receivables_open"] == "200.00"
    assert summary["totals"]["customer_credit"] == "500.00"


async def test_optional_balance_steps_restate_active_history_and_preserve_privacy(bus, session_factory):
    from datetime import timedelta

    business, owner, broker, other = await _brokerage(session_factory)
    query = BrokerageQueries(session_factory)
    base = _ctx(business, broker)
    contexts = [replace(base, now=base.now + timedelta(seconds=i)) for i in range(6)]
    buy = await purchase(bus, contexts[0], "200", classification="personal")
    await sale(bus, contexts[1], "100", classification="personal")
    await pay(bus, contexts[2], "150", outgoing=False)
    await pay(bus, contexts[3], "25")
    await bus.dispatch(RecordBrokerOperation(operation_type="sell", operation_amount="10", exchange_rate="1000",
                                            customer_name="Hum"), contexts[4])  # settled: zero customer effect
    row = await account(session_factory, business, owner)

    page = await query.customer_account(business, broker, row["customer_id"], limit=2, include_balance_steps=True)
    assert page["total"] == 5 and page["balance_usd"] == "25.00"
    assert [(m["customer_balance_before"], m["customer_balance_delta"], m["customer_balance_after"])
            for m in page["items"]] == [("25.00", "0.00", "25.00"), ("50.00", "-25.00", "25.00")]
    assert "customer_balance_before" not in (await query.customer_account(business, broker, row["customer_id"]))["items"][0]
    with pytest.raises(NotFound):
        await query.customer_account(business, other, row["customer_id"], include_balance_steps=True)
    with pytest.raises(ValidationFailed):
        await query.customer_account(business, owner, row["customer_id"], offset=1, include_balance_steps=True)

    await bus.dispatch(DeleteBrokerMovement(movement_id=buy.affected[0].entity_id), contexts[5], confirmed=True)
    history = await query.customer_account(business, broker, row["customer_id"], include_balance_steps=True)
    assert history["balance_usd"] == "225.00" and history["total"] == 4
    assert history["items"][-1]["customer_balance_before"] == "0.00"
    assert history["items"][0]["customer_balance_after"] == "225.00"
