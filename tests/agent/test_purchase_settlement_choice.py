"""Purchase settlement must be chosen by the user, never inferred by a model."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.application.conversation import conversation_scope
from app.db.models import BrokerAttachment, BrokerMovement, Customer, Receivable
from app.domain.enums import MemberRole
from tests.agent.test_brokerage_vertical import _receipt_image, _set_brokerage
from tests.agent.test_pipeline import PHONE, send


@pytest.mark.parametrize("role", [MemberRole.OWNER, MemberRole.BROKER])
@pytest.mark.parametrize("tool", ["record_broker_purchase", "record_pending_broker_purchase", "record_broker_operation"])
@pytest.mark.parametrize("answer", ["Pendiente", "Liquidada"])
async def test_every_purchase_path_requires_user_choice(env, script, business, session_factory, role, tool, answer):
    container, console = env
    await _set_brokerage(session_factory, role=role)
    async with session_factory() as session, session.begin():
        session.add(Customer(business_id=business.id, name="Karol", normalized_name="karol", classification="global"))

    async def download_media(_url):
        return _receipt_image(), "image/png"

    console.download_media = download_media
    args = {"customer_name": "Karol", "monetary_from_attachment": True,
            "extraction": {"amount_bs": "475000,00", "confidence": "clear"}}
    if tool == "record_broker_operation":
        args.update(operation_type="buy", operation_amount="500", settlement="settled")
    else:
        args["amount"] = "500"
        if tool == "record_broker_purchase":
            args["settlement"] = "settled"
    script.steps = [{"tool": tool, "args": args}]
    proposed = await send(container, "Karol 500$", kind="image", media_url="https://media.invalid/receipt", media_mime_type="image/png")
    assert "¿La compra está pendiente o liquidada?" in proposed.replies[0]["text"]
    assert {choice["title"] for choice in proposed.replies[0]["choices"]} == {"Pendiente", "Liquidada"}
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 0
        assert await session.scalar(select(func.count()).select_from(Receivable)) == 0
    model_calls = script.calls
    completed = await send(container, answer)
    assert script.calls == model_calls
    assert "registrada" in completed.replies[0]["text"]
    async with session_factory() as session:
        row = (await session.scalars(select(BrokerMovement))).one()
        assert row.operation_amount == 500 and row.operation_amount_bs == 475000 and row.exchange_rate == 950
        assert row.settlement_status == ("pending" if answer == "Pendiente" else "settled")
        assert row.commission == row.balance_delta == (0 if role == MemberRole.OWNER else 5)
        assert (row.related_receivable_id is not None) == (answer == "Pendiente")
        attachment = (await session.scalars(select(BrokerAttachment))).one()
        assert attachment.broker_movement_id == row.id


async def test_money_then_settlement_then_classification_preserves_draft(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_pending_broker_purchase", "args": {"amount": "100", "customer_name": "Juan"}}]
    await send(container, "Juan 100 pendiente")
    scope = conversation_scope(business.id, PHONE)
    original = await container.store.get_broker_money_draft(scope)
    script.steps = [{"tool": "resume_broker_operation", "args": {
        "exchange_rate": "800", "text_money_sources": {"exchange_rate": "800"}}}]
    proposed = await send(container, "Tasa 800")
    assert "pendiente o liquidada" in proposed.replies[0]["text"]
    classified = await send(container, "1")
    assert "tipo de cliente" in classified.replies[0]["text"]
    await send(container, "Personal")
    async with session_factory() as session:
        row = (await session.scalars(select(BrokerMovement))).one()
        assert row.source_message_id.hex == original.command_payload["origin_message_id"].replace("-", "")
        assert row.operation_amount_bs == 80000 and row.balance_delta == Decimal("-99")
        assert await session.scalar(select(func.count()).select_from(Receivable)) == 1
    assert await container.store.get_broker_money_draft(scope) is None


async def test_pending_without_customer_keeps_money_and_user_choice(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_purchase", "args": {
        "amount": "100", "exchange_rate": "800", "received_amount": "105",
        "text_money_sources": {"principal_usd": "100", "exchange_rate": "800"}}}]
    proposed = await send(container, "Compré 100, recibí 105, a tasa 800")
    assert "pendiente o liquidada" in proposed.replies[0]["text"]
    missing_customer = await send(container, "Pendiente")
    assert "Qué cliente" in missing_customer.replies[0]["text"]
    script.steps = [{"tool": "resume_broker_operation", "args": {"customer_name": "Juan"}}]
    classification = await send(container, "Juan")
    assert "tipo de cliente" in classification.replies[0]["text"]
    await send(container, "Personal")
    async with session_factory() as session:
        row = (await session.scalars(select(BrokerMovement))).one()
        assert row.operation_amount == 100 and row.operation_amount_bs == 80000
        assert row.received_amount == 105 and row.excess == 5 and row.balance_delta == -94
        assert row.settlement_status == "pending"
    assert await container.store.get_broker_money_draft(conversation_scope(business.id, PHONE)) is None


async def test_legacy_frozen_choice_cannot_bypass_settlement_question(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_purchase", "args": {
        "amount": "100", "exchange_rate": "800",
        "text_money_sources": {"principal_usd": "100", "exchange_rate": "800"}}}]
    await send(container, "Compré 100 a tasa 800")
    scope = conversation_scope(business.id, PHONE)
    choice = await container.store.pop_choice(scope)
    for option in choice.options:
        option.command_payload.pop("purchase_settlement_confirmed")
        option.command_payload.pop("draft_origin_command_kind")
    await container.store.set_choice(scope, choice)
    repeated = await send(container, "Liquidada")
    assert "pendiente o liquidada" in repeated.replies[0]["text"]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 0
    selected = next(item for item in repeated.replies[0]["choices"] if item["title"] == "Liquidada")
    await send(container, "Liquidada", button_id=selected["id"])
    await send(container, "Liquidada", button_id=selected["id"])
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 1
