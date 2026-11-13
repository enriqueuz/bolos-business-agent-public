"""Puerto de mensajería. Hoy: Kapso (producción) y Console (desarrollo/tests). Meta directo: no implementado a propósito."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from app.channels.types import InboundMessage, OutboundInteractive, SendResult


class MessagingProvider(Protocol):
    name: str

    def for_phone_number(self, phone_number_id: str | None) -> MessagingProvider:
        """Proveedor ligado al número de Bolos receptor, sin mutar la instancia compartida. None usa el principal."""
        ...

    def verify_signature(self, raw_body: bytes, headers: Mapping[str, str]) -> bool: ...

    def parse_webhook(self, payload: dict[str, Any], headers: Mapping[str, str]) -> list[InboundMessage]: ...

    async def send_text(self, to_phone: str, text: str) -> SendResult: ...

    async def send_image(self, to_phone: str, image_url: str, caption: str) -> SendResult: ...

    async def send_interactive(self, to_phone: str, message: OutboundInteractive) -> SendResult: ...

    async def show_typing(self, provider_message_id: str) -> None: ...

    async def mark_read(self, provider_message_id: str) -> None:
        """Solo el visto azul, sin «escribiendo…»: para los turnos que se acusan con una reacción y no escriben nada."""
        ...

    async def react(self, to_phone: str, provider_message_id: str, emoji: str) -> None: ...

    async def send_contact_request(self, bsuid: str, body: str) -> SendResult:
        """Botón nativo «compartir contacto» dirigido al BSUID. Lo único que se envía sin teléfono."""
        ...

    async def lookup_phone(self, bsuid: str) -> str | None:
        """Teléfono que el proveedor ya tenga enlazado a ese BSUID, o None. Nunca lanza."""
        ...

    async def download_media(self, url: str) -> tuple[bytes, str | None]: ...
