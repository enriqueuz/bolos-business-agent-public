"""Real tools/commands and native quote references; the scripted model does not test LLM reasoning."""
from datetime import timedelta

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy import func, select

from app.application.commands.brokerage import RecordBrokerOperation, RecordPendingBrokerPurchase
from app.application.context import Actor, ExecContext
from app.channels.kapso.provider import parse_message_event
from app.db.models import BrokerMovement, BusinessMember
from app.domain.enums import ActorType, MemberRole
from tests.agent.test_brokerage_vertical import _set_brokerage
from tests.agent.test_pipeline import PHONE, send


async def seed_accounts(container, session_factory, business):
    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    async with session_factory() as db:
        member = (await db.scalars(select(BusinessMember))).one()
    ctx = ExecContext(business_id=business.id, actor=Actor(ActorType.PANEL, member.id))
    for i, command in enumerate([
        RecordPendingBrokerPurchase(customer_name="Juan", amount="1000", exchange_rate="900", customer_classification="global"),
        RecordPendingBrokerPurchase(customer_name="Juan", amount="20", exchange_rate="900"),
        RecordBrokerOperation(customer_name="Juan", operation_type="sell", settlement="pending", operation_amount="500", exchange_rate="900"),
        RecordBrokerOperation(customer_name="Maria Perez", operation_type="sell", settlement="pending", operation_amount="100", exchange_rate="950", customer_classification="global"),
    ]):
        await container.bus.dispatch(command, ExecContext(business_id=business.id, actor=ctx.actor,
                                                          now=ctx.now + timedelta(seconds=i)), confirmed=True)


@pytest.mark.parametrize("customer,before,delta,after", [("Juan", "-1020.00", "500.00", "-520.00"),
                                                        ("Maria Perez", "0.00", "100.00", "100.00")])
async def test_read_composition_and_explain_without_writes(env, script, business, session_factory,
                                                         customer, before, delta, after):
    container, _ = env
    await seed_accounts(container, session_factory, business)

    def explain(messages, info):
        result = script.seen_tool_returns[-1]
        assert result["ok"] and result["total"] == 1
        account = result["customer_accounts"][0]
        assert account["balance_usd"] == after and account["history_complete"]
        sale = account["movements"][0]
        assert sale["logical_type"] == "sell" and sale["original_settlement"] == "pending"
        assert (sale["customer_balance_before"], sale["customer_balance_delta"], sale["customer_balance_after"]) == (before, delta, after)
        assert "balance_delta" not in sale and "id" not in sale
        assert result["records"]  # authorized references are available for follow-up
        return ModelResponse(parts=[TextPart("La venta quedó registrada correctamente. Mi explicación anterior fue incorrecta.")])

    script.steps = [{"tool": "find_broker_receivables", "args": {"customer_name": customer, "include_movements": True}}, explain]
    out = await send(container, f"¿Por qué está mal {customer}?")
    assert out.status == "ok"
    assert "explicación anterior fue incorrecta" in out.replies[0]["text"]
    assert script.calls == 2  # no spurious grounding rewrite
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 4


async def test_kapso_receipt_quote_is_recovered_but_current_customer_is_primary(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    script.steps = [{"tool": "record_broker_operation", "args": {"operation_type": "sell", "settlement": "pending",
        "operation_amount": "500", "exchange_rate": "900", "customer_name": "Juan", "customer_classification": "global",
        "text_money_sources": {"principal_usd": "500$", "exchange_rate": "900"}}}]
    saved = await send(container, "Venta pendiente 500$ tasa 900 Juan global")
    quote_id = saved.replies[-1]["provider_message_id"]
    event = {"message": {"id": "kapso-followup", "from": PHONE, "type": "text",
                         "text": {"body": "¿Por qué está mal Maria?"}, "context": {"id": quote_id}}}
    inbound = parse_message_event(event)
    body, refs = await container.turns.quoted_message_refs(business.id, quote_id)
    assert "Juan" in body and refs

    def inspect_quote(messages, info):
        instructions = info.instructions or ""
        assert "Juan" in instructions and "Registros vinculados a ese mensaje" in instructions
        assert "tienen prioridad sobre el sujeto de la cita" in instructions
        return ModelResponse(parts=[TextPart("Necesito consultar la cuenta de Maria para explicarte su saldo.")])

    script.steps = [inspect_quote]
    # Same persistence path as the webhook, after protocol parsing; no fabricated quoted_text injection.
    inbound_id = await container.turns.save_inbound(provider=inbound.provider, provider_message_id=inbound.provider_message_id,
        from_phone=inbound.from_phone, contact_name="Enrique", type=inbound.type, body=inbound.text, transcript=None,
        media_url=None, media_mime_type=None, reply_to_provider_message_id=inbound.reply_to_provider_message_id, raw=event)
    out = await container.pipeline.process(inbound_id)
    assert out.status == "ok" and "Maria" in out.replies[0]["text"]
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 1


async def test_missing_customer_does_not_invent_history(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory, role=MemberRole.OWNER)

    def missing(messages, info):
        result = script.seen_tool_returns[-1]
        assert result["ok"] and result["total"] == 0 and not result["customer_accounts"]
        return ModelResponse(parts=[TextPart("No encontré esa cuenta. Necesito el nombre o apodo del cliente para explicar su saldo.")])

    script.steps = [{"tool": "find_broker_receivables", "args": {"customer_name": "Desconocido", "include_movements": True}}, missing]
    out = await send(container, "¿Cómo llegaste al saldo de Desconocido?")
    assert out.status == "ok" and "No encontré" in out.replies[0]["text"]
