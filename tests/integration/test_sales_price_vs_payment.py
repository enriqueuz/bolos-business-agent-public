"""Ventas: precio acordado vs dinero realmente recibido (definición "tasas, precios y pagos", secciones 7–9 y precio menor).

El negocio de estas pruebas cobra a BCV 800 (mercado 900). Nunca se inventa una tasa: si no cuadra, se pregunta.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.agent.receipts import render
from app.application.bus import load_command
from app.application.commands.catalog import SetProductPrice
from app.application.commands.rates import SetRate
from app.application.commands.records import EditRecord, RecordChanges
from app.application.commands.sales import RecordSale, SaleItemInput
from app.db.models import Business, Product, Receivable, Sale
from app.domain.enums import Currency, EntityType, PaymentMethod, PaymentStatus, RateScope, SaleResolution
from app.domain.errors import NeedsChoice


@pytest.fixture
async def bcv(session_factory, business):
    async with session_factory() as s, s.begin():
        await s.execute(update(Business).where(Business.id == business.id).values(charge_rate_mode="official"))
    return business


def _item(product="espejo", price=10, currency=Currency.USD, qty=1):
    return SaleItemInput(product=product, quantity=Decimal(qty), unit_price=Decimal(price) if price is not None else None,
                         currency=currency)


async def _sale(session_factory, res) -> Sale:
    async with session_factory() as s:
        return await s.get(Sale, res.affected[0].entity_id)


async def _pick(bus, ctx, asked: NeedsChoice, title: str):
    option = next(o for o in asked.options if o["title"] == title)
    assert option["action"] == "command", f"«{title}» no ejecuta un comando"
    return await bus.dispatch(load_command(option["command_kind"], option["command_payload"]), ctx)


async def test_dollar_cash_sale_keeps_dollars_in_the_box(bus, agent_ctx, session_factory, bcv):
    """§7: vendí un caucho en $40 en efectivo → entraron $40, no bolívares."""
    res = await bus.dispatch(RecordSale(items=[_item("caucho", 40)], payment_method=PaymentMethod.CASH), agent_ctx)
    sale = await _sale(session_factory, res)
    assert (sale.total_amount, sale.paid_amount, sale.paid_currency, sale.settlement) == (40, 40, "USD", "exact")
    assert sale.payment_method == "cash" and sale.rate_official == 800 and sale.rate_market == 900  # referencias


async def test_dollar_price_paid_in_bolivars_at_the_charge_rate(bus, agent_ctx, session_factory, bcv):
    """§8: espejo $10, pagó 8.000 Bs por pago móvil a BCV 800 → pagada, sin preguntar."""
    res = await bus.dispatch(RecordSale(items=[_item()], amount_paid=Decimal(8000), paid_currency=Currency.VES,
                                        payment_method=PaymentMethod.MOBILE_PAYMENT), agent_ctx)
    sale = await _sale(session_factory, res)
    assert (sale.total_amount, sale.total_currency) == (10, "USD")
    assert (sale.paid_amount, sale.paid_currency, sale.payment_status) == (8000, "VES", "paid")
    assert (sale.rate_applied, sale.rate_applied_source) == (800, "official")
    text = render(res)
    assert "Total: *$10,00 (≈ 8.000,00 Bs)*" in text and "Cobrado: *8.000,00 Bs* · Pago móvil (tasa BCV 800,00)" in text


async def test_mobile_payment_implies_bolivars(bus, agent_ctx, session_factory, bcv):
    res = await bus.dispatch(RecordSale(items=[_item("caucho", 40)], payment_method=PaymentMethod.MOBILE_PAYMENT), agent_ctx)
    sale = await _sale(session_factory, res)
    assert (sale.paid_amount, sale.paid_currency) == (32000, "VES")


async def test_own_rate_sale(bus, panel_ctx, session_factory, bcv):
    """§8 con tasa propia 300: $10 y pagó 3.000 Bs → pagada, con la tasa propia congelada."""
    await bus.dispatch(SetRate(rate=Decimal(300), purpose=RateScope.CHARGE), panel_ctx, confirmed=True)  # lejos de BCV: se confirma
    res = await bus.dispatch(RecordSale(items=[_item()], amount_paid=Decimal(3000)), panel_ctx)
    sale = await _sale(session_factory, res)
    assert (sale.paid_currency, sale.payment_status, sale.rate_applied_source) == ("VES", "paid", "custom")


async def test_short_payment_asks_what_happened(bus, agent_ctx, session_factory, bcv):
    """§9: a BCV debía pagar 8.000 Bs y entregó 3.000 → no se marca pagada: se pregunta."""
    cmd = RecordSale(items=[_item()], amount_paid=Decimal(3000), paid_currency=Currency.VES)
    with pytest.raises(NeedsChoice) as asked:
        await bus.dispatch(cmd, agent_ctx)
    assert asked.value.message == ("El precio son 8.000,00 Bs a la tasa BCV de 800,00 y el cliente entregó 3.000,00 Bs. "
                                   "Faltan *5.000,00 Bs*.\n\n¿Cómo quedó la venta?")
    assert [o["title"] for o in asked.value.options] == ["Quedó debiendo", "Pagada con descuento", "Corregir"]
    assert asked.value.options[0]["action"] == "reply"  # sin cliente: el agente pregunta a quién anotarle la deuda
    async with session_factory() as s:
        assert (await s.execute(select(Sale))).first() is None


async def test_short_payment_left_owing_stays_in_the_price_currency(bus, agent_ctx, session_factory, bcv):
    cmd = RecordSale(items=[_item()], amount_paid=Decimal(3000), paid_currency=Currency.VES, customer_name="Ana",
                     force_new_customer=True)
    with pytest.raises(NeedsChoice) as asked:
        await bus.dispatch(cmd, agent_ctx)
    res = await _pick(bus, agent_ctx, asked.value, "Quedó debiendo")
    if res.needs_confirmation:  # cliente nuevo con deuda: se confirma
        res = await bus.dispatch(load_command("record_sale", res.command_payload), agent_ctx, confirmed=True)
    sale = await _sale(session_factory, res)
    assert (sale.payment_status, sale.settlement, sale.paid_amount) == ("partial", "debt", 3000)
    async with session_factory() as s:
        debt = (await s.execute(select(Receivable))).scalar_one()
    assert (debt.amount, debt.currency) == (Decimal("6.2500"), "USD")  # $10 − 3.000 Bs ÷ 800
    assert "Quedó debiendo: *$6,25* (≈ 5.000,00 Bs hoy)" in render(res)


async def test_short_payment_accepted_as_full_is_a_discount_not_a_new_rate(bus, agent_ctx, session_factory, bcv):
    cmd = RecordSale(items=[_item()], amount_paid=Decimal(3000), paid_currency=Currency.VES)
    with pytest.raises(NeedsChoice) as asked:
        await bus.dispatch(cmd, agent_ctx)
    res = await _pick(bus, agent_ctx, asked.value, "Pagada con descuento")
    sale = await _sale(session_factory, res)
    assert (sale.payment_status, sale.settlement) == ("paid", "discount")
    assert (sale.list_total_amount, sale.discount_amount, sale.total_amount) == (10, Decimal("6.2500"), Decimal("3.7500"))
    assert sale.rate_applied == 800  # la tasa sigue siendo la del negocio: la diferencia se ve como descuento


async def test_mentioned_rate_is_never_applied(bus, agent_ctx, session_factory, bcv):
    """"Se lo calculé a 700": se avisa que el negocio cobra a BCV 800 y se pregunta; no se usa 700."""
    with pytest.raises(NeedsChoice) as asked:
        await bus.dispatch(RecordSale(items=[_item()], mentioned_rate=Decimal(700)), agent_ctx)
    assert asked.value.message.startswith("Tu negocio está configurado para cobrar con la tasa BCV de 800,00 "
                                          "(serían 8.000,00 Bs), no a 700,00. El precio son 8.000,00 Bs")
    assert "Faltan *1.000,00 Bs*" in asked.value.message


async def test_overpayment_is_recorded_and_flagged(bus, agent_ctx, session_factory, bcv):
    res = await bus.dispatch(RecordSale(items=[_item()], amount_paid=Decimal(12), paid_currency=Currency.USD), agent_ctx)
    assert "Pagó $2,00 de más" in render(res)


# ------------------------------------------------------------------------------------------------ precio menor al habitual


async def _catalog(bus, panel_ctx) -> None:
    await bus.dispatch(SetProductPrice(product_name="Espejo", price=Decimal(10), currency=Currency.USD), panel_ctx)


async def test_lower_price_asks_once_with_the_three_outcomes(bus, agent_ctx, panel_ctx, bcv):
    """UNA sola pregunta. Cada pregunta de más son dos mensajes de WhatsApp, así que el conteo de opciones es
    parte del contrato: si alguien vuelve a encadenar preguntas, esto falla."""
    await _catalog(bus, panel_ctx)
    with pytest.raises(NeedsChoice) as asked:
        await bus.dispatch(RecordSale(items=[_item(price=2000, currency=Currency.VES)]), agent_ctx)
    assert asked.value.message == ("*Espejo* normalmente cuesta $10,00, que hoy son 8.000,00 Bs a BCV. "
                                   "Me dijiste que lo vendiste en 2.000,00 Bs.\n\n¿Qué pasó? "
                                   "Si cambio el precio, queda en $2,50 de ahora en adelante.")
    assert [o["title"] for o in asked.value.options] == ["Le hice un descuento", "Precio especial", "Cambiar el precio"]


def test_the_three_titles_survive_a_whatsapp_button():
    """Los títulos se cortan a 20 caracteres SIN AVISAR (BUTTON_TITLE_MAX); pasado ese largo el botón llega mutilado."""
    from app.channels.types import BUTTON_TITLE_MAX, MAX_BUTTONS

    titles = ["Le hice un descuento", "Precio especial", "Cambiar el precio"]
    assert len(titles) <= MAX_BUTTONS
    assert all(len(t) <= BUTTON_TITLE_MAX for t in titles), [(t, len(t)) for t in titles]


async def test_a_button_from_the_old_three_step_flow_reasks_instead_of_hanging(bus, agent_ctx, panel_ctx, bcv):
    """Al desplegar el cambio quedan mensajes viejos en los chats. Si el dueño toca uno de esos botones llega un
    `lower_price`, que ya nadie produce: tiene que caer en la pregunta nueva, no quedarse trabado ni registrar."""
    await _catalog(bus, panel_ctx)
    cmd = RecordSale(items=[_item(price=2000, currency=Currency.VES)], price_resolution=SaleResolution.LOWER_PRICE)
    with pytest.raises(NeedsChoice) as asked:
        await bus.dispatch(cmd, agent_ctx)
    assert [o["title"] for o in asked.value.options] == ["Le hice un descuento", "Precio especial", "Cambiar el precio"]


async def test_cheaper_only_this_sale_keeps_the_usual_price(bus, agent_ctx, panel_ctx, session_factory, bcv):
    await _catalog(bus, panel_ctx)
    with pytest.raises(NeedsChoice) as first:
        await bus.dispatch(RecordSale(items=[_item(price=2000, currency=Currency.VES)]), agent_ctx)
    res = await _pick(bus, agent_ctx, first.value, "Precio especial")
    sale = await _sale(session_factory, res)
    assert (sale.total_amount, sale.total_currency, sale.discount_amount, sale.paid_currency) == (2000, "VES", 0, "VES")
    async with session_factory() as s:
        assert (await s.execute(select(Product.price_amount))).scalar_one() == 10
    assert "El precio habitual de Espejo sigue siendo $10,00 (esta venta: 2.000,00 Bs ($2,50))." in render(res)


async def test_changing_the_usual_price_takes_one_tap(bus, agent_ctx, panel_ctx, session_factory, bcv):
    """El precio de lista se cambia con UN toque: el monto nuevo ya iba en la pregunta, así que la confirmación
    aparte solo repetía lo que el dueño acababa de elegir."""
    await _catalog(bus, panel_ctx)
    with pytest.raises(NeedsChoice) as asked:
        await bus.dispatch(RecordSale(items=[_item(price=2000, currency=Currency.VES)]), agent_ctx)
    res = await _pick(bus, agent_ctx, asked.value, "Cambiar el precio")
    async with session_factory() as s:
        product = (await s.execute(select(Product))).scalar_one()
    assert (product.price_amount, product.price_currency) == (Decimal("2.5000"), "USD")
    assert "Precio habitual de *Espejo*: $10,00 → $2,50" in render(res)


async def test_price_discount_keeps_usual_price_and_records_the_discount(bus, agent_ctx, panel_ctx, session_factory, bcv):
    await _catalog(bus, panel_ctx)
    with pytest.raises(NeedsChoice) as first:
        await bus.dispatch(RecordSale(items=[_item(price=2000, currency=Currency.VES)]), agent_ctx)
    res = await _pick(bus, agent_ctx, first.value, "Le hice un descuento")
    sale = await _sale(session_factory, res)
    assert (sale.list_total_amount, sale.discount_amount, sale.total_amount, sale.total_currency) == (
        10, Decimal("7.5000"), Decimal("2.5000"), "USD")
    assert (sale.paid_amount, sale.paid_currency, sale.settlement) == (2000, "VES", "discount")
    assert sale.discount_local == 6000 and sale.items[0].unit_price_amount == 10
    assert "Precio habitual: $10,00 · Descuento: $7,50 (6.000,00 Bs)" in render(res)


async def test_catalog_price_without_stated_price_does_not_ask(bus, agent_ctx, panel_ctx, bcv):
    await _catalog(bus, panel_ctx)
    res = await bus.dispatch(RecordSale(items=[_item(price=None, currency=None)]), agent_ctx)
    assert res.status == "ok"


# ------------------------------------------------------------------------------------------------ correcciones


async def test_editing_the_payment_currency_recomputes_with_the_frozen_rate(bus, panel_ctx, session_factory, bcv):
    res = await bus.dispatch(RecordSale(items=[_item()], customer_name="Ana", payment_status=PaymentStatus.PARTIAL,
                                        amount_paid=Decimal(4)), panel_ctx)
    await bus.dispatch(SetRate(rate=Decimal(1000), purpose=RateScope.CHARGE), panel_ctx)  # no debe afectar la corrección
    ref = res.affected[0]
    await bus.dispatch(EditRecord(entity_type=EntityType.SALE, entity_id=ref.entity_id, expected_version=ref.version,
                                  changes=RecordChanges(amount_paid=Decimal(4000), paid_currency=Currency.VES)), panel_ctx)
    sale = await _sale(session_factory, res)
    assert (sale.paid_amount, sale.paid_currency, sale.payment_status) == (4000, "VES", "partial")
    async with session_factory() as s:
        debt = (await s.execute(select(Receivable).where(Receivable.deleted_at.is_(None)))).scalar_one()
    assert debt.amount == 5  # $10 − 4.000 Bs ÷ 800 (tasa congelada de la venta)


# ------------------------------------------------------------------------------------------------ precio tomado del pago


async def test_payment_capture_sets_the_price_of_a_product_without_price(bus, agent_ctx, session_factory, bcv):
    """"Vendí un aceite 20W50 por pago móvil" + captura de 8.253,66 Bs: el precio es el pago ÷ la tasa para cobrar, en $."""
    res = await bus.dispatch(RecordSale(items=[SaleItemInput(product="aceite 20W50")], amount_paid=Decimal("8253.66"),
                                        payment_method=PaymentMethod.MOBILE_PAYMENT), agent_ctx)
    sale = await _sale(session_factory, res)
    assert (sale.total_amount, sale.total_currency) == (Decimal("10.3171"), "USD")  # 8.253,66 ÷ 800
    assert (sale.paid_amount, sale.paid_currency, sale.payment_status) == (Decimal("8253.66"), "VES", "paid")
    assert sale.items[0].price_source == "payment"
    assert "Tomé el precio del pago: 8.253,66 Bs ÷ tasa BCV 800,00." in render(res)
    async with session_factory() as s:
        assert (await s.execute(select(Product.price_amount))).scalar_one() is None  # no se inventa un precio habitual


async def test_payment_in_dollars_is_the_price(bus, agent_ctx, session_factory, bcv):
    res = await bus.dispatch(RecordSale(items=[SaleItemInput(product="batería")], amount_paid=Decimal(60),
                                        payment_method=PaymentMethod.ZELLE), agent_ctx)
    sale = await _sale(session_factory, res)
    assert (sale.total_amount, sale.total_currency, sale.paid_currency) == (60, "USD", "USD")


async def test_product_with_price_compares_the_payment_instead(bus, agent_ctx, panel_ctx, bcv):
    """Si el producto ya tiene precio, el pago se compara con él: aquí no alcanza, así que se pregunta."""
    await _catalog(bus, panel_ctx)
    with pytest.raises(NeedsChoice) as asked:
        await bus.dispatch(RecordSale(items=[SaleItemInput(product="espejo")], amount_paid=Decimal(7000),
                                      payment_method=PaymentMethod.MOBILE_PAYMENT), agent_ctx)
    assert "El precio son 8.000,00 Bs" in asked.value.message and "entregó 7.000,00 Bs" in asked.value.message


async def test_price_is_not_taken_from_the_payment_when_it_is_ambiguous(bus, agent_ctx, bcv):
    from app.domain.errors import ValidationFailed

    with pytest.raises(ValidationFailed):  # dos productos y un solo pago: no se sabe cuánto costó cada uno
        await bus.dispatch(RecordSale(items=[SaleItemInput(product="aceite"), SaleItemInput(product="filtro")],
                                      amount_paid=Decimal(9000), payment_method=PaymentMethod.MOBILE_PAYMENT), agent_ctx)
