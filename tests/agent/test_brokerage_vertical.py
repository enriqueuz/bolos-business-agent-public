"""Comportamiento conversacional específico de la vertical de corredores."""

import io
from decimal import Decimal

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.db.models import (
    BrokerAttachment,
    BrokerMovement,
    Business,
    BusinessMember,
    Customer,
    CustomerPayment,
    LlmCall,
    Receivable,
    ToolCall,
)
from app.domain.enums import BusinessVertical, MemberRole
from tests.agent.harness import handle_in_quoted_section
from tests.agent.test_pipeline import send


def _receipt_image() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (720, 480), "white").save(out, "PNG")
    return out.getvalue()


async def _set_brokerage(session_factory, *, role: MemberRole = MemberRole.BROKER) -> None:
    async with session_factory() as session, session.begin():
        business = (await session.execute(select(Business))).scalar_one()
        business.vertical = BusinessVertical.BROKERAGE.value
        member = (await session.execute(select(BusinessMember))).scalar_one()
        member.role = role.value
        if role == MemberRole.BROKER:
            member.name = "Alex Example"
            member.abbreviation = "SB"


@pytest.mark.parametrize("lane", ["agent", "fast"])
async def test_broker_completes_global_customer_name_from_chat(env, script, business, session_factory, lane):
    container, _ = env
    await _set_brokerage(session_factory)
    async with session_factory() as session, session.begin():
        customer = Customer(business_id=business.id, name=None, normalized_name="", nickname="ALM", classification="global")
        session.add(customer)
        await session.flush()
        cid = customer.id
    script.lane = lane
    script.steps = [{"tool": "edit_customer_profile", "args": {
        "customer_name": "ALM", "name": "Ángel Leon Macas", "nickname": "ALM",
    }}, {"text": "Actualicé el nombre de ALM a Ángel Leon Macas."}]
    out = await send(container, "Su nombre completo es Ángel Leon Macas y su apodo es ALM")
    assert out.status == "ok"
    async with session_factory() as session:
        customer = await session.get(Customer, cid)
        assert customer.name == "Ángel Leon Macas" and customer.nickname == "ALM" and customer.version == 2
        calls = (await session.scalars(select(ToolCall).where(ToolCall.tool_name == "edit_customer_profile"))).all()
        assert len(calls) == 1 and calls[0].outcome == "ok"
        assert (await session.scalar(select(func.count()).select_from(BrokerMovement))) == 0


@pytest.mark.parametrize("lane", ["agent", "fast"])
async def test_physical_sale_uses_typed_rejection_without_ledger_write(
    env, script, business, session_factory, lane: str,
):
    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = lane
    script.steps = [{"tool": "reject_physical_sale"}]

    out = await send(container, "Vendí 2 bujías a 5 dólares cada una en efectivo")

    assert out.status == "ok" and out.lane == lane
    assert len(out.replies) == 1
    assert out.replies[0]["text"].startswith("No registré nada.")
    assert "no registra productos" in out.replies[0]["text"]
    assert script.calls == 1, "la pregunta semántica corresponde al modelo; el aviso cierra el turno"
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(BrokerMovement))).scalar_one() == 0
        calls = (await session.execute(select(ToolCall))).scalars().all()
        assert [(call.tool_name, call.outcome) for call in calls] == [("reject_physical_sale", "ok")]
        assert (await session.execute(select(func.count()).select_from(LlmCall))).scalar_one() > 0


