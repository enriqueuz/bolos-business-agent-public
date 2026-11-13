"""Signed webhooks → real Postgres/Redis → worker → the same receiving WhatsApp number."""

import asyncio
import json
import os

import httpx
import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from sqlalchemy import select

from app.channels.kapso.provider import KapsoProvider
from app.container import build_container
from app.db.models import InboundMessage, OutboundMessage
from app.main import create_app
from tests.agent.harness import FakeRates, Script, build_test_container
from tests.api.test_api import kapso_event, post_signed
from tests.api.test_unavailable_messages import drain

KEYS = {"123": "primary-key", "456": "backup-key"}
SECRETS = {"123": "primary-secret", "456": "backup-secret"}


def event(account, mid, **message):
    payload = kapso_event(mid, "mensaje")
    payload["phone_number_id"] = account
    payload["conversation"]["phone_number_id"] = account
    payload["message"].update(message or {"type": "unsupported"})
    return payload


@pytest_asyncio.fixture
async def multi_env(redis, session_factory, business):
    base, _ = build_test_container(Script())
    settings, roles = base.settings, base.roles
    await base.close()
    requests = []

    async def send(request):
        await asyncio.sleep(0)
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(200, json={"messages": [{"id": f"wamid.out.{len(requests)}"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(send)) as http:
        provider = KapsoProvider(api_key=KEYS["123"], phone_number_id="123", webhook_secret=SECRETS["123"],
                                 additional_numbers={"456": (KEYS["456"], SECRETS["456"])},
                                 base_url="https://kapso.invalid", api_version="v24.0", http=http)
        c = build_container(settings, messaging=provider, roles=roles, rate_fetchers={"dolarapi_ve": FakeRates()})
        app = create_app(settings, c)
        app.state.container = c
        app.state.arq = await create_pool(RedisSettings.from_dsn(os.environ["REDIS_URL"]))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, c, requests, app.state.arq
        await app.state.arq.aclose()
        await c.close()


async def test_two_numbers_persist_their_route_and_reply_through_the_worker(multi_env, session_factory):
    client, c, requests, pool = multi_env
    for account in KEYS:
        payload = event(account, f"wamid.in.{account}")
        assert (await post_signed(client, payload, secret=SECRETS[account])).json()["queued"] == 1
        assert (await post_signed(client, payload, secret=SECRETS[account])).json()["queued"] == 0
    await drain(c, pool)
    async with session_factory() as s:
        incoming = (await s.scalars(select(InboundMessage))).all()
        outgoing = (await s.scalars(select(OutboundMessage))).all()
    assert {m.business_phone_number_id for m in incoming} == set(KEYS)
    assert {m.business_phone_number_id for m in outgoing} == set(KEYS)
    texts = [r for r in requests if json.loads(r.content).get("type") == "text"]
    assert len(texts) == 2
    for request in requests:
        account = request.url.path.split("/")[-2]
        assert request.headers["x-api-key"] == KEYS[account]
        body = json.loads(request.content)
        if body.get("message_id"):
            assert body["message_id"] == f"wamid.in.{account}"
    assert {r.url.path.split("/")[-2] for r in texts} == set(KEYS)


async def test_same_account_batch_keeps_the_backup_route(multi_env, session_factory):
    client, c, requests, pool = multi_env
    payload = {"batch": True, "data": [event("456", "wamid.b1"), event("456", "wamid.b2")]}
    response = await post_signed(client, payload, secret=SECRETS["456"], headers={"x-webhook-batch": "true"})
    assert response.json()["queued"] == 2
    await drain(c, pool)
    assert requests and all("/456/messages" in r.url.path for r in requests)
    async with session_factory() as s:
        assert {m.business_phone_number_id for m in await s.scalars(select(OutboundMessage))} == {"456"}


@pytest.mark.parametrize(("account", "secret"), [("123", "backup-secret"), ("456", "primary-secret"),
                                                   ("999", "primary-secret")])
async def test_spoofed_number_is_never_enqueued(multi_env, session_factory, account, secret):
    client, _, requests, _ = multi_env
    response = await post_signed(client, event(account, "wamid.spoof"), secret=secret)
    assert response.status_code == 401 and requests == []
    async with session_factory() as s:
        assert (await s.scalars(select(InboundMessage))).all() == []


async def test_contact_request_cooldown_does_not_hide_the_request_on_the_backup(multi_env):
    client, c, requests, pool = multi_env
    for account in KEYS:
        payload = event(account, f"wamid.bsuid.{account}", type="text", from_user_id="VE.test")
        payload["message"].pop("from")
        payload["conversation"].update(phone_number=None, business_scoped_user_id="VE.test")
        assert (await post_signed(client, payload, secret=SECRETS[account])).json()["queued"] == 1
    await drain(c, pool)
    prompts = [r for r in requests if r.method == "POST" and
               json.loads(r.content).get("interactive", {}).get("type") == "request_contact_info"]
    assert len(prompts) == 2
    assert {r.url.path.split("/")[-2] for r in prompts} == set(KEYS)


async def test_concurrent_turns_do_not_mutate_the_shared_pipeline_provider(multi_env):
    client, c, requests, _ = multi_env
    # Acceso denegado: termina con una respuesta determinista, sin estado de conversación ni LLM.
    for account in KEYS:
        payload = event(account, f"wamid.concurrent.{account}", type="text", **{"from": f"584140000{account}"})
        assert (await post_signed(client, payload, secret=SECRETS[account])).json()["queued"] == 1
    async with c.session_factory() as s:
        incoming = (await s.scalars(select(InboundMessage))).all()
    await asyncio.gather(*(c.pipeline.process(m.id) for m in incoming))
    for request in requests:
        recipient = json.loads(request.content)["to"]
        assert request.url.path.endswith(f"/{recipient[-3:]}/messages")
    assert len(requests) == 2
    assert c.pipeline.messaging is c.messaging
