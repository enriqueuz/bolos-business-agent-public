"""Un registro simple se cierra con UNA sola llamada al modelo (ítem D1 de docs/evaluation.md).

Un turno de registro hacía dos: la primera pedía la tool y la segunda escribía un texto que `_compose`
descartaba, porque el recibo determinista ya lo contaba. Esa segunda llamada reenvía el prompt entero
(instrucciones + esquemas de las tools + contexto), así que era la mitad del costo del turno sin cambiar nada
de lo que ve el dueño.

Estos tests fijan las dos mitades del trato: que el registro simple se cierre en una llamada, y que el modelo
SIGA teniendo la palabra en todo lo demás (correcciones, consultas, confirmaciones, errores).
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import Expense, Sale
from tests.agent.test_pipeline import send

SALE = {"items": [{"product": "Bujía NGK", "quantity": 2, "unit_price": 3, "currency": "USD"}]}


async def test_simple_record_closes_with_one_model_call(env, script, business, session_factory):
    script.steps = [{"tool": "record_sale", "args": SALE}]  # sin segundo paso: si lo pidiera, el arnés falla
    container, _ = env
    out = await send(container, "vendí 2 bujías a 3$")

    assert out.status == "ok"
    assert script.calls == 1, "se le pidió al modelo un texto que el recibo ya cuenta"
    assert len(out.replies) == 1
    assert "Bujía NGK" in out.replies[0]["text"], "el recibo determinista sigue llegando igual"
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Sale))).scalar_one() == 1


async def test_several_simple_records_also_close_with_one_call(env, script, business, session_factory):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    def two_writes(messages, info):
        return ModelResponse(parts=[
            ToolCallPart("record_sale", SALE, tool_call_id="a"),
            ToolCallPart("record_expense", {"amount": 20, "currency": "USD", "category": "utilities",
                                            "description": "luz"}, tool_call_id="b"),
        ])

    script.steps = [two_writes]
    container, _ = env
    out = await send(container, "vendí 2 bujías a 3$ y pagué 20$ de luz")

    assert out.status == "ok" and script.calls == 1
    assert len(out.replies) == 1
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Sale))).scalar_one() == 1
        assert (await s.execute(select(func.count()).select_from(Expense))).scalar_one() == 1


async def test_a_query_still_gets_the_model_text(env, script, business):
    """Una consulta sin render propio no produce recibo: la respuesta ES el texto del modelo, y la segunda llamada va.

    `get_cash_status` ya no sirve de ejemplo: la caja tiene una presentación determinista candidata
    (ver test_the_cash_summary_is_rendered_by_the_system).
    """
    script.steps = [{"tool": "query_metrics", "args": {"metrics": ["sales_total"], "period": "today"}},
                    {"text": "Hoy no has vendido nada."}]
    container, _ = env
    out = await send(container, "¿cuánto vendí hoy?")

    assert out.status == "ok" and script.calls == 2
    assert "Hoy no has vendido nada." in out.replies[0]["text"]


async def test_the_cash_summary_is_rendered_by_the_system(env, script, business):
    """La caja es determinista: la arma `render_cash_summary` y el modelo no escribe una palabra.

    Que la redactara él produjo el "Cierre de ventas" inventado del 25-09-2026: cifras correctas, sacadas de
    las tools, pero sin `close_cash` detrás y sin `cash_session` en la base.
    """
    script.steps = [{"tool": "get_cash_status", "args": {}}, {"text": "La caja está sin cerrar."}]
    container, _ = env
    out = await send(container, "¿cómo va la caja?")

    assert out.status == "ok" and script.calls == 2
    assert "La caja está sin cerrar." not in out.replies[0]["text"]
    assert "Caja de hoy (sin cerrar)" in out.replies[0]["text"]


async def test_a_correction_still_gets_the_model_text(env, script, business, session_factory):
    """Tras corregir, el prompt le pide al modelo cosas que el recibo no dice (p. ej. por un saldo a favor)."""
    from tests.agent.harness import handle_in_quoted_section

    script.steps = [{"tool": "record_sale", "args": SALE}]
    container, _ = env
    first = await send(container, "vendí 2 bujías a 3$")
    quoted = first.replies[0]["provider_message_id"]
    script.calls = 0  # el contador es acumulativo; aquí solo interesa lo que gasta la corrección

    def correct(messages, info):
        from pydantic_ai.messages import ModelResponse, ToolCallPart
        handle = handle_in_quoted_section(info)
        return ModelResponse(parts=[ToolCallPart("edit_record", {"handle": handle, "changes": {"unit_price": 4}})])

    # Una pregunta es justo lo que se perdería si esto se cerrara solo: el prompt le pide al modelo, por
    # ejemplo, preguntar si el saldo a favor que quedó en otro cliente era del cliente nuevo.
    # (Sin cifras a propósito: con ellas el grounding pediría una revisión y serían tres llamadas.)
    script.steps = [correct, {"text": "¿El saldo a favor que quedó era de este cliente?"}]
    out = await send(container, "eran a 4", reply_to=quoted)

    assert out.status == "ok" and script.calls == 2, "una corrección no se cierra sola: el modelo puede tener que aclarar"
    assert "¿El saldo a favor que quedó era de este cliente?" in out.replies[0]["text"]


async def test_a_record_needing_confirmation_closes_with_its_buttons(env, script, business):
    """Con una confirmación pendiente el turno ya tiene respuesta: la pregunta y sus botones, sin texto del modelo."""
    script.steps = [{"tool": "create_receivable", "args": {"customer_name": "Moisés", "amount": 20, "currency": "USD"}}]
    container, _ = env
    out = await send(container, "Moisés me debe 20$")

    # Una llamada: la confirmación ya es respuesta determinista, así que pedirle texto al modelo sería pagar por
    # algo que `_compose` descarta igual (antes se pedía y se tiraba).
    assert out.status == "ok" and script.calls == 1
    assert "Moisés" in out.replies[0]["text"], "la pregunta de confirmación tiene que llegar"
    assert out.replies[0]["choices"], "y con sus botones Sí/No"


async def test_retail_cash_and_metrics_use_one_model_composition(env, script, business):
    answer = "La caja está sin cerrar y no hay ventas registradas hoy."
    script.steps = [
        {"tool": "get_cash_status"},
        {"tool": "query_metrics", "args": {"metrics": ["sales_total"], "period": "today"}},
        {"text": answer},
    ]
    container, _ = env
    out = await send(container, "Revisa caja y ventas hoy")
    assert out.status == "ok" and script.calls == 3
    assert out.replies[0]["text"] == answer


async def test_reading_an_already_closed_cash_uses_the_trusted_presentation(env, script, business):
    container, _ = env
    script.steps = [{"tool": "close_cash"}, {"text": "Listo."}]
    closed = await send(container, "Cierra la caja")
    assert closed.status == "ok" and "Cierre de caja" in closed.replies[0]["text"]
    script.steps = [{"tool": "get_cash_status"}, {"text": "La caja ya está cerrada."}]
    out = await send(container, "Muéstrame la caja")
    assert out.status == "ok"
    text = out.replies[0]["text"]
    assert "Cierre de caja" in text
    assert "No registré" not in text and "No se registró" not in text
    assert "La caja ya está cerrada." not in text
