from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.application.conversation import conversation_scope
from app.db.models import BrokerAttachment, BrokerMovement, Customer, Receivable
from tests.agent.test_brokerage_vertical import _receipt_image, _set_brokerage
from tests.agent.test_pipeline import PHONE, send


@pytest.mark.parametrize("lane", ["fast", "agent"])
@pytest.mark.parametrize("money", [{"operation_amount_bs": "80000", "text_money_sources": {'operation_amount_bs': "80000"}}, {"exchange_rate": "800", "text_money_sources": {'exchange_rate': "800"}}])
async def test_sale_waits_and_resumes_original_operation(env, script, business, session_factory, lane, money):
    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = lane
    script.steps = [{"tool": "record_broker_operation", "args": {
        "operation_type": "sell", "operation_amount": "100", "description": "Lote de ayer",
    }}]
    first = await send(container, "Vendí 100, concepto Lote de ayer")
    assert first.replies[0]["text"] == "Dime la cantidad de bolívares o envíame el capture del pago."
    scope = conversation_scope(business.id, PHONE)
    draft = await container.store.get_broker_money_draft(scope)
    assert draft is not None and draft.command_payload["description"] == "Lote de ayer"
    assert await container.store.peek_pending(scope) is None
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 0
    script.steps = [{"tool": "resume_broker_operation", "args": money}]
    out = await send(container, "80000" if "operation_amount_bs" in money else "La tasa fue 800")
    assert "Venta de divisas" in out.replies[0]["text"]
    async with session_factory() as session:
        row = (await session.scalars(select(BrokerMovement))).one()
        assert row.operation_type == "sell" and row.operation_amount == 100
        assert row.operation_amount_bs == 80000 and row.exchange_rate == 800
        assert str(row.source_message_id) == draft.command_payload["origin_message_id"]
        assert row.business_date.isoformat() == draft.command_payload["business_date"]
        assert row.description == "Lote de ayer" and row.balance_delta == 1
    assert await container.store.get_broker_money_draft(scope) is None


@pytest.mark.parametrize("confidence", ["clear", "ambiguous", "unreadable"])
async def test_image_only_can_resume_unrecorded_sale(env, script, business, session_factory, confidence):
    container, console = env
    await _set_brokerage(session_factory)
    async def download_media(_url):
        return _receipt_image(), "image/png"
    console.download_media = download_media
    script.steps = [{"tool": "record_broker_operation", "args": {"operation_type": "sell", "operation_amount": "100"}}]
    await send(container, "Vendí 100")
    script.steps = [{"tool": "resume_broker_operation", "args": {"extraction": {
        "amount_bs": None if confidence == "unreadable" else "80000", "confidence": confidence,
    }}}]
    if confidence != "clear":
        script.steps.append({"text": "¿Son 80.000 o 90.000 Bs los del comprobante?"})
    calls_before_image = script.calls
    out = await send(container, "", kind="image", media_url="https://media.invalid/receipt", media_mime_type="image/png")
    assert out.status == "ok" and "indica cuál operación" not in out.replies[0]["text"]
    async with session_factory() as session:
        rows = (await session.scalars(select(BrokerMovement))).all()
        attachment = (await session.scalars(select(BrokerAttachment))).one()
        if confidence == "clear":
            assert len(rows) == 1 and rows[0].operation_amount_bs == Decimal("80000")
            assert rows[0].monetary_provenance["operation_amount_bs"]["source"] == "attachment"
            assert attachment.broker_movement_id == rows[0].id
        else:
            assert rows == [] and attachment.broker_movement_id is None
            assert "capture más claro" in out.replies[0]["text"]
            assert script.calls == calls_before_image + 1
            assert "90.000" not in out.replies[0]["text"]


async def test_pending_shortcut_preserves_customer_until_bs_and_classification(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_pending_broker_purchase", "args": {"customer_name": "Pedro", "amount": "100"}}]
    await send(container, "Pedro 100")
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Customer)) == 0
        assert await session.scalar(select(func.count()).select_from(Receivable)) == 0
    script.steps = [{"tool": "resume_broker_operation", "args": {"exchange_rate": "800", "text_money_sources": {'exchange_rate': "800"}}}]
    choice = await send(container, "A tasa 800")
    assert "pendiente o liquidada" in choice.replies[0]["text"]
    choice = await send(container, "Pendiente")
    assert "tipo de cliente" in choice.replies[0]["text"]
    await send(container, "Personal")
    async with session_factory() as session:
        row = (await session.scalars(select(BrokerMovement))).one()
        debt = (await session.scalars(select(Receivable))).one()
        assert row.operation_amount_bs == 80000 and row.balance_delta == -99
        assert debt.amount == 100


