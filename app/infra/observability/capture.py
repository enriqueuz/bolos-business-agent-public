"""Explicit capture avoids duplicate SDK logging/ASGI/arq events and recursive incidents."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import traceback
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import sentry_sdk
from arq import create_pool
from arq.connections import RedisSettings

from app.infra.observability.privacy import Scrubber
from app.logging import get_logger

log = get_logger(__name__)


def make_scrubber(settings) -> Scrubber:
    secrets = [str(v) for k, v in settings.model_dump().items()
               if any(s in k for s in ("secret", "password", "token", "api_key", "database_url", "redis_url", "dsn")) and v]
    for number in settings.kapso_additional_numbers.values():
        secrets.extend([number.webhook_secret, number.api_key or ""])
    return Scrubber(secrets)


def configure_sentry(settings) -> None:
    if not settings.sentry_dsn:
        return
    scrubber = make_scrubber(settings)

    def before_send(event, hint):
        # No automatic request, SQL, LLM, logging or stack-local payloads.
        if not event.get("tags", {}).get("bolos_incident"):
            return None
        for key in ("request", "breadcrumbs", "extra", "user"):
            event.pop(key, None)
        for value in event.get("exception", {}).get("values", []):
            for frame in value.get("stacktrace", {}).get("frames", []):
                frame.pop("vars", None)
        return scrubber.clean(event)

    sentry_sdk.init(
        dsn=settings.sentry_dsn, environment=settings.incident_environment,
        release=settings.sentry_release or os.getenv("RAILWAY_GIT_COMMIT_SHA"),
        default_integrations=False, auto_enabling_integrations=False,
        send_default_pii=False, include_local_variables=False,
        max_request_body_size="never", traces_sample_rate=0.0,
        before_send=before_send, max_value_length=40000,
    )


def fingerprint(exc: Exception, stage: str) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    # No message, amounts, customer, line numbers or release in grouping key.
    frame = next((f for f in reversed(frames) if "/app/" in f.filename), frames[-1] if frames else None)
    location = (frame.filename.split("/app/")[-1] + ":" + frame.name) if frame else stage
    return hashlib.sha256(f"{type(exc).__module__}.{type(exc).__name__}:{location}:{stage}".encode()).hexdigest()[:24]


class SentryIncidentReporter:
    def __init__(self, settings, turns, alerter, *, pool_factory=create_pool):
        self.settings, self.turns, self.alerter = settings, turns, alerter
        self.pool_factory = pool_factory
        self.scrubber = make_scrubber(settings)

    async def report(self, exc: Exception, *, context: dict[str, Any]) -> None:
        if not self.settings.incidents_enabled:
            await self.alerter.alert(self.scrubber.text(
                f"🚨 Bolos Negocios — {context.get('stage', 'error')}\nTurno {context.get('turn_id', '—')}\n{type(exc).__name__}: {exc}"))
            return
        try:
            async with asyncio.timeout(8):
                await self._report(exc, context)
        except Exception as failure:
            # Never report the reporter through itself; never log exception URLs/secrets.
            log.error("incident.capture_failed", error=type(failure).__name__)
            try:
                async with asyncio.timeout(2):
                    await self.alerter.alert(self.scrubber.text(
                        f"🚨 Bolos · fallo capturando incidente\n{type(exc).__name__}: {exc}\nTurno {context.get('turn_id', '—')}\nRevisar logs de Railway."))
            except Exception:
                pass

    async def _report(self, exc, context):
        data = dict(context)
        if data.get("business_id") and data.get("turn_id"):
            try:
                async with asyncio.timeout(1):
                    from uuid import UUID
                    detail = await self.turns.turn_detail(UUID(str(data["business_id"])), UUID(str(data["turn_id"])))
                if detail:
                    data.setdefault("message", detail.get("user_text") or "")
                    # Do not copy prompts, binaries or entire provider responses.
                    data["turn"] = {k: v for k, v in detail.items() if k != "llm_calls"}
                    data["llm_calls"] = [{k: v for k, v in c.items() if k not in {"request", "response"}}
                                         for c in detail.get("llm_calls", [])]
            except Exception as failure:
                data["evidence_unavailable"] = type(failure).__name__
        key = fingerprint(exc, str(data.get("stage", "unknown")))
        incident = self.scrubber.clean({
            "id": uuid4().hex, "fingerprint": key, "environment": self.settings.incident_environment,
            "release": self.settings.sentry_release or os.getenv("RAILWAY_GIT_COMMIT_SHA", "unknown"),
            "occurred_at": datetime.now(UTC).isoformat(), "error_type": type(exc).__name__,
            "error": str(exc), "traceback": "".join(traceback.format_exception(exc)),
            "financial_state": "pending_verification", "context": data,
        })
        if self.settings.sentry_dsn:
            try:
                with sentry_sdk.new_scope() as scope:
                    scope.set_tag("bolos_incident", True)
                    scope.set_tag("turn_id", data.get("turn_id", "none"))
                    scope.set_tag("business_id", data.get("business_id", "none"))
                    scope.set_tag("stage", data.get("stage", "unknown"))
                    scope.fingerprint = [self.settings.incident_environment, key]
                    scope.set_context("incident", incident)
                    incident["sentry_event_id"] = sentry_sdk.capture_exception(exc)
            except Exception:
                log.error("incident.sentry_capture_failed")
        # Enqueue evidence, not merely an event ID: Sentry ingestion/quota failure must not lose it.
        try:
            pool = await self.pool_factory(RedisSettings.from_dsn(self.settings.redis_url))
            try:
                await pool.enqueue_job("process_incident", incident, _job_id=f"incident:{incident['id']}")
                notify = await pool.set(f"incident:initial:{incident['environment']}:{key}", "1", nx=True, ex=300)
            finally:
                await pool.aclose()
        except Exception as failure:
            log.error("incident.enqueue_failed", incident_id=incident["id"], error=type(failure).__name__,
                      evidence=json.dumps(incident, ensure_ascii=False))
            await self.alerter.alert(f"🚨 Bolos · {incident['environment']}\n{incident['error_type']}: {incident['error']}\n"
                                     f"Turno: {data.get('turn_id', '—')}\nEstado financiero: pendiente de verificar\n"
                                     "Cola de incidentes no disponible; evidencia en logs de Railway.")
            return
        # Worker sends the final report; initial notices are limited per fingerprint.
        if not notify:
            return
        await self.alerter.alert(
            f"🚨 Bolos · {incident['environment']}\n{incident['error_type']}: {incident['error'][:300]}\n"
            f"Negocio: {incident['context'].get('business_name', data.get('business_id', '—'))}\n"
            f"Usuario: {incident['context'].get('user_name', '—')} · {data.get('role', '—')}\n"
            f"Mensaje: {incident['context'].get('message', '')[:1200]}\nTurno: {data.get('turn_id', '—')}\n"
            "Estado financiero: pendiente de verificar\nInforme en preparación.")
