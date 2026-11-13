"""Financial acceptance of the persisted snapshot, OOXML and authorized download."""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal as D
from uuid import uuid4

import httpx
import pytest

from app.application.commands.brokerage import (
    DeleteBrokerMovement,
    EditBrokerMovement,
    RecordBrokerAccountPayment,
    RecordBrokerAccountPurchase,
    RecordBrokerOperation,
    RecordBrokerPurchase,
    RecordBrokerReceivablePayment,
)
from app.application.exports.brokerage import BrokerageWorkbook, render_workbook, workbook_filename
from app.application.exports.brokerage_data import WorkbookData, load_workbook_data
from app.db.models import BrokerMovement, BusinessMember
from app.domain.errors import NotFound, ValidationFailed
from app.main import create_app
from scripts.example_brokerage_workbook import example_data
from tests.agent.harness import Script, build_test_container
from tests.integration.test_brokerage import _brokerage, _ctx
from tests.integration.test_brokerage_required_bs import seed_legacy_incomplete
from tests.workbook_helpers import NS, WorkbookView

START, END = date(2026, 10, 1), date(2026, 10, 10)


def ctx(business, member, day=10):
    return replace(_ctx(business, member), now=datetime(2026, 10, day, 16, tzinfo=UTC))


async def export(session_factory, business, **kwargs):
    return WorkbookView(await BrokerageWorkbook(session_factory).build(
        business, date_from=START, date_to=END, **kwargs,
    ))


async def test_six_editable_sheets_tables_formulas_and_safe_text():
    data = example_data()
    data = replace(data, movements=(replace(data.movements[0], customer='=HYPERLINK("https://invalid","x")', description="https://invalid"), *data.movements[1:]))
    book = WorkbookView(render_workbook(data))
    assert list(book.sheets) == ["Resumen", "Compra", "Venta", "Clientes y CxC", "Corredores", "Ajustes manuales"]
    assert set(book.tables) == {"Compras", "Ventas", "Cartera", "MovimientosCliente", "Cuentas", "Diario", "Ajustes", "Meses"}
    assert not any("externalLink" in name or "vbaProject" in name for name in book.names)
    for name, sheet in book.sheets.items():
        assert sheet.find("s:sheetProtection", NS) is None
        assert sheet.find("s:sheetViews/s:sheetView/s:pane", NS) is not None
        for formula in sheet.findall(".//s:f", NS):
            assert "#REF!" not in formula.text and "_xlfn" not in formula.text
            assert "http" not in formula.text
        if name != "Resumen":
            assert sheet.find('s:cols/s:col[@hidden="1"]', NS) is not None or name == "Ajustes manuales"
    for _, table in book.tables.values():
        assert table.find("s:autoFilter", NS) is not None
    assert book.formula("Compra", "B8") is None
    assert book.value("Compra", "B8").startswith("=HYPERLINK")
    assert book.formula("Resumen", "E20").endswith('(B10-E10)*E7/E10,"")')
    assert book.formula("Compra", "E9") and not book.formula("Compra", "D9")  # USD/Bs
    assert book.formula("Compra", "D10") and not book.formula("Compra", "E10")  # USD/rate
    assert "ROUND" in book.formula("Compra", "D10")
    assert book.value("Compra", "J9") == 1000  # received, not a debt payment
    assert book.value("Compra", "K9") == 13
    assert book.value("Compra", "M9") == D("9.87")
    assert book.value("Resumen", "B25") == D("28.87")
    assert book.value("Resumen", "B27") == 1100
    assert book.value("Resumen", "B31") == D("-605.13")
    assert book.value("Resumen", "E7") == 2787
    assert book.value("Resumen", "E8") == 2227600
    assert book.value("Resumen", "B7") == 500
    assert book.value("Resumen", "B8") == 411000
    assert abs(book.value("Resumen", "E20") - ((D(822) - D(2227600)/D(2787))*D(2787)/(D(2227600)/D(2787)))) < D("0.0000001")
    assert all(book.value("Resumen", cell) == 0 for cell in ["E25", "E26", "E27", "E29", "E30", "E31"])
    assert len(book.records("Ajustes")) == 1
    assert all(v is None for v in book.records("Ajustes")[0].values())
    assert "Orden" in book.formula("Corredores", "X8")  # independent of current row order


