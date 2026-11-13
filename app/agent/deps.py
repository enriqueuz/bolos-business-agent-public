"""Dependencias de un turno del agente (lo que recibe cada tool en `RunContext.deps`).

Todo lo que el modelo NO debe poder falsificar (negocio, miembro, turno, hora) vive aquí y lo inyecta el pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.application.attachments import AttachmentService
from app.application.bus import CommandBus
from app.application.context import EntityRef
from app.application.conversation import (
    ConversationStore,
    Handle,
    PendingAction,
    PendingChoice,
    ReconciliationDraft,
)
from app.application.identity import BusinessSnapshot, Identity
from app.application.queries.brokerage import BrokerageQueries
from app.application.queries.service import QueryService
from app.application.turns import TurnLog
from app.channels.types import OutboundImage
from app.domain.enums import Lane


@dataclass
class TurnDeps:
    identity: Identity
    scope: str
    turn_id: UUID
    inbound_id: UUID
    now: datetime
    user_text: str
    bus: CommandBus
    queries: QueryService
    brokerage_queries: BrokerageQueries
    attachments: AttachmentService
    store: ConversationStore
    turns: TurnLog
    lane: Lane = Lane.AGENT
    read_only: bool = False  # una revisión de la respuesta no debe volver a registrar movimientos
    reconciliation: ReconciliationDraft | None = None
    # El turno nació DENTRO del panel (nota de voz o chat del panel), no en WhatsApp. Lo que cambia: no se le
    # ofrece un enlace al panel a quien ya lo está mirando.
    from_panel: bool = False
    # Carga ya persistida por el pipeline. Es confiable y nunca forma parte del schema visible para el modelo.
    attachment_upload_id: UUID | None = None
    preceding_receipt_ids: list[UUID] = field(default_factory=list)
    outbound_image: OutboundImage | None = None
    # contexto preparado por el pipeline
    quoted_text: str | None = None
    quoted_handles: list[Handle] = field(default_factory=list)
    quoted_deleted: list[str] = field(default_factory=list)
    discarded_pending: PendingAction | None = None
    discarded_choice: PendingChoice | None = None  # había opciones pendientes y el dueño escribió otra cosa
    choice_note: str | None = None  # "el dueño tocó «X» en tu pregunta «Q»"
    context_text: str = ""  # contexto dinámico que vio el modelo (sus cifras cuentan como fuente para el grounding)
    # acumulado durante el turno
    receipts: list[str] = field(default_factory=list)
    # Presentaciones candidatas de lecturas. Solo se usa una si cubre la única consulta del turno.
    # Nunca cierran el loop: el agente puede necesitar otras herramientas para completar la petición.
    renders: list[str] = field(default_factory=list)
    # Respuestas deterministas de acciones tipadas sin escritura (p. ej. una operación fuera del alcance de la
    # vertical). No son lecturas ni recibos y el modelo no puede reescribirlas.
    notices: list[str] = field(default_factory=list)
    changed_refs: list[EntityRef] = field(default_factory=list)
    # El objeto PRINCIPAL de cada escritura (uno por comando, su `affected[0]`), que es lo que el enlace al panel
    # puede abrir. Contar `changed_refs` no sirve: una venta fiada emite dos refs y no son dos escrituras.
    # Los borrados no entran: el enlace llevaría a un registro que ya no existe.
    linkable: list[EntityRef] = field(default_factory=list)
    shown_refs: list[EntityRef] = field(default_factory=list)
    tool_results: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)  # tool calls del turno, para la memoria corta
    pending: PendingAction | None = None
    choice: PendingChoice | None = None  # opciones a mostrar (ask_choice o cliente ambiguo)
    question: str | None = None  # pregunta abierta de need_more_info: la manda el sistema, no el modelo
    kept_text: str = ""  # el texto del modelo que SÍ se envió; lo único suyo que puede volver a la memoria corta
    undoable: list[EntityRef] = field(default_factory=list)  # registros nuevos que el botón Deshacer puede borrar
    ack_reaction: str | None = None  # emoji con que se acusa recibo de un botón que salió bien y no lleva respuesta
    tool_seq: int = 0
    call_fingerprints: set[str] = field(default_factory=set)
    internal_errors: list[str] = field(default_factory=list)
    internal_exceptions: list[tuple[Exception, str]] = field(default_factory=list)
    # Escrituras del turno: los registros NUEVOS y simples (venta, gasto, compra, fiado, abono, cambio) los
    # cuenta entero el recibo; el resto —correcciones, borrados, tasas, notas— puede merecer una aclaración
    # del modelo. Los separa `registry.execute_command`, que es quien conoce el comando.
    simple_writes: int = 0
    other_writes: int = 0
    contract_violations: int = 0  # veces que un carril cerró sin llamar herramienta y hubo que repararlo

    @property
    def business(self) -> BusinessSnapshot:
        return self.identity.business

    @property
    def business_id(self) -> UUID:
        return self.identity.business.id

    def next_seq(self) -> int:
        self.tool_seq += 1
        return self.tool_seq

    def reset_turn_output(self) -> None:
        """Borra lo que produjo un intento fallido antes de repetirlo, para que el reintento no herede su basura
        (un `choice` a medias, un recibo de algo que sí se escribió, resultados que ensucian el grounding)."""
        self.receipts.clear()
        self.renders.clear()
        self.notices.clear()
        self.tool_results.clear()
        self.calls.clear()
        self.changed_refs.clear()
        self.linkable.clear()
        self.shown_refs.clear()
        self.undoable.clear()
        self.internal_errors.clear()
        self.pending = self.choice = self.question = None
        self.simple_writes = self.other_writes = 0

    @property
    def read_results(self) -> list[Any]:
        return [r for r in self.tool_results if isinstance(r, dict)
                and r.get("ok") and "handles" not in r and "status" not in r]

    @property
    def unrendered_reads(self) -> bool:
        """¿Hubo consultas cuya respuesta NO sabe armar el sistema? Entonces el texto del modelo hace falta."""
        return self.has_read_results and self.read_presentation is None

    @property
    def read_presentation(self) -> str | None:
        """Una vista estándar completa o una composición del modelo, nunca ambas.

        Cualquier resultado adicional (también rechazos), referencia, escritura o error exige
        composición. No se intenta deducir qué datos pidió el usuario a partir del texto final.
        """
        if (len(self.renders) == 1 and len(self.tool_results) == 1
                and self.has_read_results and not self.shown_refs
                and not self.receipts and not self.wrote_something and not self.internal_errors
                and not self.notices and self.pending is None and self.choice is None
                and self.question is None):
            return self.renders[0]
        return None

    @property
    def has_read_results(self) -> bool:
        """¿Hubo consultas en el turno? Entonces el texto del modelo trae cifras que el recibo no tiene."""
        return any(r.get("ok") and "handles" not in r and "status" not in r
                   for r in self.tool_results if isinstance(r, dict))

    @property
    def wrote_something(self) -> bool:
        return bool(self.simple_writes or self.other_writes)

    @property
    def used_a_tool(self) -> bool:
        """¿Corrió ALGUNA herramienta en el turno? Incluye las rechazadas: lo que importa es que el modelo la pidió."""
        return self.tool_seq > 0

    @property
    def turn_is_answered(self) -> bool:
        """¿El sistema ya puede responder solo, y pedirle un texto al modelo sería pagar por algo que se descarta?

        Es la misma condición con la que `TurnPipeline._compose` tira ese texto, pero evaluada ANTES de pedirlo
        (ver `PydanticAIOrchestrator._run_agent`). Formas de tener respuesta determinista:

        - un RECIBO de registros nuevos y simples (tras una corrección no basta: el prompt le pide al modelo
          cosas que el recibo no dice, como preguntar por el saldo a favor que quedó en otro cliente),
        - un AVISO determinista de una acción sin escritura,
        - unas OPCIONES que mostrar (`ask_choice`, cliente ambiguo, o la pregunta de un comando),
        - una CONFIRMACIÓN pendiente, o una PREGUNTA abierta de `need_more_info`.

        Cortar también en los tres últimos no es solo ahorro: con `tool_choice` forzado el loop volvería a
        pedirle una herramienta al modelo, y la repetiría hasta agotar el límite del carril.
        """
        if self.pending is not None or self.choice is not None or self.question is not None:
            return True
        if self.outbound_image is not None:
            return True
        if self.notices:
            return True
        if not self.receipts or self.simple_writes == 0 or self.other_writes:
            return False
        return not (self.shown_refs or self.internal_errors or self.has_read_results)
