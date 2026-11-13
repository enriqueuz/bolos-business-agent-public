"""El acuse con 👍: cuándo Bolos reacciona en vez de gastar un mensaje, y cuándo no puede hacerlo.

La regla de UX que se prueba aquí: la reacción significa UNA sola cosa, "recibido, no hace falta más". Si hay algo que
contarle al dueño —un recibo, una cifra, una duda— va como mensaje. El dueño no tiene que aprender ningún lenguaje de
reacciones: solo ve que Bolos le pone 👍 cuando ya no hay nada que decir.
"""

from __future__ import annotations

from app.db.models import Sale
from tests.agent.confirmations import ask_sale_customer_correction, sale_customer_name
from tests.agent.test_buttons import count
from tests.agent.test_pipeline import onboard, send

BUJIAS = {"tool": "record_sale", "args": {"items": [{"product": "bujía NGK", "quantity": 2, "unit_price": 3, "currency": "USD"}]}}


def emojis(console) -> list[str]:
    return [emoji for _, emoji in console.reactions]


# ------------------------------------------------------------------------------------------------ acuses del dueño


async def test_a_plain_thanks_is_answered_with_a_reaction_and_never_reaches_the_llm(env, script):
    container, console = env
    await onboard(container, script)
    script.steps = []  # cualquier llamada al modelo levanta NoLLM: este turno no puede costar ni un token
    calls, sent = script.calls, len(console.sent)

    out = await send(container, "Ok, gracias")

    assert out.replies == [] and len(console.sent) == sent  # ni un mensaje más en el hilo
    assert out.reactions == ["👍"] and emojis(console)[-1:] == ["👍"]
    assert script.calls == calls and out.lane == "deterministic"


async def test_an_ok_after_a_question_from_bolos_still_reaches_the_agent(env, script):
    """Si Bolos dejó una pregunta abierta, un «ok» no es un cierre: puede ser la respuesta."""
    container, console = env
    await onboard(container, script)
    script.steps = [{"text": "¿A cuánto lo vendiste?"}]
    await send(container, "vendí un caucho")

    script.steps = [{"text": "Dime el monto y lo registro."}]
    out = await send(container, "ok")

    assert out.reactions == [] and "Dime el monto" in out.replies[0]["text"]


async def test_an_ok_that_answers_a_pending_confirmation_records_and_replies_with_the_receipt(env, script, session_factory):
    """«ok» con una confirmación viva significa "sí, hazlo": el recibo es información, nunca un 👍."""
    container, console = env
    await onboard(container, script)
    await ask_sale_customer_correction(container, script)

    out = await send(container, "ok")

    assert "Corregí" in out.replies[0]["text"] and out.reactions == []
    assert await count(session_factory, Sale) == 1
    assert await sale_customer_name(session_factory) == "Pedro"


async def test_a_thanks_that_ignores_a_pending_confirmation_goes_to_the_agent(env, script, session_factory):
    """«gracias» no es sí ni no: la confirmación se descarta y hay que explicarlo, no acusarlo con 👍."""
    container, console = env
    await onboard(container, script)
    await ask_sale_customer_correction(container, script)

    script.steps = [{"text": "No corregí la venta de Moisés. Dime si la cambio."}]
    out = await send(container, "gracias")

    assert out.reactions == [] and "No corregí" in out.replies[0]["text"]
    assert await sale_customer_name(session_factory) == "Moisés"


async def test_a_typed_no_cancels_with_a_reaction_instead_of_a_message(env, script, session_factory):
    """El "no" escrito se acusa igual que el botón No: 👍 sobre su mensaje, sin gastar un mensaje."""
    container, console = env
    await onboard(container, script)
    await ask_sale_customer_correction(container, script)
    sent = len(console.sent)

    out = await send(container, "no")

    assert out.replies == [] and len(console.sent) == sent
    assert out.reactions == ["👍"] and await sale_customer_name(session_factory) == "Moisés"


# ------------------------------------------------------------------------------------------------ acuses del agente


async def test_the_agent_can_close_with_a_reaction_when_it_has_nothing_to_add(env, script):
    """"ok, mañana te paso las ventas" no es un acuse seco: lo resuelve el modelo, que pide la reacción con "👍"."""
    container, console = env
    await onboard(container, script)
    script.steps = [{"text": "👍"}]
    sent = len(console.sent)

    out = await send(container, "ok, mañana te paso las ventas de hoy")

    assert out.replies == [] and len(console.sent) == sent
    assert out.reactions == ["👍"] and script.calls == 1


async def test_a_thumbs_up_from_the_model_never_replaces_a_receipt(env, script, session_factory):
    """Si el turno registró algo, el 👍 del modelo no se envía ni como reacción ni como mensaje: manda el recibo."""
    container, console = env
    await onboard(container, script)
    script.lane = "fast"
    script.steps = [BUJIAS, {"text": "👍"}]

    out = await send(container, "vendí 2 bujías NGK a 3$")

    assert out.reactions == [] and "Venta registrada" in out.replies[0]["text"]
    assert await count(session_factory, Sale) == 1


async def test_a_quoted_correction_is_never_closed_with_a_reaction(env, script, session_factory):
    """Citar un mensaje es pedir un cambio: aunque el modelo se quede sin texto, el dueño merece una respuesta."""
    container, console = env
    await onboard(container, script)
    script.lane = "fast"
    script.steps = [BUJIAS, {"text": "Listo, registré la venta."}]
    sale = await send(container, "vendí 2 bujías NGK a 3$")

    script.lane = "agent"
    script.steps = [{"text": ""}]
    out = await send(container, "ok", reply_to=sale.replies[0]["provider_message_id"])

    assert out.reactions == [] and out.replies  # algo le responde, no un 👍 mudo


# ------------------------------------------------------------------------------------------------ stickers y reacciones


async def test_a_reaction_from_the_owner_is_ignored_in_silence(env, script):
    """Hoy esto respondía "Por ahora entiendo texto, notas de voz y fotos": un mensaje regalado por un gesto."""
    container, console = env
    await onboard(container, script)
    script.steps = []
    sent = len(console.sent)

    out = await send(container, "👍", kind="reaction", reply_to="wamid.de-bolos")

    assert out.replies == [] and out.reactions == [] and len(console.sent) == sent
    assert out.status == "ok"


async def test_a_sticker_is_acknowledged_with_a_reaction_unless_bolos_is_waiting(env, script, session_factory):
    container, console = env
    await onboard(container, script)
    script.steps = []

    out = await send(container, None, kind="sticker")
    assert out.replies == [] and out.reactions == ["👍"]

    await ask_sale_customer_correction(container, script)
    waiting = await send(container, None, kind="sticker")
    assert waiting.reactions == [] and "entiendo texto" in waiting.replies[0]["text"]

    yes = await send(container, "sí")  # el sticker no se llevó la confirmación por delante
    assert "Corregí" in yes.replies[0]["text"] and await sale_customer_name(session_factory) == "Pedro"