async def test_empty_workbook_has_no_fictitious_operations_or_rates():
    book = WorkbookView(render_workbook(WorkbookData("Vacío", START, END)))
    assert book.value("Resumen", "B11") == 0 and book.value("Resumen", "E11") == 0
    assert book.value("Resumen", "E20") is None
    assert book.value("Resumen", "B10") is None
    assert book.formula("Compra", "R8")  # completeness also works in initially empty tables


async def test_persisted_sales_excess_scope_corrections_and_archive(bus, session_factory):
    business, owner, broker, other = await _brokerage(session_factory)
    other_business, other_owner, _, _ = await _brokerage(session_factory)
    for member in (owner, broker):
        await bus.dispatch(RecordBrokerOperation(operation_type="sell", operation_amount="200", exchange_rate="800"), ctx(business, member))
    purchase = await bus.dispatch(RecordBrokerPurchase(amount="987", received_amount="1000", operation_amount_bs="789600"), ctx(business, broker))
    corrected = await bus.dispatch(RecordBrokerOperation(operation_type="buy", operation_amount="100", exchange_rate="800"), ctx(business, other))
    await bus.dispatch(EditBrokerMovement(movement_id=corrected.affected[0].entity_id, expected_version=1, amount="120"), ctx(business, other))
    archived = await bus.dispatch(RecordBrokerOperation(operation_type="sell", operation_amount="999", exchange_rate="800"), ctx(business, broker))
    await bus.dispatch(DeleteBrokerMovement(movement_id=archived.affected[0].entity_id, expected_version=1), ctx(business, broker), confirmed=True)
    await bus.dispatch(RecordBrokerOperation(operation_type="sell", operation_amount="777", exchange_rate="800"), ctx(other_business, other_owner))
    book = await export(session_factory, business)
    sales = book.records("Ventas")
    assert len(sales) == 2
    house = next(s for s in sales if s["Cuenta / corredor"] == "Casa")
    broker_sale = next(s for s in sales if s["Cuenta / corredor"] == "Samu")
    assert house["Comisión USD"] == house["Efecto en balance USD"] == 0
    assert broker_sale["Comisión USD"] == 2 and broker_sale["Efecto en balance USD"] == 2
    buys = book.records("Compras")
    excess = next(b for b in buys if b["ID movimiento"] == str(purchase.affected[0].entity_id))
    assert (excess["Compra USD"], excess["Compra Bs"], excess["Tasa de compra"], excess["USD recibidos"], excess["Excedente USD"], excess["Comisión USD"]) == (987, 789600, 800, 1000, 13, D("9.87"))
    assert any(b["Compra USD"] == 120 for b in buys)
    assert book.value("Resumen", "B7") == 400
    assert book.value("Resumen", "B8") == 320000
    assert book.value("Resumen", "E7") == 1107
    assert book.value("Resumen", "E8") == 885600
    assert all(r["Corredor"] != "Casa" for r in book.records("Cuentas"))
    scoped = await export(session_factory, business, broker_id=broker, account_scope="broker")
    assert len(scoped.records("Ventas")) == 1
    with pytest.raises(NotFound):
        await export(session_factory, business, broker_id=other_owner, account_scope="broker")
    with pytest.raises(ValidationFailed):
        await export(session_factory, business, broker_id=broker, account_scope="house")


