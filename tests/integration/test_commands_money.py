"""Comandos de dinero contra Postgres real: patas, auditoría, fiado automático, confirmación."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.application.commands.expenses import RecordExpense
from app.application.commands.purchases import PurchaseItemInput, RecordPurchase
from app.application.commands.receivables import CreateReceivable, RecordPayment
from app.application.commands.sales import RecordSale, SaleItemInput
from app.db.models import AuditLog, Product, Receivable, Sale
from app.domain.enums import Currency, ExpenseCategory, PaymentMethod, PaymentStatus
from app.domain.errors import Ambiguous, NotFound, ValidationFailed


async def test_paid_sale_freezes_three_legs_and_audits(bus, agent_ctx, session_factory):
    res = await bus.dispatch(
        RecordSale(items=[SaleItemInput(product="bujía NGK", quantity=Decimal(2), unit_price=Decimal(3), currency=Currency.USD)],
                   payment_method=PaymentMethod.CASH),
        agent_ctx,
    )
    assert res.status == "ok"
    total = res.data["sale"]["total"]
    assert total["amount"] == "6.0000" and total["usd_official"] == "6.0000"
    assert total["local"] == "5400.0000"  # 6 * 900 (mercado)
    async with session_factory() as s:
        sale = (await s.execute(select(Sale))).scalar_one()
        assert sale.rate_official == Decimal("800") and sale.rate_market == Decimal("900")
        assert (await s.execute(select(func.count()).select_from(AuditLog))).scalar_one() == 1
        assert (await s.execute(select(func.count()).select_from(Product))).scalar_one() == 1


async def test_sale_uses_catalog_price_and_asks_when_missing(bus, agent_ctx, panel_ctx):
    from app.application.commands.catalog import SetProductPrice

    await bus.dispatch(SetProductPrice(product_name="Cadena 428", price=Decimal(12), currency=Currency.USD), panel_ctx)
    res = await bus.dispatch(RecordSale(items=[SaleItemInput(product="cadena 428")]), agent_ctx)
    assert res.data["sale"]["items"][0]["price_source"] == "catalog"
    with pytest.raises(ValidationFailed) as exc:
        await bus.dispatch(RecordSale(items=[SaleItemInput(product="kit de arrastre")]), agent_ctx)
    assert exc.value.details["missing"] == "unit_price"


async def test_credit_sale_with_new_customer_records_without_confirmation(bus, agent_ctx, session_factory):
    cmd = RecordSale(items=[SaleItemInput(product="aceite", unit_price=Decimal(8), currency=Currency.USD)],
                     customer_name="Moisés", payment_status=PaymentStatus.CREDIT)
    res = await bus.dispatch(cmd, agent_ctx)
    assert res.status == "ok"
    assert res.data["receivable"]["customer_balance"]["owed"] == {"USD": "8.0000"}
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Sale))).scalar_one() == 1
        assert (await s.execute(select(func.count()).select_from(Receivable))).scalar_one() == 1


async def test_partial_sale_creates_receivable_for_remainder(bus, panel_ctx):
    res = await bus.dispatch(
        RecordSale(items=[SaleItemInput(product="caucho", unit_price=Decimal(40), currency=Currency.USD)],
                   customer_name="Pedro", payment_status=PaymentStatus.PARTIAL, amount_paid=Decimal(15)),
        panel_ctx,
    )
    assert res.data["receivable"]["pending"] == "$25,00"


async def test_payment_fifo_across_receivables_and_currencies(bus, panel_ctx, agent_ctx):
    await bus.dispatch(CreateReceivable(customer_name="Moisés", amount=Decimal(10), currency=Currency.USD), panel_ctx)
    await bus.dispatch(CreateReceivable(customer_name="Moisés", amount=Decimal(20), currency=Currency.USD), panel_ctx)
    # 13.500 Bs a 900 = $15 → salda el primero ($10) y abona $5 al segundo
    res = await bus.dispatch(RecordPayment(customer_name="moises", amount=Decimal(13500), currency=Currency.VES), agent_ctx)
    assert res.status == "ok"
    assert [a["applied"] for a in res.data["applied_to"]] == ["$10,00", "$5,00"]
    assert res.data["customer_balance"]["owed"] == {"USD": "15.0000"}


async def test_overpayment_requires_confirmation(bus, panel_ctx, agent_ctx):
    await bus.dispatch(CreateReceivable(customer_name="Ana", amount=Decimal(5), currency=Currency.USD), panel_ctx)
    res = await bus.dispatch(RecordPayment(customer_name="Ana", amount=Decimal(8), currency=Currency.USD), agent_ctx)
    assert res.needs_confirmation and res.data["unapplied"] == "$3,00"


async def test_payment_for_unknown_customer_is_not_found(bus, agent_ctx):
    with pytest.raises(NotFound):
        await bus.dispatch(RecordPayment(customer_name="Fantasma", amount=Decimal(5), currency=Currency.USD), agent_ctx)


async def test_ambiguous_customer_asks(bus, panel_ctx, agent_ctx):
    await bus.dispatch(CreateReceivable(customer_name="José Pérez", amount=Decimal(5), currency=Currency.USD), panel_ctx)
    await bus.dispatch(CreateReceivable(customer_name="José Pereira", amount=Decimal(5), currency=Currency.USD, force_new_customer=True), panel_ctx)
    with pytest.raises(Ambiguous) as exc:
        await bus.dispatch(RecordPayment(customer_name="José", amount=Decimal(1), currency=Currency.USD), agent_ctx)
    assert set(exc.value.details["candidates"]) == {"José Pérez", "José Pereira"}


async def test_purchase_items_update_cost_and_next_sale_freezes_cost(bus, agent_ctx, session_factory):
    await bus.dispatch(
        RecordPurchase(supplier_name="Repuestos El Gocho",
                       items=[PurchaseItemInput(product="Bujía NGK", quantity=Decimal(10), unit_cost=Decimal("1.5"), currency=Currency.USD)]),
        agent_ctx,
    )
    res = await bus.dispatch(RecordSale(items=[SaleItemInput(product="bujia ngk", quantity=Decimal(2), unit_price=Decimal(3), currency=Currency.USD)]), agent_ctx)
    assert res.data["sale"]["items"][0]["cost_known"] is True
    async with session_factory() as s:
        sale = (await s.execute(select(Sale))).scalar_one()
        assert sale.items[0].cost_usd_official == Decimal("3.0000")


async def test_purchase_without_items(bus, agent_ctx):
    # regresión encontrada por los evals: la auditoría hacía lazy-load de items en una compra sin ítems
    res = await bus.dispatch(RecordPurchase(description="mercancía", amount=Decimal(300), currency=Currency.USD,
                                            supplier_name="Valencia"), agent_ctx)
    assert res.data["purchase"]["amount"]["amount"] == "300.0000"


async def test_expense_assumes_currency_by_magnitude(bus, agent_ctx):
    small = await bus.dispatch(RecordExpense(description="delivery", amount=Decimal(4), category=ExpenseCategory.TRANSPORT), agent_ctx)
    big = await bus.dispatch(RecordExpense(description="luz", amount=Decimal(45000), category=ExpenseCategory.UTILITIES), agent_ctx)
    assert small.data["expense"]["amount"]["currency"] == "USD" and small.data["expense"]["currency_assumed"]
    assert big.data["expense"]["amount"]["currency"] == "VES"


async def test_future_date_rejected(bus, agent_ctx):
    from datetime import date

    with pytest.raises(ValidationFailed):
        await bus.dispatch(RecordExpense(description="x", amount=Decimal(1), currency=Currency.USD, business_date=date(2030, 1, 1)), agent_ctx)


async def test_movement_uses_business_charge_rate(bus, agent_ctx):
    res = await bus.dispatch(RecordExpense(description="repuesto urgente", amount=Decimal(10), currency=Currency.USD), agent_ctx)
    assert res.data["expense"]["amount"]["local"] == "9000.0000"  # el negocio del fixture cobra a mercado (900)
    assert res.data["expense"]["amount"]["rate_applied_source"] == "market"


async def test_panel_actor_never_needs_confirmation(bus, panel_ctx):
    res = await bus.dispatch(CreateReceivable(customer_name="Nuevo", amount=Decimal(5), currency=Currency.USD), panel_ctx)
    assert res.status == "ok"
    agent = replace(panel_ctx)
    assert agent is not panel_ctx
