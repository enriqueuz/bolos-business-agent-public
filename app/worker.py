"""Worker de arq: procesa turnos fuera del request del webhook + tareas periódicas.

Por qué cola: un turno agéntico tarda segundos; el webhook responde 200 en milisegundos y el proveedor no reintenta
(causa raíz del BUG-001 de Personal). Un lock por conversación garantiza que los mensajes de un chat se procesen en orden.

Este módulo es el ENTRYPOINT del worker como proceso separado (`arq app.worker.WorkerSettings`), para cuando el
tráfico justifique escalar API y worker por separado. `JOB_FUNCTIONS`/`CRON_JOBS` se reexportan para que
`app/inprocess_worker.py` (modo de un solo servicio) corra exactamente el mismo trabajo sin duplicar esta definición.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import httpx
from arq import Retry, cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.container import build_container
from app.domain.text import is_acknowledgement
from app.infra.observability.jobs import report_job_errors
from app.infra.redis.coordination import ConversationBusy, chat_lock_scope, conversation_lock
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


async def process_incident(ctx: dict[str, Any], incident: dict) -> None:
    # Do not wrap this job: failures in observability must never recurse.
    from app.infra.alerts.telegram import TelegramAlerter
    from app.infra.observability.processor import IncidentProcessor
    c = ctx["container"]
    if not c.settings.incidents_enabled:
        return
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            alerter = TelegramAlerter(c.settings.telegram_bot_token, c.settings.telegram_chat_id)
            await IncidentProcessor(c.settings, c.redis, alerter, client=client).run(incident, ctx.get("job_try", 1))
    except Retry:
        raise
    except Exception as exc:
        log.error("incident.worker_failed", incident_id=incident["id"], error=type(exc).__name__)
        if ctx.get("job_try", 1) < 5:
            raise Retry(defer=30) from exc
        await c.alerter.alert(f"🚨 Informe de incidente {incident['id']} no entregado. Revisar logs de Railway.")
        log.error("incident.delivery_exhausted", evidence=incident)



async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.is_production)
    ctx["container"] = build_container(settings)
    await ctx["container"].rates.refresh()


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["container"].close()


def _expects_a_message(inbound: Any) -> bool:
    """¿El turno va a responder con un mensaje? Un acuse ("ok", "gracias") o un sticker se acusan con una reacción 👍:
    prometer «escribiendo…» y acabar en un 👍 se lee como que Bolos se quedó a medias."""
    if inbound.type in ("reaction", "sticker"):
        return False
    return not (inbound.body and is_acknowledgement(inbound.body))


async def _read_receipt(container: Any, inbound: Any) -> None:
    """Visto azul y, si el turno va a escribir, «escribiendo…». Cosmético: cualquier fallo se registra y se ignora."""
    if not inbound.provider_message_id or inbound.type == "reaction":  # una reacción del dueño no se marca ni se responde
        return
    try:
        messaging = container.messaging.for_phone_number(inbound.business_phone_number_id)
        if _expects_a_message(inbound):
            await messaging.show_typing(inbound.provider_message_id)
        else:
            await messaging.mark_read(inbound.provider_message_id)
    except Exception:
        log.warning("worker.typing_failed", exc_info=True)


@report_job_errors
async def process_inbound(ctx: dict[str, Any], inbound_id: str) -> str:
    container = ctx["container"]
    inbound = await container.turns.get_inbound(UUID(inbound_id))
    if inbound is None:
        return "missing"
    try:
        async with conversation_lock(container.redis, chat_lock_scope(inbound.from_phone, inbound.from_bsuid), timeout_s=300):
            # «Escribiendo…» en paralelo al turno (no lo retrasa); se espera al final para no dejar la tarea suelta.
            typing = asyncio.create_task(_read_receipt(container, inbound))
            try:
                outcome = await container.pipeline.process(UUID(inbound_id))
                return outcome.status
            finally:
                await typing
    except ConversationBusy as exc:
        # otro turno del mismo chat está en curso: reintentar en breve preserva el orden
        raise Retry(defer=2) from exc


@report_job_errors
async def refresh_rates(ctx: dict[str, Any]) -> dict[str, str]:
    return await ctx["container"].rates.refresh()


@report_job_errors
async def purge_llm_payloads(ctx: dict[str, Any]) -> int:
    c = ctx["container"]
    return await c.turns.purge_llm_payloads(c.settings.llm_payload_retention_days)


@report_job_errors
async def purge_attachments(ctx: dict[str, Any]) -> int:
    return await ctx["container"].attachments.purge_due()


JOB_FUNCTIONS = [process_inbound, process_incident]
CRON_JOBS = [
    cron(refresh_rates, minute={5, 35}),
    cron(purge_llm_payloads, hour={7}, minute={15}),
    cron(purge_attachments, hour={7}, minute={30}),
]


class WorkerSettings:
    """Entrypoint para el proceso standalone del worker (construye su PROPIO Container)."""

    functions = JOB_FUNCTIONS
    cron_jobs = CRON_JOBS
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_tries = 150
    job_timeout = 240
    keep_result = 3600
