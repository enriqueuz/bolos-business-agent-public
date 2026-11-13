"""La reacción acusa un toque sin gastar un mensaje: se envía bien y jamás rompe ni retrasa un turno."""

from __future__ import annotations

import json

import httpx
import pytest

from app.channels.kapso.provider import KapsoProvider


def _provider(handler) -> KapsoProvider:
    return KapsoProvider(api_key="k", phone_number_id="1", webhook_secret=None, base_url="https://x", api_version="v24.0",
                         http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_reaction_posts_the_emoji_over_the_owners_message():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})

    await _provider(handler).react("584140000000", "wamid.abc", "👍")
    assert len(seen) == 1
    assert seen[0].url.path == "/v24.0/1/messages"
    assert seen[0].headers["x-api-key"] == "k"
    assert json.loads(seen[0].content) == {"messaging_product": "whatsapp", "to": "584140000000", "type": "reaction",
                                           "reaction": {"message_id": "wamid.abc", "emoji": "👍"}}


@pytest.mark.asyncio
async def test_reaction_http_error_is_swallowed_and_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="(131047) re-engagement required")

    await _provider(handler).react("584140000000", "wamid.abc", "👍")  # no lanza
    assert calls == 1  # un solo intento: no se reintenta algo cosmético


@pytest.mark.asyncio
async def test_reaction_network_error_is_swallowed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    await _provider(handler).react("584140000000", "wamid.abc", "👍")  # no lanza