async def test_weighted_complete_coverage_and_unknown_historical_rates(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    for usd, bs in [("100", "75000"), ("300", "240000")]:
        await bus.dispatch(RecordBrokerOperation(operation_type="buy", operation_amount=usd, operation_amount_bs=bs), ctx(business, broker))
    legacy = await bus.dispatch(RecordBrokerOperation(operation_type="buy", operation_amount="200", exchange_rate="800"), ctx(business, broker))
    await seed_legacy_incomplete(session_factory, legacy.affected[0].entity_id)
    await bus.dispatch(RecordBrokerOperation(operation_type="sell", operation_amount="100", exchange_rate="820"), ctx(business, broker))
    book = await export(session_factory, business)
    assert book.value("Resumen", "E7") == 600
    assert book.value("Resumen", "E8") == 315000
    assert book.value("Resumen", "E9") == 400
    assert book.value("Resumen", "E10") == D("787.50")
    assert book.value("Resumen", "E12") == 1
    assert book.value("Resumen", "E13") == 200
    assert book.value("Resumen", "E20") is None
    assert book.value("Resumen", "E21").startswith("No calculable")
    row = next(r for r in book.records("Compras") if r["ID movimiento"] == str(legacy.affected[0].entity_id))
    assert row["Compra Bs"] is None and row["Tasa de compra"] is None


@pytest.mark.parametrize("classification,delta", [("global", "10"), ("personal", "-990")])
async def test_cutoff_cartera_openings_payments_and_purchase_settlement(bus, session_factory, classification, delta):
    business, _, broker, other = await _brokerage(session_factory)
    await bus.dispatch(RecordBrokerAccountPurchase(amount="300", exchange_rate="800", business_date=date(2026, 9, 30)), ctx(business, broker))
    purchase = await bus.dispatch(RecordBrokerPurchase(amount="1000", exchange_rate="800", customer_name="Cliente", customer_classification=classification,
                                                     settlement="pending", business_date=START), ctx(business, broker))
    payer = other if classification == "global" else broker
    await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Cliente", amount="400", business_date=date(2026, 10, 5)), ctx(business, payer))
    await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Cliente", amount="600", business_date=date(2026, 10, 12)), ctx(business, payer, 16))
    await bus.dispatch(RecordBrokerAccountPayment(amount="50", business_date=END), ctx(business, broker))
    # Latest status is settled, but the Oct 10 workbook must be partial.
    async with session_factory() as session:
        original = await session.get(BrokerMovement, purchase.affected[0].entity_id)
        assert original.settlement_status == "settled"
    book = await export(session_factory, business)
    debt = book.records("Cartera")[0]
    assert debt["Compras pendientes USD"] == 1000
    assert debt["Pagos recibidos USD"] == 400
    assert debt["Por cobrar USD"] == 600
    assert debt["Tipo de cliente"] == classification.capitalize()
    assert debt["Cartera responsable"] == ("Global / Casa" if classification == "global" else "Samu")
    assert book.records("Compras")[0]["Estado de liquidación"] == "Parcial"
    assert book.records("Compras")[0]["USD recibidos"] is None
    assert len(book.records("MovimientosCliente")) == 2
    assert sum(r["Importe USD"] or 0 for r in book.records("MovimientosCliente") if r["Tipo"] == "Pago recibido") == 400
    balances = {r["Corredor"]: r for r in book.records("Cuentas")}
    assert balances["Samu"]["Balance inicial USD"] == -297
    assert balances["Samu"]["Variación USD"] == D(delta) + 50 + (400 if classification == "personal" else 0)
    if classification == "global":
        assert balances["Pedro"]["Balance final USD"] == 0
    # A later period carries the earlier debt and payment into its opening.
    later = WorkbookView(await BrokerageWorkbook(session_factory).build(business, date_from=date(2026, 10, 6), date_to=END))
    assert later.records("Cartera")[0]["Saldo inicial USD"] == -600
    assert later.records("Cartera")[0]["Por cobrar USD"] == 600
    assert later.records("Compras")[0]["Compra USD"] is None


async def test_split_payments_and_inactive_broker_with_opening(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    for amount in ("100", "200"):
        await bus.dispatch(RecordBrokerPurchase(amount=amount, exchange_rate="800", customer_name="Cliente", customer_classification="personal",
                                               settlement="pending", business_date=date(2026, 9, 30)), ctx(business, broker))
    await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Cliente", amount="150", business_date=START), ctx(business, broker))
    async with session_factory() as session, session.begin():
        member = await session.get(BusinessMember, broker)
        member.is_active = False
    book = await export(session_factory, business)
    assert len(book.records("MovimientosCliente")) == 1
    assert sum(r["Importe USD"] or 0 for r in book.records("MovimientosCliente") if r["Tipo"] == "Pago recibido") == 150
    assert sum(r["Por cobrar USD"] for r in book.records("Cartera")) == 150
    account = next(r for r in book.records("Cuentas") if r["Corredor"] == "Samu")
    assert account["Balance inicial USD"] == -297
    assert account["Balance final USD"] == -147
    assert account["Acceso"] == "Inactivo"


async def test_original_monetary_pair_tolerance_and_date_boundaries(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    for day in [date(2026, 9, 30), START, END, date(2026, 10, 11)]:
        await bus.dispatch(RecordBrokerOperation(operation_type="buy", operation_amount="1", operation_amount_bs="1", exchange_rate="1.01", business_date=day), ctx(business, broker, 16))
    book = await export(session_factory, business)
    assert len(book.records("Compras")) == 2
    assert book.value("Resumen", "E7") == 2
    for r in book.records("Compras"):
        assert r["Compra Bs"] == 1
        assert r["Tasa de compra"] == D("1.01")
        assert r["Tasa original"] == D("1.01")
    assert 'Tasa original' in book.formula("Compra", "E8")


async def test_export_endpoint_authorization_defaults_dates_and_tenant(redis, session_factory):
    business, owner, broker, _ = await _brokerage(session_factory)
    foreign_business, _, foreign_broker, _ = await _brokerage(session_factory)
    container, _ = build_test_container(Script())
    app = create_app(container.settings, container)
    app.state.container = container
    async with session_factory() as session:
        owner_row = await session.get(BusinessMember, owner)
        owner_phone = owner_row.phone
        broker_row = await session.get(BusinessMember, broker)
        broker_phone = broker_row.phone
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/api/panel/v1/brokerage/export.xlsx"
        assert (await client.get(path)).status_code == 401
        for phone, expected in [(broker_phone, 403), (owner_phone, 200)]:
            token = await container.links.mint(phone, business)
            assert (await client.post("/api/panel/v1/auth/link", json={"token": token})).status_code == 200
            result = await client.get(path)
            assert result.status_code == expected
            if expected == 200:
                assert result.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                assert "Bolos_Casa_de_Cambio_" in result.headers["content-disposition"]
                assert result.headers["cache-control"] == "private, no-store"
                WorkbookView(result.content)
        assert (await client.get(path, params={"account_scope": "broker", "broker_id": str(foreign_broker)})).status_code == 404
        assert (await client.get(path, params={"date_from": "2026-10-10", "date_to": "2026-10-01"})).status_code == 422
        assert (await client.get(path, params={"date_from": "2099-01-01"})).status_code == 422
        assert (await client.get(path, params={"account_scope": "broker"})).status_code == 422
        assert (await client.get(path, params={"broker_id": str(uuid4())})).status_code == 404
    await container.close()


async def test_filename_and_empty_cutoff(session_factory):
    assert workbook_filename(date(2026, 11, 1), date(2026, 11, 30)) == "Bolos_Casa_de_Cambio_2026-11.xlsx"
    assert workbook_filename(START, END) == "Bolos_Casa_de_Cambio_2026-10-01_2026-10-10.xlsx"
    business, _, _, _ = await _brokerage(session_factory)
    snapshot = await load_workbook_data(session_factory, business, start=START, end=END)
    assert snapshot.debts == () and snapshot.payments == () and snapshot.movements == ()
    assert all(b.opening == 0 for b in snapshot.brokers)


@pytest.mark.parametrize("mode,classification,delta,commission,excess", [
    ("settled", "global", "22.87", "9.87", "13"),
    ("pending", "global", "22.87", "9.87", "13"),
    ("pending", "personal", "-964.13", "9.87", "13"),
    ("account", None, "-964.13", "9.87", "13"),
    ("house", None, "0", "0", "0"),
])
async def test_all_purchase_modalities_preserve_recognized_effects(bus, session_factory, mode, classification, delta, commission, excess):
    business, owner, broker, _ = await _brokerage(session_factory)
    common = dict(amount="987", received_amount="1000", operation_amount_bs="789600")
    if mode == "account":
        command = RecordBrokerAccountPurchase(**common)
    elif mode == "house":
        command = RecordBrokerPurchase(**common)
    else:
        command = RecordBrokerPurchase(**common, customer_name="Gamal", customer_classification=classification, settlement=mode)
    await bus.dispatch(command, ctx(business, owner if mode == "house" else broker))
    book = await export(session_factory, business)
    row = book.records("Compras")[0]
    assert (row["Compra USD"], row["Compra Bs"], row["Tasa de compra"], row["USD recibidos"]) == (987, 789600, 800, 1000)
    assert (row["Comisión USD"], row["Excedente USD"], row["Efecto en balance USD"]) == (D(commission), D(excess), D(delta))
    if mode == "pending":
        assert book.records("Cartera")[0]["Por cobrar USD"] == 987
    else:
        assert book.records("Cartera")[0]["Cliente"] is None


async def test_unapplied_payment_and_deleted_payment_do_not_inflate_debt_or_commission(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    await bus.dispatch(RecordBrokerPurchase(amount="100", exchange_rate="800", customer_name="Cliente", customer_classification="personal", settlement="pending"), ctx(business, broker))
    payment = await bus.dispatch(RecordBrokerReceivablePayment(customer_name="Cliente", amount="150"), ctx(business, broker), confirmed=True)
    book = await export(session_factory, business)
    assert book.records("Cartera")[0]["Por cobrar USD"] == 0
    assert book.records("Cartera")[0]["A favor USD"] == 50
    assert sum(r["Importe USD"] or 0 for r in book.records("MovimientosCliente") if r["Tipo"] == "Pago recibido") == 150
    assert book.value("Resumen", "B32") == 50
    assert book.value("Resumen", "B25") == 1
    await bus.dispatch(DeleteBrokerMovement(movement_id=payment.affected[0].entity_id, expected_version=1), ctx(business, broker), confirmed=True)
    book = await export(session_factory, business)
    assert book.records("Cartera")[0]["Por cobrar USD"] == 100
    assert all(r["Tipo"] != "Pago recibido" for r in book.records("MovimientosCliente"))
    assert book.value("Resumen", "B32") == 0
    assert book.value("Resumen", "B25") == 1


async def test_custom_range_monthly_estimates_are_not_added_together():
    from app.application.exports.brokerage_data import MovementRow
    moves = tuple(MovementRow(str(i), day, "", "Casa", operation=kind, usd=D(usd), bs=D(bs),
                              rate=D(bs)/D(usd), basis="usd_bs") for i, (day, kind, usd, bs) in enumerate([
        (date(2026, 10, 15), "buy", "100", "75000"),
        (date(2026, 10, 16), "sell", "50", "40000"),
        (date(2026, 11, 15), "buy", "300", "240000"),
        (date(2026, 11, 16), "sell", "150", "127500"),
    ]))
    book = WorkbookView(render_workbook(WorkbookData("Dos meses", date(2026, 10, 10), date(2026, 11, 20), movements=moves)))
    months = book.records("Meses")
    assert len(months) == 2
    assert (months[0]["Compra USD"], months[1]["Compra USD"]) == (100, 300)
    assert abs(book.value("Resumen", "E20") - (D(50)*D(400)/D("787.5"))) < D("0.000001")
    assert abs(sum(r["Diferencial teórico USD"] for r in months) - book.value("Resumen", "E20")) > D("0.01")
