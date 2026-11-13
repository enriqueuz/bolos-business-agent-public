"""Consecutive uploads reuse the normal commands; semantic choices remain model-owned."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db.models import BrokerAttachment, BrokerMovement, InboundMessage, Receivable
from app.domain.enums import MemberRole
from tests.agent.test_brokerage_vertical import _receipt_image, _set_brokerage
from tests.agent.test_pipeline import send


async def image(container, console):
    async def download(_url):
        return _receipt_image(), "image/png"
    console.download_media = download
    return await send(container, "", kind="image", media_url="https://media.invalid/receipt")


@pytest.mark.parametrize("role,classification", [
    (MemberRole.OWNER, None), (MemberRole.OWNER, "global"),
    (MemberRole.BROKER, None), (MemberRole.BROKER, "global"), (MemberRole.BROKER, "personal"),
])
@pytest.mark.parametrize("placement", ["before", "caption", "after"])
async def test_immediate_purchase_receipt_and_classification(
    env, script, business, session_factory, role, classification, placement,
):
    container, console = env
    await _set_brokerage(session_factory, role=role)
    text = "100$ Jessica zelle recibido"
    args = dict(amount="100", customer_name="Jessica", settlement="settled")
    if classification:
        args["customer_classification"] = classification
    if placement == "before":
        alone = await image(container, console)
        assert "no registré nada" in alone.replies[0]["text"]
        assert script.calls == 0
        args.update(extraction={"amount_bs": "95000", "confidence": "clear"}, operation_amount_bs="95000", monetary_from_attachment=True, use_previous_receipt=True)
    elif placement == "caption":
        async def download(_url):
            return _receipt_image(), "image/png"
        console.download_media = download
        args.update(extraction={"amount_bs": "95000", "confidence": "clear"}, operation_amount_bs="95000", monetary_from_attachment=True)
    script.steps = [{"tool": "record_broker_purchase", "args": args}]
    out = await send(container, text, **({"kind": "image", "media_url": "https://media.invalid/receipt"}
                                      if placement == "caption" else {}))
    if placement == "after":
        assert "bolívares" in out.replies[0]["text"]
        script.steps = [{"tool": "resume_broker_operation", "args": {
            "extraction": {"amount_bs": "95000", "confidence": "clear"}}}]
        out = await image(container, console)
    assert "pendiente o liquidada" in out.replies[0]["text"]
    out = await send(container, "Liquidada")
    if classification is None:
        assert "tipo de cliente" in out.replies[0]["text"]
        out = await send(container, "Global")
    assert out.status == "ok"
    async with session_factory() as session:
        row = (await session.scalars(select(BrokerMovement))).one()
        attachment = (await session.scalars(select(BrokerAttachment))).one()
        assert row.operation_type == "buy" and row.settlement_status == "settled"
        assert row.operation_amount == 100 and row.operation_amount_bs == 95000 and row.exchange_rate == 950
        assert row.customer.name == "Jessica"
        assert row.balance_delta == (0 if role == MemberRole.OWNER else 1)
        assert row.commission == (0 if role == MemberRole.OWNER else 1)
        assert attachment.broker_movement_id == row.id
        assert await session.scalar(select(func.count()).select_from(Receivable)) == 0
        source = row.source_message_id
    # Reprocessing the same inbound cannot duplicate the operation or its commission.
    await container.pipeline.process(source)
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 1


@pytest.mark.parametrize("barrier", ["text", "expired", "quoted", "other_member", "other_business", "consumed", "other_channel", "caption", "current_quote"])
async def test_preceding_receipt_boundaries(env, script, business, session_factory, barrier):
    container, console = env
    await _set_brokerage(session_factory)
    await image(container, console)
    async with session_factory() as session, session.begin():
        attachment = (await session.scalars(select(BrokerAttachment))).one()
        inbound = await session.get(InboundMessage, attachment.source_message_id)
        member_id = attachment.uploaded_by_member_id
        if barrier == "expired":
            inbound.received_at = datetime.now(UTC) - timedelta(minutes=11)
        elif barrier == "quoted":
            inbound.reply_to_provider_message_id = "explicit-target"
        elif barrier == "other_channel":
            inbound.provider = "panel"
        elif barrier == "caption":
            inbound.body = "Comprobante de otra operación"
    if barrier in ("text", "consumed"):
        script.steps = [{"tool": "record_broker_operation", "args": {
            "operation_type": "sell", "operation_amount": "50", "exchange_rate": "800", "text_money_sources": {"principal_usd": "50", 'exchange_rate': "800"}}}]
        await send(container, "Otra operación: vendí 50 a tasa 800")
    if barrier == "consumed":
        async with session_factory() as session, session.begin():
            row = (await session.scalars(select(BrokerAttachment))).one()
            row.broker_movement_id = (await session.scalars(select(BrokerMovement.id))).one()
    script.steps = [{"tool": "need_more_info", "args": {"question": "¿Qué deseas registrar?"}}]
    await send(container, "100$ Jessica zelle recibido", reply_to="explicit-target" if barrier == "current_quote" else None)
    async with session_factory() as session:
        current = (await session.scalars(select(InboundMessage).order_by(InboundMessage.received_at.desc()).limit(1))).one()
        if barrier == "other_member":
            from uuid import uuid4
            member_id = uuid4()
        target_business = business.id
        if barrier == "other_business":
            from uuid import uuid4
            target_business = uuid4()
    assert await container.attachments.preceding_receipts(
        target_business, member_id, current.id, now=datetime.now(UTC)) == []


async def test_two_preceding_receipts_cannot_be_selected_implicitly(env, script, business, session_factory):
    container, console = env
    await _set_brokerage(session_factory)
    await image(container, console)
    await image(container, console)
    script.steps = [{"tool": "record_broker_purchase", "args": {
        "amount": "100", "settlement": "settled", "operation_amount_bs": "95000",
        "use_previous_receipt": True,
    "text_money_sources": {"principal_usd": "100", 'operation_amount_bs': "95000"}}}, {"tool": "need_more_info", "args": {"question": "¿Cuál de los dos comprobantes corresponde? Reenvíalo con la descripción."}}]
    out = await send(container, "100$ Jessica zelle recibido")
    assert "dos comprobantes" in out.replies[0]["text"]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 0


async def test_used_checksum_is_not_offered_again(env, script, business, session_factory):
    container, console = env
    await _set_brokerage(session_factory)
    async def download(_url):
        return _receipt_image(), "image/png"
    console.download_media = download
    script.steps = [{"tool": "record_broker_operation", "args": {
        "operation_type": "sell", "operation_amount": "100", "operation_amount_bs": "95000",
        "monetary_from_attachment": True, "extraction": {"amount_bs": "95000", "confidence": "clear"}, "text_money_sources": {"principal_usd": "100", 'operation_amount_bs': "95000"}}}]
    await send(container, "Vendí 100$", kind="image", media_url="https://media.invalid/receipt")
    await image(container, console)
    script.steps = [{"tool": "record_broker_purchase", "args": {
        "amount": "100", "settlement": "settled", "operation_amount_bs": "95000", "use_previous_receipt": True,
    "text_money_sources": {"principal_usd": "100", 'operation_amount_bs': "95000"}}}, {"tool": "need_more_info", "args": {"question": "Reenvía el comprobante correcto."}}]
    out = await send(container, "100$ Jessica zelle recibido")
    assert "correcto" in out.replies[0]["text"]
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 1
        assert await session.scalar(select(func.count()).select_from(BrokerAttachment).where(
            BrokerAttachment.broker_movement_id.is_not(None))) == 1


async def test_receipt_already_offered_to_draft_is_not_reassigned(env, script, business, session_factory):
    container, console = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_operation", "args": {"operation_type": "sell", "operation_amount": "200"}}]
    await send(container, "Vendí 200")
    script.steps = [{"tool": "need_more_info", "args": {"question": "No se lee el importe. Dime los bolívares."}}]
    await image(container, console)
    script.steps = [{"tool": "record_broker_purchase", "args": {
        "amount": "100", "settlement": "settled", "operation_amount_bs": "95000", "use_previous_receipt": True,
    "text_money_sources": {"principal_usd": "100", 'operation_amount_bs': "95000"}}}, {"tool": "need_more_info", "args": {"question": "Reenvía el comprobante de esta compra."}}]
    await send(container, "Otra operación: compré 100$ a Jessica, recibido")
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerMovement)) == 0
