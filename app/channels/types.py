"""Tipos neutrales de mensajería. El pipeline solo conoce estos tipos, nunca el payload de un proveedor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.application.choices import Choice

# contact_shared: el usuario tocó «compartir contacto» (request_contact_info); trae su número verificado por WhatsApp
# reaction: reaccionó con un emoji a un mensaje de Bolos (en `text` el emoji, en reply_to_... el mensaje reaccionado)
# unavailable: el proveedor recibió el evento, pero no entregó su contenido; no es un formato incompatible.
MessageType = Literal["text", "voice", "image", "unsupported", "unavailable", "contact_shared", "reaction", "sticker"]


@dataclass(slots=True)
class InboundMessage:
    provider: str
    from_phone: str | None  # None = Meta ocultó el número (usuario con username); queda el BSUID
    type: MessageType
    from_bsuid: str | None = None  # id de usuario por negocio que manda Meta; alias de canal del teléfono
    provider_message_id: str | None = None
    contact_name: str | None = None
    text: str | None = None  # cuerpo de texto o pie de foto
    transcript: str | None = None  # nota de voz ya transcrita por el proveedor
    media_url: str | None = None
    media_mime_type: str | None = None
    reply_to_provider_message_id: str | None = None
    interactive_reply_id: str | None = None  # id del botón / fila de lista que tocó (lo definimos nosotros)
    raw: dict[str, Any] = field(default_factory=dict)
    business_phone_number_id: str | None = None  # ID del número de WhatsApp de Bolos receptor, no el teléfono del usuario

    @property
    def content_text(self) -> str:
        return (self.text or self.transcript or "").strip()


# Límites del envío interactivo según el esquema de Kapso (docs.kapso.ai/api/meta/whatsapp/openapi-whatsapp.yaml), que es
# quien recibe nuestra petición: el cuerpo es de 1024 para botones Y listas (Meta documenta 4096 para listas; manda Kapso).
MAX_BUTTONS = 3
MAX_LIST_ROWS = 10
BUTTON_TITLE_MAX = 20
ROW_TITLE_MAX = 24
ROW_DESCRIPTION_MAX = 72
LIST_BUTTON_MAX = 20
BUTTONS_BODY_MAX = 1024
LIST_BODY_MAX = 1024


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class OutboundInteractive:
    """Mensaje con opciones. ≤3 opciones sin descripción → botones; si no → lista (hasta 10)."""

    body: str
    choices: tuple[Choice, ...]
    list_button: str = "Ver opciones"

    @property
    def as_buttons(self) -> bool:
        return len(self.choices) <= MAX_BUTTONS and not any(c.description for c in self.choices)

    @property
    def body_max(self) -> int:
        return BUTTONS_BODY_MAX if self.as_buttons else LIST_BODY_MAX

    def fits(self) -> bool:
        """¿Cabe en UN mensaje? Si no, el pipeline manda todo como texto (nunca un mensaje extra solo para botones)."""
        return 0 < len(self.choices) <= MAX_LIST_ROWS and 0 < len(self.body) <= self.body_max

    def clipped(self) -> tuple[Choice, ...]:
        if self.as_buttons:
            return tuple(Choice(c.id, _clip(c.title, BUTTON_TITLE_MAX)) for c in self.choices)
        return tuple(Choice(c.id, _clip(c.title, ROW_TITLE_MAX), _clip(c.description, ROW_DESCRIPTION_MAX) if c.description else None)
                     for c in self.choices)

    def as_text(self) -> str:
        """Versión en texto (respaldo): las opciones enumeradas. Responder el número equivale a tocar."""
        if [c.title for c in self.choices] == ["Sí", "No"]:
            return f"{self.body}\n\nResponde *sí* o *no*."
        lines = [f"{n}. {c.title}" + (f" — {c.description}" if c.description else "") for n, c in enumerate(self.choices, start=1)]
        numbers = ", ".join(str(n) for n in range(1, len(self.choices))) + f" o {len(self.choices)}"
        return f"{self.body}\n\n" + "\n".join(lines) + f"\n\nResponde {numbers}."

    def to_dict(self) -> dict[str, Any]:
        return {"body": self.body, "list_button": self.list_button,
                "choices": [{"id": c.id, "title": c.title, "description": c.description} for c in self.choices]}


@dataclass(frozen=True, slots=True)
class OutboundImage:
    image_url: str
    caption: str


@dataclass(slots=True)
class SendResult:
    ok: bool
    provider_message_id: str | None = None
    error: str | None = None
