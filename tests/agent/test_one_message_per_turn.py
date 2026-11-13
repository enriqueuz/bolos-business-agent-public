"""Un turno que registra varias cosas sale en UN SOLO mensaje (ítem B2 de docs/evaluation.md).

Cada mensaje saliente cuesta dinero (ver docs/evaluation.md), así que la regla es un mensaje por turno. Esto es
además el prerrequisito del debounce (ítem A1): si el dueño manda una ráfaga y se agrupa en un turno, pero
Bolos responde un mensaje por registro, el debounce no ahorraría nada.

Lo que se verifica aquí NO es una optimización nueva sino una garantía del comportamiento actual: que
`_compose` concatene todos los recibos y `_send` entregue un único mensaje, incluso cuando el cuerpo pasa el
límite de 1024 caracteres de un interactivo y hay que caer a texto plano.
"""

from __future__ import annotations

from pydantic_ai.messages import ModelResponse, ToolCallPart
from sqlalchemy import func, select

from app.db.models import Expense, Sale
from tests.agent.test_pipeline import send

WHATSAPP_MAX = 3900  # el mismo tope que app/agent/pipeline.py
INTERACTIVE_MAX = 1024  # límite del cuerpo de un mensaje interactivo de Kapso


def _sale(product: str, quantity: int, price: int) -> dict:
    return {"items": [{"product": product, "quantity": quantity, "unit_price": price, "currency": "USD"}]}


async def test_three_records_in_one_turn_send_a_single_message(env, script, business, session_factory):
    """Dos ventas y un gasto en un mismo turno → 1 mensaje con los 3 recibos."""

    def three_writes(messages, info):
        return ModelResponse(parts=[
            ToolCallPart("record_sale", _sale("Bujía NGK", 2, 3), tool_call_id="a"),
            ToolCallPart("record_sale", _sale("Aceite 20w50", 1, 5), tool_call_id="b"),
            ToolCallPart("record_expense", {"amount": 20, "currency": "USD", "category": "utilities",
                                            "description": "luz"}, tool_call_id="c"),
        ])

    script.steps = [three_writes, {"text": "Listo"}]
    container, _ = env
    out = await send(container, "vendí 2 bujías a 3$, un aceite a 5$ y pagué 20$ de luz")

    assert out.status == "ok"
    assert len(out.replies) == 1, f"se enviaron {len(out.replies)} mensajes: {[r['text'][:60] for r in out.replies]}"

    body = out.replies[0]["text"]
    assert "Bujía NGK" in body and "Aceite 20w50" in body and "luz" in body

    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Sale))).scalar_one() == 2
        assert (await s.execute(select(func.count()).select_from(Expense))).scalar_one() == 1


async def test_many_records_still_send_a_single_message(env, script, business, session_factory):
    """Una ráfaga larga (12 ventas) supera los 1024 caracteres de un interactivo y aun así sale en 1 mensaje.

    Es el caso al que lleva el debounce: al agrupar varios mensajes del dueño en un turno, el cuerpo crece.
    Por encima de 1024 se pierden los botones (Deshacer) y se cae a texto, pero debe seguir siendo un
    único mensaje mientras no pase de 3.900 caracteres.
    """
    # Nombres de UNA palabra y sin raíces compartidas: `resolve_product` usa `word_similarity` y manda a la
    # zona gris (WEAK=0.34) cualquier par que comparta una palabra — "Pastillas de freno" y "Manilla de freno"
    # chocarían, y una venta quedaría sin registrar por ambigüedad en vez de por lo que prueba este test.
    products = ["Bujia", "Aceite", "Cadena", "Filtro", "Espejo", "Farola", "Bateria",
                "Llanta", "Camara", "Guaya", "Pinon", "Corona", "Radiador", "Manubrio"]

    def many_writes(messages, info):
        return ModelResponse(parts=[
            ToolCallPart("record_sale", _sale(name, 1, 10 + n), tool_call_id=f"t{n}")
            for n, name in enumerate(products)
        ])

    script.steps = [many_writes, {"text": "Listo"}]
    container, _ = env
    out = await send(container, "vendí " + ", ".join(products))

    assert out.status == "ok"
    assert len(out.replies) == 1, f"se enviaron {len(out.replies)} mensajes: {[r['text'][:60] for r in out.replies]}"

    body = out.replies[0]["text"]
    assert len(body) > INTERACTIVE_MAX, (
        f"el cuerpo mide {len(body)} caracteres y no llega a superar {INTERACTIVE_MAX}: el test ya no prueba "
        "la caída a texto plano, hay que subir el número de ventas"
    )
    assert len(body) <= WHATSAPP_MAX
    assert all(name in body for name in products)
    # Sin botones: no cabe en un interactivo, así que el Deshacer no se ofrece (pero el mensaje sigue siendo uno)
    assert out.replies[0]["choices"] == []

    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Sale))).scalar_one() == len(products)
