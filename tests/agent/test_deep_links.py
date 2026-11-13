"""El CTA al panel al pie del recibo: cuándo va, a dónde apunta y cuál de ellos lleva credencial.

Las tres reglas que se protegen:

1. Lo pone el SISTEMA con el id real del comando. El modelo no escribe URLs (`prompts/system.py`), así que un
   enlace que no salga de aquí es un enlace a un sitio que no existe.
2. Solo los turnos que escribieron algo. Donde hoy va un 👍 sigue yendo un 👍, y una consulta no lleva enlace.
3. Solo el PRIMER enlace del negocio lleva token. Los recibos se reenvían; una URL con credencial en cada
   recibo convierte un reenvío en entregar el negocio.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from app.db.models import Expense, Sale
from tests.agent.harness import handle_in_quoted_section
from tests.agent.test_buttons import CREDIT_SALE
from tests.agent.test_pipeline import onboard, send

SALE = {"tool": "record_sale", "args": {"items": [{"product": "bujía NGK", "quantity": 2, "unit_price": 3, "currency": "USD"}]}}
EXPENSE = {"tool": "record_expense", "args": {"description": "luz", "amount": 25, "currency": "USD", "category": "utilities"}}
LINK = re.compile(r"http://localhost:5173/ir/(\w+)(?:/([0-9a-f-]{36}))?(\?t=(\S+))?")


def link(out) -> re.Match[str] | None:
    return LINK.search("\n".join(r["text"] for r in out.replies))


async def first_sale(container, script):
    script.steps = [SALE]
    return await send(container, "vendí 2 bujías a 3$")


async def test_the_receipt_of_a_sale_links_to_that_sale_with_its_real_id(env, script, session_factory):
    container, _ = env
    await onboard(container, script)

    out = await first_sale(container, script)

    async with session_factory() as s:
        sale = (await s.execute(select(Sale))).scalar_one()
    found = link(out)
    assert found is not None, out.replies
    assert found.group(1) == "venta" and found.group(2) == str(sale.id)


async def test_the_first_link_of_the_business_carries_the_welcome_token_and_the_next_one_does_not(env, script, session_factory):
    """El token es la excepción, no la regla: un solo mensaje en la vida del negocio lo lleva."""
    container, _ = env
    await onboard(container, script)

    first = await first_sale(container, script)
    assert (found := link(first)) is not None and found.group(4), "el primer enlace debería entrar sin pedir código"
    assert "aparece organizado" in first.replies[0]["text"]  # la frase que enseña la relación entre los dos lados

    script.steps = [EXPENSE]
    second = await send(container, "pagué 25$ de luz")

    async with session_factory() as s:
        expense = (await s.execute(select(Expense))).scalar_one()
    assert (found := link(second)) is not None
    assert found.group(1) == "gasto" and found.group(2) == str(expense.id)
    assert found.group(4) is None, "un recibo de rutina no puede llevar credencial: se reenvían"
    assert "aparece organizado" not in second.replies[0]["text"]


async def test_a_turn_that_wrote_twice_links_to_the_list_instead_of_picking_one(env, script):
    """Con dos escrituras no hay "el" objeto que mostrar: el enlace lleva a la lista, no elige uno por su cuenta."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    def two_writes(messages, info):
        return ModelResponse(parts=[ToolCallPart(SALE["tool"], SALE["args"], tool_call_id="a"),
                                    ToolCallPart(EXPENSE["tool"], EXPENSE["args"], tool_call_id="b")])

    container, _ = env
    await onboard(container, script)
    script.steps = [two_writes]

    out = await send(container, "vendí 2 bujías a 3$ y pagué 25$ de luz")

    assert (found := link(out)) is not None
    assert found.group(1) == "movimientos" and found.group(2) is None


async def test_a_credit_sale_links_to_the_sale_and_not_to_the_list(env, script, session_factory):
    """Una venta fiada emite dos refs —la venta y el fiado— y sigue siendo UNA escritura.

    Regresión: contando refs en vez de escrituras, "Moisés se llevó un caucho fiado" mandaba a la lista de
    movimientos en vez de a la venta. Crear el cliente ya no exige confirmación (ADR 0016).
    """
    container, _ = env
    await onboard(container, script)
    script.steps = [CREDIT_SALE, {"text": "Ok."}]
    out = await send(container, "Moisés se llevó un caucho de 40$ fiado")

    async with session_factory() as s:
        sale = (await s.execute(select(Sale))).scalar_one()
    assert (found := link(out)) is not None
    assert found.group(1) == "venta" and found.group(2) == str(sale.id)


async def test_a_deletion_gets_no_link_to_what_no_longer_exists(env, script):
    """Regresión: el recibo del borrado enlazaba al registro borrado, que el panel ya no sirve (404)."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    container, _ = env
    await onboard(container, script)
    first = await first_sale(container, script)
    quoted = first.replies[0]["provider_message_id"]

    def delete(messages, info):
        return ModelResponse(parts=[ToolCallPart("delete_record", {"handle": handle_in_quoted_section(info)})])

    script.steps = [delete]
    await send(container, "bórrala", reply_to=quoted)
    out = await send(container, "sí")

    assert "Borré" in out.replies[0]["text"]
    assert link(out) is None


async def test_a_question_gets_no_link_because_nothing_was_written(env, script):
    container, _ = env
    await onboard(container, script)
    script.steps = [{"tool": "query_metrics", "args": {"metrics": ["sales_total"], "period": "today"}},
                    {"text": "Hoy no has vendido nada todavía."}]

    out = await send(container, "¿cuánto vendí hoy?")

    assert link(out) is None


async def test_a_plain_thanks_keeps_its_reaction_and_gains_no_link(env, script):
    """La regla del 👍 no se toca: el enlace solo existe donde ya había un mensaje."""
    container, console = env
    await onboard(container, script)
    script.steps = []

    out = await send(container, "ok, gracias")

    assert out.replies == [] and out.reactions == ["👍"]


async def test_a_turn_born_in_the_panel_gets_no_link_to_the_panel(env, script, session_factory):
    """El dueño que registra desde el panel ya está en el panel: ofrecerle el enlace sería absurdo."""
    from app.agent.pipeline import PANEL_PROVIDER

    container, _ = env
    await onboard(container, script)
    script.steps = [SALE]
    inbound_id = await container.turns.save_inbound(
        provider=PANEL_PROVIDER, provider_message_id="panel-1", business_id=None, from_phone="584140000001",
        contact_name=None, type="text", body="vendí 2 bujías a 3$", transcript=None, media_url=None,
        media_mime_type=None, reply_to_provider_message_id=None, interactive_reply_id=None, raw={"source": "panel"})

    out = await container.pipeline.process(inbound_id, deliver=False)

    assert "Venta registrada" in out.replies[0]["text"]
    assert link(out) is None


async def test_a_change_that_is_not_an_openable_record_gets_no_link(env, script):
    """Cambiar con qué tasa cobra el negocio no es un registro que se pueda abrir: mejor sin enlace que a una lista."""
    container, _ = env
    await onboard(container, script)
    # Cambiar un ajuste no es un registro simple: el pipeline le da al modelo una segunda vuelta para cerrar.
    script.steps = [{"tool": "set_charge_rate_mode", "args": {"mode": "market"}}, {"text": "Listo."}]

    out = await send(container, "de ahora en adelante cobro con la tasa de mercado")

    assert "tasa" in out.replies[0]["text"].lower()
    assert link(out) is None
