"""A settlement answer must survive customer identification without rebuilding the purchase."""

from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import func, select

from app.application.conversation import conversation_scope
from app.db.models import (
    BrokerAttachment,
    BrokerDraftExecution,
    BrokerMovement,
    Business,
    BusinessMember,
    Customer,
    Receivable,
)
from app.domain.enums import MemberRole
from tests.agent.test_brokerage_vertical import _receipt_image, _set_brokerage
from tests.agent.test_pipeline import PHONE, send


@pytest.mark.parametrize("role", [MemberRole.OWNER, MemberRole.BROKER])
@pytest.mark.parametrize("settlement", ["pending", "settled"])
@pytest.mark.parametrize("entrypoint", ["record_broker_purchase", "record_pending_broker_purchase", "record_broker_operation"])
@pytest.mark.parametrize("money_source", ["text", "receipt"])
@pytest.mark.parametrize("customer_selection", ["existing", "new_global"])
async def test_purchase_keeps_settlement_money_and_identity_through_customer_choice(
    env, script, business, session_factory, role, settlement, entrypoint, money_source, customer_selection,
):
    container, console = env
    await _set_brokerage(session_factory, role=role)
    async with session_factory() as session, session.begin():
        customer = Customer(business_id=business.id, name="Norma", normalized_name="norma", classification="global")
        session.add(customer)
        await session.flush()
        existing_customer_id = customer.id

    args = {"customer_name": "Nora", "received_amount": "105"}
    if entrypoint == "record_broker_operation":
        args.update(operation_type="buy", operation_amount="100")
    else:
        args["amount"] = "100"
    if money_source == "receipt":
        async def download(_url):
            return _receipt_image(), "image/png"

        console.download_media = download
        args.update(monetary_from_attachment=True, extraction={"amount_bs": "80000", "confidence": "clear"})
        send_options = {"kind": "image", "media_url": "https://media.invalid/synthetic", "media_mime_type": "image/png"}
        message = "Nora 100$, recibí 105"
    else:
        args.update(exchange_rate="800", text_money_sources={"principal_usd": "100", "exchange_rate": "800"})
        send_options = {}
        message = "Nora 100, recibí 105, tasa 800"
    script.steps = [{"tool": entrypoint, "args": args}]
    proposed = await send(container, message, **send_options)
    assert "pendiente o liquidada" in proposed.replies[0]["text"]
    scope = conversation_scope(business.id, PHONE)
    original_choice = await container.store.peek_choice(scope)
    frozen_payload = deepcopy(original_choice.options[0].command_payload)
    model_calls_before = script.calls
    answer = "Pendiente" if settlement == "pending" else "Liquidada"
    selected = next(option for option in proposed.replies[0]["choices"] if option["title"] == answer)
    ambiguous = await send(container, answer, button_id=selected["id"])
    assert "¿Quién es «Nora»?" in ambiguous.replies[0]["text"]
    assert {option["title"] for option in ambiguous.replies[0]["choices"]} == {"Norma", "Es un cliente nuevo"}
    assert script.calls == model_calls_before
    choice = await container.store.peek_choice(scope)
    for option in choice.options:
        for field in ("amount", "received_amount", "operation_amount_bs", "exchange_rate", "money_evidence",
                      "attachment_upload_id", "origin_message_id", "business_date", "draft_operation_id",
                      "draft_origin_command_kind", "purchase_settlement_confirmed", "broker_id", "broker_name"):
            assert option.command_payload[field] == frozen_payload[field]
        assert option.command_payload["settlement"] == settlement
    window = await container.store.get_window(scope)
    assert window[-1].user == answer
    assert window[-1].assistant == ""  # system outcomes are not fabricated model speech
    assert any("¿Quién es «Nora»?" in action for action in window[-1].actions)
    assert await container.store.get_broker_money_draft(scope) is None
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 0
        assert await session.scalar(select(func.count()).select_from(Receivable)) == 0

    if customer_selection == "existing":
        completed = await send(container, "Norma")
    else:
        classification = await send(container, "Es un cliente nuevo")
        assert "tipo de cliente" in classification.replies[0]["text"]
        classification_choice = await container.store.peek_choice(scope)
        assert all(option.command_payload.get("settlement", "pending" if option.command_kind == "record_pending_broker_purchase" else "settled") == settlement
                   and option.command_payload["purchase_settlement_confirmed"]
                   and option.command_payload["draft_operation_id"] == frozen_payload["draft_operation_id"]
                   for option in classification_choice.options)
        completed = await send(container, "Global")
    assert "registrada" in completed.replies[0]["text"]
    assert script.calls == model_calls_before
    assert await container.store.peek_choice(scope) is None
    async with session_factory() as session:
        row = (await session.scalars(select(BrokerMovement))).one()
        customer = await session.get(Customer, row.customer_id)
        assert row.customer_id == existing_customer_id if customer_selection == "existing" else customer.name == "Nora"
        assert customer.classification == "global"
        assert row.operation_amount == 100 and row.operation_amount_bs == 80000 and row.exchange_rate == 800
        assert row.received_amount == 105
        assert row.settlement_status == settlement
        assert row.source_message_id == UUID(frozen_payload["origin_message_id"])
        assert row.business_date.isoformat() == frozen_payload["business_date"]
        assert row.commission == (0 if role == MemberRole.OWNER else 1)
        assert row.balance_delta == (0 if role == MemberRole.OWNER else 6)
        assert await session.scalar(select(func.count()).select_from(Receivable)) == (1 if settlement == "pending" else 0)
        if money_source == "receipt":
            attachment = (await session.scalars(select(BrokerAttachment))).one()
            assert attachment.broker_movement_id == row.id
            assert str(attachment.id) == frozen_payload["attachment_upload_id"]
        assert await session.scalar(select(func.count()).select_from(BrokerDraftExecution)) == 1

    # A late press on the original settlement must recover the same committed result.
    await send(container, answer, button_id=selected["id"])
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 1
        assert await session.scalar(select(func.count()).select_from(BrokerDraftExecution)) == 1


