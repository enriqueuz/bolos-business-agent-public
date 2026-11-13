"""Verifica integración del prompt y conservación de respuestas; la conducta del LLM vive en evals."""

import pytest

from app.agent.prompts.conversation import BUSINESS_CONVERSATION
from app.domain.enums import BusinessVertical
from tests.agent.test_brokerage_vertical import _set_brokerage
from tests.agent.test_pipeline import send


@pytest.mark.parametrize("vertical", list(BusinessVertical))
async def test_repeated_greetings_receive_shared_rules_and_history(env, script, business, session_factory, vertical):
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    if vertical == BusinessVertical.BROKERAGE:
        await _set_brokerage(session_factory)
    container, _ = env
    first_reply = "¡Hola! Puedes registrar operaciones y consultar los resultados de tu negocio. ¿Qué necesitas hacer?"
    second_reply = "¡Hola de nuevo! ¿Qué operación del negocio necesitas registrar?"

    def second_turn(messages, info):
        assert BUSINESS_CONVERSATION in info.instructions
        assert any(
            part.content == first_reply
            for message in messages if isinstance(message, ModelResponse)
            for part in message.parts if isinstance(part, TextPart)
        )
        assert any(
            part.content == "Hola otra vez" or part.content == ["Hola otra vez"]
            for message in messages if isinstance(message, ModelRequest)
            for part in message.parts if isinstance(part, UserPromptPart)
        )
        return ModelResponse(parts=[TextPart(second_reply)])

    script.steps = [{"text": first_reply}, second_turn]
    first = await send(container, "Hola")
    second = await send(container, "Hola otra vez")
    assert first.status == second.status == "ok"
    assert first.replies[0]["text"] == first_reply
    assert second.replies[0]["text"] == second_reply
    assert BUSINESS_CONVERSATION in script.seen_instructions[0]


@pytest.mark.parametrize("vertical", list(BusinessVertical))
async def test_capability_question_uses_agent_and_explicit_menu_still_uses_catalog(
    env, script, business, session_factory, vertical,
):
    if vertical == BusinessVertical.BROKERAGE:
        await _set_brokerage(session_factory)
    container, _ = env
    reply = "Puedes registrar operaciones y consultar resultados de tu negocio. ¿Qué necesitas?"
    script.steps = [{"text": reply}]
    answer = await send(container, "¿Qué puedes hacer?")
    assert answer.status == "ok" and answer.lane == "agent"
    assert answer.replies[0]["text"] == reply
    assert script.calls == 1
    menu = await send(container, "menú")
    assert menu.lane == "deterministic"
    assert "Esto es lo que sé hacer" in menu.replies[0]["text"]
    assert "responde citando el mensaje" in menu.replies[0]["text"]
    assert "todavía no está disponible" not in menu.replies[0]["text"]
    assert script.calls == 1


@pytest.mark.parametrize("vertical", list(BusinessVertical))
async def test_missing_transaction_data_preserves_exact_question(env, script, business, session_factory, vertical):
    if vertical == BusinessVertical.BROKERAGE:
        await _set_brokerage(session_factory)
    container, _ = env
    question = "¿Cuál fue el monto?"
    script.steps = [{"tool": "need_more_info", "args": {"question": question}}]
    out = await send(container, "Pagué al negocio" if vertical == BusinessVertical.BROKERAGE else "Pagué la luz")
    assert out.status == "ok"
    assert out.replies[0]["text"] == question
    assert script.calls == 1
    assert BUSINESS_CONVERSATION in script.seen_instructions[0]
