"""Webhook de WhatsApp: verificar firma → parsear → deduplicar → resolver remitente → persistir → encolar → 200.

Nunca procesa el turno dentro del request. Siempre responde 200 a payloads válidos (incluidos duplicados) para que
el proveedor no reintente.

Remitente sin número (usuario con username de WhatsApp): el BSUID se traduce a teléfono aquí, antes de persistir, para
que el lock del worker y el pipeline sigan trabajando por teléfono. Si nadie lo conoce, el mensaje queda en espera
(from_phone NULL) y el pipeline le pide el contacto; al compartirlo, se reprocesan los mensajes en espera.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.api.deps import get_container
from app.channels.types import InboundMessage
from app.infra.redis.coordination import claim_once
from app.logging import get_logger

log = get_logger(__name__)
router = APIRouter(tags=["webhooks"])

PARKED_REPLAY_WINDOW = timedelta(hours=24)


async def resolve_sender(container: Any, msg: InboundMessage) -> None:
    """Completa `msg.from_phone` desde el BSUID: payload → nuestra tabla → contactos del proveedor. Y aprende el enlace
    cada vez que llegan juntos."""
    if not msg.from_bsuid:
        return
    if msg.from_phone:
        await container.identity.remember_bsuid(msg.from_bsuid, msg.from_phone,
                                                "contact_request" if msg.type == "contact_shared" else "webhook")
        return
    if phone := await container.identity.phone_for_bsuid(msg.from_bsuid):
        msg.from_phone = phone
        log.info("webhook.sender_resolved", via="table", provider_message_id=msg.provider_message_id)
        return
    if phone := await container.messaging.for_phone_number(msg.business_phone_number_id).lookup_phone(msg.from_bsuid):
        msg.from_phone = phone
        await container.identity.remember_bsuid(msg.from_bsuid, phone, "kapso")
        log.info("webhook.sender_resolved", via="provider", provider_message_id=msg.provider_message_id)
        return
    log.info("webhook.sender_unknown", bsuid=msg.from_bsuid, provider_message_id=msg.provider_message_id)


async def has_access(container: Any, phone: str) -> bool:
    return container.identity.can_signup(phone) or await container.identity.member_by_phone(phone) is not None


@router.post("/webhooks/whatsapp")
async def whatsapp_webhook(request: Request) -> dict:
    container = get_container(request)
    provider = container.messaging
    raw = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not provider.verify_signature(raw, headers):
        log.warning("webhook.bad_signature")
        raise HTTPException(status_code=401, detail="invalid signature")
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")

    try:
        messages = provider.parse_webhook(payload, headers)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid messaging account") from exc
    log.info("webhook.received", webhook_event=headers.get("x-webhook-event"), idempotency_key=headers.get("x-idempotency-key"),
             batch=headers.get("x-webhook-batch") == "true", parsed=len(messages))
    queued = 0
    for msg in messages:
        dedup_key = f"{provider.name}:{msg.provider_message_id}"
        if msg.provider_message_id and await container.redis.exists(f"dedup:{dedup_key}"):
            log.info("webhook.duplicate", provider_message_id=msg.provider_message_id)
            continue
        await resolve_sender(container, msg)
        inbound_id = await container.turns.save_inbound(
            provider=provider.name, provider_message_id=msg.provider_message_id, from_phone=msg.from_phone,
            business_phone_number_id=msg.business_phone_number_id,
            from_bsuid=msg.from_bsuid, contact_name=msg.contact_name, type=msg.type, body=msg.text, transcript=msg.transcript,
            media_url=msg.media_url, media_mime_type=msg.media_mime_type, reply_to_provider_message_id=msg.reply_to_provider_message_id,
            interactive_reply_id=msg.interactive_reply_id, raw=msg.raw,
        )
        if inbound_id is None and msg.provider_message_id:
            inbound_id = await container.turns.unstarted_inbound(provider.name, msg.provider_message_id)
        if inbound_id is None:
            continue
        await request.app.state.arq.enqueue_job("process_inbound", str(inbound_id), _job_id=f"inbound:{inbound_id}")
        if msg.type == "contact_shared" and msg.from_bsuid and msg.from_phone and await has_access(container, msg.from_phone):
            # Después del «✅ listo», lo que mandó mientras esperaba. Otro job id: arq rechaza uno repetido mientras
            # guarde el resultado del turno anterior (el que pidió el contacto). Sin acceso no se reprocesa: sería un
            # aviso de acceso privado por cada mensaje en espera.
            parked = await container.turns.adopt_parked(msg.from_bsuid, msg.from_phone, within=PARKED_REPLAY_WINDOW)
            for parked_id in parked:
                await request.app.state.arq.enqueue_job("process_inbound", str(parked_id), _job_id=f"inbound:{parked_id}:replay")
            if parked:
                log.info("webhook.parked_replayed", count=len(parked))
        if msg.provider_message_id:
            await claim_once(container.redis, dedup_key)
        queued += 1
    return {"status": "ok", "queued": queued}
