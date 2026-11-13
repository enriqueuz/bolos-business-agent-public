"""Correcciones y borrados: tasas congeladas, propagación al fiado, bloqueo optimista, confirmación, caja."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from app.application.commands.cash import CloseCash
from app.application.commands.customers import MergeCustomers, UpsertCustomer
from app.application.commands.expenses import RecordExpense
from app.application.commands.rates import SetRate
from app.application.commands.receivables import CreateReceivable, RecordPayment
from app.application.commands.records import DeleteRecord, EditRecord, RecordChanges
from app.application.commands.sales import RecordSale, SaleItemInput
from app.application.common import customer_balance
from app.db.models import CustomerPayment, CustomRate, Expense, Product, Receivable, Sale, SaleItem
from app.domain.enums import Currency, EntityType, PaymentMethod, PaymentStatus, RateScope
from app.domain.errors import NotFound, Stale, ValidationFailed


async def test_merge_applies_credit_to_combined_debt_and_invalidates_old_versions(bus, panel_ctx, session_factory):
    ana = await bus.dispatch(UpsertCustomer(name="Ana"), panel_ctx)
    luis = await bus.dispatch(UpsertCustomer(name="Luis"), panel_ctx)
    aid, lid = UUID(ana.data["customer_id"]), UUID(luis.data["customer_id"])
    payment = await bus.dispatch(RecordPayment(customer_name="Ana", amount=Decimal(6), currency=Currency.USD), panel_ctx)
    await bus.dispatch(CreateReceivable(customer_name="Luis", amount=Decimal(10), currency=Currency.USD), panel_ctx)
    await bus.dispatch(MergeCustomers(source_customer_id=aid, target_customer_id=lid), panel_ctx)
    async with session_factory() as s:
        balance = await customer_balance(s, panel_ctx.business_id, lid)
        assert balance["owed"] == {"USD": "4.0000"}
        assert balance["credit_in_favor"] == {}
    ref = payment.affected[0]
    with pytest.raises(Stale):
        await bus.dispatch(DeleteRecord(entity_type=EntityType.PAYMENT, entity_id=ref.entity_id, expected_version=ref.version), panel_ctx)


async def test_multi_item_edit_is_atomic_and_preserves_partial_payment(bus, panel_ctx, session_factory):
    sale = await bus.dispatch(RecordSale(items=[
        SaleItemInput(product="Tornillo", quantity=1, unit_price=10, currency="USD"),
        SaleItemInput(product="Filtro", quantity=1, unit_price=2, currency="USD"),
    ], customer_name="Ana", payment_status="partial", amount_paid=9), panel_ctx)
    ref = sale.affected[0]
    with pytest.raises(ValidationFailed):
        await bus.dispatch(EditRecord(entity_type="sale", entity_id=ref.entity_id, changes=RecordChanges(notes="No guardar"),
            item_changes=[RecordChanges(item_position=1, unit_price=1), RecordChanges(item_position=99, unit_price=11)]), panel_ctx)
    async with session_factory() as s:
        row = await s.get(Sale, ref.entity_id)
        assert row.total_amount == 12 and row.notes is None and row.version == ref.version
    await bus.dispatch(EditRecord(entity_type="sale", entity_id=ref.entity_id, expected_version=ref.version,
        changes=RecordChanges(), item_changes=[RecordChanges(item_position=1, unit_price=1), RecordChanges(item_position=2, unit_price=11)]), panel_ctx)
    async with session_factory() as s:
        row = await s.get(Sale, ref.entity_id)
        assert row.total_amount == 12 and row.paid_amount == 9 and row.version == ref.version + 1


async def test_simultaneous_cash_closes_and_rates_do_not_create_duplicate_current_rows(bus, panel_ctx, session_factory):
    import asyncio

    from app.db.models import CashSession

    await asyncio.gather(*[bus.dispatch(CloseCash(), panel_ctx) for _ in range(3)])
    async with session_factory() as s:
        close = (await s.execute(select(CashSession))).scalar_one()
        assert close.times_closed == 3
    await asyncio.gather(*[bus.dispatch(SetRate(rate=Decimal(rate), purpose=RateScope.CHARGE), panel_ctx) for rate in (910, 920, 930)])
    async with session_factory() as s:
        rates = (await s.execute(select(CustomRate).where(CustomRate.cleared_at.is_(None)))).scalars().all()
        assert len(rates) == 1


async def _sale(bus, ctx, **kw):
    base = dict(items=[SaleItemInput(product="cadena 428", quantity=Decimal(2), unit_price=Decimal(12), currency=Currency.USD)])
    base.update(kw)
    res = await bus.dispatch(RecordSale(**base), ctx)
    return res.affected[0]


async def test_edit_quantity_recomputes_with_original_rates(bus, panel_ctx, agent_ctx, session_factory):
    ref = await _sale(bus, panel_ctx)
    # cambia la tasa del negocio: la corrección NO debe usar la nueva
    await bus.dispatch(SetRate(rate=Decimal(1000), purpose=RateScope.CHARGE), panel_ctx)
    quoted = replace(agent_ctx, target_is_quoted=True)
    res = await bus.dispatch(
        EditRecord(entity_type=EntityType.SALE, entity_id=ref.entity_id, expected_version=ref.version,
                   changes=RecordChanges(quantity=Decimal(3))), quoted)
    assert res.status == "ok"
    assert "3x Cadena 428" in res.data["after"]
    async with session_factory() as s:
        sale = await s.get(Sale, ref.entity_id)
        assert sale.total_amount == Decimal("36.0000")
        assert sale.total_local == Decimal("32400.0000")  # 36 * 900, tasa original
        assert sale.version == 2


async def test_sensitive_edit_needs_confirmation_unless_quoted(bus, panel_ctx, agent_ctx):
    ref = await _sale(bus, panel_ctx)
    cmd = EditRecord(entity_type=EntityType.SALE, entity_id=ref.entity_id, changes=RecordChanges(unit_price=Decimal(10)))
    assert (await bus.dispatch(cmd, agent_ctx)).needs_confirmation
    non_sensitive = EditRecord(entity_type=EntityType.SALE, entity_id=ref.entity_id,
                               changes=RecordChanges(payment_method=PaymentMethod.MOBILE_PAYMENT))
    assert (await bus.dispatch(non_sensitive, agent_ctx)).status == "ok"


async def test_stale_version_rejected(bus, panel_ctx):
    ref = await _sale(bus, panel_ctx)
    await bus.dispatch(EditRecord(entity_type=EntityType.SALE, entity_id=ref.entity_id,
                                  changes=RecordChanges(payment_method=PaymentMethod.CARD)), panel_ctx)
    with pytest.raises(Stale):
        await bus.dispatch(EditRecord(entity_type=EntityType.SALE, entity_id=ref.entity_id, expected_version=ref.version,
                                      changes=RecordChanges(payment_method=PaymentMethod.ZELLE)), panel_ctx)


async def test_change_customer_of_credit_sale_moves_receivable(bus, panel_ctx, session_factory):
    ref = await _sale(bus, panel_ctx, customer_name="Moisés", payment_status=PaymentStatus.CREDIT)
    await bus.dispatch(EditRecord(entity_type=EntityType.SALE, entity_id=ref.entity_id,
                                  changes=RecordChanges(customer_name="Pedro")), panel_ctx)
    async with session_factory() as s:
        rec = (await s.execute(select(Receivable).where(Receivable.deleted_at.is_(None)))).scalar_one()
        assert rec.customer.name == "Pedro"


async def test_mark_credit_sale_as_paid_removes_receivable(bus, panel_ctx, session_factory):
    ref = await _sale(bus, panel_ctx, customer_name="Moisés", payment_status=PaymentStatus.CREDIT)
    await bus.dispatch(EditRecord(entity_type=EntityType.SALE, entity_id=ref.entity_id,
                                  changes=RecordChanges(payment_status=PaymentStatus.PAID)), panel_ctx)
    async with session_factory() as s:
        assert (await s.execute(select(Receivable).where(Receivable.deleted_at.is_(None)))).scalar_one_or_none() is None


async def test_shrinking_below_payments_turns_surplus_into_credit(bus, panel_ctx):
    ref = await _sale(bus, panel_ctx, customer_name="Moisés", payment_status=PaymentStatus.CREDIT)  # $24
    await bus.dispatch(RecordPayment(customer_name="Moisés", amount=Decimal(20), currency=Currency.USD), panel_ctx)
    res = await bus.dispatch(EditRecord(entity_type=EntityType.SALE, entity_id=ref.entity_id,
                                        changes=RecordChanges(quantity=Decimal(1))), panel_ctx)  # $12 < $20 abonado
    assert res.data["customer_balance"]["owed"] == {} and res.data["customer_balance"]["credit_in_favor"] == {"USD": "8.0000"}
    deleted = await bus.dispatch(DeleteRecord(entity_type=EntityType.SALE, entity_id=ref.entity_id), panel_ctx)
    assert deleted.data["customer_balance"]["credit_in_favor"] == {"USD": "20.0000"}


async def test_moving_credit_sale_with_payments_keeps_payments_as_credit(bus, panel_ctx):
    ref = await _sale(bus, panel_ctx, customer_name="Moisés", payment_status=PaymentStatus.CREDIT)  # $24
    await bus.dispatch(RecordPayment(customer_name="Moisés", amount=Decimal(5), currency=Currency.USD), panel_ctx)
    res = await bus.dispatch(EditRecord(entity_type=EntityType.SALE, entity_id=ref.entity_id,
                                        changes=RecordChanges(customer_name="Pedro")), panel_ctx)
    assert res.data["customer_balance"]["owed"] == {"USD": "24.0000"}
    assert res.data["previous_customer_balance"]["credit_in_favor"] == {"USD": "5.0000"}


async def test_delete_always_confirms_for_agent_and_soft_deletes(bus, panel_ctx, agent_ctx, session_factory):
    res = await bus.dispatch(RecordExpense(description="café", amount=Decimal(2), currency=Currency.USD), panel_ctx)
    ref = res.affected[0]
    cmd = DeleteRecord(entity_type=EntityType.EXPENSE, entity_id=ref.entity_id, expected_version=ref.version)
    pending = await bus.dispatch(cmd, agent_ctx)
    assert pending.needs_confirmation and "café" in pending.confirmation_reason
    async with session_factory() as s:
        assert (await s.get(Expense, ref.entity_id)).deleted_at is None
    await bus.dispatch(cmd, agent_ctx, confirmed=True)
    async with session_factory() as s:
        assert (await s.get(Expense, ref.entity_id)).deleted_at is not None


async def test_deleting_payment_reallocates_remaining_payments(bus, panel_ctx, session_factory):
    await bus.dispatch(CreateReceivable(customer_name="Ana", amount=Decimal(10), currency=Currency.USD), panel_ctx)
    p1 = (await bus.dispatch(RecordPayment(customer_name="Ana", amount=Decimal(4), currency=Currency.USD), panel_ctx)).affected[0]
    await bus.dispatch(RecordPayment(customer_name="Ana", amount=Decimal(3), currency=Currency.USD), panel_ctx)
    res = await bus.dispatch(DeleteRecord(entity_type=EntityType.PAYMENT, entity_id=p1.entity_id), panel_ctx)
    assert res.data["customer_balance"]["owed"] == {"USD": "7.0000"}


async def test_edit_rejects_fields_that_do_not_apply(bus, panel_ctx):
    res = await bus.dispatch(RecordExpense(description="luz", amount=Decimal(5), currency=Currency.USD), panel_ctx)
    with pytest.raises(ValidationFailed):
        await bus.dispatch(EditRecord(entity_type=EntityType.EXPENSE, entity_id=res.affected[0].entity_id,
                                      changes=RecordChanges(quantity=Decimal(2))), panel_ctx)


async def test_edit_payment_amount_reallocates(bus, panel_ctx):
    await bus.dispatch(CreateReceivable(customer_name="Luis", amount=Decimal(10), currency=Currency.USD), panel_ctx)
    p = (await bus.dispatch(RecordPayment(customer_name="Luis", amount=Decimal(4), currency=Currency.USD), panel_ctx)).affected[0]
    res = await bus.dispatch(EditRecord(entity_type=EntityType.PAYMENT, entity_id=p.entity_id,
                                        changes=RecordChanges(amount=Decimal(6))), panel_ctx)
    assert res.data["customer_balance"]["owed"] == {"USD": "4.0000"}


async def test_close_cash_summarizes_day_and_is_idempotent(bus, panel_ctx, agent_ctx, session_factory):
    await _sale(bus, panel_ctx, payment_method=PaymentMethod.CASH)  # +$24
    await _sale(bus, panel_ctx, customer_name="Moisés", payment_status=PaymentStatus.PARTIAL, amount_paid=Decimal(4),
                payment_method=PaymentMethod.MOBILE_PAYMENT)  # +$4 cobrado, $20 fiado
    await bus.dispatch(RecordExpense(description="almuerzo", amount=Decimal(5), currency=Currency.USD,
                                     payment_method=PaymentMethod.CASH), panel_ctx)
    first = await bus.dispatch(CloseCash(), agent_ctx)
    summary = first.data["summary"]
    assert summary["sales"]["count"] == 2
    assert summary["raw"]["cash_in"] == "28.0000" and summary["raw"]["net_cash"] == "23.0000"
    assert summary["raw"]["new_credit"] == "20.0000"
    assert summary["by_payment_method_net"]["Efectivo"] == {"USD": "$19,00"}  # método y moneda van por separado
    again = await bus.dispatch(CloseCash(), agent_ctx)
    assert again.data["already_closed_before"] is True


async def test_custom_rate_sanity_check_asks_then_allows(bus, panel_ctx, agent_ctx, session_factory):
    # BCV 800 → 90 parece un error de tipeo: se pide confirmación a cualquier actor (panel y agente), no se rechaza.
    for ctx in (panel_ctx, agent_ctx):
        res = await bus.dispatch(SetRate(rate=Decimal(90), purpose=RateScope.CHARGE), ctx)
        assert res.needs_confirmation and "¿Seguro" in res.confirmation_reason
    async with session_factory() as s:
        assert (await s.execute(select(CustomRate))).first() is None
    res = await bus.dispatch(SetRate(rate=Decimal(90), purpose=RateScope.CHARGE), panel_ctx, confirmed=True)
    assert res.status == "ok"
    assert (await bus.dispatch(SetRate(rate=Decimal(950), purpose=RateScope.CHARGE), panel_ctx)).status == "ok"  # dentro del rango: sin preguntar


async def test_no_special_rate_per_movement(bus, panel_ctx):
    """Una operación no trae su propia tasa: todo se registra con la tasa para cobrar del negocio."""
    with pytest.raises(ValueError):
        RecordExpense(description="flete", amount=Decimal(5000), currency=Currency.VES, rate_override=Decimal(900))


async def test_payment_rows_keep_frozen_legs(bus, panel_ctx, session_factory):
    await bus.dispatch(CreateReceivable(customer_name="Ana", amount=Decimal(10), currency=Currency.USD), panel_ctx)
    await bus.dispatch(RecordPayment(customer_name="Ana", amount=Decimal(1800), currency=Currency.VES), panel_ctx)
    async with session_factory() as s:
        p = (await s.execute(select(CustomerPayment))).scalar_one()
        assert p.amount_usd_market == Decimal("2.0000") and p.amount_usd_official == Decimal("2.2500")


async def test_country_cannot_change_after_onboarding(bus, panel_ctx):
    from app.application.commands.business import UpdateBusiness

    with pytest.raises(ValidationFailed):  # los montos ya quedaron congelados en la moneda local
        await bus.dispatch(UpdateBusiness(country_code="CO"), panel_ctx)


async def test_credit_in_favor_is_applied_to_new_receivable(bus, panel_ctx):
    await bus.dispatch(CreateReceivable(customer_name="Ana", amount=Decimal(10), currency=Currency.USD), panel_ctx)
    paid = await bus.dispatch(RecordPayment(customer_name="Ana", amount=Decimal(15), currency=Currency.USD), panel_ctx)
    assert paid.data["customer_balance"]["credit_in_favor"] == {"USD": "5.0000"}
    res = await bus.dispatch(CreateReceivable(customer_name="Ana", amount=Decimal(10), currency=Currency.USD), panel_ctx)
    assert res.data["credit_applied"] == "$5,00"
    assert res.data["customer_balance"]["owed"] == {"USD": "5.0000"} and res.data["customer_balance"]["credit_in_favor"] == {}


async def test_credit_in_favor_is_applied_to_credit_sale(bus, panel_ctx):
    await bus.dispatch(CreateReceivable(customer_name="Ana", amount=Decimal(10), currency=Currency.USD), panel_ctx)
    await bus.dispatch(RecordPayment(customer_name="Ana", amount=Decimal(30), currency=Currency.USD), panel_ctx)
    res = await bus.dispatch(RecordSale(items=[SaleItemInput(product="cadena 428", quantity=Decimal(1), unit_price=Decimal(12),
                                                             currency=Currency.USD)],
                                        customer_name="Ana", payment_status=PaymentStatus.CREDIT), panel_ctx)
    rec = res.data["receivable"]
    assert rec["credit_applied"] == "$12,00"  # tenía $20 a favor: cubre toda la venta
    assert rec["customer_balance"]["owed"] == {} and rec["customer_balance"]["credit_in_favor"] == {"USD": "8.0000"}


async def test_new_receivable_without_credit_changes_nothing(bus, panel_ctx):
    res = await bus.dispatch(CreateReceivable(customer_name="Ana", amount=Decimal(10), currency=Currency.USD), panel_ctx)
    assert res.data["credit_applied"] is None and res.data["customer_balance"]["owed"] == {"USD": "10.0000"}


async def test_delete_product_confirms_first_and_keeps_the_sales_history(bus, agent_ctx, session_factory):
    from app.application.commands.catalog import DeleteProduct, SetProductPrice

    await bus.dispatch(SetProductPrice(product_name="Bujías", price=Decimal(3), currency=Currency.USD), agent_ctx)
    await bus.dispatch(RecordSale(items=[SaleItemInput(product="Bujías", quantity=Decimal(2))],
                                  payment_method=PaymentMethod.CASH), agent_ctx)

    cmd = DeleteProduct(product_name="bujias")
    preview = await bus.dispatch(cmd, agent_ctx)
    assert preview.needs_confirmation and preview.data["product"] == "Bujías" and preview.data["movements"] == 1
    async with session_factory() as s:  # rollback real: sigue en el catálogo
        assert (await s.execute(select(Product).where(Product.deleted_at.is_(None)))).scalar_one().name == "Bujías"

    res = await bus.dispatch(cmd, agent_ctx, confirmed=True)
    assert res.status == "ok"
    async with session_factory() as s:
        product = (await s.execute(select(Product))).scalar_one()
        assert product.deleted_at is not None and product.is_active is False
        sale_item = (await s.execute(select(SaleItem))).scalar_one()
        assert sale_item.product_id == product.id and sale_item.description == "Bujías"


async def test_delete_product_rejects_unknown_names_and_frees_the_name(bus, agent_ctx):
    from app.application.commands.catalog import DeleteProduct, SetProductPrice

    with pytest.raises(NotFound):
        await bus.dispatch(DeleteProduct(product_name="bujías"), agent_ctx, confirmed=True)

    await bus.dispatch(SetProductPrice(product_name="Bujías", price=Decimal(3), currency=Currency.USD), agent_ctx)
    await bus.dispatch(DeleteProduct(product_name="Bujías"), agent_ctx, confirmed=True)
    # el índice único ignora las borradas: el mismo nombre se puede volver a crear
    again = await bus.dispatch(SetProductPrice(product_name="Bujías", price=Decimal(4), currency=Currency.USD), agent_ctx)
    assert again.data["created"] is True and again.data["previous_price"] is None
