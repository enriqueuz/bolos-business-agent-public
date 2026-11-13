"""Resolución de productos: nombres específicos no deben convertirse en fricción ni en otro SKU."""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.application.commands.catalog import SetProductPrice
from app.application.commands.sales import RecordSale, SaleItemInput
from app.application.resolvers import search_products
from app.db.models import Product, SaleItem
from app.domain.enums import Currency
from app.domain.errors import Ambiguous


async def _price(bus, ctx, name: str, amount: str):
    return await bus.dispatch(SetProductPrice(product_name=name, price=Decimal(amount), currency=Currency.USD), ctx)


async def test_specific_name_and_price_create_a_distinct_product_without_asking(bus, panel_ctx, session_factory):
    await _price(bus, panel_ctx, "Bujía DEF", "5")

    result = await bus.dispatch(RecordSale(items=[
        SaleItemInput(product="Bujía ABC", unit_price=Decimal("10"), currency=Currency.USD),
    ]), panel_ctx)

    assert result.data["sale"]["items"][0]["product"] == "Bujía ABC"
    async with session_factory() as session:
        products = (await session.execute(select(Product).order_by(Product.name))).scalars().all()
        item = (await session.execute(select(SaleItem))).scalar_one()
    assert [product.name for product in products] == ["Bujía ABC", "Bujía DEF"]
    assert item.product_id == next(product.id for product in products if product.name == "Bujía ABC")


async def test_specific_brand_is_new_but_bare_family_name_still_asks(bus, panel_ctx, session_factory):
    await _price(bus, panel_ctx, "Aceite Mobil", "13")

    await bus.dispatch(RecordSale(items=[
        SaleItemInput(product="Aceite Oilven", unit_price=Decimal("11"), currency=Currency.USD),
    ]), panel_ctx)

    with pytest.raises(Ambiguous) as asked:
        await bus.dispatch(RecordSale(items=[
            SaleItemInput(product="Aceite", unit_price=Decimal("10"), currency=Currency.USD),
        ]), panel_ctx)
    assert set(asked.value.details["candidates"]) == {"Aceite Mobil", "Aceite Oilven"}


async def test_product_search_does_not_cross_families_because_of_a_shared_qa_suffix(bus, panel_ctx, session_factory):
    await _price(bus, panel_ctx, "Bujía QA Brand 260926", "1.2")
    await _price(bus, panel_ctx, "Aceite Mobil QA260926", "13")

    async with session_factory() as session:
        matches = await search_products(session, panel_ctx.business_id, "Aceite QA Late 260926")

    assert all("Bujía" not in match.entity.name for match in matches)


async def test_numeric_model_tokens_are_distinct_even_when_only_one_digit_changes(bus, panel_ctx, session_factory):
    await _price(bus, panel_ctx, "Kit rodamiento QA260926", "13")

    await bus.dispatch(RecordSale(items=[
        SaleItemInput(product="Kit rodamiento QA260927", unit_price=Decimal("14"), currency=Currency.USD),
    ]), panel_ctx)

    async with session_factory() as session:
        names = (await session.execute(select(Product.name).order_by(Product.name))).scalars().all()
    assert names == ["Kit rodamiento QA260926", "Kit rodamiento QA260927"]


@pytest.mark.parametrize(("existing", "incoming"), [
    ("Combo hamburguesa clásica", "Combo hamburguesa doble"),
    ("Camisa algodón azul", "Camisa algodón roja"),
    ("Curso Excel básico", "Curso Excel avanzado"),
    ("Café molido 500 g", "Café molido 1 kg"),
])
async def test_specific_name_and_price_work_the_same_in_any_business_vertical(
    bus, panel_ctx, session_factory, existing, incoming
):
    """La identidad sale de la estructura del nombre, no de vocabulario de una industria."""
    await _price(bus, panel_ctx, existing, "5")

    result = await bus.dispatch(RecordSale(items=[
        SaleItemInput(product=incoming, unit_price=Decimal("10"), currency=Currency.USD),
    ]), panel_ctx)

    assert result.data["sale"]["items"][0]["product"] == incoming
    async with session_factory() as session:
        names = set((await session.execute(select(Product.name))).scalars().all())
    assert names == {existing, incoming}


async def test_incomplete_name_still_asks_between_variants_in_any_vertical(bus, panel_ctx):
    await _price(bus, panel_ctx, "Combo hamburguesa clásica", "5")
    await _price(bus, panel_ctx, "Combo hamburguesa doble", "8")

    with pytest.raises(Ambiguous) as asked:
        await bus.dispatch(RecordSale(items=[
            SaleItemInput(product="Combo hamburguesa", unit_price=Decimal("6"), currency=Currency.USD),
        ]), panel_ctx)

    assert set(asked.value.details["candidates"]) == {
        "Combo hamburguesa clásica",
        "Combo hamburguesa doble",
    }