async def test_complete_broker_operation_still_writes(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = "fast"
    script.steps = [{"tool": "record_broker_operation", "args": {
        "exchange_rate": "800",
        "operation_type": "sell", "operation_amount": "1000",
        "description": "operación",
    "text_money_sources": {"principal_usd": "1000", 'exchange_rate': "800"}}}]

    out = await send(container, "Vendí 1000 a tasa 800")

    assert out.status == "ok" and out.lane == "fast"
    assert "Venta de divisas" in out.replies[0]["text"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        assert movement.operation_amount == 1000
        assert movement.delivered_amount == 1000


async def test_broker_image_with_caption_creates_movement_and_attachment(
    env, script, business, session_factory,
):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    container, console = env
    await _set_brokerage(session_factory)

    async def download_media(_url: str):
        return _receipt_image(), "image/png"

    console.download_media = download_media
    script.lane = "fast"
    script.steps = [{"tool": "record_broker_operation", "args": {
        "exchange_rate": "800",
        "operation_type": "sell", "operation_amount": "100",
    "text_money_sources": {"principal_usd": "100", 'exchange_rate': "800"}}}]

    out = await send(
        container,
        "Vendí 100 a tasa 800",
        kind="image",
        media_url="https://media.invalid/receipt",
        media_mime_type="image/png",
    )

    assert out.status == "ok"
    assert "Comprobante guardado" in out.replies[0]["text"]
    receipt_id = out.replies[0]["provider_message_id"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        attachment = (await session.execute(select(BrokerAttachment))).scalar_one()
        assert attachment.broker_movement_id == movement.id

    def show(_messages, info):
        return ModelResponse(parts=[ToolCallPart(
            "show_broker_attachment",
            {"handle": handle_in_quoted_section(info)},
        )])

    script.steps = [show]
    shown = await send(container, "Muéstrame el comprobante", reply_to=receipt_id)
    assert shown.replies[0]["image"] is True
    assert console.sent[-1].image_url.startswith("data:image/png;base64,")
    assert "Venta" in console.sent[-1].text
    assert "data:image" not in repr(script.seen_tool_returns)


async def test_broker_image_without_caption_does_not_attach_to_unrelated_operation(
    env, script, business, session_factory,
):
    container, console = env
    await _set_brokerage(session_factory)

    async def download_media(_url: str):
        return _receipt_image(), "image/png"

    console.download_media = download_media
    alone = await send(
        container,
        None,
        kind="image",
        media_url="https://media.invalid/receipt",
        media_mime_type="image/png",
    )

    assert "no registré nada" in alone.replies[0]["text"]
    assert script.calls == 0
    script.steps = [{"tool": "record_broker_operation", "args": {
        "exchange_rate": "800",
        "operation_type": "sell", "operation_amount": "50",
    "text_money_sources": {"principal_usd": "50", 'exchange_rate': "800"}}}]
    created = await send(container, "Otra operación distinta: vendí 50 a tasa 800")
    assert "Comprobante guardado" not in created.replies[0]["text"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        attachment = (await session.execute(select(BrokerAttachment))).scalar_one()
        assert attachment.broker_movement_id is None
        assert movement.operation_amount == Decimal("50.00")


async def test_classification_choice_preserves_original_message_provenance(env, script, business, session_factory):
    from app.db.models import InboundMessage
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_pending_broker_purchase", "args": {"exchange_rate": "800", "customer_name": "Juan", "amount": "1000", "text_money_sources": {"principal_usd": "1000", 'exchange_rate': "800"}}}]
    proposed = await send(container, "1000 Juan a tasa 800")
    assert "pendiente o liquidada" in proposed.replies[0]["text"]
    proposed = await send(container, "Pendiente")
    choice = next(item for item in proposed.replies[0]["choices"] if item["title"] == "Global")
    await send(container, "Global", button_id=choice["id"])
    async with session_factory() as session:
        row = (await session.execute(select(BrokerMovement))).scalar_one()
        message = await session.get(InboundMessage, row.source_message_id)
        assert message.body == "1000 Juan a tasa 800"
        assert row.monetary_provenance["operation_amount"]["message_id"] == str(message.id)


async def test_conversational_money_correction_preserves_historical_business_date(env, script, business, session_factory):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_operation", "args": {
        "exchange_rate": "800",
        "operation_type": "sell", "operation_amount": "100", "business_date": "2026-10-01",
    "text_money_sources": {"principal_usd": "100", 'exchange_rate': "800"}}}]
    created = await send(container, "Vendí 100 el 1 de octubre de 2026 a tasa 800")
    def correction(_messages, info):
        return ModelResponse(parts=[ToolCallPart("edit_broker_movement", {
            "handle": handle_in_quoted_section(info), "changes": {"amount": "200"},
        })])
    script.steps = [correction, {"text": ""}]
    await send(container, "Eran 200", reply_to=created.replies[0]["provider_message_id"])
    async with session_factory() as session:
        row = (await session.execute(select(BrokerMovement))).scalar_one()
        assert row.operation_amount == Decimal("200")
        assert row.business_date.isoformat() == "2026-10-01"


async def test_broker_image_reply_extracts_and_completes_the_quoted_receipt(
    env, script, business, session_factory,
):
    container, console = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_operation", "args": {
        "exchange_rate": "800",
        "operation_type": "sell", "operation_amount": "75",
    "text_money_sources": {"principal_usd": "75", 'exchange_rate': "800"}}}]
    created = await send(container, "Vendí 75 a tasa 800")
    receipt_id = created.replies[0]["provider_message_id"]
    calls_before = script.calls
    script.steps = [{"tool": "complete_broker_receipt", "args": {"extraction": {"amount_bs": "60000", "confidence": "clear"}}}]

    async def download_media(_url: str):
        return _receipt_image(), "image/png"

    console.download_media = download_media
    attached = await send(
        container,
        None,
        kind="image",
        media_url="https://media.invalid/receipt",
        media_mime_type="image/png",
        reply_to=receipt_id,
    )

    assert script.calls == calls_before + 1
    assert "Comprobante guardado" in attached.replies[0]["text"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        attachment = (await session.execute(select(BrokerAttachment))).scalar_one()
        assert attachment.broker_movement_id == movement.id


async def test_sold_amount_alone_is_a_settled_sale(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = "fast"
    script.steps = [{"tool": "record_broker_operation", "args": {
        "exchange_rate": "800",
        "operation_type": "sell", "operation_amount": "200",
    "text_money_sources": {"principal_usd": "200", 'exchange_rate': "800"}}}]

    out = await send(container, "Vendí 200 a tasa 800")

    assert out.status == "ok" and "Venta de divisas" in out.replies[0]["text"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        assert movement.delivered_amount == 200
        assert movement.excess == 0


async def test_textual_null_broker_name_is_treated_as_absent_for_authenticated_broker(
    env, script, business, session_factory,
):
    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = "fast"
    script.steps = [{"tool": "record_broker_operation", "args": {
        "exchange_rate": "800",
        "operation_type": "sell", "operation_amount": "1000", "broker_name": "null",
    "text_money_sources": {"principal_usd": "1000", 'exchange_rate': "800"}}}]

    out = await send(container, "vendí 1000 a tasa 800")

    assert out.status == "ok" and "Venta de divisas" in out.replies[0]["text"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        assert movement.operation_type == "sell"
        assert movement.operation_amount == Decimal("1000.00")
        assert movement.delivered_amount == Decimal("1000.00")


async def test_settled_buy_keeps_customer(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = "fast"
    script.steps = [{"tool": "record_broker_operation", "args": {
        "exchange_rate": "800",
        "operation_type": "buy", "operation_amount": "450", "customer_name": "Lissy", "customer_classification": "personal", "description": "null",
    "text_money_sources": {"principal_usd": "450", 'exchange_rate': "800"}}}]

    out = await send(container, "Compré 450 a Lissy de contado, es personal, a tasa 800")
    assert "pendiente o liquidada" in out.replies[0]["text"]
    out = await send(container, "Liquidada")

    assert out.status == "ok" and "Compra de divisas" in out.replies[0]["text"]
    assert "Lissy" in out.replies[0]["text"]
    assert "null" not in out.replies[0]["text"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        assert movement.operation_type == "buy"
        assert movement.customer is not None and movement.customer.name == "Lissy"
        assert movement.description is None


@pytest.mark.parametrize(("message", "description"), [
    ("Saqué 40", None),
    ("SB 40", None),
    ("40", None),
    ("SB 40 Farmatodo", "Farmatodo"),
])
async def test_broker_account_purchase_credits_commission_without_receivable(
    env, script, business, session_factory, message: str, description: str | None,
):
    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = "fast"
    script.steps = [{"tool": "record_broker_account_purchase", "args": {
        "exchange_rate": "800",
        "amount": "40", "description": description,
    "text_money_sources": {"principal_usd": "40", 'exchange_rate': "800"}}}]

    out = await send(container, message + " a tasa 800")

    assert out.status == "ok" and out.lane == "fast"
    assert "Compra propia registrada" in out.replies[0]["text"]
    assert "Comisión: +$0,40" in out.replies[0]["text"]
    assert "Tu balance actual: *-$39,60*" in out.replies[0]["text"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        assert movement.settlement_status == "account"
        assert movement.description == description
        assert movement.balance_delta == Decimal("-39.60")
        assert (await session.execute(select(func.count()).select_from(Receivable))).scalar_one() == 0


@pytest.mark.parametrize("message", ["Pagué 1000 al negocio", "Súmale 1000 a mi balance"])
async def test_broker_account_payment_does_not_touch_receivables(
    env, script, business, session_factory, message,
):
    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = "fast"
    script.steps = [{"tool": "record_broker_account_payment", "args": {
        "amount": "1000",
    }}]

    out = await send(container, message)

    assert out.status == "ok" and "Abono a cuenta" in out.replies[0]["text"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        assert movement.kind == "in"
        assert movement.balance_delta == Decimal("1000.00")
        assert (await session.execute(select(func.count()).select_from(Receivable))).scalar_one() == 0
        assert (await session.execute(select(func.count()).select_from(CustomerPayment))).scalar_one() == 0


@pytest.mark.parametrize("lane", ["agent", "fast"])
async def test_direct_withdrawal_is_audited_linked_and_undoable(env, script, business, session_factory, lane):
    from app.db.models import AuditLog, BrokerDraftExecution, InboundMessage
    from tests.agent.test_buttons import ids, titles

    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = lane
    script.steps = [{"tool": "record_broker_account_withdrawal", "args": {"amount": "500"}}]
    out = await send(container, "Réstale 500$ a mi balance")
    assert out.status == "ok" and "Salida de cuenta registrada" in out.replies[0]["text"]
    assert "-$500,00" in out.replies[0]["text"] and titles(out) == ["Deshacer"]
    async with session_factory() as session:
        movement = (await session.scalars(select(BrokerMovement))).unique().one()
        assert movement.balance_delta == -500 and movement.kind == "out"
        source = await session.get(InboundMessage, movement.source_message_id)
        assert source.body == "Réstale 500$ a mi balance"
        assert await session.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.entity_id == movement.id)) == 1
        assert await session.scalar(select(func.count()).select_from(BrokerDraftExecution)) == 1
        movement_id = movement.id
    await send(container, "Deshacer", button_id=ids(out)[0])
    async with session_factory() as session:
        assert (await session.get(BrokerMovement, movement_id)).deleted_at is not None


@pytest.mark.parametrize(("message", "amount", "commission", "balance"), [
    ("Entregamos los fondos a Orlando y quedó debiendo 197.77", 197.77, "$1,98", Decimal("-195.79")),
    ("Orlando 100$", 100, "$1,00", Decimal("-99.00")),
    ("Orlando 100", 100, "$1,00", Decimal("-99.00")),
])
async def test_pending_purchase_and_shortcut_record_operation_commission_and_customer_debt(
    env, script, business, session_factory, message, amount, commission, balance,
):
    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = "fast"
    script.steps = [{"tool": "record_pending_broker_purchase", "args": {"exchange_rate": "800", "customer_classification": "personal",
        "customer_name": "Orlando", "amount": str(amount), "force_new_customer": True,
    "text_money_sources": {"principal_usd": str(amount), 'exchange_rate': "800"}}}]

    created = await send(container, message + " a tasa 800")
    assert "pendiente o liquidada" in created.replies[0]["text"]
    created = await send(container, "Pendiente")
    assert "Orlando" in created.replies[0]["text"]
    assert "Compra pendiente" in created.replies[0]["text"]
    assert f"Comisión: +{commission}" in created.replies[0]["text"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        receivable = (await session.execute(select(Receivable))).scalar_one()
        assert (movement.kind, movement.operation_type, movement.settlement_status) == (
            "operation", "buy", "pending",
        )
        assert movement.balance_delta == balance
        assert movement.related_receivable_id == receivable.id


async def test_pending_purchase_amount_can_be_corrected_from_quoted_receipt(
    env, script, business, session_factory,
):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = "fast"
    script.steps = [{"tool": "record_pending_broker_purchase", "args": {"exchange_rate": "800", "customer_classification": "personal",
        "customer_name": "Pedro", "amount": "1200", "force_new_customer": True,
    "text_money_sources": {"principal_usd": "1200", 'exchange_rate': "800"}}}]
    created = await send(container, "Pedro 1200 a tasa 800")
    created = await send(container, "Pendiente")
    receipt_id = created.replies[0]["provider_message_id"]

    def correct(_messages, info):
        return ModelResponse(parts=[ToolCallPart("edit_broker_movement", {
            "handle": handle_in_quoted_section(info),
            "changes": {"amount": "1000"},
        })])

    script.lane = "agent"
    script.steps = [correct, {"text": "Listo 👍"}]
    corrected = await send(container, "aquí eran 1000", reply_to=receipt_id)

    assert "Compra pendiente corregida" in corrected.replies[0]["text"]
    assert "$1.000,00" in corrected.replies[0]["text"]
    assert "Comisión: +$10,00" in corrected.replies[0]["text"]
    assert "Tu balance actual: *-$990,00*" in corrected.replies[0]["text"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        receivable = (await session.execute(select(Receivable))).scalar_one()
        assert movement.deleted_at is None
        assert movement.operation_amount == Decimal("1000.00")
        assert movement.commission == Decimal("10.00")
        assert movement.balance_delta == Decimal("-990.00")
        assert receivable.deleted_at is None
        assert receivable.amount == Decimal("1000.00")
        calls = (await session.execute(select(ToolCall).order_by(ToolCall.created_at))).scalars().all()
        assert calls[-1].tool_name == "edit_broker_movement"


async def test_customer_payment_is_applied_to_broker_debt_without_asking_destination(
    env, script, business, session_factory,
):
    container, _ = env
    await _set_brokerage(session_factory)
    script.lane = "fast"
    script.steps = [{"tool": "record_pending_broker_purchase", "args": {"exchange_rate": "800", "customer_classification": "personal",
        "customer_name": "Carlos", "amount": "100", "force_new_customer": True,
    "text_money_sources": {"principal_usd": "100", 'exchange_rate': "800"}}}]
    await send(container, "Carlos 100 a tasa 800")
    await send(container, "Pendiente")

    script.steps = [{"tool": "record_broker_receivable_payment", "args": {
        "customer_name": "Carlos", "amount": "50",
    }}]
    out = await send(container, "Carlos pagó 50")

    assert "Abono registrado" in out.replies[0]["text"]
    assert "Carlos debe a Casa: *$50,00*" in out.replies[0]["text"]
    assert "Tu balance actual: *-$49,00*" in out.replies[0]["text"]
    assert "¿Carlos te entregó" not in out.replies[0]["text"]
    async with session_factory() as session:
        movements = (await session.execute(
            select(BrokerMovement).order_by(BrokerMovement.created_at)
        )).scalars().all()
        payment = (await session.execute(select(CustomerPayment))).scalar_one()
        receivable = (await session.execute(select(Receivable))).scalar_one()
        assert [(row.kind, row.balance_delta) for row in movements] == [
            ("operation", Decimal("-99.00")),
            ("in", Decimal("50.00")),
        ]
        assert movements[0].settlement_status == "partial"
        assert movements[0].delivered_amount == Decimal("50.00")
        assert movements[1].related_payment_id == payment.id
        assert payment.allocations[0].receivable_id == receivable.id
        assert payment.allocations[0].applied_amount == Decimal("50.00")


@pytest.mark.parametrize(("message", "question"), [
    ("Recibí 50 por una venta", "¿Quién entregó esos $50 a quién?"),
    ("Salieron 100", "¿Quién entregó esos $100 a quién?"),
])
async def test_ambiguous_broker_phrases_ask_instead_of_writing(
    env, script, business, session_factory, message: str, question: str,
):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "need_more_info", "args": {"question": question}}]

    out = await send(container, message)

    assert out.replies[0]["text"] == question
    assert script.calls == 1
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(BrokerMovement))).scalar_one() == 0
        call = (await session.execute(select(ToolCall))).scalar_one()
        assert call.tool_name == "need_more_info" and call.outcome == "ok"


async def _seed_query_accounts(container, session_factory):
    from app.application.commands.brokerage import CreateBrokerMember, RecordPendingBrokerPurchase
    from app.application.context import Actor, ExecContext
    from app.domain.enums import ActorType

    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    async with session_factory() as session:
        member = (await session.execute(select(BusinessMember))).scalar_one()
        ctx = ExecContext(business_id=member.business_id, actor=Actor(ActorType.PANEL, member.id))
    for command in [
        CreateBrokerMember(name="Enrique", phone="584140000099"),
        RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="global", customer_name="Cliente Casa", amount=Decimal("800")),
        RecordPendingBrokerPurchase(exchange_rate="800", customer_classification="personal", customer_name="Cliente corredor", amount=Decimal("100"), broker_name="Enrique",
                                   force_new_customer=True),
    ]:
        result = await container.bus.dispatch(command, ctx)
        assert not result.needs_confirmation


@pytest.mark.parametrize("args, expected, excluded", [
    ({}, "$900,00", "Balance actual"),
    ({"account_scope": "house"}, "$800,00", "Comisiones"),
    ({"broker_name": "Enrique"}, "$100,00", "Cliente Casa"),
])
async def test_query_scope_and_standard_presentation(env, script, business, session_factory, args, expected, excluded):
    container, _ = env
    await _seed_query_accounts(container, session_factory)
    script.steps = [
        {"tool": "get_broker_balances", "args": {**args, "presentation": "summary"}},
        {"text": "¿Quieres otra explicación?"},
    ]
    out = await send(container, "Muéstrame el resumen de la cuenta seleccionada")
    assert out.status == "ok"
    text = out.replies[0]["text"]
    assert expected in text and excluded not in text
    assert "otra explicación" not in text
    if not args:
        assert "Resumen del negocio" in text and "Enrique" not in text
        assert "Global: $800,00" in text and "Personal: $100,00" in text
    if args.get("broker_name"):
        assert "Balance actual: -$99,00" in text


@pytest.mark.parametrize("parallel", [False, True])
async def test_multiple_reads_have_one_complete_model_answer(env, script, business, session_factory, parallel):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    container, _ = env
    await _seed_query_accounts(container, session_factory)
    calls = [
        {"tool": "get_broker_balances", "args": {"presentation": "summary"}},
        {"tool": "find_broker_receivables", "args": {}},
        {"tool": "find_broker_movements", "args": {}},
    ]
    answer = "Compras: $900,00. Cartera: Casa $800,00 y corredor $100,00. Movimientos de ambas cuentas."
    if parallel:
        def batch(_messages, _info):
            return ModelResponse(parts=[ToolCallPart(c["tool"], c["args"]) for c in calls])
        script.steps = [batch, {"text": answer}]
    else:
        script.steps = [*calls, {"text": answer}]
    out = await send(container, "Revisa compras, cartera y movimientos de todo el negocio")
    assert out.status == "ok"
    assert len(out.replies) == 1 and out.replies[0]["text"] == answer
    results = [r for r in script.seen_tool_returns if isinstance(r, dict) and r.get("ok")]
    assert len(results) == 3
    assert any(r.get("total_to_collect") == "900.00" for r in results)
    assert any(r.get("total") == 2 and "movements" in r for r in results)
    movements = next(r["movements"] for r in results if "movements" in r)
    house = next(row for row in movements if row["account_kind"] == "house")
    assert not {"customer_id", "commission", "commission_rate", "balance_delta", "balance_delta_display"} & house.keys()
    broker = next(row for row in movements if row["account_kind"] == "broker")
    assert Decimal(broker["commission"]) == Decimal("1.00")
    assert Decimal(broker["balance_delta"]) == Decimal("-99.00")


@pytest.mark.parametrize("scope", ["default", "all", "broker"])
async def test_broker_queries_are_always_own_account(env, script, business, session_factory, scope):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [
        {"tool": "get_broker_balances", "args": {"account_scope": scope, "presentation": "summary"}},
        {"text": "Tu resumen."},
    ]
    out = await send(container, "¿Cómo va mi cuenta?")
    assert out.status == "ok"
    result = script.seen_tool_returns[-1]
    assert result["account_scope"] == "broker"
    assert [row["name"] for row in result["balances"]] == ["Alex Example"]
    assert "Casa" not in out.replies[0]["text"]


@pytest.mark.parametrize("args", [{"account_scope": "house"}, {"broker_name": "Enrique"}])
@pytest.mark.parametrize("tool_name", ["get_broker_balances", "find_broker_receivables", "find_broker_movements"])
async def test_broker_cannot_select_other_accounts(env, script, business, session_factory, args, tool_name):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": tool_name, "args": {**args, **({"presentation": "summary"} if tool_name == "get_broker_balances" else {})}},
                    {"text": "Solo puedes consultar tu propia cuenta."}]
    out = await send(container, "Consulta otra cuenta")
    assert out.status == "ok"
    result = script.seen_tool_returns[-1]
    assert result["ok"] is False
    assert result["message"] == "Solo puedes consultar tu propia cuenta."
    assert "totals" not in result and "balances" not in result and "por_cobrar" not in result


@pytest.mark.parametrize("role", [MemberRole.OWNER, MemberRole.BROKER])
async def test_identity_answer_uses_authenticated_context_without_tools(env, script, business, session_factory, role):
    container, _ = env
    await _set_brokerage(session_factory, role=role)
    async with session_factory() as session, session.begin():
        member = (await session.execute(select(BusinessMember))).scalar_one()
        member.name = "Enrique"
        if role == MemberRole.OWNER:
            session.add(BusinessMember(business_id=member.business_id, name="Enrique", phone="584140000099",
                                       role=MemberRole.BROKER.value))
    script.steps = [{"text": f"Eres Enrique, {role.value} del negocio."}]
    out = await send(container, "¿Quién soy y qué rol tengo?")
    assert out.status == "ok" and "Enrique" in out.replies[0]["text"]
    assert any(f"Miembro: Enrique ({role.value})" in prompt for prompt in script.seen_instructions)
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(ToolCall))).scalar_one() == 0


