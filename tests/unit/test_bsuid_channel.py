"""Usernames de WhatsApp (BSUID): el parser no descarta a quien llega sin número, solo un contacto verificado por
WhatsApp enlaza un número, y la consulta a los contactos de Kapso nunca rompe el webhook."""

from __future__ import annotations

import json

import httpx
import pytest

from app.channels.kapso.provider import KapsoProvider, parse_message_event
from app.infra.redis.coordination import chat_lock_scope

BSUID = "VE.13491208655302741918"


def bsuid_only(message: dict) -> dict:
    """Payload de Kapso v2 cuando Meta oculta el número (docs.kapso.ai/docs/whatsapp/business-scoped-user-ids)."""
    return {"message": {"id": "wamid.1", "timestamp": "1730092800", "from_user_id": BSUID, "username": "@papa",
                        "kapso": {"direction": "inbound"}, **message},
            "conversation": {"id": "conv_1", "contact_name": "Papá", "phone_number": None,
                             "business_scoped_user_id": BSUID, "username": "@papa"}}


def test_message_without_phone_keeps_the_bsuid():
    msg = parse_message_event(bsuid_only({"type": "text", "text": {"body": "Cafe 6000 pesos"}}))
    assert msg is not None and msg.from_phone is None and msg.from_bsuid == BSUID
    assert msg.type == "text" and msg.text == "Cafe 6000 pesos" and msg.contact_name == "Papá"


def test_bsuid_only_in_the_conversation_is_enough():
    event = bsuid_only({"type": "text", "text": {"body": "hola"}})
    del event["message"]["from_user_id"]
    assert parse_message_event(event).from_bsuid == BSUID


def test_phone_and_bsuid_together():
    event = bsuid_only({"type": "text", "text": {"body": "hola"}, "from": "+58 414-000-0001"})
    msg = parse_message_event(event)
    assert (msg.from_phone, msg.from_bsuid) == ("584140000001", BSUID)


def test_message_without_phone_nor_bsuid_is_dropped():
    assert parse_message_event({"message": {"id": "w", "type": "text", "text": {"body": "x"}}, "conversation": {}}) is None


def test_contact_request_reply_brings_the_verified_phone():
    msg = parse_message_event(bsuid_only({"type": "contacts", "contacts": [
        {"origin": "contact_request", "phones": [{"phone": "+58 414-000-0001", "wa_id": "584140000001", "type": "CELL"}]}]}))
    assert msg.type == "contact_shared" and msg.from_phone == "584140000001" and msg.from_bsuid == BSUID


def test_a_plain_contact_card_never_links_a_phone():
    # Cualquiera puede compartir la tarjeta del dueño: sin origin=contact_request no es su número verificado.
    msg = parse_message_event(bsuid_only({"type": "contacts", "contacts": [
        {"name": {"formatted_name": "Dueño"}, "phones": [{"phone": "+58 414-000-0001", "wa_id": "584140000001"}]}]}))
    assert msg.type == "unsupported" and msg.from_phone is None


def test_lock_scope_by_bsuid_never_collides_with_a_phone():
    assert chat_lock_scope("+58 414-000-0001") == "chat:584140000001"
    assert chat_lock_scope(None, BSUID) == f"chat:bsuid:{BSUID}"
    assert chat_lock_scope("584140000001", BSUID) == "chat:584140000001"  # con teléfono, manda el teléfono


def _provider(handler) -> KapsoProvider:
    return KapsoProvider(api_key="k", phone_number_id="1", webhook_secret=None, base_url="https://x", api_version="v24.0",
                         platform_base_url="https://p/platform/v1", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_contact_request_goes_to_the_bsuid():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.req"}]})

    result = await _provider(handler).send_contact_request(BSUID, "Comparte tu número")
    assert result.ok and result.provider_message_id == "wamid.req"
    body = json.loads(seen[0].content)
    assert "to" not in body and body["recipient"] == BSUID and body["recipient_type"] == "individual"
    assert body["interactive"] == {"type": "request_contact_info", "body": {"text": "Comparte tu número"},
                                   "action": {"name": "request_contact_info"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "payload", "expected"), [
    (200, {"data": {"wa_id": "584140000001", "business_scoped_user_id": BSUID}}, "584140000001"),
    (200, {"wa_id": "+58 414 000 0001"}, "584140000001"),
    (200, {"data": {"wa_id": None, "business_scoped_user_id": BSUID}}, None),
    (404, {"error": "not found"}, None),
    (409, {"error": "ambiguous"}, None),
    (401, {"error": "unauthorized"}, None),
])
async def test_lookup_phone_in_kapso_contacts(status, payload, expected):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=payload)

    assert await _provider(handler).lookup_phone(BSUID) == expected
    assert seen[0].method == "GET" and seen[0].url.path == f"/platform/v1/whatsapp/contacts/{BSUID}"
    assert seen[0].headers["x-api-key"] == "k"


@pytest.mark.asyncio
async def test_lookup_phone_never_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("lento")

    assert await _provider(handler).lookup_phone(BSUID) is None