async def test_receipt_then_settlement_then_new_personal_customer_preserves_scope(env, script, business, session_factory):
    container, console = env
    await _set_brokerage(session_factory)
    async with session_factory() as session, session.begin():
        actor = (await session.scalars(select(BusinessMember))).one()
        actor_id = actor.id
        other_broker = BusinessMember(business_id=business.id, phone="584140000002", name="Otro corredor", role="broker")
        other_business = Business(name="Otro negocio")
        session.add_all([other_broker, other_business])
        await session.flush()
        session.add_all([
            Customer(business_id=business.id, name="Norma", normalized_name="norma", classification="global"),
            Customer(business_id=business.id, name="Nora privada", normalized_name="nora privada",
                     classification="personal", owner_broker_member_id=other_broker.id),
            Customer(business_id=other_business.id, name="Nora ajena", normalized_name="nora ajena", classification="global"),
        ])
    script.steps = [{"tool": "record_broker_purchase", "args": {"amount": "100", "customer_name": "Nora"}}]
    await send(container, "Nora 100$")
    scope = conversation_scope(business.id, PHONE)
    original_draft = await container.store.get_broker_money_draft(scope)

    async def download(_url):
        return _receipt_image(), "image/png"

    console.download_media = download
    script.steps = [{"tool": "resume_broker_operation", "args": {
        "extraction": {"amount_bs": "80000", "confidence": "clear"}}}]
    await send(container, "", kind="image", media_url="https://media.invalid/synthetic")
    settlement_choice = await container.store.peek_choice(scope)
    settlement_choice.target_is_quoted = True
    await container.store.set_choice(scope, settlement_choice)
    model_calls_before = script.calls
    result = await send(container, "Pendiente")
    assert {choice["title"] for choice in result.replies[0]["choices"]} == {"Norma", "Es un cliente nuevo"}
    assert (await container.store.peek_choice(scope)).target_is_quoted is True
    await send(container, "Es un cliente nuevo")
    classification_choice = await container.store.peek_choice(scope)
    assert classification_choice.target_is_quoted is True
    result = await send(container, "Personal")
    assert "registrada" in result.replies[0]["text"]
    assert script.calls == model_calls_before
    async with session_factory() as session:
        row = (await session.scalars(select(BrokerMovement))).one()
        customer = await session.get(Customer, row.customer_id)
        assert customer.name == "Nora" and customer.classification == "personal"
        assert customer.owner_broker_member_id == row.broker_member_id == actor_id
        assert row.portfolio_broker_member_id == row.recorded_by_member_id == actor_id
        assert row.settlement_status == "pending" and row.balance_delta == -99 and row.commission == 1
        assert row.operation_amount == 100 and row.operation_amount_bs == 80000 and row.exchange_rate == 800
        assert row.source_message_id == UUID(original_draft.command_payload["origin_message_id"])
        assert (await session.scalars(select(BrokerAttachment))).one().broker_movement_id == row.id


async def test_intervening_text_sees_customer_question_and_original_button_still_completes(
    env, script, business, session_factory,
):
    from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart

    container, _ = env
    await _set_brokerage(session_factory)
    async with session_factory() as session, session.begin():
        session.add(Customer(business_id=business.id, name="Norma", normalized_name="norma", classification="global"))
    script.steps = [{"tool": "record_broker_purchase", "args": {
        "amount": "100", "customer_name": "Nora", "exchange_rate": "800",
        "text_money_sources": {"principal_usd": "100", "exchange_rate": "800"}}}]
    await send(container, "Nora 100 a tasa 800")
    ambiguous = await send(container, "Liquidada")
    assert "¿Quién es «Nora»?" in ambiguous.replies[0]["text"]
    selected = next(choice for choice in ambiguous.replies[0]["choices"] if choice["title"] == "Norma")

    def inspect_history(messages, info):
        history_notes = [part.content for message in messages for part in message.parts
                         if isinstance(part, UserPromptPart) and isinstance(part.content, str)]
        assert any("Respuesta del sistema: ¿Quién es «Nora»?" in note for note in history_notes)
        assert "¿Quién es «Nora»?" in info.instructions
        return ModelResponse(parts=[TextPart("Te pregunté a qué cliente corresponde la compra.")])

    script.steps = [inspect_history]
    await send(container, "¿Qué me preguntaste?")
    model_calls_before = script.calls
    completed = await send(container, "Norma", button_id=selected["id"])
    assert "registrada" in completed.replies[0]["text"]
    await send(container, "Norma", button_id=selected["id"])
    assert script.calls == model_calls_before
    async with session_factory() as session:
        row = (await session.scalars(select(BrokerMovement))).one()
        assert row.settlement_status == "settled" and row.operation_amount_bs == 80000
        assert await session.scalar(select(func.count()).select_from(BrokerDraftExecution)) == 1
