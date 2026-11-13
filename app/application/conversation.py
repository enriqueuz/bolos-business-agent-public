"""Estado conversacional efímero (vive en Redis). Nada de esto es verdad financiera: eso está en Postgres.

- window: últimas interacciones resumidas (memoria de corto plazo, independiente del proveedor de LLM)
- handles: referencias efímeras R1, R2… a registros reales (el modelo nunca ve ids)
- pending: UNA acción congelada esperando "sí/no" del dueño
- choice: UNA elección pendiente (opciones tocables o respondibles con su número)
- broker_money_draft: operaciones sin guardar que esperan Bs o tasa, separadas de confirmaciones
- undo: los registros de un recibo que el botón "Deshacer" puede borrar (uno por recibo)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.application.choices import new_token
from app.domain.enums import EntityType

WINDOW_SIZE = 12
WINDOW_TTL_S = 2 * 3600
HANDLES_TTL_S = 30 * 60
PENDING_TTL_S = 10 * 60
UNDO_TTL_S = 24 * 3600  # la ventana de conversación de WhatsApp: después ya no se puede tocar
# Cuánto sigue siendo "el momento del onboarding". Si el dueño se configura y no registra nada en una semana,
# su primer movimiento ya no es una bienvenida y su enlace va desnudo como los demás.
WELCOME_LINK_TTL_S = 7 * 24 * 3600


@dataclass(slots=True)
class WindowEntry:
    """Un turno de la memoria corta.

    `assistant` es SOLO lo que escribió el modelo. Los recibos deterministas van en `calls`, como resultado de
    la herramienta que los produjo: si se guardan como texto del asistente, el modelo lee en su propio
    historial que él contesta «✅ Venta registrada» sin llamar herramienta alguna, y a los pocos turnos lo
    imita (incidente del 25-09-2026: 20 ventas perdidas).
    """

    user: str
    assistant: str
    actions: list[str] = field(default_factory=list)
    # Herramientas del turno: [{"tool": str, "args": dict, "result": str}]. `result` es el recibo o un resumen.
    calls: list[dict[str, Any]] = field(default_factory=list)
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Handle:
    handle: str
    entity_type: EntityType
    entity_id: UUID
    version: int | None
    summary: str
    quoted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"handle": self.handle, "entity_type": self.entity_type.value, "entity_id": str(self.entity_id),
                "version": self.version, "summary": self.summary, "quoted": self.quoted}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Handle:
        return cls(d["handle"], EntityType(d["entity_type"]), UUID(d["entity_id"]), d.get("version"), d["summary"], d.get("quoted", False))

    def line(self) -> str:
        return f"{self.handle} | {self.summary}"


@dataclass(slots=True)
class PendingAction:
    command_kind: str
    command_payload: dict[str, Any]
    reason: str
    preview_text: str
    target_is_quoted: bool = False
    # mensajes salientes que mostraron la pregunta: al confirmar, los registros se enlazan también a ellos,
    # así el dueño puede corregir citando la pregunta o el recibo.
    prompt_outbound_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    # identifica ESTA confirmación: un botón "Sí" de otra (vieja) no la ejecuta
    token: str = field(default_factory=new_token)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PendingAction:
        return cls(**d)


@dataclass(slots=True)
class BrokerMoneyDraft:
    """Operación sin guardar que espera Bs/tasa o el cliente de una compra ya elegida pendiente."""

    command_kind: str
    command_payload: dict[str, Any]
    other_operations: list[BrokerMoneyDraft] = field(default_factory=list)
    operation_id: str = field(default_factory=lambda: str(uuid4()))
    pending_completion: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrokerMoneyDraft:
        return cls(data["command_kind"], data["command_payload"],
                   [cls.from_dict(item) for item in data.get("other_operations", [])],
                   data.get("operation_id") or str(uuid4()), data.get("pending_completion", {}))

    def operations(self) -> list[BrokerMoneyDraft]:
        return [BrokerMoneyDraft(self.command_kind, self.command_payload, operation_id=self.operation_id), *self.other_operations]

    @classmethod
    def from_operations(cls, operations: list[BrokerMoneyDraft]) -> BrokerMoneyDraft:
        return cls(operations[0].command_kind, operations[0].command_payload, operations[1:], operations[0].operation_id)


@dataclass(slots=True)
class ReconciliationDraft:
    origin_message_id: str
    operation_id: str = field(default_factory=lambda: str(uuid4()))
    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReconciliationDraft:
        return cls(**data)


@dataclass(slots=True)
class ChoiceOption:
    """Qué pasa al elegir la opción.

    - reply:   el título llega al LLM como la respuesta del dueño (preguntas del agente con `ask_choice`)
    - command: se ejecuta el comando congelado por el CommandBus, sin LLM (p. ej. el cliente correcto de una ambigüedad)
    """

    title: str
    description: str | None = None
    action: str = "reply"
    command_kind: str | None = None
    command_payload: dict[str, Any] | None = None
    confirmed: bool = False  # True si elegir la opción ya ES la confirmación (p. ej. "Es nuevo" al crear un cliente)


@dataclass(slots=True)
class PendingChoice:
    question: str
    options: list[ChoiceOption]
    target_is_quoted: bool = False
    prompt_outbound_ids: list[str] = field(default_factory=list)
    token: str = field(default_factory=new_token)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PendingChoice:
        return cls(**{**d, "options": [ChoiceOption(**o) for o in d["options"]]})

    def option(self, n: int) -> ChoiceOption | None:
        return self.options[n - 1] if 1 <= n <= len(self.options) else None


@dataclass(slots=True)
class UndoEntry:
    """Registros creados en un turno, con la versión con que se crearon (si se editaron después, Deshacer se rechaza)."""

    refs: list[dict[str, Any]]  # [{"entity_type", "entity_id", "version"}]
    token: str = field(default_factory=new_token)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UndoEntry:
        return cls(**d)


class ConversationStore(Protocol):
    async def set_reconciliation(self, scope: str, draft: ReconciliationDraft) -> None: ...
    async def get_reconciliation(self, scope: str) -> ReconciliationDraft | None: ...
    async def clear_reconciliation(self, scope: str) -> None: ...

    async def set_broker_money_draft(self, scope: str, draft: BrokerMoneyDraft) -> None: ...
    async def get_broker_money_draft(self, scope: str) -> BrokerMoneyDraft | None: ...
    async def clear_broker_money_draft(self, scope: str) -> None: ...

    async def get_window(self, scope: str) -> list[WindowEntry]: ...
    async def append_window(self, scope: str, entry: WindowEntry) -> None: ...

    async def register_handle(self, scope: str, entity_type: EntityType, entity_id: UUID, version: int | None,
                              summary: str, quoted: bool = False) -> Handle: ...
    async def get_handle(self, scope: str, handle: str) -> Handle | None: ...
    async def list_handles(self, scope: str) -> list[Handle]: ...
    async def update_handle_version(self, scope: str, handle: str, version: int | None, summary: str | None = None) -> None: ...
    async def clear_quoted(self, scope: str) -> None: ...

    async def set_pending(self, scope: str, action: PendingAction) -> None: ...
    async def pop_pending(self, scope: str) -> PendingAction | None: ...
    async def peek_pending(self, scope: str) -> PendingAction | None: ...

    async def set_choice(self, scope: str, choice: PendingChoice) -> None: ...
    async def pop_choice(self, scope: str) -> PendingChoice | None: ...
    async def peek_choice(self, scope: str) -> PendingChoice | None: ...
    async def choice_by_token(self, scope: str, token: str) -> PendingChoice | None: ...

    async def set_undo(self, scope: str, entry: UndoEntry) -> None: ...
    async def pop_undo(self, scope: str, token: str) -> UndoEntry | None: ...

    # Control de idempotencia para botones interactivos (confirmación, opciones, deshacer).
    # Un segundo toque al mismo botón se ignora en silencio en vez de avisar que ya no está vigente.
    async def mark_token_used(self, scope: str, token: str) -> None:
        """Registra un token interactivo como utilizado/respondido en este ámbito."""
        ...

    async def token_used(self, scope: str, token: str) -> bool:
        """Verifica si el token interactivo ya fue utilizado previamente."""
        ...

    # El enlace de bienvenida al panel: el único CTA que entra sin pedir código (ver `panel_links.py`).
    # Lo arma el onboarding al terminar y lo consume el primer recibo. Si se pierde, el CTA sale desnudo:
    # el modo degradado es el conservador.
    async def arm_welcome_link(self, scope: str) -> None:
        """Deja listo el enlace de bienvenida para el próximo recibo de este ámbito."""
        ...

    async def take_welcome_link(self, scope: str) -> bool:
        """Lo consume: devuelve True una sola vez, para que el token no viaje en dos mensajes."""
        ...


def conversation_scope(business_id: UUID, phone: str) -> str:
    return f"conv:{business_id}:{phone}"