async def test_concrete_query_has_only_the_requested_amount(env, script, business, session_factory):
    container, _ = env
    await _seed_query_accounts(container, session_factory)
    script.steps = [{"tool": "get_broker_balances"}, {"text": "Compramos $900,00 hoy."}]
    out = await send(container, "¿Cuánto compramos?")
    assert out.status == "ok" and out.replies[0]["text"] == "Compramos $900,00 hoy."


async def test_multiple_summary_candidates_are_composed_once(env, script, business, session_factory):
    container, _ = env
    await _seed_query_accounts(container, session_factory)
    answer = "Casa compró $800,00; Enrique compró $100,00 y tiene un balance de -$99,00."
    script.steps = [
        {"tool": "get_broker_balances", "args": {"account_scope": "house", "presentation": "summary"}},
        {"tool": "get_broker_balances", "args": {"broker_name": "Enrique", "presentation": "summary"}},
        {"text": answer},
    ]
    out = await send(container, "Compara lo de Casa y Enrique")
    assert out.status == "ok" and out.replies[0]["text"] == answer


async def test_unreadable_replacement_cancellation_has_no_success_receipt_or_financial_change(
    env, script, business, session_factory,
):
    container, console = env
    await _set_brokerage(session_factory)
    color = "white"

    async def download_media(_url):
        out = io.BytesIO()
        Image.new("RGB", (720, 480), color).save(out, "PNG")
        return out.getvalue(), "image/png"

    console.download_media = download_media
    script.steps = [{"tool": "record_broker_operation", "args": {
        "operation_type": "sell", "operation_amount": "100", "operation_amount_bs": "80000", "monetary_from_attachment": True,
    "text_money_sources": {"principal_usd": "100", 'operation_amount_bs': "80000"}}}]
    created = await send(container, "Vendí 100$ por 80000bs", kind="image", media_url="https://media.invalid/first", media_mime_type="image/png")
    receipt_id = created.replies[0]["provider_message_id"]
    async with session_factory() as session:
        original_id = (await session.execute(select(BrokerAttachment.id).where(BrokerAttachment.broker_movement_id.is_not(None)))).scalar_one()
    color = "green"
    script.steps = [{"tool": "complete_broker_receipt", "args": {"extraction": {"confidence": "unreadable", "amount_bs": None}}}]
    proposed = await send(container, None, kind="image", media_url="https://media.invalid/new", media_mime_type="image/png", reply_to=receipt_id)
    assert "No pude leer el monto" in proposed.replies[0]["text"]
    assert "Sin guardar" in proposed.replies[0]["text"]
    assert "Comprobante guardado" not in proposed.replies[0]["text"]
    cancelled = await send(container, "no")
    assert cancelled.status == "ok"
    assert all("Comprobante guardado" not in reply["text"] for reply in cancelled.replies)
    async with session_factory() as session:
        movements = (await session.execute(select(BrokerMovement))).scalars().all()
        assert len(movements) == 1 and movements[0].operation_amount_bs == Decimal("80000")
        assert movements[0].commission == Decimal("1")
        active = (await session.execute(select(BrokerAttachment).where(BrokerAttachment.broker_movement_id == movements[0].id,
                                                                     BrokerAttachment.deleted_at.is_(None)))).scalar_one()
        assert active.id == original_id
        assert active.extracted_data is None


