"""Simulador de WhatsApp para desarrollo (deshabilitado en producción).

Corre el MISMO pipeline que el webhook, de forma síncrona, con el proveedor que esté configurado. Con
MESSAGING_PROVIDER=console las respuestas no salen a WhatsApp: se devuelven en la respuesta HTTP.
"""

from __future__ import annotations

import base64
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import get_container
from app.infra.redis.coordination import chat_lock_scope, conversation_lock

router = APIRouter(prefix="/api/dev", tags=["dev"])


class DevMessage(BaseModel):
    phone: str = Field(min_length=6, max_length=20)
    text: str | None = None
    contact_name: str | None = None
    reply_to_provider_message_id: str | None = None
    image_base64: str | None = None
    image_mime_type: str = "image/jpeg"
    button_id: str | None = Field(default=None, max_length=256)  # botón u opción tocada (como en WhatsApp)


@router.post("/chat")
async def dev_chat(body: DevMessage, request: Request) -> dict:
    container = get_container(request)
    if container.settings.is_production:
        raise HTTPException(404)
    media_url = None
    if body.image_base64:
        # Guardamos la imagen en Redis y la servimos desde /api/dev/media para reutilizar el mismo camino de descarga.
        key = f"devmedia:{uuid.uuid4()}"
        await container.redis.set(key, body.image_base64, ex=600)
        media_url = f"{container.settings.public_base_url}/api/dev/media/{key}"
    inbound_id = await container.turns.save_inbound(
        provider=container.messaging.name, provider_message_id=f"dev-{uuid.uuid4()}", from_phone="".join(c for c in body.phone if c.isdigit()),
        contact_name=body.contact_name, type="image" if body.image_base64 else "text", body=body.text, transcript=None,
        media_url=media_url, media_mime_type=body.image_mime_type if body.image_base64 else None,
        reply_to_provider_message_id=body.reply_to_provider_message_id, interactive_reply_id=body.button_id, raw={"dev": True},
    )
    async with conversation_lock(container.redis, chat_lock_scope(body.phone), timeout_s=180, wait_s=60):
        outcome = await container.pipeline.process(inbound_id)
    return {"turn_id": str(outcome.turn_id), "lane": outcome.lane, "status": outcome.status, "replies": outcome.replies}


@router.get("/media/{key}")
async def dev_media(key: str, request: Request):
    from fastapi.responses import Response

    container = get_container(request)
    if container.settings.is_production:
        raise HTTPException(404)
    if not key.startswith("devmedia:"):
        raise HTTPException(404)
    data = await container.redis.get(key)
    if not data:
        raise HTTPException(404)
    return Response(content=base64.b64decode(data), media_type="image/jpeg")
