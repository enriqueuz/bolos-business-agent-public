"""Frozen customer choices pin identity and revalidate the business, not a name."""

import pytest
from sqlalchemy import select

from app.application.commands.receivables import CreateReceivable, RecordPayment
from app.application.commands.sales import RecordSale, SaleItemInput
from app.db.models import Business, Customer, CustomerPayment, Receivable, Sale
from app.domain.errors import NotFound


@pytest.mark.parametrize("kind", ["sale", "receivable", "payment"])
async def test_frozen_customer_id_is_respected_and_tenant_checked(bus, agent_ctx, business, session_factory, kind):
    async with session_factory() as session, session.begin():
        customer = Customer(business_id=business.id, name="Ana Pérez", normalized_name="ana perez")
        other_business = Business(name="Otro negocio")
        session.add_all([customer, other_business])
        await session.flush()
        other_customer = Customer(business_id=other_business.id, name="Ana Pérez", normalized_name="ana perez")
        session.add(other_customer)
        await session.flush()
        customer_id, foreign_customer_id = customer.id, other_customer.id

    def command(selected_id):
        common = {"customer_name": "Ana", "customer_id": selected_id}
        if kind == "sale":
            return RecordSale(**common, items=[SaleItemInput(product="Bujía", unit_price="2", currency="USD")])
        if kind == "receivable":
            return CreateReceivable(**common, amount="2", currency="USD")
        return RecordPayment(**common, amount="2", currency="USD")

    # A valid same-business name must not hide a foreign selected ID.
    with pytest.raises(NotFound):
        await bus.dispatch(command(foreign_customer_id), agent_ctx, confirmed=True)
    await bus.dispatch(command(customer_id), agent_ctx, confirmed=True)
    model = {"sale": Sale, "receivable": Receivable, "payment": CustomerPayment}[kind]
    async with session_factory() as session:
        row = (await session.scalars(select(model))).one()
        assert row.customer_id == customer_id