async def test_cancel_and_expiry_do_not_create_movements(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    scope = conversation_scope(business.id, PHONE)
    script.steps = [{"tool": "record_broker_account_purchase", "args": {"amount": "40"}}]
    await send(container, "Saqué 40")
    assert await container.store.get_broker_money_draft(scope) is not None
    assert 0 < await container.redis.ttl(f"{scope}:broker_money_draft") <= 600
    script.steps = [{"tool": "cancel_broker_operation"}]
    await send(container, "Cancela esa compra")
    assert await container.store.get_broker_money_draft(scope) is None
    script.steps = [{"tool": "record_broker_account_purchase", "args": {"amount": "40"}}]
    await send(container, "Saqué 40")
    await container.redis.expire(f"{scope}:broker_money_draft", 0)
    script.steps = [{"tool": "resume_broker_operation", "args": {"operation_amount_bs": "32000", "text_money_sources": {'operation_amount_bs': "32000"}}},
                    {"tool": "need_more_info", "args": {"question": "Indica la compra completa."}}]
    await send(container, "32000")
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 0


async def test_several_drafts_require_selection_and_preserve_the_other(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    for amount in ("100", "200"):
        script.steps = [{"tool": "record_broker_operation", "args": {"operation_type": "sell", "operation_amount": amount}}]
        await send(container, f"Vendí {amount}")
    scope = conversation_scope(business.id, PHONE)
    draft = await container.store.get_broker_money_draft(scope)
    assert len(draft.operations()) == 2
    script.steps = [{"tool": "resume_broker_operation", "args": {"exchange_rate": "800", "text_money_sources": {'exchange_rate': "800"}}},
                    {"tool": "need_more_info", "args": {"question": "¿A cuál venta corresponde la tasa?"}}]
    out = await send(container, "La tasa fue 800")
    assert "cuál venta" in out.replies[0]["text"]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 0
    script.steps = [{"tool": "resume_broker_operation", "args": {"operation_number": 2, "exchange_rate": "800", "text_money_sources": {'exchange_rate': "800"}}}]
    await send(container, "La venta de 200")
    async with session_factory() as session:
        row = (await session.scalars(select(BrokerMovement))).one()
        assert row.operation_amount == 200 and row.operation_amount_bs == 160000
    remaining = await container.store.get_broker_money_draft(scope)
    assert len(remaining.operations()) == 1 and remaining.command_payload["operation_amount"] == "100"


@pytest.mark.parametrize("settlement", ["settled", "pending"])
async def test_sale_intent_and_commission_survive_missing_bs_and_receipt(env, script, business, session_factory, settlement):
    container, console = env
    await _set_brokerage(session_factory)

    async def download_media(_url):
        return _receipt_image(), "image/png"

    console.download_media = download_media
    script.steps = [{"tool": "record_broker_operation", "args": {
        "operation_type": "sell", "settlement": settlement, "operation_amount": "900",
        "customer_name": "Carlos", "customer_classification": "personal",
    }}]
    await send(container, "Venta 900 a Carlos personal" + (" pendiente" if settlement == "pending" else ""))
    scope = conversation_scope(business.id, PHONE)
    draft = await container.store.get_broker_money_draft(scope)
    assert draft.command_payload["operation_type"] == "sell"
    assert draft.command_payload["settlement"] == settlement
    script.steps = [{"tool": "resume_broker_operation", "args": {"extraction": {
        "amount_bs": "990000", "confidence": "clear",
    }}}]
    out = await send(container, "", kind="image", media_url="https://media.invalid/receipt", media_mime_type="image/png")
    assert "Comprobante guardado" in out.replies[0]["text"]
    assert "Variación del balance: +$9,00" in out.replies[0]["text"]
    assert "Salida por venta" not in out.replies[0]["text"]
    async with session_factory() as db:
        row = (await db.scalars(select(BrokerMovement))).one()
        assert row.settlement_status == settlement and row.sales_balance_rule == "commission_only"
        assert row.operation_amount == 900 and row.operation_amount_bs == 990000 and row.exchange_rate == 1100
        assert row.commission == row.balance_delta == 9
        assert row.customer.classification == "personal"
        assert await db.scalar(select(func.count()).select_from(Receivable)) == 0
