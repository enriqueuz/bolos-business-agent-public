"""Webhook firmado → persistencia → cola real → worker/pipeline; todo en test y sin red saliente."""

import json
import os
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from arq import create_pool
from arq.connections import RedisSettings
from arq.worker import Worker
from sqlalchemy import func, select

from app.agent.pipeline import UNAVAILABLE_TEXT, UNSUPPORTED_TEXT
from app.channels.kapso.provider import KapsoProvider
from app.container import build_container
from app.db.models import (
    AgentTurn,
    BrokerMovement,
    Business,
    InboundMessage,
    LlmCall,
    OutboundMessage,
    ToolCall,
)
from app.domain.enums import BusinessVertical, OnboardingState
from app.main import create_app
from app.worker import process_inbound
from tests.agent.harness import FakeRates, Script, build_test_container
from tests.api.test_api import SECRET, kapso_event, post_signed

FIXTURE = Path(__file__).parents[1] / "fixtures" / "kapso_unavailable.json"


@pytest_asyncio.fixture
async def d06_env(redis, session_factory, business):
    script = Script(lane="fast")
    base, _ = build_test_container(script)
    settings, roles = base.settings, base.roles
    await base.close()
    requests = []

    def send(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": f"wamid.out.{len(requests)}"}]})

    provider = KapsoProvider(api_key="test", phone_number_id="123", webhook_secret=SECRET,
                             base_url="https://kapso.invalid", api_version="v24.0",
                             http=httpx.AsyncClient(transport=httpx.MockTransport(send)))
    container = build_container(settings, messaging=provider, roles=roles, rate_fetchers={"dolarapi_ve": FakeRates()})
    app = create_app(settings, container)
    app.state.container = container
    app.state.arq = await create_pool(RedisSettings.from_dsn(os.environ["REDIS_URL"]))
    async with session_factory() as s, s.begin():
        row = await s.get(Business, business.id)
        row.vertical = BusinessVertical.BROKERAGE.value
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, container, script, requests, app.state.arq
    await app.state.arq.aclose()
    await container.close()


async def drain(container, pool):
    worker = Worker([process_inbound], redis_pool=pool, burst=True, max_jobs=1, poll_delay=0.01,
                    ctx={"container": container}, handle_signals=False)
    try:
        await worker.async_run()
        assert worker.jobs_failed == 0
    finally:
        await worker.close()


@pytest.mark.parametrize("onboarded", [True, False])
async def test_131060_is_persisted_visible_deterministic_and_webhook_replay_has_no_reply(d06_env, session_factory, business, onboarded):
    client, container, script, requests, pool = d06_env
    if not onboarded:
        async with session_factory() as s, s.begin():
            row = await s.get(Business, business.id)
            row.onboarding_state = OnboardingState.IN_PROGRESS.value
    event = json.loads(FIXTURE.read_text())
    assert (await post_signed(client, event)).json()["queued"] == 1
    # Duplicado antes y después de procesar: no crea otro job ni otra respuesta.
    assert (await post_signed(client, event)).json()["queued"] == 0
    await drain(container, pool)
    assert (await post_signed(client, event)).json()["queued"] == 0
    async with session_factory() as s:
        inbound = (await s.execute(select(InboundMessage))).scalar_one()
        assert inbound.type == "unavailable" and "131060" in inbound.body and inbound.raw == event
        turn = (await s.execute(select(AgentTurn))).scalar_one()
        assert "message_unavailable" in turn.stages and turn.lane == "deterministic"
        for table in (BrokerMovement, ToolCall, LlmCall):
            assert (await s.scalar(select(func.count()).select_from(table))) == 0
        assert await s.scalar(select(func.count()).select_from(OutboundMessage)) == 1
    assert script.calls == 0 and script.onboarding_instructions == []
    texts = [r["text"]["body"] for r in requests if r.get("type") == "text"]
    assert texts == [UNAVAILABLE_TEXT] and UNSUPPORTED_TEXT not in texts
    activity = await container.turns.list_turns(business.id)
    assert activity[0]["message_type"] == "unavailable" and "131060" in activity[0]["user_text"]


async def test_other_unsupported_type_keeps_generic_response(d06_env, session_factory):
    client, container, script, requests, pool = d06_env
    event = json.loads(FIXTURE.read_text())
    event["message"]["errors"][0]["code"] = 131051
    event["message"]["kapso"]["content"] = "[Unsupported message - Error 131051]: Unknown message type."
    assert (await post_signed(client, event)).json()["queued"] == 1
    await drain(container, pool)
    assert [r["text"]["body"] for r in requests if r.get("type") == "text"] == [UNSUPPORTED_TEXT]
    assert script.calls == 0
    async with session_factory() as s:
        assert (await s.execute(select(InboundMessage.type))).scalar_one() == "unsupported"


async def test_valid_resend_and_four_successive_texts_are_processed_once(d06_env, session_factory):
    client, container, script, requests, pool = d06_env
    event = json.loads(FIXTURE.read_text())
    await post_signed(client, event)
    await drain(container, pool)
    script.steps = [{"tool": "record_broker_operation", "args": {"exchange_rate": "800", "operation_type": "sell", "operation_amount": amount, "text_money_sources": {"principal_usd": amount, 'exchange_rate': "800"}}}
                    for amount in ("601", "602", "603", "604")]
    # Incluye el reenvío válido; todos llegan antes de que el worker procese el lote.
    events = [kapso_event(f"wamid.valid.{i}", f"Vendí {amount}$ a tasa 800") for i, amount in enumerate((601, 602, 603, 604))]
    for item in events:
        assert (await post_signed(client, item)).json()["queued"] == 1
    await drain(container, pool)
    for item in [event, *events]:
        assert (await post_signed(client, item)).json()["queued"] == 0
    async with session_factory() as s:
        movements = (await s.execute(select(BrokerMovement))).scalars().all()
        assert sorted(m.operation_amount for m in movements) == list(map(Decimal, ("601", "602", "603", "604")))
        assert all(m.operation_type == "sell" and m.commission == 0 for m in movements)  # Casa, dueño
        assert await s.scalar(select(func.count()).select_from(InboundMessage)) == 5
        assert await s.scalar(select(func.count()).select_from(AgentTurn)) == 5
        assert await s.scalar(select(func.count()).select_from(OutboundMessage)) == 5
    # Los recibos válidos pueden incluir el botón existente «Deshacer» (interactive).
    assert len([r for r in requests if r.get("type") in {"text", "interactive"}]) == 5
