"""Contexto de ejecución de un comando: quién, para qué negocio, cuándo y desde qué mensaje.

Todo lo que el LLM nunca debe poder falsificar vive aquí y lo inyecta el llamador (tool executor o panel).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID

from app.domain.dates import local_today
from app.domain.enums import ActorType, EntityType, LinkRelation


@dataclass(frozen=True, slots=True)
class Actor:
    type: ActorType
    member_id: UUID | None = None


@dataclass(slots=True)
class ExecContext:
    business_id: UUID
    actor: Actor
    timezone: str = "UTC"  # quien construye el contexto pasa la zona del negocio
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    turn_id: UUID | None = None
    source_message_id: UUID | None = None
    # Banderas de riesgo inyectadas (no vienen del modelo). Ej.: el registro objetivo fue citado.
    target_is_quoted: bool = False
    require_money_evidence: bool = False
    require_purchase_settlement_choice: bool = False
    # Set only by the conversational pipeline, never by a tool argument or panel body.
    reconciliation_operation_id: UUID | None = None
    command_confirmed: bool = False

    @property
    def today(self) -> date:
        return local_today(self.timezone, self.now)


@dataclass(frozen=True, slots=True)
class EntityRef:
    entity_type: EntityType
    entity_id: UUID
    relation: LinkRelation = LinkRelation.CREATED
    version: int | None = None
    summary: str | None = None
