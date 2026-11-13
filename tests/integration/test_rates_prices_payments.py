"""Tasas, precios y pagos: tasa aplicada según el modo del negocio, pago real de la venta, tasa para cobrar (confirmación
diaria, para qué es una tasa) y cambios de moneda (tasa real de los bolívares)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.application.commands.business import UpdateBusiness
from app.application.commands.cash import CloseCash
from app.application.commands.exchanges import RecordExchange
from app.application.commands.expenses import RecordExpense
from app.application.commands.rates import SetChargeRateMode, SetRate
from app.application.commands.sales import RecordSale, SaleItemInput
from app.application.queries.cash import cash_summary
from app.db.models import Business, CurrencyExchange, Expense, Sale
from app.domain.enums import (
    ChargeRateMode,
    Currency,
    CurrencyView,
    ExpenseCategory,
    PaymentMethod,
    PaymentStatus,
    RateScope,
)
from tests.conftest import NOW


async def _set_mode(session_factory, business_id, mode: ChargeRateMode) -> None:
    async with session_factory() as s, s.begin():
        await s.execute(update(Business).where(Business.id == business_id).values(charge_rate_mode=mode.value))


async def test_applied_rate_follows_charge_mode(bus, panel_ctx, session_factory, business):
    """BCV 800 y mercado 900 (fixture). El dólar se cobra a la tasa del modo; BCV y mercado quedan como referencia."""
    await _set_mode(session_factory, business.id, ChargeRateMode.OFFICIAL)
    official = await bus.dispatch(RecordExpense(description="flete", amount=Decimal(10), currency=Currency.USD,
                                                category=ExpenseCategory.TRANSPORT), panel_ctx)
    assert official.data["expense"]["amount"]["local"] == "8000.0000"
    assert official.data["expense"]["amount"]["rate_applied_source"] == "official"

    await bus.dispatch(SetRate(rate=Decimal(850), purpose=RateScope.CHARGE), panel_ctx)  # fijar tasa propia = cobrar con ella
    custom = await bus.dispatch(RecordExpense(description="luz", amount=Decimal(8500), currency=Currency.VES,
                                              category=ExpenseCategory.UTILITIES), panel_ctx)
    amount = custom.data["expense"]["amount"]
    assert amount["usd_applied"] == "10.0000" and amount["rate_applied_source"] == "custom"
    assert Decimal(amount["rate_market"]) == 900  # la de mercado NO se pisa con la propia
    assert amount["usd_market"] == "9.4444" and amount["usd_official"] == "10.6250"

    await bus.dispatch(SetChargeRateMode(mode=ChargeRateMode.MARKET), panel_ctx)
    async with session_factory() as s:
        assert (await s.get(Business, business.id)).charge_rate_mode == ChargeRateMode.MARKET.value


async def test_sale_stores_real_payment_and_settlement(bus, panel_ctx, session_factory):
    paid = await bus.dispatch(RecordSale(items=[SaleItemInput(product="caucho", unit_price=Decimal(40), currency=Currency.USD)],
                                         payment_method=PaymentMethod.CASH), panel_ctx)
    partial = await bus.dispatch(RecordSale(items=[SaleItemInput(product="espejo", unit_price=Decimal(10), currency=Currency.USD)],
                                            customer_name="Ana", payment_status=PaymentStatus.PARTIAL,
                                            amount_paid=Decimal(4)), panel_ctx)
    async with session_factory() as s:
        cash_sale = await s.get(Sale, paid.affected[0].entity_id)
        assert (cash_sale.paid_amount, cash_sale.paid_currency, cash_sale.settlement) == (Decimal("40.0000"), "USD", "exact")
        assert cash_sale.list_total_amount == Decimal("40.0000") and cash_sale.discount_amount == 0
        assert cash_sale.paid_usd_applied == Decimal("40.0000") and cash_sale.paid_local == Decimal("36000.0000")
        debt_sale = await s.get(Sale, partial.affected[0].entity_id)
        assert (debt_sale.paid_amount, debt_sale.settlement) == (Decimal("4.0000"), "debt")
        assert debt_sale.items[0].item_usd_applied == Decimal("10.0000")


async def test_exchange_moves_cash_but_nets_zero_and_is_not_income(bus, panel_ctx, session_factory, business):
    async with session_factory() as s, s.begin():
        s.add(CurrencyExchange(business_id=business.id, business_date=date(2026, 9, 14), occurred_at=NOW,
                               out_amount=Decimal(200), out_currency="USD", out_method="binance",
                               in_amount=Decimal(160000), in_currency="VES", in_method="binance",
                               rate=Decimal(800), rate_official=Decimal(800), rate_market=Decimal(900)))
    async with session_factory() as s:
        summary = await cash_summary(s, business.id, date(2026, 9, 14), CurrencyView.USD_APPLIED)
    assert summary["exchanges"] == [{"text": "Binance: salieron $200,00 y entraron 160.000,00 Bs", "rate": "800,00"}]
    assert summary["by_currency_net"] == {"USD": "-$200,00", "VES": "160.000,00 Bs"}  # el dinero real sí cambió de forma
    assert "Binance" not in summary["by_payment_method_net"]
    assert summary["raw"]["cash_in"] == "0" and summary["raw"]["net_cash"] == "0"  # no es venta ni gasto

    from app.db.views import money_movements as mm
    async with session_factory() as s:
        rows = (await s.execute(select(mm.c.direction, mm.c.cashflow_usd_applied).where(mm.c.kind == "exchange"))).all()
    assert sum(d * v for d, v in rows) == 0  # $200 salen, 160.000 Bs ÷ 800 = $200 entran


async def test_update_business_translates_old_view_choice_into_charge_mode(bus, panel_ctx, session_factory, business):
    await bus.dispatch(UpdateBusiness(display_currency=CurrencyView.USD_OFFICIAL), panel_ctx)
    async with session_factory() as s:
        b = await s.get(Business, business.id)
        assert (b.display_currency, b.charge_rate_mode) == ("usd_applied", "official")
    await bus.dispatch(UpdateBusiness(display_currency=CurrencyView.LOCAL), panel_ctx)  # ver en Bs no cambia cómo cobra
    async with session_factory() as s:
        b = await s.get(Business, business.id)
        assert (b.display_currency, b.charge_rate_mode) == ("local", "official")


async def test_close_cash_reports_in_applied_view(bus, panel_ctx, session_factory):
    await bus.dispatch(RecordExpense(description="almuerzo", amount=Decimal(9000), currency=Currency.VES,
                                     category=ExpenseCategory.FOOD, payment_method=PaymentMethod.MOBILE_PAYMENT), panel_ctx)
    res = await bus.dispatch(CloseCash(), panel_ctx)
    summary = res.data["summary"]
    assert summary["currency_view"] == "usd_applied" and summary["expenses"]["total"] == "$10,00"  # 9.000 Bs ÷ 900
    assert summary["by_payment_method_net"]["Pago móvil"] == {"VES": "-9.000,00 Bs"}
    async with session_factory() as s:
        expense = (await s.execute(select(Expense))).scalar_one()
        assert expense.rate_funds is None and expense.amount_usd_real is None  # sin tasa real aún: no se inventa


# ------------------------------------------------------------------------------------------------ Parte 3: tasa para cobrar


async def test_rate_without_purpose_asks_what_it_is_for(bus, agent_ctx, session_factory, business):
    """"Pon la tasa en 800": no se adivina si es para cobrar o la tasa real de los bolívares."""
    from app.application.bus import load_command
    from app.db.models import CustomRate
    from app.domain.errors import NeedsChoice

    with pytest.raises(NeedsChoice) as asked:
        await bus.dispatch(SetRate(rate=Decimal(800)), agent_ctx)
    assert "¿Para qué quieres usar la tasa de *800,00 Bs/$*?" in asked.value.message
    assert [o["title"] for o in asked.value.options] == ["Para cobrar", "Tasa real de mis Bs", "Para ambas cosas"]
    async with session_factory() as s:
        assert (await s.execute(select(CustomRate))).first() is None  # nada guardado hasta que elija

    funds = asked.value.options[1]
    res = await bus.dispatch(load_command(funds["command_kind"], funds["command_payload"]), agent_ctx)
    assert res.status == "ok" and res.data["purpose"] == "funds"
    async with session_factory() as s:
        b = await s.get(Business, business.id)
        assert b.charge_rate_mode == "market"  # la tasa real no cambia cómo cobra
        rates = await bus._deps.rates.effective(s, business.id)
        assert rates.funds == 800 and rates.applied == 900


async def test_charge_rate_is_frozen_per_movement_and_old_ones_do_not_change(bus, panel_ctx, session_factory, business):
    first = await bus.dispatch(RecordExpense(description="luz", amount=Decimal(9000), currency=Currency.VES,
                                             category=ExpenseCategory.UTILITIES), panel_ctx)
    await bus.dispatch(SetRate(rate=Decimal(820), purpose=RateScope.CHARGE), panel_ctx)
    second = await bus.dispatch(RecordExpense(description="agua", amount=Decimal(8200), currency=Currency.VES,
                                              category=ExpenseCategory.UTILITIES), panel_ctx)
    assert second.data["expense"]["amount"]["usd_applied"] == "10.0000"
    async with session_factory() as s:
        old = await s.get(Expense, first.affected[0].entity_id)
        assert old.rate_applied == 900 and old.amount_usd_applied == Decimal("10.0000")  # sigue con la de su día
        b = await s.get(Business, business.id)
        assert b.charge_rate_mode == "custom" and b.charge_rate_confirmed_on == NOW.date()


async def test_charge_mode_custom_needs_a_rate_and_leaving_it_retires_the_rate(bus, panel_ctx, session_factory, business):
    from app.db.models import CustomRate
    from app.domain.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        await bus.dispatch(SetChargeRateMode(mode=ChargeRateMode.CUSTOM), panel_ctx)
    await bus.dispatch(SetRate(rate=Decimal(850), purpose=RateScope.BOTH), panel_ctx)
    res = await bus.dispatch(SetChargeRateMode(mode=ChargeRateMode.OFFICIAL), panel_ctx)
    assert res.data["label"] == "BCV" and res.data["rate"] == "800,00"
    async with session_factory() as s:
        current = (await s.execute(select(CustomRate.purpose).where(CustomRate.cleared_at.is_(None)))).scalars().all()
        assert current == ["funds"]  # la tasa real de los bolívares sigue; la de cobrar se retiró


async def _own_rate_set_yesterday(session_factory, business_id) -> None:
    from datetime import timedelta

    from app.db.models import CustomRate

    async with session_factory() as s, s.begin():
        s.add(CustomRate(business_id=business_id, purpose="charge", source="manual", rate=Decimal(300), set_at=NOW - timedelta(days=1)))
        await s.execute(update(Business).where(Business.id == business_id).values(charge_rate_mode="custom",
                                                                                   charge_rate_confirmed_on=None))


async def test_own_rate_is_confirmed_once_a_day_before_the_first_bolivar_operation(bus, agent_ctx, panel_ctx, session_factory,
                                                                                    business):
    from app.application.bus import load_command
    from app.domain.errors import NeedsChoice

    await _own_rate_set_yesterday(session_factory, business.id)
    usd_only = await bus.dispatch(RecordExpense(description="flete", amount=Decimal(10), currency=Currency.USD,
                                                category=ExpenseCategory.TRANSPORT), agent_ctx)
    assert usd_only.status == "ok"  # en $ no depende de la tasa: no pregunta
    assert (await bus.dispatch(RecordExpense(description="luz", amount=Decimal(3000), currency=Currency.VES,
                                             category=ExpenseCategory.UTILITIES), panel_ctx)).status == "ok"  # el panel no

    cmd = RecordExpense(description="almuerzo", amount=Decimal(3000), currency=Currency.VES, category=ExpenseCategory.FOOD)
    with pytest.raises(NeedsChoice) as asked:
        await bus.dispatch(cmd, agent_ctx)
    assert asked.value.message == "Tu tasa para cobrar es *300,00 Bs/$* y la fijaste ayer. ¿La mantenemos para hoy?"
    keep, update_ = asked.value.options
    assert (keep["title"], update_["title"], update_["action"]) == ("Mantener 300,00", "Actualizar tasa", "reply")
    res = await bus.dispatch(load_command(keep["command_kind"], keep["command_payload"]), agent_ctx)
    assert res.data["expense"]["amount"]["usd_applied"] == "10.0000"
    again = await bus.dispatch(cmd, agent_ctx)  # ya la confirmó hoy: no vuelve a preguntar
    assert again.status == "ok"
    async with session_factory() as s:
        assert (await s.get(Business, business.id)).charge_rate_confirmed_on == NOW.date()


# ------------------------------------------------------------------------------------------------ Parte 4: cambios de moneda


def _exchange(**kw) -> RecordExchange:
    base = dict(out_amount=Decimal(200), out_currency=Currency.USD, out_method=PaymentMethod.BINANCE,
                in_amount=Decimal(160000), in_currency=Currency.VES, in_method=PaymentMethod.BINANCE)
    return RecordExchange(**{**base, **kw})


async def test_exchange_sets_the_real_rate_of_the_bolivars_and_is_not_a_sale(bus, panel_ctx, session_factory, business):
    from app.agent.receipts import render
    from app.db.models import CustomRate

    res = await bus.dispatch(_exchange(), panel_ctx)
    assert render(res) == ("💱 *Cambio registrado*: salieron $200,00 y entraron 160.000,00 Bs por Binance. "
                           "Tu tasa real fue *800,00 Bs/$*.\n_No cuenta como venta ni como gasto._")
    async with session_factory() as s:
        funds = (await s.execute(select(CustomRate).where(CustomRate.purpose == "funds", CustomRate.cleared_at.is_(None)))).scalar_one()
        assert (funds.rate, funds.source, funds.exchange_id) == (800, "exchange", res.affected[0].entity_id)
        assert (await s.get(Business, business.id)).charge_rate_mode == "market"  # cobrar no cambia
        assert (await s.execute(select(Sale))).first() is None
    async with session_factory() as s:
        summary = await cash_summary(s, business.id, NOW.date(), CurrencyView.USD_APPLIED)
    assert summary["sales"]["count"] == 0 and summary["raw"]["net_cash"] == "0"


async def test_buying_dollars_does_not_change_the_real_rate(bus, panel_ctx, session_factory):
    from app.agent.receipts import render
    from app.db.models import CustomRate

    res = await bus.dispatch(_exchange(out_amount=Decimal(42000), out_currency=Currency.VES, in_amount=Decimal(50),
                                       in_currency=Currency.USD, out_method=PaymentMethod.MOBILE_PAYMENT,
                                       in_method=PaymentMethod.CASH), panel_ctx)
    assert "salieron 42.000,00 Bs (Pago móvil) y entraron $50,00 (Efectivo)" in render(res)
    assert "Los compraste a *840,00 Bs/$*" in render(res)
    async with session_factory() as s:
        assert (await s.execute(select(CustomRate))).first() is None


async def test_exchange_validation(bus, panel_ctx):
    from app.domain.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        await bus.dispatch(_exchange(in_currency=Currency.USD, in_amount=Decimal(200)), panel_ctx)
    odd = await bus.dispatch(_exchange(in_amount=Decimal(20000)), panel_ctx)  # 100 Bs/$ con BCV en 800: ¿error de tipeo?
    assert odd.needs_confirmation


async def test_deleting_the_exchange_restores_the_previous_real_rate(bus, panel_ctx, session_factory):
    from app.application.commands.records import DeleteRecord
    from app.db.models import CustomRate
    from app.domain.enums import EntityType

    await bus.dispatch(SetRate(rate=Decimal(750), purpose=RateScope.FUNDS), panel_ctx)
    res = await bus.dispatch(_exchange(), panel_ctx)
    deleted = await bus.dispatch(DeleteRecord(entity_type=EntityType.EXCHANGE, entity_id=res.affected[0].entity_id), panel_ctx)
    assert deleted.data["note"] == "Tu tasa real de los bolívares vuelve a ser 750,00."
    async with session_factory() as s:
        current = (await s.execute(select(CustomRate).where(CustomRate.purpose == "funds", CustomRate.cleared_at.is_(None)))).scalar_one()
        assert current.rate == 750


async def test_backdated_exchange_does_not_replace_a_newer_real_rate(bus, panel_ctx, session_factory):
    from datetime import timedelta

    from app.db.models import CustomRate

    await bus.dispatch(_exchange(), panel_ctx)  # hoy a 800
    old = await bus.dispatch(_exchange(in_amount=Decimal(150000), business_date=NOW.date() - timedelta(days=3)), panel_ctx)
    assert old.data["funds_rate_set"] is False
    async with session_factory() as s:
        current = (await s.execute(select(CustomRate).where(CustomRate.purpose == "funds", CustomRate.cleared_at.is_(None)))).scalar_one()
        assert current.rate == 800


# ------------------------------------------------------------------------------------------------ Parte 6: costo real


async def test_payment_in_bolivars_has_real_cost_and_official_reference(bus, panel_ctx, session_factory):
    """Bs obtenidos a 700; BCV 800. 4.000 Bs de almuerzo costaron realmente $5,71 (a BCV serían $5,00)."""
    from app.agent.receipts import render

    await bus.dispatch(SetRate(rate=Decimal(700), purpose=RateScope.FUNDS), panel_ctx)
    res = await bus.dispatch(RecordExpense(description="almuerzo", amount=Decimal(4000), currency=Currency.VES,
                                           category=ExpenseCategory.FOOD), panel_ctx)
    assert "Costo real: *$5,71* (tu tasa real 700,00 Bs/$) · Referencia BCV: $5,00" in render(res)
    async with session_factory() as s:
        expense = (await s.execute(select(Expense))).scalar_one()
    assert (expense.rate_funds, expense.amount_usd_real) == (700, Decimal("5.7143"))

    dollars = await bus.dispatch(RecordExpense(description="flete", amount=Decimal(10), currency=Currency.USD,
                                               category=ExpenseCategory.TRANSPORT), panel_ctx)
    assert "Costo real" not in render(dollars)  # en $ el costo real es el mismo monto


async def test_editing_the_amount_keeps_the_real_rate_of_that_day(bus, panel_ctx, session_factory):
    from app.application.commands.records import EditRecord, RecordChanges
    from app.domain.enums import EntityType

    await bus.dispatch(SetRate(rate=Decimal(700), purpose=RateScope.FUNDS), panel_ctx)
    res = await bus.dispatch(RecordExpense(description="luz", amount=Decimal(7000), currency=Currency.VES,
                                           category=ExpenseCategory.UTILITIES), panel_ctx)
    await bus.dispatch(SetRate(rate=Decimal(1000), purpose=RateScope.FUNDS), panel_ctx)
    ref = res.affected[0]
    await bus.dispatch(EditRecord(entity_type=EntityType.EXPENSE, entity_id=ref.entity_id, expected_version=ref.version,
                                  changes=RecordChanges(amount=Decimal(14000))), panel_ctx)
    async with session_factory() as s:
        expense = await s.get(Expense, ref.entity_id)
    assert (expense.rate_funds, expense.amount_usd_real) == (700, Decimal("20.0000"))


async def test_fx_loss_metric_compares_real_cost_with_the_official_rate(bus, panel_ctx, session_factory, rates, business):
    from app.application.commands.purchases import RecordPurchase
    from app.application.metrics.compiler import MetricsQuery
    from app.application.queries.service import QueryService

    await bus.dispatch(SetRate(rate=Decimal(700), purpose=RateScope.FUNDS), panel_ctx)
    await bus.dispatch(RecordPurchase(description="mercancía", amount=Decimal(84000), currency=Currency.VES), panel_ctx)
    await bus.dispatch(RecordExpense(description="flete", amount=Decimal(10), currency=Currency.USD,
                                     category=ExpenseCategory.TRANSPORT), panel_ctx)  # en $: no hay diferencia cambiaria
    res = await QueryService(session_factory, rates).metrics(
        business.id, MetricsQuery(metrics=["fx_loss_usd", "purchases_real_usd"], period="today"), NOW)
    totals = res.data["totals"]
    assert totals["fx_loss_usd"]["display"] == "$15,00"  # 84.000 Bs ÷ 700 = $120 real vs $105 a BCV
    assert totals["purchases_real_usd"]["display"] == "$120,00"


# ------------------------------------------------------------------------------------------------ Parte 7: caja y métricas


async def test_cash_shows_real_money_by_currency_then_exchanges_then_a_labeled_equivalent(bus, panel_ctx, session_factory,
                                                                                         business):
    from app.agent.receipts import render

    await bus.dispatch(RecordSale(items=[SaleItemInput(product="caucho", unit_price=Decimal(40), currency=Currency.USD)],
                                  payment_method=PaymentMethod.CASH), panel_ctx)
    await bus.dispatch(RecordSale(items=[SaleItemInput(product="espejo", unit_price=Decimal(10), currency=Currency.USD)],
                                  payment_method=PaymentMethod.MOBILE_PAYMENT), panel_ctx)  # mercado 900 → 9.000 Bs
    await bus.dispatch(RecordExpense(description="proveedor", amount=Decimal(60), currency=Currency.USD,
                                     category=ExpenseCategory.OTHER, payment_method=PaymentMethod.ZELLE), panel_ctx)
    await bus.dispatch(_exchange(), panel_ctx)
    res = await bus.dispatch(CloseCash(), panel_ctx)
    s = res.data["summary"]
    assert s["by_payment_method_net"] == {"Efectivo": {"USD": "$40,00"}, "Pago móvil": {"VES": "9.000,00 Bs"},
                                          "Zelle": {"USD": "-$60,00"}}
    assert s["exchanges"][0]["text"] == "Binance: salieron $200,00 y entraron 160.000,00 Bs"
    assert s["by_currency_net"] == {"USD": "-$220,00", "VES": "169.000,00 Bs"}  # nunca se suman $ con Bs
    report = s["report"]
    assert report["label"] == "Equivalente para reporte usando la tasa de mercado de 900,00 Bs/$"
    assert report["equivalent"] == "-$32,22" and report["rate_raw"] == "900.000000"  # la tasa exacta queda en el cierre
    text = render(res)
    assert text.index("Dinero real") < text.index("Cambio:") < text.index("Equivalente para reporte")


async def test_discounts_are_reported_apart_from_net_sales(bus, agent_ctx, session_factory, rates, business):
    from app.application.bus import load_command
    from app.application.metrics.compiler import MetricsQuery
    from app.application.queries.service import QueryService
    from app.domain.errors import NeedsChoice

    await _set_mode(session_factory, business.id, ChargeRateMode.OFFICIAL)
    with pytest.raises(NeedsChoice) as asked:
        await bus.dispatch(RecordSale(items=[SaleItemInput(product="espejo", unit_price=Decimal(10), currency=Currency.USD)],
                                      amount_paid=Decimal(2000), paid_currency=Currency.VES), agent_ctx)
    discount = next(o for o in asked.value.options if o["title"] == "Pagada con descuento")
    await bus.dispatch(load_command(discount["command_kind"], discount["command_payload"]), agent_ctx)
    res = await QueryService(session_factory, rates).metrics(business.id, MetricsQuery(
        metrics=["gross_sales", "discounts_total", "sales_total", "collected_total", "item_revenue"], period="today"), NOW)
    t = res.data["totals"]
    assert (t["gross_sales"]["display"], t["discounts_total"]["display"], t["sales_total"]["display"]) == (
        "$10,00", "$7,50", "$2,50")
    assert t["collected_total"]["display"] == "$2,50" and t["item_revenue"]["display"] == "$2,50"  # neto por producto
