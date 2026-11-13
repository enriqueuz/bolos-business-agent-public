"""Usuario con username de WhatsApp cuyo número nadie conoce: nunca silencio. Se le pide el contacto (sin LLM, un solo
botón), y al compartirlo se confirma y lo que mandó en espera se procesa normal."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.agent.pipeline import CONTACT_LINKED_TEXT, CONTACT_REQUEST_TEXT, GATE_TEXT
from app.db.models import InboundMessage, OutboundMessage

PHONE = "584140000001"
BSUID = "VE.13491208655302741918"


async def save(container, text: str | None, *, phone: str | None = None, type: str = "text") -> uuid.UUID:
    return await container.turns.save_inbound(
        provider="console", provider_message_id=f"in-{uuid.uuid4()}", from_phone=phone, from_bsuid=BSUID, contact_name="Papá",
        type=type, body=text, transcript=None, media_url=None, media_mime_type=None, reply_to_provider_message_id=None,
        interactive_reply_id=None, raw={})


async def test_unknown_bsuid_gets_a_contact_request_without_llm(env, script, business, session_factory):
    c, console = env
    out = await c.pipeline.process(await save(c, "Cafe 6000 pesos"))
    assert out.status == "ok" and script.calls == 0
    assert [(m.to_bsuid, m.to_phone, m.text) for m in console.sent] == [(BSUID, None, CONTACT_REQUEST_TEXT)]
    async with session_factory() as s:
        row = (await s.execute(select(OutboundMessage))).scalar_one()
        assert (row.to_bsuid, row.to_phone, row.status) == (BSUID, None, "sent")


async def test_only_one_contact_request_while_waiting(env, script, business):
    c, console = env
    for text in ("Cafe 6000 pesos", "José me debe 600", "hola?"):
        assert (await c.pipeline.process(await save(c, text))).status == "ok"
    assert len(console.sent) == 1 and script.calls == 0


async def test_failed_contact_request_alerts_and_is_retried(env, business, monkeypatch):
    from app.channels.types import SendResult

    c, console = env
    alerts: list[str] = []

    async def fail(bsuid, body):
        return SendResult(False, error="HTTP 400: recipient not supported")

    async def alert(text):
        alerts.append(text)

    monkeypatch.setattr(console, "send_contact_request", fail)
    monkeypatch.setattr(c.pipeline.alerter, "alert", alert)
    await c.pipeline.process(await save(c, "hola"))
    assert alerts and "recipient not supported" in alerts[0]
    monkeypatch.undo()
    await c.pipeline.process(await save(c, "hola otra vez"))  # el fallido no cuenta para la espera de 5 minutos
    assert [m.to_bsuid for m in console.sent] == [BSUID]


async def test_contact_shared_confirms_to_the_phone(env, script, business):
    c, console = env
    out = await c.pipeline.process(await save(c, None, phone=PHONE, type="contact_shared"))
    assert out.status == "ok" and script.calls == 0
    assert [(m.to_phone, m.text) for m in console.sent] == [(PHONE, CONTACT_LINKED_TEXT)]


async def test_contact_shared_without_access_gets_the_gate(env, script):
    c, console = env
    await c.pipeline.process(await save(c, None, phone="584149999999", type="contact_shared"))
    assert [(m.to_phone, m.text) for m in console.sent] == [("584149999999", GATE_TEXT)]


async def test_parked_message_is_processed_after_the_contact_is_shared(env, script, business, session_factory):
    c, console = env
    parked = await save(c, "hola")
    await c.pipeline.process(parked)  # pidió el contacto
    assert await c.turns.adopt_parked(BSUID, PHONE, within=timedelta(hours=24)) == [parked]
    script.steps = [{"text": "¡Hola! ¿Qué registramos?"}]
    out = await c.pipeline.process(parked)
    assert out.status == "ok" and script.calls == 1
    assert (console.sent[-1].to_phone, console.sent[-1].text) == (PHONE, "¡Hola! ¿Qué registramos?")


async def test_adopt_parked_skips_messages_whose_turn_has_not_started(env, business, session_factory):
    c, _ = env
    handled = await save(c, "uno")
    await c.pipeline.process(handled)
    pending = await save(c, "dos")  # su job aún no corrió: lo procesará él mismo, ya con teléfono
    assert await c.turns.adopt_parked(BSUID, PHONE, within=timedelta(hours=24)) == [handled]
    async with session_factory() as s:
        phones = dict((await s.execute(select(InboundMessage.id, InboundMessage.from_phone))).all())
    assert phones == {handled: PHONE, pending: PHONE}
    assert await c.turns.adopt_parked(BSUID, PHONE, within=timedelta(hours=24)) == []  # idempotente
