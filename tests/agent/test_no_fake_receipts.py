"""El modelo no puede afirmar que registró algo que no registró (incidente del 25-09-2026).

Ese día Moisés dictó 29 ventas y se guardaron 9: en 20 turnos el modelo respondió «✅ *Venta registrada* …»
**sin llamar `record_sale`**. El grounding numérico no lo vio porque los montos salían del propio mensaje del
dueño, y la memoria corta guardaba el recibo determinista como si lo hubiera escrito el modelo, así que a los
pocos turnos DeepSeek imitaba el patrón en vez de llamar la herramienta.

Estos tests fijan las cuatro defensas: el historial no miente, el turno sin herramienta se repara, un texto
con forma de recibo nunca sale, y una afirmación de escritura sin escritura no pasa el grounding.
"""

from __future__ import annotations

import pytest
from pydantic_ai.messages import TextPart, ToolCallPart, ToolReturnPart
from sqlalchemy import func, select

from app.agent import grounding
from app.agent.context_builder import history_from_window
from app.application.conversation import WindowEntry
from app.db.models import Sale
from tests.agent.test_pipeline import send

SALE = {"items": [{"product": "Antiespiche", "quantity": 1, "unit_price": 2, "currency": "USD"}]}
FAKE = "✅ *Venta registrada*\n• 1 × Antiespiche a $2,00 = $2,00"


# ---------------------------------------------------------------- el historial no miente

def test_history_replays_tool_calls_not_prose():
    """El recibo cuelga de SU tool call, no del texto del asistente: es lo que enseñaba a narrar en vez de actuar."""
    window = [WindowEntry(user="Vendí 2 aceites Mobil 13$ cada uno", assistant="",
                          actions=["sale created"],
                          calls=[{"tool": "record_sale", "args": {"items": []}, "result": FAKE}])]
    messages = history_from_window(window)

    call_parts = [p for m in messages for p in m.parts if isinstance(p, ToolCallPart)]
    return_parts = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
    assert [p.tool_name for p in call_parts] == ["record_sale"]
    assert FAKE in str(return_parts[0].content)

    texts = [p.content for m in messages for p in m.parts if isinstance(p, TextPart)]
    assert not any("Venta registrada" in t for t in texts), "el recibo no puede volver como texto del asistente"


def test_history_keeps_real_model_text():
    """Lo que el modelo SÍ escribió sigue en su sitio; si no escribió nada, no se inventa un turno suyo."""
    window = [WindowEntry(user="¿cuánto vendí?", assistant="Llevas $191,60 hoy.",
                          calls=[{"tool": "query_metrics", "args": {}, "result": '{"ok": true}'}])]
    texts = [p.content for m in history_from_window(window) for p in m.parts if isinstance(p, TextPart)]
    assert texts == ["Llevas $191,60 hoy."]


# ---------------------------------------------------------------- el turno sin herramienta se repara

async def test_fast_lane_without_tool_is_repaired(env, script, business, session_factory):
    """El carril rápido contesta prosa (el bug). El sistema lo detecta, repite el turno forzando la herramienta
    y la venta termina en la base."""
    script.lane = "fast"
    script.steps = [{"text": FAKE}, {"tool": "record_sale", "args": SALE}]
    container, _ = env
    out = await send(container, "Vendí un antiespiche 2$")

    assert out.status == "ok"
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Sale))).scalar_one() == 1, "la venta debe existir"
    assert "Antiespiche" in out.replies[0]["text"]
    assert out.replies[0]["text"].count("Venta registrada") == 1, "un solo recibo, el de verdad"


async def test_fake_receipt_is_never_delivered(env, script, business, session_factory):
    """Si ni el reintento llama la herramienta, el dueño recibe la verdad, nunca el recibo inventado."""
    script.lane = "fast"
    script.steps = [{"text": FAKE}, {"text": FAKE}]
    container, _ = env
    out = await send(container, "Vendí un antiespiche 2$")

    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Sale))).scalar_one() == 0
    body = out.replies[0]["text"]
    assert "Venta registrada" not in body, f"se le envió un recibo inventado: {body!r}"
    assert "No pude registrar" in body


async def test_a_question_without_tool_is_not_a_violation(env, script, business):
    """El escape legítimo sigue vivo: preguntar por un dato que falta no es fingir un registro."""
    script.lane = "fast"
    script.steps = [{"tool": "need_more_info", "args": {"question": "¿A cuánto vendiste el antiespiche?"}}]
    container, _ = env
    out = await send(container, "Vendí un antiespiche")

    assert script.calls == 1, "la pregunta la manda el sistema; no hace falta una segunda llamada"
    assert out.replies[0]["text"] == "¿A cuánto vendiste el antiespiche?"


# ---------------------------------------------------------------- grounding de acciones

def test_claiming_a_write_without_writing_fails_grounding():
    report = grounding.check("Listo, ya lo registré.", [], user_text="Vendí un antiespiche 2$", wrote_something=False,
                             claims_current_turn_write=True)
    assert not report.ok and report.claimed_write


def test_the_same_text_is_fine_when_something_was_written():
    report = grounding.check("Listo, ya lo registré.", [], user_text="Vendí un antiespiche 2$", wrote_something=True)
    assert report.ok and not report.claimed_write


def test_numbers_from_the_owner_no_longer_launder_a_fake_receipt():
    """El agujero exacto del incidente: el $2,00 venía del mensaje del dueño, así que el eje numérico lo aprobaba."""
    numeric_only = grounding.check(FAKE, [], user_text="Vendí un antiespiche 2$", wrote_something=True)
    assert numeric_only.ok, "las cifras sí están respaldadas: por eso hacía falta el segundo eje"
    with_claim = grounding.check(FAKE, [], user_text="Vendí un antiespiche 2$", wrote_something=False)
    assert not with_claim.ok and with_claim.claimed_write


def test_explaining_a_historical_record_is_not_claiming_a_new_write():
    report = grounding.check("La venta quedó registrada correctamente. Mi explicación anterior fue incorrecta.",
                             [{"customer": "Juan", "balance_usd": "-520.00"}], wrote_something=False)
    assert report.ok


async def test_pending_purchase_explanation_is_not_a_receipt(env, script, business, session_factory):
    from app.domain.enums import MemberRole
    from tests.agent.test_brokerage_vertical import _set_brokerage

    await _set_brokerage(session_factory, role=MemberRole.OWNER)
    script.steps = [{"text": "Compra pendiente significa que el cliente todavía debe entregar los dólares a la Casa."}]
    container, _ = env
    out = await send(container, "¿Qué significa compra pendiente?")
    assert out.status == "ok"
    assert out.replies[0]["text"].startswith("Compra pendiente significa")


@pytest.mark.parametrize("text", [
    "✅ *Venta pendiente registrada*\nCliente: Juan\nUSD 500",
    "Venta registrada: USD 500", "📒 *Compra pendiente*\nJuan USD 1000",
    "Precio de *Bujía*: USD 500", "Anotado: Juan USD 500",
])
def test_receipt_formats_remain_blocked_without_writes(text):
    assert grounding.check(text, [], user_text="Juan 500 1000", wrote_something=False).claimed_write


def test_balance_composition_bullets_are_not_receipt_headers():
    text = ("- Compra pendiente: −1000 (saldo −1000)\n- Compra pendiente: −20 (saldo −1020)\n"
            "- Venta pendiente (R1): +500 (saldo −520)\nLa venta quedó registrada correctamente.")
    assert grounding.check(text, [{"amounts": [1000, 20, 1020, 500, 520]}], wrote_something=False).ok
