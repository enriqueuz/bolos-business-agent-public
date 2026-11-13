"""Adapter de Kapso (proxy de WhatsApp Cloud API).

Referencias (docs.kapso.ai, webhooks v2):
- Headers: X-Webhook-Event, X-Webhook-Signature (HMAC-SHA256 hex del body CRUDO), X-Idempotency-Key,
  X-Webhook-Batch / X-Batch-Size cuando el envío viene en lote.
- Evento: whatsapp.message.received → {message, conversation, is_new_conversation, phone_number_id}
- Notas de voz: Kapso adjunta la transcripción en message.kapso.transcript.text
- Media: message.kapso.media_url es descargable sin token de Meta
- Respuestas citadas: el esquema de Kapso nombra `context.message_id`; Cloud API usa `context.id`. Se aceptan los dos
  (both quoted-message protocol variants are supported)
- Botones y listas: message.type = "interactive" con interactive.button_reply / list_reply = {id, title}
- Envío: POST {base}/{version}/{phone_number_id}/messages → {"messages": [{"id": "wamid..."}]}
- Usernames (BSUID): si Meta oculta el número, llega message.from_user_id / conversation.business_scoped_user_id y
  conversation.phone_number = null. Con `recipient` se envía al BSUID; `request_contact_info` pide el número y vuelve
  como type="contacts" con contacts[0].origin = "contact_request" y el número verificado en phones[0].wa_id.
  Contactos de Kapso (Platform API): GET /whatsapp/contacts/{bsuid} → {wa_id, ...}; 409 si el BSUID es ambiguo.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import socket
from collections.abc import Mapping
from copy import copy
from typing import Any
from urllib.parse import quote

import httpx

from app.channels.types import InboundMessage, OutboundInteractive, SendResult
from app.logging import get_logger

log = get_logger(__name__)

SEND_TIMEOUT_S = 15.0
TYPING_TIMEOUT_S = 4.0
LOOKUP_TIMEOUT_S = 3.0
MAX_ATTEMPTS = 3
MAX_MEDIA_BYTES = 16 * 1024 * 1024
MESSAGE_UNAVAILABLE_CODE = 131060  # Código de protocolo de WhatsApp; no es una configuración del negocio.


async def validate_media_url(url: httpx.URL) -> None:
    if url.scheme != "https" or not url.host or url.username or url.password or url.port not in (None, 443):
        raise ValueError("URL de archivo no permitida")
    addresses = await asyncio.to_thread(socket.getaddrinfo, url.host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(info[4][0]).is_global for info in addresses):
        raise ValueError("El archivo no puede apuntar a una dirección privada")


def compute_signature(secret: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def _digits(value: Any) -> str | None:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits or None


def _shared_phone(message: dict[str, Any]) -> str | None:
    """Número que WhatsApp verificó al tocar «compartir contacto». Una tarjeta de contacto cualquiera (vCard) NO
    cuenta: cualquiera podría compartir el contacto del dueño y hacerse pasar por él."""
    for contact in message.get("contacts") or []:
        if contact.get("origin") == "contact_request":
            for phone in contact.get("phones") or []:
                if found := _digits(phone.get("wa_id") or phone.get("phone")):
                    return found
    return None


def parse_message_event(event: dict[str, Any]) -> InboundMessage | None:
    message = event.get("message") or {}
    kapso = message.get("kapso") or {}
    if kapso.get("direction") == "outbound":
        return None
    conversation = event.get("conversation") or {}
    sender = _digits(message.get("from") or conversation.get("phone_number"))
    bsuid = message.get("from_user_id") or conversation.get("business_scoped_user_id") or None
    if not sender and not bsuid:
        # Sin teléfono ni BSUID no hay a quién responder. Se registra la forma del payload (nunca el contenido).
        log.warning("kapso.message_without_identity", message_id=message.get("id"), msg_type=message.get("type"),
                    message_keys=sorted(message), conversation_keys=sorted(conversation))
        return None
    msg_type = message.get("type")
    context = message.get("context") or {}
    common = dict(
        provider="kapso",
        from_phone=sender,
        from_bsuid=bsuid,
        provider_message_id=message.get("id"),
        contact_name=conversation.get("contact_name"),
        reply_to_provider_message_id=context.get("id") or context.get("message_id") or kapso.get("reply_to_message_id"),
        raw=event,
        business_phone_number_id=str(event["phone_number_id"]) if event.get("phone_number_id") else None,
    )
    # Error de recepción, no de formato. El código estructurado tiene prioridad sobre el tipo declarado.
    # Kapso también puede representar el error como el placeholder observado en QA; solo se reconoce dentro
    # de unsupported, nunca buscando números/palabras en un texto normal del usuario.
    errors = message.get("errors") or []
    unavailable = any(isinstance(error, dict) and str(error.get("code")) == str(MESSAGE_UNAVAILABLE_CODE) for error in errors)
    content = kapso.get("content")
    placeholder = (msg_type == "unsupported" and isinstance(content, str)
                   and content.startswith(f"[Unsupported message - Error {MESSAGE_UNAVAILABLE_CODE}]:"))
    if unavailable or placeholder:
        log.warning("kapso.message_unavailable", error_code=MESSAGE_UNAVAILABLE_CODE, provider_message_id=message.get("id"),
                    phone_number_id=event.get("phone_number_id"),
                    source="message.errors" if unavailable else "kapso.content")
        return InboundMessage(type="unavailable", text=f"Mensaje no disponible · WhatsApp {MESSAGE_UNAVAILABLE_CODE}", **common)
    if msg_type == "contacts" and (shared := _shared_phone(message)):
        return InboundMessage(type="contact_shared", **{**common, "from_phone": shared})
    if msg_type == "text":
        return InboundMessage(type="text", text=(message.get("text") or {}).get("body"), **common)
    if msg_type == "audio":
        transcript = (kapso.get("transcript") or {}).get("text")
        return InboundMessage(type="voice", transcript=transcript, media_url=kapso.get("media_url"),
                              media_mime_type=(message.get("audio") or {}).get("mime_type"), **common)
    if msg_type == "reaction":  # el dueño reaccionó a un mensaje de Bolos: no es una instrucción (ver TurnPipeline)
        reaction = message.get("reaction") or {}
        return InboundMessage(type="reaction", **{**common, "text": reaction.get("emoji") or None,
                                                  "reply_to_provider_message_id": (reaction.get("message_id")
                                                                                   or common["reply_to_provider_message_id"])})
    # Un sticker es un acuse, no un dato: el pipeline lo acusa con 👍 si no espera nada. Kapso NO documenta el entrante
    # (verificado el 2026-09-23 en docs.kapso.ai): Meta manda type="sticker", pero si Kapso lo normalizara como media,
    # el objeto `sticker` sigue ahí y hay que reconocerlo ANTES que la imagen, o el modelo intentaría leerlo como recibo.
    if msg_type == "sticker" or message.get("sticker"):
        return InboundMessage(type="sticker", **common)
    if msg_type == "image":
        image = message.get("image") or {}
        return InboundMessage(type="image", text=image.get("caption") or (kapso.get("message_type_data") or {}).get("caption"),
                              media_url=kapso.get("media_url") or image.get("link") or image.get("url"),
                              media_mime_type=image.get("mime_type") or "image/jpeg", **common)
    if msg_type == "interactive":
        reply = (message.get("interactive") or {})
        picked = reply.get("button_reply") or reply.get("list_reply") or {}
        return InboundMessage(type="text", text=picked.get("title"), interactive_reply_id=picked.get("id"), **common)
    if msg_type == "button":  # botón de una plantilla (no los usamos, pero se entiende como texto)
        button = message.get("button") or {}
        return InboundMessage(type="text", text=button.get("text"), interactive_reply_id=button.get("payload"), **common)
    # Nunca el contenido, solo la forma: así un tipo no documentado (o nuevo) se ve en los logs sin filtrar nada del dueño.
    log.info("kapso.unsupported_message_type", msg_type=msg_type, message_keys=sorted(message))
    return InboundMessage(type="unsupported", text=kapso.get("content"), **common)


def interactive_payload(to_phone: str, message: OutboundInteractive) -> dict[str, Any]:
    choices = message.clipped()
    if message.as_buttons:
        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": message.body},
            "action": {"buttons": [{"type": "reply", "reply": {"id": c.id, "title": c.title}} for c in choices]},
        }
    else:
        rows = [{"id": c.id, "title": c.title, **({"description": c.description} if c.description else {})} for c in choices]
        interactive = {
            "type": "list",
            "body": {"text": message.body},
            "action": {"button": message.list_button[:20], "sections": [{"title": "Opciones", "rows": rows}]},
        }
    return {"messaging_product": "whatsapp", "to": to_phone, "type": "interactive", "interactive": interactive}


class KapsoProvider:
    name = "kapso"

    def __init__(self, *, api_key: str, phone_number_id: str, webhook_secret: str | None, base_url: str, api_version: str,
                 platform_base_url: str = "https://api.kapso.ai/platform/v1", http: httpx.AsyncClient | None = None,
                 additional_numbers: Mapping[str, tuple[str, str]] | None = None) -> None:
        self._api_key = api_key
        self._secret = webhook_secret
        self._phone_number_id = phone_number_id
        self._messages_base = f"{base_url.rstrip('/')}/{api_version}"
        self._url = f"{self._messages_base}/{phone_number_id}/messages"
        self._phone_numbers = {phone_number_id: (api_key, webhook_secret), **(additional_numbers or {})}
        self._contacts_url = f"{platform_base_url.rstrip('/')}/whatsapp/contacts"
        self._http = http or httpx.AsyncClient(timeout=SEND_TIMEOUT_S, follow_redirects=True)

    def for_phone_number(self, phone_number_id: str | None) -> KapsoProvider:
        if phone_number_id is None or phone_number_id == self._phone_number_id:
            return self
        if phone_number_id not in self._phone_numbers:
            raise ValueError("Número de WhatsApp no configurado")
        bound = copy(self)
        bound._phone_number_id = phone_number_id
        bound._api_key, bound._secret = self._phone_numbers[phone_number_id]
        bound._url = f"{self._messages_base}/{phone_number_id}/messages"
        return bound

    def verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        if len(self._phone_numbers) > 1:
            # El id aún no es confiable: solo selecciona el secreto. Se autentican body completo Y cuenta.
            # Un lote de Kapso pertenece a un solo número; no aceptar ids mezclados ni fallback a la principal.
            try:
                payload = json.loads(raw_body)
                events = payload.get("data", []) if payload.get("batch") or headers.get("x-webhook-batch") == "true" else [payload]
                ids = {str(event["phone_number_id"]) for event in events}
                if len(ids) != 1:
                    return False
                account = self.for_phone_number(ids.pop())
            except (ValueError, KeyError, TypeError, AttributeError):
                return False
            if not account._secret:
                return False
            received = headers.get("x-webhook-signature", "")
            return hmac.compare_digest(compute_signature(account._secret, raw_body), received.strip())
        if not self._secret:
            log.warning("kapso.webhook_secret_missing_signature_not_verified")
            return True
        received = headers.get("x-webhook-signature", "")
        return hmac.compare_digest(compute_signature(self._secret, raw_body), received.strip())

    def parse_webhook(self, payload: dict[str, Any], headers: Mapping[str, str]) -> list[InboundMessage]:
        event_type = headers.get("x-webhook-event", "whatsapp.message.received")
        if event_type != "whatsapp.message.received":
            return []
        events = payload.get("data", []) if payload.get("batch") or headers.get("x-webhook-batch") == "true" else [payload]
        messages = [m for e in events if (m := parse_message_event(e)) is not None]
        for message in messages:
            self.for_phone_number(message.business_phone_number_id)  # rechazar cuentas desconocidas también con un solo número
            message.business_phone_number_id = message.business_phone_number_id or self._phone_number_id
        return messages

    async def send_text(self, to_phone: str, text: str) -> SendResult:
        return await self._post({"messaging_product": "whatsapp", "to": to_phone, "type": "text",
                                 "text": {"body": text, "preview_url": False}})

    async def send_image(self, to_phone: str, image_url: str, caption: str) -> SendResult:
        return await self._post({
            "messaging_product": "whatsapp",
            "to": to_phone,
            "type": "image",
            "image": {"link": image_url, "caption": caption},
        })

    async def send_interactive(self, to_phone: str, message: OutboundInteractive) -> SendResult:
        result = await self._post(interactive_payload(to_phone, message))
        if not result.ok and result.error and result.error.startswith("HTTP 4"):
            # Rechazo permanente del formato: el mensaje NO llegó, así que mandarlo como texto sigue siendo uno solo.
            log.warning("kapso.interactive_rejected_fallback_text", error=result.error)
            return await self.send_text(to_phone, message.as_text())
        return result

    async def send_contact_request(self, bsuid: str, body: str) -> SendResult:
        return await self._post({"messaging_product": "whatsapp", "recipient_type": "individual", "recipient": bsuid,
                                 "type": "interactive",
                                 "interactive": {"type": "request_contact_info", "body": {"text": body},
                                                 "action": {"name": "request_contact_info"}}})

    async def lookup_phone(self, bsuid: str) -> str | None:
        """Kapso concilia teléfono y BSUID en sus contactos. Un intento corto: si falla, el usuario comparte su contacto."""
        try:
            resp = await self._http.get(f"{self._contacts_url}/{quote(bsuid, safe='')}", headers={"X-API-Key": self._api_key},
                                        timeout=LOOKUP_TIMEOUT_S)
        except httpx.HTTPError as exc:
            log.warning("kapso.contact_lookup", outcome="error", error=type(exc).__name__)
            return None
        if resp.status_code != 200:
            log.info("kapso.contact_lookup", outcome="not_found" if resp.status_code == 404 else "failed",
                     status=resp.status_code)
            return None
        try:
            data = resp.json()
        except ValueError:
            log.warning("kapso.contact_lookup", outcome="bad_json")
            return None
        contact = data.get("data", data) if isinstance(data, dict) else {}
        phone = _digits(contact.get("wa_id")) if isinstance(contact, dict) else None
        log.info("kapso.contact_lookup", outcome="found" if phone else "no_phone")
        return phone

    async def _post(self, body: dict[str, Any]) -> SendResult:
        headers = {"X-API-Key": self._api_key, "Content-Type": "application/json"}
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = await self._http.post(self._url, json=body, headers=headers)
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    return SendResult(False, error=f"HTTP {resp.status_code}: {resp.text[:300]}")  # permanente
                resp.raise_for_status()
                data = resp.json()
                ids = [m.get("id") for m in data.get("messages", []) if m.get("id")]
                return SendResult(True, provider_message_id=ids[0] if ids else None)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("kapso.send_retry", attempt=attempt + 1, error=last_error)
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(2**attempt)
        return SendResult(False, error=last_error)

    async def show_typing(self, provider_message_id: str) -> None:
        """Marca el mensaje entrante como leído y muestra «escribiendo…» (WhatsApp lo quita al responder o a los ~25 s).

        Es cosmético: un solo intento, sin reintentos ni excepciones, para no retrasar ni tumbar el turno.
        """
        await self._read_receipt(provider_message_id, typing=True)

    async def mark_read(self, provider_message_id: str) -> None:
        """Visto azul sin «escribiendo…»: el turno se va a acusar con una reacción, no con un mensaje.

        Prometer «escribiendo…» y acabar en una reacción se lee como que Bolos se quedó a medias.
        """
        await self._read_receipt(provider_message_id, typing=False)

    async def _read_receipt(self, provider_message_id: str, *, typing: bool) -> None:
        body: dict[str, Any] = {"messaging_product": "whatsapp", "status": "read", "message_id": provider_message_id}
        if typing:
            body["typing_indicator"] = {"type": "text"}
        try:
            resp = await self._http.post(self._url, json=body, headers={"X-API-Key": self._api_key}, timeout=TYPING_TIMEOUT_S)
            if resp.status_code >= 400:
                log.warning("kapso.typing_failed", status=resp.status_code, typing=typing, body=resp.text[:200])
        except httpx.HTTPError as exc:
            log.warning("kapso.typing_failed", typing=typing, error=f"{type(exc).__name__}: {exc}")

    async def react(self, to_phone: str, provider_message_id: str, emoji: str) -> None:
        """Reacciona a un mensaje del dueño. Acusa recibo sin gastar un mensaje en el hilo.

        Cosmético como show_typing: un intento, sin reintentos ni excepciones. Una reacción solo dispara
        el webhook de `sent` (nunca delivered/read), así que no esperamos confirmación de entrega.
        """
        body = {"messaging_product": "whatsapp", "to": to_phone, "type": "reaction",
                "reaction": {"message_id": provider_message_id, "emoji": emoji}}
        try:
            resp = await self._http.post(self._url, json=body, headers={"X-API-Key": self._api_key}, timeout=TYPING_TIMEOUT_S)
            if resp.status_code >= 400:
                log.warning("kapso.reaction_failed", status=resp.status_code, body=resp.text[:200])
        except httpx.HTTPError as exc:
            log.warning("kapso.reaction_failed", error=f"{type(exc).__name__}: {exc}")

    async def download_media(self, url: str) -> tuple[bytes, str | None]:
        target = httpx.URL(url)
        api = httpx.URL(self._url)
        for _ in range(5):
            await validate_media_url(target)
            # Las URLs de almacenamiento son directas; solo nuestra API recibe la credencial.
            headers = {"X-API-Key": self._api_key} if (target.scheme, target.host, target.port) == (api.scheme, api.host, api.port) else {}
            async with self._http.stream("GET", target, headers=headers, follow_redirects=False) as resp:
                if resp.is_redirect and "location" in resp.headers:
                    target = target.join(resp.headers["location"])
                    continue
                resp.raise_for_status()
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_MEDIA_BYTES:
                        raise ValueError("El archivo supera los 16 MB")
                return bytes(data), resp.headers.get("content-type")
        raise ValueError("Demasiadas redirecciones al descargar el archivo")
