"""El acuse de lectura es cosmético: se envía bien y jamás rompe ni retrasa un turno.

«Escribiendo…» solo si el turno va a escribir. Un acuse ("ok", "gracias") o un sticker se responden con una reacción
👍, y prometer que se está escribiendo para acabar en un 👍 se lee como que Bolos se quedó a medias.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app.channels.kapso.provider import KapsoProvider
from app.worker import _read_receipt


def _provider(handler) -> KapsoProvider:
    return KapsoProvider(api_key="k", phone_number_id="1", webhook_secret=None, base_url="https://x", api_version="v24.0",
                         http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_typing_marks_read_with_typing_indicator():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True})

    await _provider(handler).show_typing("wamid.abc")
    assert len(seen) == 1
    assert seen[0].url.path == "/v24.0/1/messages"
    assert seen[0].headers["x-api-key"] == "k"
    assert json.loads(seen[0].content) == {"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.abc",
                                           "typing_indicator": {"type": "text"}}


@pytest.mark.asyncio
async def test_typing_http_error_is_swallowed_and_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="boom")

    await _provider(handler).show_typing("wamid.abc")  # no lanza
    assert calls == 1  # un solo intento: no se reintenta algo cosmético


@pytest.mark.asyncio
async def test_typing_network_error_is_swallowed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    await _provider(handler).show_typing("wamid.abc")  # no lanza


@pytest.mark.asyncio
async def test_mark_read_is_the_same_receipt_without_the_indicator():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True})

    await _provider(handler).mark_read("wamid.abc")
    assert json.loads(seen[0].content) == {"messaging_product": "whatsapp", "status": "read", "message_id": "wamid.abc"}


class _Spy:
    def for_phone_number(self, phone_number_id):
        return self

    def __init__(self) -> None:
        self.typing: list[str] = []
        self.read: list[str] = []

    async def show_typing(self, provider_message_id: str) -> None:
        self.typing.append(provider_message_id)

    async def mark_read(self, provider_message_id: str) -> None:
        self.read.append(provider_message_id)


def _inbound(kind: str = "text", body: str | None = None, wamid: str | None = "wamid.abc") -> SimpleNamespace:
    return SimpleNamespace(type=kind, body=body, provider_message_id=wamid, business_phone_number_id=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("inbound", [_inbound(body="vendí 2 bujías a 3$"), _inbound(body=None), _inbound("image")])
async def test_a_turn_that_will_write_shows_the_indicator(inbound):
    spy = _Spy()
    await _read_receipt(SimpleNamespace(messaging=spy), inbound)
    assert spy.typing == ["wamid.abc"] and spy.read == []


@pytest.mark.asyncio
@pytest.mark.parametrize("inbound", [_inbound(body="ok gracias"), _inbound(body="👍"), _inbound("sticker")])
async def test_a_turn_that_will_only_react_just_gets_the_blue_ticks(inbound):
    spy = _Spy()
    await _read_receipt(SimpleNamespace(messaging=spy), inbound)
    assert spy.read == ["wamid.abc"] and spy.typing == []


@pytest.mark.asyncio
async def test_a_reaction_from_the_owner_is_not_even_marked_as_read():
    spy = _Spy()
    await _read_receipt(SimpleNamespace(messaging=spy), _inbound("reaction", body="👍"))
    await _read_receipt(SimpleNamespace(messaging=spy), _inbound(wamid=None))  # sin id no hay nada que marcar
    assert spy.typing == [] and spy.read == []


@pytest.mark.asyncio
async def test_worker_helper_ignores_provider_failures():
    calls: list[str] = []

    class Exploding(_Spy):
        async def show_typing(self, provider_message_id: str) -> None:
            calls.append(provider_message_id)
            raise RuntimeError("provider bug")

    await _read_receipt(SimpleNamespace(messaging=Exploding()), _inbound(body="vendí algo"))  # no lanza
    assert calls == ["wamid.abc"]
