import pytest

from app.agent.orchestrator import EXHAUSTED_TEXT
from app.agent.prompts.conversation import BUSINESS_CONVERSATION
from app.agent.prompts.system import static_instructions
from app.agent.tools.registry import capabilities_text
from app.domain.enums import BusinessVertical
from app.domain.text import is_menu_request
from evals.conversation import (
    business_reply,
    operational_reply,
    repeated_greeting_reply,
    uncertain_receipt_reply,
    visible_reply_text,
)


def test_receipt_clarification_evaluates_visible_buttons_without_exposing_tokens():
    reply = {"text": "¿Cuál de estos montos corresponde a la venta de 100$?", "choices": [
        {"id": "o:private-token:1", "title": "Transferencia 80.000 Bs", "description": None},
        {"id": "o:private-token:2", "title": "Transferencia 90.000 Bs", "description": None},
    ]}
    # The body alone omits the currency shown on the buttons. The actual message
    # asks for a choice instead of claiming that either amount was recorded.
    assert not uncertain_receipt_reply(reply["text"])
    rendered = visible_reply_text(reply)
    assert uncertain_receipt_reply(rendered)
    assert "80.000 Bs" in rendered and "90.000 Bs" in rendered
    assert "private-token" not in rendered
    assert not uncertain_receipt_reply("No pude verificar la respuesta. Revisa el panel.")
    assert not uncertain_receipt_reply("Venta registrada: 80.000 Bs del comprobante.")


def test_eval_keeps_plain_reply_text_unchanged():
    assert visible_reply_text({"text": "Dime el monto en bolívares del capture."}) == (
        "Dime el monto en bolívares del capture."
    )


@pytest.mark.parametrize("vertical", list(BusinessVertical))
def test_shared_policy_uses_existing_vertical_capabilities(vertical):
    prompt = static_instructions(vertical)
    assert prompt.count(BUSINESS_CONVERSATION) == 1
    assert capabilities_text(vertical) in prompt
    # No catálogo duplicado ni ejemplos retail dentro de la política compartida.
    assert not any(word in BUSINESS_CONVERSATION for word in ("inventario", "fiados", "mercancía"))


def test_conversation_checks_fail_on_generic_or_cross_vertical_replies():
    assert not business_reply("¡Hola! ¿En qué te ayudo?", BusinessVertical.RETAIL)
    assert not business_reply("¡Hola! Estoy aquí para ayudarte con lo que necesites.", BusinessVertical.RETAIL)
    assert business_reply("Para lo que necesites con tu negocio: ventas y gastos.", BusinessVertical.RETAIL)
    assert not business_reply("Te ayudo con ventas y cualquier tema.", BusinessVertical.RETAIL)
    assert not business_reply("Puedes consultar balances y registrar gastos.", BusinessVertical.BROKERAGE)
    assert business_reply("Puedes registrar ventas y consultar resultados del negocio.", BusinessVertical.RETAIL)
    assert business_reply("Puedes consultar tus balances, deudas y comisiones.", BusinessVertical.BROKERAGE)
    assert business_reply("Puedes registrar una compra o venta y consultar tus resultados.", BusinessVertical.BROKERAGE)
    assert not operational_reply("Soy *Bolos*, puedo ayudarte a registrar ventas.")
    assert operational_reply("¿Cuál fue el monto?")
    assert repeated_greeting_reply("¿Registramos algo o quieres revisar cómo va el día?")
    assert not repeated_greeting_reply("Soy Bolos, puedo ayudarte con ventas, gastos y compras.")


def test_request_limit_fallback_asks_for_a_business_task():
    assert "operación o consulta del negocio" in EXHAUSTED_TEXT


@pytest.mark.parametrize("question", ["¿Qué puedes hacer?", "qué puedes hacer", "que puedes hacer"])
def test_capability_questions_reach_the_business_agent(question):
    assert not is_menu_request(question)
    assert is_menu_request("menú")
