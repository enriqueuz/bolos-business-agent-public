"""Mensajes con botones y listas: forma exacta del payload de Kapso/Cloud API, límites y respaldo a texto."""

import httpx
import pytest

from app.application.choices import Choice, parse_button
from app.channels.kapso.provider import KapsoProvider, interactive_payload, parse_message_event
from app.channels.types import OutboundInteractive


def _choices(n: int, desc: bool = False) -> tuple[Choice, ...]:
    return tuple(Choice(f"o:tok:{i}", f"Opción número {i} con título largo", "detalle" if desc else None) for i in range(1, n + 1))


def test_up_to_three_options_are_reply_buttons_with_clipped_titles():
    body = interactive_payload("584140000001", OutboundInteractive("¿Lo hago?", _choices(3)))
    assert body["type"] == "interactive" and body["interactive"]["type"] == "button"
    buttons = body["interactive"]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["o:tok:1", "o:tok:2", "o:tok:3"]
    assert all(b["type"] == "reply" and len(b["reply"]["title"]) <= 20 for b in buttons)
    assert body["interactive"]["body"]["text"] == "¿Lo hago?"


def test_more_than_three_or_descriptions_become_a_list():
    body = interactive_payload("584140000001", OutboundInteractive("¿Cuál?", _choices(7)))
    assert body["interactive"]["type"] == "list"
    rows = body["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) == 7 and all(len(r["title"]) <= 24 for r in rows)
    assert len(body["interactive"]["action"]["button"]) <= 20
    assert OutboundInteractive("x", _choices(2, desc=True)).as_buttons is False  # con descripción → lista


def test_fits_only_in_a_single_message():
    assert OutboundInteractive("a" * 1024, _choices(2)).fits()
    assert not OutboundInteractive("a" * 1025, _choices(2)).fits()  # botones: cuerpo ≤ 1024
    assert OutboundInteractive("a" * 1024, _choices(5)).fits()  # lista: también ≤ 1024 en Kapso
    assert not OutboundInteractive("a" * 1025, _choices(5)).fits()
    assert not OutboundInteractive("¿?", _choices(11)).fits()


def test_text_version_enumerates_options():
    yes_no = OutboundInteractive("¿Lo hago?", (Choice("c:t:yes", "Sí"), Choice("c:t:no", "No")))
    assert yes_no.as_text().endswith("Responde *sí* o *no*.")
    text = OutboundInteractive("¿Cuál?", _choices(3)).as_text()
    assert "1. Opción número 1" in text and "Responde 1, 2 o 3." in text


def test_inbound_button_and_list_replies_keep_our_id():
    base = {"message": {"from": "584140000001", "id": "wamid.1", "type": "interactive"}}
    button = {**base, "message": {**base["message"], "interactive": {"type": "button_reply", "button_reply": {"id": "c:tok:yes", "title": "Sí"}}}}
    listed = {**base, "message": {**base["message"], "interactive": {"type": "list_reply", "list_reply": {"id": "o:tok:4", "title": "Ana"}}}}
    b, li = parse_message_event(button), parse_message_event(listed)
    assert (b.type, b.text, b.interactive_reply_id) == ("text", "Sí", "c:tok:yes")
    assert (li.text, li.interactive_reply_id) == ("Ana", "o:tok:4")


def test_quoted_reply_id_accepts_kapso_and_cloud_api_names():
    base = {"from": "584140000001", "id": "wamid.2", "type": "text", "text": {"body": "no, eran 3"}}
    assert parse_message_event({"message": {**base, "context": {"id": "wamid.1"}}}).reply_to_provider_message_id == "wamid.1"
    assert parse_message_event({"message": {**base, "context": {"message_id": "wamid.1"}}}).reply_to_provider_message_id == "wamid.1"
    assert parse_message_event({"message": {**base, "kapso": {"reply_to_message_id": "wamid.1"}}}).reply_to_provider_message_id == "wamid.1"


def test_button_ids_parse_and_reject_garbage():
    assert parse_button("c:abc:yes").kind == "c" and parse_button("u:abc").token == "abc"
    assert parse_button("o:abc:3").value == "3" and parse_button("onb:currency:local").token == "currency"
    for junk in (None, "", "hola", "c:abc", "u:", "x:y:z", "o::1"):
        assert parse_button(junk) is None


@pytest.mark.asyncio
async def test_interactive_rejected_by_whatsapp_falls_back_to_a_single_text_message():
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        sent.append(body)
        if body["type"] == "interactive":
            return httpx.Response(400, json={"error": {"message": "invalid parameter"}})
        return httpx.Response(200, json={"messages": [{"id": "wamid.ok"}]})

    provider = KapsoProvider(api_key="k", phone_number_id="1", webhook_secret=None, base_url="https://x", api_version="v24.0",
                             http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await provider.send_interactive("584140000001", OutboundInteractive("¿Lo hago?", (Choice("c:t:yes", "Sí"), Choice("c:t:no", "No"))))
    assert result.ok and result.provider_message_id == "wamid.ok"
    assert [b["type"] for b in sent] == ["interactive", "text"]  # el interactivo no llegó: solo UN mensaje entregado
    assert sent[1]["text"]["body"].endswith("Responde *sí* o *no*.")


# ------------------------------------------------------------------------------------------ reacciones y stickers
# Formas verificadas en docs.kapso.ai (2026-09-23): la reacción está documentada; el sticker entrante NO, así que se
# reconoce por el tipo de Meta o por el objeto `sticker`, y cualquier tipo desconocido queda en el log.


def _event(message: dict) -> dict:
    return {"message": {"id": "wamid.in", "from": "584140000001", **message}, "conversation": {"contact_name": "Enrique"}}


def test_a_reaction_carries_the_emoji_and_the_message_it_reacts_to():
    msg = parse_message_event(_event({"type": "reaction", "reaction": {"message_id": "wamid.de-bolos", "emoji": "👍"}}))
    assert msg is not None and msg.type == "reaction" and msg.text == "👍"
    assert msg.reply_to_provider_message_id == "wamid.de-bolos"  # sobre qué mensaje de Bolos reaccionó


def test_a_sticker_is_recognized_by_its_type_or_by_its_payload_never_as_an_image():
    """Si Kapso normalizara el sticker como media, el modelo intentaría leer un webp como si fuera un recibo."""
    by_type = parse_message_event(_event({"type": "sticker", "sticker": {"id": "1", "mime_type": "image/webp"}}))
    as_media = parse_message_event(_event({"type": "image", "sticker": {"id": "1", "mime_type": "image/webp"}}))
    assert by_type is not None and by_type.type == "sticker"
    assert as_media is not None and as_media.type == "sticker"


def test_an_unknown_type_still_falls_back_to_unsupported():
    msg = parse_message_event(_event({"type": "location", "location": {"latitude": 10.5, "longitude": -66.9}}))
    assert msg is not None and msg.type == "unsupported"