async def test_broker_nullable_description_is_not_stored_as_null_text(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_account_payment", "args": {"amount": "100", "description": "null"}}]
    result = await send(container, "Aboné 100 a mi cuenta")
    assert "null" not in result.replies[0]["text"]
    async with session_factory() as session:
        movement = (await session.execute(select(BrokerMovement))).scalar_one()
        assert movement.description is None


@pytest.mark.parametrize("answer", ["sí", "no"])
async def test_customer_overpayment_confirmation_receipt_and_panel_link(
    env, script, business, session_factory, answer,
):
    from app.application.commands.brokerage import (
        RecordBrokerAccountPayment,
        RecordPendingBrokerPurchase,
    )
    from app.application.context import Actor, ExecContext
    from app.domain.enums import ActorType

    container, _ = env
    await _set_brokerage(session_factory)
    async with session_factory() as session:
        member = (await session.execute(select(BusinessMember))).scalar_one()
        ctx = ExecContext(business_id=member.business_id, actor=Actor(ActorType.PANEL, member.id))
    await container.bus.dispatch(RecordPendingBrokerPurchase(exchange_rate="800",
        customer_name="Juan", customer_classification="personal", amount="500",
    ), ctx)
    await container.bus.dispatch(RecordBrokerAccountPayment(amount="1196"), ctx)
    script.lane = "fast"
    script.steps = [{"tool": "record_broker_receivable_payment", "args": {"customer_name": "Juan", "amount": "600"}}]
    out = await send(container, "Juan pagó 600")
    preview = out.replies[0]["text"]
    assert preview.count("¿") == 1 and preview.count("⚠️") == 1
    assert "¿Lo hago?" not in preview and "Sin guardar" in preview
    assert "Saldo a favor de Juan quedaría en: *$100,00*" in preview
    assert "Tu balance quedaría en: *+$1.301,00*" in preview
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(CustomerPayment))).scalar_one() == 0
        assert (await session.execute(select(func.sum(BrokerMovement.balance_delta)))).scalar_one() == Decimal("701")
    calls = script.calls
    done = await send(container, answer)
    assert script.calls == calls  # Frozen confirmation/cancellation never invokes the model.
    async with session_factory() as session:
        count = (await session.execute(select(func.count()).select_from(CustomerPayment))).scalar_one()
        balance = (await session.execute(select(func.sum(BrokerMovement.balance_delta)))).scalar_one()
        assert count == (1 if answer == "sí" else 0)
        assert balance == Decimal("1301" if answer == "sí" else "701")
        if answer == "sí":
            movement = (await session.execute(select(BrokerMovement).where(BrokerMovement.related_payment_id.is_not(None)))).scalar_one()
            receipt = done.replies[0]["text"]
            assert "Juan pagó $600,00" in receipt
            assert "Saldo a favor de Juan: *$100,00*" in receipt
            assert "Tu balance actual: *+$1.301,00*" in receipt
            assert "📊 Abrir en el panel:" in receipt and str(movement.id) in receipt
            assert "¿" not in receipt and "Sin guardar" not in receipt
