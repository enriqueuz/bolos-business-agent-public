"""The existing agent interprets; the existing pipeline confirms and renders."""
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from sqlalchemy import func, select

from app.application.conversation import conversation_scope
from app.db.models import BrokerMovement, BusinessMember, Customer, CustomerBalanceAdjustment, ToolCall
from app.domain.enums import MemberRole
from tests.agent.harness import handle_in_quoted_section
from tests.agent.test_brokerage_vertical import _set_brokerage

NOW = datetime(2026, 11, 4, 16, tzinfo=UTC)


async def send_at(container, text, *, reply_to=None, button_id=None):
    inbound = await container.turns.save_inbound(provider="console", provider_message_id=f"reconciliation-{uuid4()}",
        from_phone="584140000001", contact_name="Carlos", type="text", body=text, transcript=None,
        media_url=None, media_mime_type=None, reply_to_provider_message_id=reply_to, interactive_reply_id=button_id, raw={})
    return await container.pipeline.process(inbound, now=NOW)


async def seed_accounts(factory, business_id):
    async with factory() as db, db.begin():
        broker = BusinessMember(business_id=business_id, phone="584140008888", name="Alexandra", role="broker")
        customer = Customer(business_id=business_id, name="María", normalized_name="maria", classification="global")
        db.add_all([broker, customer])


@pytest.mark.parametrize("message,direction,amount", [
    ("ajustar saldo Alexandra 31/10 deuda 900", "debt", "900"),
    ("ajuste saldo Alexandra debia 1000 al 31-10", "debt", "1000"),
    ("ajusta el saldo de Alexandra al 31/10 a favor 500", "credit", "500"),
    ("quiero ajustar el saldo de Alexandra al 31/10 deuda 900", "debt", "900"),
    (" AJUSTAR   SALDO Alexandra 31/10 -700", None, "-700"),
    ("Ajustar saldo Alexandra al 31 de octubre, debía 1200 dólares", "debt", "1200"),
    ("ajustar saldo Alexandra 31/10 favor 500", "credit", "500"),
    ("ajustar saldo Alexandra 31/10 a favor 500", "credit", "500"),
    ("AJUSTAR SALDO Alexandra 31/10 +300", None, "+300"),
])
async def test_activation_and_confirmation_without_second_llm(env, script, business, session_factory, message, direction, amount):
    container, _ = env
    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    await seed_accounts(session_factory, business.id)
    script.steps = [{"tool": "reconcile_balance", "args": {"holder_name": "Alexandra",
        "historical_day": 31, "historical_month": 10, "amount": amount, **({"direction": direction} if direction else {})}}]
    prepared = await send_at(container, message)
    assert prepared.status == "ok"
    text = prepared.replies[0]["text"]
    assert "Confirmar ajuste — Alexandra (Corredor)" in text
    assert "Saldo actual (04/11)" in text and "Nuevo saldo actual (04/11)" in text
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 0
        tool_call = (await db.scalars(select(ToolCall).where(ToolCall.tool_name == "reconcile_balance"))).one()
        assert "account_id" not in tool_call.result["preview"]
        assert "portfolio_id" not in tool_call.result["preview"]
    # Trusted account identifiers stay inside the frozen command, not the LLM result.
    scope = conversation_scope(business.id, "584140000001")
    window = await container.store.get_window(scope)
    assert all("account_id" not in call["result"] and "portfolio_id" not in call["result"]
               for entry in window for call in entry.calls)
    calls = script.calls
    confirmed = await send_at(container, "sí")
    assert confirmed.status == "ok" and "Ajuste de saldo registrado" in confirmed.replies[0]["text"]
    assert script.calls == calls
    async with session_factory() as db:
        movement = (await db.scalars(select(BrokerMovement))).one()
        assert movement.kind == "adjustment" and movement.commission is None
    scope = conversation_scope(business.id, "584140000001")
    assert await container.store.get_reconciliation(scope) is None


async def test_missing_direction_date_and_continuation(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    await seed_accounts(session_factory, business.id)
    script.steps = [{"tool": "reconcile_balance", "args": {"holder_name": "María", "amount": "500"}}]
    first = await send_at(container, "ajustar saldo María 500")
    assert "fecha" in first.replies[0]["text"]
    script.steps = [{"tool": "reconcile_balance", "args": {"historical_day": 31, "historical_month": 10}}]
    second = await send_at(container, "al 31/10")
    assert "deuda" in second.replies[0]["text"]
    script.steps = [{"tool": "reconcile_balance", "args": {"direction": "credit"}}]
    preview = await send_at(container, "a favor")
    assert "Confirmar ajuste — María (Cliente)" in preview.replies[0]["text"]
    confirmed = await send_at(container, "sí")
    assert "+$500,00" in confirmed.replies[0]["text"]
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(CustomerBalanceAdjustment)) == 1


