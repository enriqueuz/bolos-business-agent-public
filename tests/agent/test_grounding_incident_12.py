"""Regressions for incident #12, with synthetic data and real command persistence."""

import pytest
from sqlalchemy import func, select

from app.agent import grounding
from app.application.conversation import conversation_scope
from app.db.models import AgentTurn, Sale, ToolCall
from tests.agent.test_pipeline import PHONE, send


@pytest.mark.parametrize("amount_first", [False, True])
def test_direct_evidence_is_not_lost_after_eighty_numbers(amount_first):
    evidence = {"metadata": list(range(10000, 10080)), "operation_amount_bs": "47500.00"}
    if amount_first:
        evidence = dict(reversed(list(evidence.items())))
    assert grounding.check("Compra: 47.500,00 Bs", [evidence], wrote_something=False).ok


@pytest.mark.parametrize("question", ["¿Quieres corregir algún dato?", "¿Corregí mal el nombre ayer?"])
async def test_question_about_correcting_does_not_trigger_rewrite(env, script, business, session_factory, question):
    script.steps = [{"text": question}]
    container, _ = env
    out = await send(container, "Es Diego Example")
    assert out.replies[0]["text"] == question
    assert script.calls == 1
    async with session_factory() as session:
        turn = await session.get(AgentTurn, out.turn_id)
        assert turn.grounding_ok is True


async def test_semantic_write_claim_without_command_is_blocked(env, script, business, session_factory):
    script.steps = [{"text": "Listo, ya lo dejé actualizado."}, {"text": "Ya quedó actualizado por mí."}]
    script.write_claim_assessments = [{"claims_current_turn_write": True, "uncertain": False}] * 2
    container, _ = env
    out = await send(container, "Corrige el cliente")
    assert "actualizado" not in out.replies[0]["text"]
    assert "No pude registrar" in out.replies[0]["text"]
    assert script.action_grounding_calls == 2
    async with session_factory() as session:
        report = (await session.get(AgentTurn, out.turn_id)).grounding_detail
        assert report["claimed_write"] and report["ungrounded"] == []
        assert report["failure_reasons"] == ["unverified_write_claim"]


@pytest.mark.parametrize("assessment", [RuntimeError("classifier unavailable"),
                                       {"claims_current_turn_write": False, "uncertain": True}])
async def test_unavailable_or_uncertain_claim_checker_fails_safely(env, script, business, session_factory, assessment):
    script.steps = [{"text": "¿Quieres corregir algún dato?"}]
    script.write_claim_assessments = [assessment]
    container, _ = env
    out = await send(container, "Es Diego Example")
    assert "verificar" in out.replies[0]["text"]
    assert script.calls == 1  # uncertainty never causes an operational retry
    async with session_factory() as session:
        report = (await session.get(AgentTurn, out.turn_id)).grounding_detail
        assert report["action_check_failed"] and not report["ok"]
        assert report["failure_reasons"] == ["action_verification_failed"]


@pytest.mark.parametrize("revision_text", ["El saldo es $987.654,32.", ""])
async def test_persistently_unverified_figures_are_not_sent_or_remembered(
    env, script, business, session_factory, revision_text,
):
    script.steps = [{"text": "El saldo es $987.654,32."}, {"text": revision_text}]
    container, _ = env
    out = await send(container, "¿Cuál es el saldo?")
    assert out.status == "ok"
    assert "987" not in out.replies[0]["text"]
    assert "verificar" in out.replies[0]["text"]
    window = await container.store.get_window(conversation_scope(business.id, PHONE))
    assert all("987" not in entry.assistant for entry in window)
    async with session_factory() as session:
        turn = await session.get(AgentTurn, out.turn_id)
        assert turn.grounding_ok is False
        assert "987" in turn.model_text  # rejected evidence stays available for diagnosis
        assert "987" not in turn.reply_text
        assert await session.scalar(select(func.count()).select_from(Sale)) == 0


@pytest.mark.parametrize("revision_raises", [False, True])
async def test_grounding_repair_failure_preserves_one_committed_receipt(
    env, script, business, session_factory, revision_raises,
):
    # A read after a successful write keeps the model loop open for an explanation.
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    def write_and_read(messages, info):
        return ModelResponse(parts=[
            ToolCallPart("record_sale", {"items": [{"product": "Bujía", "unit_price": 2, "currency": "USD"}]},
                         tool_call_id="sale"),
            ToolCallPart("query_metrics", {"metrics": ["sales_total"], "period": "today"}, tool_call_id="read"),
        ])

    def unavailable_revision(messages, info):
        raise RuntimeError("revision model unavailable")

    script.steps = [write_and_read, {"text": "Tu saldo es $987.654,32."},
                    unavailable_revision if revision_raises else {"text": "Tu saldo es $987.654,32."}]
    container, _ = env
    out = await send(container, "Vendí una bujía a 2 dólares y dime el total de hoy")
    body = out.replies[0]["text"]
    assert "Venta registrada" in body and "Bujía" in body
    assert "987" not in body and "No pude registrar" not in body
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Sale)) == 1
        writes = (await session.scalars(select(ToolCall).where(ToolCall.turn_id == out.turn_id,
                                                              ToolCall.tool_name == "record_sale"))).all()
        assert len(writes) == 1
        assert (await session.get(AgentTurn, out.turn_id)).grounding_ok is False


