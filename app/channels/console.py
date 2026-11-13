"""Proveedor de consola: para desarrollo local y tests. Guarda lo enviado en memoria y lo loguea."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.channels.types import InboundMessage, OutboundInteractive, SendResult
from app.logging import get_logger

log = get_logger(__name__)


@dataclass
class SentMessage:
    to_phone: str | None
    text: str
    provider_message_id: str
    interactive: OutboundInteractive | None = None
    to_bsuid: str | None = None  # solo en la solicitud de contacto
    image_url: str | None = None


@dataclass
class ConsoleProvider:
    name: str = "console"
    sent: list[SentMessage] = field(default_factory=list)
    reactions: list[tuple[str, str]] = field(default_factory=list)  # (provider_message_id, emoji)
    typing: list[str] = field(default_factory=list)  # mensajes acusados con «escribiendo…»
    read: list[str] = field(default_factory=list)  # mensajes marcados solo como leídos
    known_contacts: dict[str, str] = field(default_factory=dict)  # BSUID → teléfono (simula los contactos de Kapso)

    def for_phone_number(self, phone_number_id: str | None) -> ConsoleProvider:
        return self

    def verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool:
        return True

    def parse_webhook(self, payload: dict[str, Any], headers: Mapping[str, str]) -> list[InboundMessage]:
        return [InboundMessage(provider=self.name, from_phone=str(payload["from"]) if payload.get("from") else None,
                               from_bsuid=payload.get("bsuid"), type=payload.get("type", "text"),
                               provider_message_id=payload.get("id") or f"console-{uuid.uuid4()}", text=payload.get("text"),
                               reply_to_provider_message_id=payload.get("reply_to"), contact_name=payload.get("contact_name"),
                               interactive_reply_id=payload.get("button_id"), raw=payload)]

    async def send_text(self, to_phone: str, text: str) -> SendResult:
        mid = f"console-out-{uuid.uuid4()}"
        self.sent.append(SentMessage(to_phone, text, mid))
        log.info("console.send", to=to_phone, text=text)
        return SendResult(True, provider_message_id=mid)

    async def send_image(self, to_phone: str, image_url: str, caption: str) -> SendResult:
        mid = f"console-out-{uuid.uuid4()}"
        self.sent.append(SentMessage(to_phone, caption, mid, image_url=image_url))
        log.info("console.send_image", to=to_phone, caption=caption)
        return SendResult(True, provider_message_id=mid)

    async def send_interactive(self, to_phone: str, message: OutboundInteractive) -> SendResult:
        mid = f"console-out-{uuid.uuid4()}"
        self.sent.append(SentMessage(to_phone, message.body, mid, interactive=message))
        log.info("console.send_interactive", to=to_phone, text=message.body, choices=[c.title for c in message.choices])
        return SendResult(True, provider_message_id=mid)

    async def send_contact_request(self, bsuid: str, body: str) -> SendResult:
        mid = f"console-out-{uuid.uuid4()}"
        self.sent.append(SentMessage(None, body, mid, to_bsuid=bsuid))
        log.info("console.send_contact_request", to_bsuid=bsuid, text=body)
        return SendResult(True, provider_message_id=mid)

    async def lookup_phone(self, bsuid: str) -> str | None:
        return self.known_contacts.get(bsuid)

    async def show_typing(self, provider_message_id: str) -> None:
        self.typing.append(provider_message_id)
        log.info("console.typing", message_id=provider_message_id)

    async def mark_read(self, provider_message_id: str) -> None:
        self.read.append(provider_message_id)
        log.info("console.read", message_id=provider_message_id)

    async def react(self, to_phone: str, provider_message_id: str, emoji: str) -> None:
        self.reactions.append((provider_message_id, emoji))
        log.info("console.react", to=to_phone, message_id=provider_message_id, emoji=emoji)

    async def download_media(self, url: str) -> tuple[bytes, str | None]:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content, resp.headers.get("content-type")