@pytest.mark.parametrize("message", ["Alexandra debía 900 al 31/10", "¿Cuánto debía María?", "explica ajustar saldo Alexandra"])
async def test_available_tools_do_not_start_draft_without_model_action(env, script, business, session_factory, message):
    container, _ = env
    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    def verify_tools(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert "reconcile_balance" in names
        assert "cancel_balance_reconciliation" not in names
        return ModelResponse(parts=[ToolCallPart("need_more_info", {"question": "¿Qué necesitas consultar?"})])
    script.steps = [verify_tools]
    await send_at(container, message)
    scope = conversation_scope(business.id, "584140000001")
    assert await container.store.get_reconciliation(scope) is None
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 0
        assert await db.scalar(select(func.count()).select_from(CustomerBalanceAdjustment)) == 0


async def test_cancel_and_duplicate_confirmation(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    await seed_accounts(session_factory, business.id)
    args = {"holder_name": "Alexandra", "historical_day": 31, "historical_month": 10, "amount": "900", "direction": "debt"}
    script.steps = [{"tool": "reconcile_balance", "args": args}]
    await send_at(container, "ajustar saldo Alexandra 31/10 deuda 900")
    await send_at(container, "no")
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 0
    script.steps = [{"tool": "reconcile_balance", "args": args}]
    preview = await send_at(container, "ajustar saldo Alexandra 31/10 deuda 900")
    button = preview.replies[0]["choices"][0]["id"]
    await send_at(container, "Sí", button_id=button)
    await send_at(container, "Sí", button_id=button)
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 1


async def test_delete_customer_adjustment_by_quoted_receipt(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    await seed_accounts(session_factory, business.id)
    script.steps = [{"tool": "reconcile_balance", "args": {"holder_name": "María", "historical_day": 31,
        "historical_month": 10, "amount": "500", "direction": "credit"}}]
    await send_at(container, "ajustar saldo María 31/10 a favor 500")
    saved = await send_at(container, "sí")
    def delete_quoted(messages, info):
        return ModelResponse(parts=[ToolCallPart("delete_balance_adjustment", {"handle": handle_in_quoted_section(info)})])
    script.steps = [delete_quoted]
    preview = await send_at(container, "ajustar saldo elimina este ajuste", reply_to=saved.replies[0]["provider_message_id"])
    assert "Confirmar eliminación de ajuste" in preview.replies[0]["text"]
    deleted = await send_at(container, "sí")
    assert "Ajuste de saldo eliminado" in deleted.replies[0]["text"]


async def test_new_adjustment_request_does_not_inherit_missing_direction(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    await seed_accounts(session_factory, business.id)
    script.steps = [{"tool": "reconcile_balance", "args": {"holder_name": "Alexandra", "historical_day": 31,
        "historical_month": 10, "amount": "900", "direction": "debt"}}]
    await send_at(container, "ajuste saldo Alexandra 31/10 deuda 900")
    script.steps = [{"tool": "reconcile_balance", "args": {"holder_name": "María", "historical_day": 31,
        "historical_month": 10, "amount": "500", "new_request": True}}]
    response = await send_at(container, "quiero ajustar el saldo de María al 31/10 a 500")
    assert "deuda" in response.replies[0]["text"]
    draft = await container.store.get_reconciliation(conversation_scope(business.id, "584140000001"))
    assert draft.fields["holder_name"] == "María" and "direction" not in draft.fields
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 0
        assert await db.scalar(select(func.count()).select_from(CustomerBalanceAdjustment)) == 0


async def test_broker_deletes_own_adjustment_to_other_broker_without_prefix(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory, role=MemberRole.BROKER)
    await seed_accounts(session_factory, business.id)
    script.steps = [{"tool": "reconcile_balance", "args": {"holder_name": "Alexandra", "historical_day": 31,
        "historical_month": 10, "amount": "500", "direction": "credit"}}]
    await send_at(container, "ajuste saldo Alexandra 31/10 a favor 500")
    saved = await send_at(container, "sí")
    def delete_quoted(messages, info):
        return ModelResponse(parts=[ToolCallPart("delete_balance_adjustment", {"handle": handle_in_quoted_section(info)})])
    script.steps = [delete_quoted]
    preview = await send_at(container, "elimina este ajuste", reply_to=saved.replies[0]["provider_message_id"])
    assert "Confirmar eliminación de ajuste" in preview.replies[0]["text"]
    deleted = await send_at(container, "sí")
    assert "Ajuste de saldo eliminado" in deleted.replies[0]["text"]
