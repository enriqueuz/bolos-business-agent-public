"""Lecturas y capa semántica de métricas contra Postgres real."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.application.commands.expenses import RecordExpense
from app.application.commands.notes import RememberNote
from app.application.commands.purchases import PurchaseItemInput, RecordPurchase
from app.application.commands.receivables import CreateReceivable, RecordPayment
from app.application.commands.sales import RecordSale, SaleItemInput
from app.application.metrics.compiler import MetricsQuery
from app.application.metrics.definitions import METRICS, Dim, Filter, catalog_text
from app.application.queries.service import QueryService
from app.domain.dates import CompareTo, PeriodName
from app.domain.enums import Currency, EntityType, NoteKind, PaymentMethod, PaymentStatus
from app.domain.errors import ValidationFailed
from tests.conftest import NOW


@pytest.fixture
def queries(session_factory, rates) -> QueryService:
    return QueryService(session_factory, rates)


def item(product, qty, price, currency=Currency.USD):
    return SaleItemInput(product=product, quantity=Decimal(qty), unit_price=Decimal(price), currency=currency)


async def seed(bus, ctx):
    today = NOW.date()
    await bus.dispatch(RecordPurchase(items=[PurchaseItemInput(product="Bujía NGK", quantity=Decimal(20), unit_cost=Decimal("1.5"),
                                                              currency=Currency.USD)], supplier_name="El Gocho",
                                      business_date=today - timedelta(days=8)), ctx)
    await bus.dispatch(RecordSale(items=[item("Bujía NGK", 4, 3)], payment_method=PaymentMethod.CASH), ctx)  # $12, costo $6
    await bus.dispatch(RecordSale(items=[item("Cadena 428", 1, 20)], customer_name="Moisés", payment_status=PaymentStatus.CREDIT), ctx)
    await bus.dispatch(RecordSale(items=[item("Bujía NGK", 2, 3)], business_date=today - timedelta(days=7),
                                  payment_method=PaymentMethod.MOBILE_PAYMENT), ctx)  # semana pasada
    await bus.dispatch(RecordExpense(description="luz", amount=Decimal(8), currency=Currency.USD), ctx)
    await bus.dispatch(RecordPayment(customer_name="Moisés", amount=Decimal(5), currency=Currency.USD), ctx)


async def test_sales_by_product_with_margin_and_coverage(bus, panel_ctx, queries, business):
    await seed(bus, panel_ctx)
    res = await queries.metrics(business.id, MetricsQuery(
        metrics=["item_revenue", "units_sold", "gross_margin", "margin_pct"], group_by=[Dim.PRODUCT], period=PeriodName.TODAY), NOW)
    rows = {r["product"]: r for r in res.data["rows"]}
    assert rows["Bujía NGK"]["item_revenue"]["value"] == "12.0000"
    assert rows["Bujía NGK"]["gross_margin"]["value"] == "6.0000"
    assert rows["Bujía NGK"]["margin_pct"]["display"] == "50.0%"
    assert rows["Cadena 428"]["gross_margin"]["value"] is None  # sin costo: nunca se inventa
    assert "38%" in res.data["cost_coverage"]  # 12 de 32 vendidos tienen costo


async def test_compare_with_previous_period(bus, panel_ctx, queries, business):
    await seed(bus, panel_ctx)
    res = await queries.metrics(business.id, MetricsQuery(metrics=["sales_total", "sales_count"], period=PeriodName.LAST_7_DAYS,
                                                          compare_to=CompareTo.PREVIOUS_PERIOD), NOW)
    assert res.data["totals"]["sales_total"]["value"] == "32.0000"
    assert res.data["comparison"]["totals"]["sales_total"]["value"] == "6.0000"
    assert res.data["comparison"]["change"]["sales_total"] == "433.3%"


async def test_net_cash_flow_and_filters(bus, panel_ctx, queries, business):
    await seed(bus, panel_ctx)
    res = await queries.metrics(business.id, MetricsQuery(metrics=["net_cash_flow"], period=PeriodName.TODAY), NOW)
    assert res.data["totals"]["net_cash_flow"]["value"] == "9.0000"  # 12 cobrado + 5 abono - 8 luz
    filtered = await queries.metrics(business.id, MetricsQuery(metrics=["sales_total"], period=PeriodName.LAST_30_DAYS,
                                                               filters={Filter.CUSTOMER: "moises"}), NOW)
    assert filtered.data["totals"]["sales_total"]["value"] == "20.0000"


async def test_group_by_weekday_and_payment_method(bus, panel_ctx, queries, business):
    await seed(bus, panel_ctx)
    res = await queries.metrics(business.id, MetricsQuery(metrics=["sales_total"], group_by=[Dim.WEEKDAY],
                                                          period=PeriodName.LAST_30_DAYS), NOW)
    assert [r["weekday"] for r in res.data["rows"]] == ["lunes"]
    res2 = await queries.metrics(business.id, MetricsQuery(metrics=["collected_total"], group_by=[Dim.PAYMENT_METHOD],
                                                           period=PeriodName.LAST_30_DAYS), NOW)
    labels = {r["payment_method"] for r in res2.data["rows"]}
    assert {"Efectivo", "Pago móvil"} <= labels


async def test_invalid_combinations_explain_what_is_supported(queries, business):
    with pytest.raises(ValidationFailed) as exc:
        await queries.metrics(business.id, MetricsQuery(metrics=["expenses_total"], group_by=[Dim.PRODUCT]), NOW)
    assert "expense_category" in exc.value.details["supported_group_by"]
    with pytest.raises(ValidationFailed):
        await queries.metrics(business.id, MetricsQuery(metrics=["ganancia_magica"]), NOW)


async def test_other_business_data_is_isolated(bus, panel_ctx, queries, business, session_factory):
    from app.application.context import Actor, ExecContext
    from app.db.models import Business
    from app.domain.enums import ActorType, OnboardingState

    async with session_factory() as s, s.begin():
        other = Business(name="Otro", onboarding_state=OnboardingState.DONE.value, welcomed=True)
        s.add(other)
    other_ctx = ExecContext(business_id=other.id, actor=Actor(ActorType.PANEL), now=NOW)
    await bus.dispatch(RecordSale(items=[item("x", 1, 999)]), other_ctx)
    await seed(bus, panel_ctx)
    res = await queries.metrics(business.id, MetricsQuery(metrics=["sales_total"], period=PeriodName.LAST_30_DAYS), NOW)
    assert res.data["totals"]["sales_total"]["value"] == "38.0000"


async def test_customer_balance_and_receivables_list(bus, panel_ctx, queries, business):
    await seed(bus, panel_ctx)
    bal = await queries.customer_balance(business.id, "moises")
    assert bal.data["balance"]["owed"] == {"USD": "15.0000"}
    assert bal.refs and bal.refs[0].entity_type == EntityType.RECEIVABLE
    lst = await queries.list_receivables(business.id)
    assert lst.data["customers_with_debt"] == 1 and lst.data["top"][0]["customer"] == "Moisés"


async def test_find_records_by_customer_and_text(bus, panel_ctx, queries, business):
    await seed(bus, panel_ctx)
    fixed_period = {
        "period": PeriodName.CUSTOM,
        "date_from": NOW.date() - timedelta(days=30),
        "date_to": NOW.date(),
    }
    by_customer = await queries.find_records(business.id, customer_name="moisés", **fixed_period)
    kinds = {r.entity_type for r in by_customer.refs}
    assert kinds == {EntityType.SALE, EntityType.PAYMENT}
    by_text = await queries.find_records(business.id, text_query="luz", **fixed_period)
    assert [r.entity_type for r in by_text.refs] == [EntityType.EXPENSE]
    by_product = await queries.find_records(business.id, product="bujia", kind=EntityType.SALE, **fixed_period)
    assert len(by_product.refs) == 2
    assert all(" a $" in ref.summary for ref in by_product.refs)  # alertas/correcciones ven el precio unitario exacto


async def test_notes_memory(bus, panel_ctx, queries, business):
    await bus.dispatch(CreateReceivable(customer_name="Moisés", amount=Decimal(1), currency=Currency.USD), panel_ctx)
    await bus.dispatch(RememberNote(text="Moisés siempre paga los viernes", note_kind=NoteKind.CUSTOMER, subject_name="moises"), panel_ctx)
    await bus.dispatch(RememberNote(text="Cerramos los domingos", note_kind=NoteKind.POLICY), panel_ctx)
    found = await queries.search_notes(business.id, query="viernes")
    assert found.data["notes"][0]["subject"] == "Moisés"
    assert await queries.policy_notes(business.id) == ["Cerramos los domingos"]
    subj = await queries.notes_for_subjects(business.id, ["Moisés"])
    assert subj and "viernes" in subj[0]["text"]


def test_catalog_text_lists_every_metric():
    txt = catalog_text()
    assert all(name in txt for name in METRICS)


async def test_date_custom_range(bus, panel_ctx, queries, business):
    await seed(bus, panel_ctx)
    d = NOW.date() - timedelta(days=7)
    res = await queries.metrics(business.id, MetricsQuery(metrics=["sales_total"], period=PeriodName.CUSTOM, date_from=d,
                                                          date_to=date(2026, 9, 7)), NOW)
    assert res.data["totals"]["sales_total"]["value"] == "6.0000"
