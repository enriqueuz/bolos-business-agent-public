"""Service-contract tests use injected clients, synthetic evidence and no production data."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from arq import Retry

from app.config import Settings
from app.infra.observability.capture import SentryIncidentReporter, configure_sentry, fingerprint
from app.infra.observability.http import IncidentMiddleware
from app.infra.observability.privacy import Scrubber
from app.infra.observability.processor import IncidentProcessor
from app.llm.incident_analyzer import DeepSeekIncidentAnalyzer


class MemoryRedis:
    def __init__(self):
        self.data = {}
        self.locked = False
        self.jobs = []

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, *, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def eval(self, script, numkeys, key, *args):
        self.data[key] = int(self.data.get(key, 0)) + 1
        return self.data[key]

    def lock(self, *args, **kwargs):
        redis = self

        class Lock:
            async def acquire(self, **kwargs):
                if redis.locked:
                    return False
                redis.locked = True
                return True

            async def release(self):
                redis.locked = False
        return Lock()

    async def enqueue_job(self, *args, **kwargs):
        self.jobs.append(args)

    async def aclose(self):
        pass


def settings(**kwargs):
    return Settings(database_url="postgresql+asyncpg://unused", redis_url="redis://unused", incidents_enabled=True,
                    github_token="github_pat_synthetic", deepseek_api_key="sk-synthetic", **kwargs)


def incident(event="a", **overrides):
    return {"id": event * 32, "fingerprint": "f" * 24, "environment": "staging", "release": "abc123",
            "occurred_at": "2026-10-30T17:00:00Z", "error_type": "RuntimeError", "error": "synthetic error",
            "traceback": "app/agent/pipeline.py\nRuntimeError: synthetic error",
            "financial_state": "pending_verification",
            "context": {"message": "Orlando 100$ a tasa 800", "stage": "pipeline", "user_name": "Carlos"}, **overrides}


ANALYSIS = {"summary": "Error sintético", "hypothesis": "Posible fallo; requiere código", "confidence": "low",
            "evidence": ["RuntimeError"], "impact": "Estado desconocido", "investigate": [], "regression_tests": []}


class Services:
    def __init__(self):
        self.issues = []
        self.comments = []
        self.analysis_calls = 0
        self.fail_analysis = False
        self.fail_github = False
        self.lose_issue_response = False
        self.sentry_pending = False
        self.private = True

    def handle(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if request.url.host == "api.deepseek.com":
            self.analysis_calls += 1
            if self.fail_analysis:
                return httpx.Response(503)
            assert body["thinking"] == {"type": "disabled"}
            assert body["response_format"] == {"type": "json_object"}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ANALYSIS)}}]})
        if request.url.host == "sentry.io":
            return httpx.Response(404 if self.sentry_pending else 200, json={"groupID": "123"})
        if self.fail_github:
            return httpx.Response(503)
        if path.endswith("/disabled"):
            return httpx.Response(200, json={"private": self.private})
        if path.endswith("/labels"):
            return httpx.Response(422, json={})
        if path.endswith("/comments"):
            if request.method == "POST":
                self.comments.append(body)
                return httpx.Response(201, json=body)
            return httpx.Response(200, json=self.comments)
        if path.endswith("/issues"):
            if request.method == "POST":
                self.issues.append({**body, "number": 1, "html_url": "https://github.com/test/issues/1", "state": "open"})
                if self.lose_issue_response:
                    self.lose_issue_response = False
                    raise httpx.ReadTimeout("ambiguous POST")
                return httpx.Response(201, json=self.issues[-1])
            return httpx.Response(200, json=self.issues)
        if path.endswith("/issues/1"):
            if request.method == "PATCH":
                self.issues[0].update(body)
            return httpx.Response(200, json=self.issues[0])
        raise AssertionError((request.method, path))


def processor(services, redis=None, config=None):
    alerter = SimpleNamespace(send=AsyncMock(return_value=True))
    p = IncidentProcessor(config or settings(), redis or MemoryRedis(), alerter,
                          client=httpx.AsyncClient(transport=httpx.MockTransport(services.handle)))
    return p


def test_scrub_preserves_financial_evidence():
    clean = Scrubber(["super-secret-value"]).clean({"message": "Orlando 100$ a tasa 800 tel: +584141234567 cuenta: 01020123456789012345 sk-hello super-secret-value",
                                                   "amount": "100.00", "exchange_rate": "800", "token": "never", "media_url": "never"})
    assert "Orlando 100$ a tasa 800" in clean["message"]
    for forbidden in ("584141234567", "01020123456789012345", "sk-hello", "super-secret-value"):
        assert forbidden not in clean["message"]
    assert clean["amount"] == "100.00" and clean["exchange_rate"] == "800"
    assert "token" not in clean and "media_url" not in clean


def test_grouping_does_not_use_money_or_message():
    assert fingerprint(ValueError("100"), "tool:buy") == fingerprint(ValueError("200"), "tool:buy")
    assert fingerprint(ValueError("100"), "tool:buy") != fingerprint(ValueError("100"), "tool:sell")


async def test_end_to_end_issue_and_telegram_without_sentry():
    services = Services()
    p = processor(services)
    await p.run(incident())
    assert len(services.issues) == 1 and services.analysis_calls == 1
    assert "Orlando 100$ a tasa 800" in services.issues[0]["body"]
    assert "RuntimeError" in services.issues[0]["body"] and "pending_verification" in services.issues[0]["body"]
    assert "hipótesis" in p.alerter.send.call_args.args[0] and "https://github.com/test/issues/1" in p.alerter.send.call_args.args[0]
    await p.run(incident())
    assert not services.comments and p.alerter.send.await_count == 1
    await p.client.aclose()


async def test_repeat_cache_loss_and_regression_reuse_issue():
    services = Services()
    p = processor(services)
    await p.run(incident())
    # Simulate Redis cache loss; durable marker discovery must recover the existing issue.
    p.redis.data.clear()
    services.issues[0]["state"] = "closed"
    await p.run(incident("b"))
    assert len(services.issues) == 1 and len(services.comments) == 1
    assert services.issues[0]["state"] == "open" and "Regresión" in services.comments[0]["body"]
    await p.run(incident("b"))
    assert len(services.comments) == 1
    await p.client.aclose()


async def test_deepseek_failure_still_creates_technical_issue():
    services = Services()
    services.fail_analysis = True
    p = processor(services)
    await p.run(incident())
    assert len(services.issues) == 1 and "unavailable" in services.issues[0]["body"]
    assert p.alerter.send.await_count == 1
    await p.client.aclose()


async def test_ambiguous_github_post_is_recovered_without_duplicate():
    services = Services()
    services.lose_issue_response = True
    p = processor(services)
    with pytest.raises(Retry):
        await p.run(incident())
    await p.run(incident(), attempt=2)
    assert len(services.issues) == 1 and services.analysis_calls == 1
    await p.client.aclose()


async def test_github_outage_still_alerts_and_retries():
    services = Services()
    services.fail_github = True
    p = processor(services)
    with pytest.raises(Retry):
        await p.run(incident())
    assert p.alerter.send.await_count == 1
    services.fail_github = False
    await p.run(incident(), attempt=2)
    assert len(services.issues) == 1
    await p.client.aclose()


async def test_telegram_failure_retries_without_new_issue_or_analysis():
    services = Services()
    p = processor(services)
    p.alerter.send.return_value = False
    with pytest.raises(Retry):
        await p.run(incident())
    p.alerter.send.return_value = True
    await p.run(incident(), attempt=2)
    assert len(services.issues) == services.analysis_calls == 1
    await p.client.aclose()


async def test_sentry_ingestion_delay_recovers_link():
    services = Services()
    services.sentry_pending = True
    p = processor(services, config=settings(sentry_org="bolos", sentry_project="agent", sentry_auth_token="synthetic"))
    event = incident(sentry_event_id="1" * 32)
    with pytest.raises(Retry):
        await p.run(event)
    services.sentry_pending = False
    await p.run(event, attempt=2)
    state = json.loads(await p.redis.get("incident:delivery:" + event["id"]))
    assert "/issues/123/" in state["sentry_url"]
    assert any("Evento Sentry:" in c["body"] for c in services.comments)
    assert "/issues/123/" in p.alerter.send.call_args.args[0]
    await p.client.aclose()


async def test_budget_disables_calls_but_not_reporting():
    services = Services()
    p = processor(services, config=settings(incident_analysis_daily_limit=0))
    await p.run(incident())
    assert services.analysis_calls == 0 and len(services.issues) == 1
    await p.client.aclose()


async def test_public_repository_rejected():
    services = Services()
    services.private = False
    p = processor(services)
    with pytest.raises(Retry):
        await p.run(incident())
    assert services.issues == []
    await p.client.aclose()


async def test_busy_group_is_retried():
    p = processor(Services())
    p.redis.locked = True
    with pytest.raises(Retry):
        await p.run(incident())
    await p.client.aclose()


async def test_collector_queues_context_and_filters_secrets():
    redis = MemoryRedis()
    alerts = SimpleNamespace(alert=AsyncMock())
    reporter = SentryIncidentReporter(settings(), None, alerts, pool_factory=AsyncMock(return_value=redis))
    await reporter.report(RuntimeError("sk-synthetic"), context={"message": "Orlando 100 a tasa 800", "stage": "test"})
    assert len(redis.jobs) == 1
    data = redis.jobs[0][1]
    assert "sk-synthetic" not in json.dumps(data) and "Orlando 100 a tasa 800" in json.dumps(data)
    assert alerts.alert.await_count == 1


async def test_collector_queue_failure_falls_back_to_alert():
    alerts = SimpleNamespace(alert=AsyncMock())
    reporter = SentryIncidentReporter(settings(), None, alerts, pool_factory=AsyncMock(side_effect=RuntimeError("Redis down")))
    await reporter.report(RuntimeError("original error"), context={"stage": "test"})
    assert "Cola de incidentes no disponible" in alerts.alert.call_args.args[0]


async def test_disabled_flag_preserves_basic_alerts():
    s = settings()
    s.incidents_enabled = False
    alerts = SimpleNamespace(alert=AsyncMock())
    factory = AsyncMock()
    reporter = SentryIncidentReporter(s, None, alerts, pool_factory=factory)
    await reporter.report(RuntimeError("original error"), context={"stage": "test"})
    assert alerts.alert.await_count == 1 and factory.await_count == 0


async def test_api_reporting_failure_preserves_original_error():
    original = RuntimeError("HTTP failure")
    async def failing(scope, receive, send):
        raise original
    container = SimpleNamespace(incidents=SimpleNamespace(report=AsyncMock(side_effect=ValueError("reporter failure"))))
    scope = {"type": "http", "method": "POST", "app": SimpleNamespace(state=SimpleNamespace(container=container))}
    with pytest.raises(RuntimeError) as caught:
        await IncidentMiddleware(failing)(scope, None, None)
    assert caught.value is original


async def test_analyzer_rejects_malformed_json():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]}))) as client:
        with pytest.raises(ValueError):
            await DeepSeekIncidentAnalyzer("synthetic").analyze(incident(), client)


def test_sentry_filter_excludes_request_breadcrumbs_and_locals():
    import sentry_sdk
    configure_sentry(settings(sentry_dsn="https://public@example.invalid/1"))
    callback = sentry_sdk.get_client().options["before_send"]
    assert callback({"message": "automatic log"}, {}) is None
    event = {"tags": {"bolos_incident": True}, "request": {"cookies": "secret"}, "extra": {"password": "secret"},
             "breadcrumbs": [{"data": "secret"}], "contexts": {"incident": {"message": "Orlando 100 a tasa 800"}},
             "exception": {"values": [{"value": "sk-synthetic", "stacktrace": {"frames": [{"vars": {"secret": "abc"}, "filename": "pipeline.py"}]}}]}}
    clean = callback(event, {})
    assert "request" not in clean and "breadcrumbs" not in clean and "extra" not in clean
    assert "vars" not in clean["exception"]["values"][0]["stacktrace"]["frames"][0]
    assert "sk-synthetic" not in json.dumps(clean) and "Orlando 100 a tasa 800" in json.dumps(clean)
    sentry_sdk.get_client().close()


async def test_real_pipeline_handler_reports_original_message_without_delivery():
    from uuid import uuid4

    from app.agent.pipeline import TurnPipeline
    turn_id, inbound_id = uuid4(), uuid4()
    failure = RuntimeError("synthetic identity resolution failure")
    turns = SimpleNamespace(start_turn=AsyncMock(return_value=turn_id), save_outbound=AsyncMock(return_value=uuid4()),
                            finish_turn=AsyncMock())
    reporter = SimpleNamespace(report=AsyncMock())
    pipeline = TurnPipeline(identity=SimpleNamespace(resolve=AsyncMock(side_effect=failure)), onboarding=None,
                            orchestrator=None, bus=None, queries=None, brokerage_queries=None, store=None,
                            turns=turns, messaging=None, transcriber=None, alerter=None, links=None,
                            attachments=None, incidents=reporter)
    inbound = SimpleNamespace(id=inbound_id, business_id=None, business_phone_number_id=None,
                              type="text", from_phone="584140000001", contact_name="Synthetic user",
                              body="Orlando 100$ a tasa 800", provider="panel")
    result = await pipeline._process(inbound_id, inbound, now=None, deliver=False)
    assert result.status == "error" and turns.finish_turn.await_count == 1
    assert reporter.report.call_args.args[0] is failure
    assert reporter.report.call_args.kwargs["context"]["message"] == "Orlando 100$ a tasa 800"
    assert turns.save_outbound.await_count == 1  # panel response persisted, no provider invoked


async def test_worker_reporter_failure_does_not_mask_business_exception():
    from app.infra.observability.jobs import report_job_errors

    failure = RuntimeError("original job failure")
    @report_job_errors
    async def job(ctx):
        raise failure
    ctx = {"container": SimpleNamespace(incidents=SimpleNamespace(report=AsyncMock(side_effect=ValueError("reporter error"))))}
    with pytest.raises(RuntimeError) as caught:
        await job(ctx)
    assert caught.value is failure