async def test_quoted_identity_clarification_reassigns_only_the_customer(env, script, business, session_factory):
    from decimal import Decimal

    from pydantic_ai.messages import ModelResponse, ToolCallPart

    from app.application.commands.brokerage import RecordBrokerOperation
    from app.application.context import Actor, ExecContext
    from app.db.models import BrokerMovement, BusinessMember, Customer
    from app.domain.enums import ActorType, MemberRole
    from tests.agent.harness import handle_in_quoted_section
    from tests.agent.test_brokerage_vertical import _set_brokerage

    container, _ = env
    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    async with session_factory() as session:
        member = (await session.scalars(select(BusinessMember))).one()
    await container.bus.dispatch(RecordBrokerOperation(
        customer_name="Diego Example", customer_classification="global", operation_type="sell",
        operation_amount="1220", exchange_rate="1035"),
        ExecContext(business_id=business.id, actor=Actor(ActorType.PANEL, member.id)), confirmed=True)
    script.steps = [{"tool": "record_broker_operation", "args": {
        "operation_type": "sell", "operation_amount": "1000", "exchange_rate": "1040",
        "customer_name": "DEX", "customer_classification": "global",
        "text_money_sources": {"principal_usd": "1000", "exchange_rate": "1040"},
    }}]
    original = await send(container, "Venta 1000 a tasa 1040 para DEX, cliente global")

    def reassign(messages, info):
        assert "pregunta por esa operación" in info.instructions
        return ModelResponse(parts=[ToolCallPart("edit_broker_movement", {
            "handle": handle_in_quoted_section(info), "changes": {"customer_name": "Diego Example"},
        })])

    script.steps = [reassign, {"text": "Corregí el cliente."}]
    out = await send(container, "Es Diego Example", reply_to=original.replies[0]["provider_message_id"])
    assert out.status == "ok"
    async with session_factory() as session:
        movements = (await session.scalars(select(BrokerMovement))).unique().all()
        assert len(movements) == 2
        corrected = next(m for m in movements if m.operation_amount == Decimal("1000"))
        customer = await session.get(Customer, corrected.customer_id)
        assert customer.name == "Diego Example"
        assert corrected.operation_amount_bs == Decimal("1040000")
        assert corrected.exchange_rate == Decimal("1040")
        assert corrected.balance_delta == 0 and corrected.commission == 0
        calls = (await session.scalars(select(ToolCall).where(ToolCall.turn_id == out.turn_id))).all()
        assert [call.tool_name for call in calls] == ["edit_broker_movement"]


async def test_composition_uses_verified_snapshot_instead_of_previous_candidate(env, business):
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.agent.deps import TurnDeps

    container, _ = env
    identity = await container.identity.resolve(PHONE)
    deps = TurnDeps(identity=identity, scope=conversation_scope(business.id, PHONE),
                    turn_id=uuid4(), inbound_id=uuid4(), now=datetime.now(UTC), user_text="Consulta",
                    bus=container.bus, queries=container.queries, brokerage_queries=container.brokerage_queries,
                    attachments=container.attachments, store=container.store, turns=container.turns)
    deps.tool_results = [{"ok": True, "balance": "0.00"}]
    deps.renders = ["Saldo inventado: $987.654,32"]
    rewritten = "No hay saldo pendiente."
    report = grounding.check(rewritten, deps.tool_results, wrote_something=False)
    reply = await container.pipeline._compose(deps, rewritten, grounding_report=report)
    assert reply.shown_text == rewritten
    assert "987" not in reply.shown_text


@pytest.mark.parametrize("rewrite_violates", [False, True])
async def test_summary_presentation_is_checked_semantically_and_persistent_violation_is_blocked(
    env, script, business, session_factory, rewrite_violates,
):
    from tests.agent.test_brokerage_vertical import _set_brokerage

    container, _ = env
    await _set_brokerage(session_factory)
    incorrect = "Casa no tiene balance ni comisión."
    corrected = "Hoy no hay compras ni ventas."
    script.steps = [
        {"tool": "get_broker_balances", "args": {"presentation": "data"}},
        {"text": incorrect},
        {"text": incorrect if rewrite_violates else corrected},
    ]
    script.write_claim_assessments = [
        {"claims_current_turn_write": False, "uncertain": False, "violates_house_presentation": True,
         "house_presentation_quote": incorrect},
        {"claims_current_turn_write": False, "uncertain": False,
         "violates_house_presentation": rewrite_violates,
         "house_presentation_quote": incorrect if rewrite_violates else None},
    ]
    outcome = await send(container, "Dame el resumen de hoy")
    body = outcome.replies[0]["text"]
    assert incorrect not in body
    assert body == ("No pude verificar la respuesta. Revisa el panel o vuelve a consultar."
                    if rewrite_violates else corrected)
    assert script.action_grounding_calls == 2
    async with session_factory() as session:
        turn = await session.get(AgentTurn, outcome.turn_id)
        assert turn.grounding_ok is not rewrite_violates
        assert turn.grounding_detail["presentation_violation"] is rewrite_violates
        if rewrite_violates:
            assert turn.grounding_detail["failure_reasons"] == ["presentation_rule_violation"]
        assert await session.scalar(select(func.count()).select_from(Sale)) == 0
