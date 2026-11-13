"""Auditoría: antes/después de cada mutación, en la misma transacción que la mutación."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.context import ExecContext
from app.db.models import AuditLog, Base
from app.domain.enums import AuditAction, EntityType

_SKIP = {"created_at", "updated_at"}


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [json_safe(v) for v in value]
    return value


def snapshot(entity: Base) -> dict[str, Any]:
    state = sa_inspect(entity)
    mapper = state.mapper
    data = {c.key: json_safe(getattr(entity, c.key)) for c in mapper.column_attrs if c.key not in _SKIP}
    for rel in ("items", "allocations"):
        # solo relaciones ya cargadas: acceder a una no cargada haría I/O implícito (prohibido en async)
        if rel in mapper.relationships and rel not in state.unloaded:
            children = getattr(entity, rel)
            data[rel] = [
                {c.key: json_safe(getattr(ch, c.key)) for c in sa_inspect(ch).mapper.column_attrs} for ch in children
            ]
    return data


def record(
    session: AsyncSession,
    ctx: ExecContext,
    *,
    entity_type: EntityType,
    entity_id: uuid.UUID,
    action: AuditAction,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    command: str | None = None,
) -> None:
    session.add(
        AuditLog(
            business_id=ctx.business_id,
            entity_type=entity_type.value,
            entity_id=entity_id,
            action=action.value,
            before=before,
            after=after,
            actor_type=ctx.actor.type.value,
            actor_id=ctx.actor.member_id,
            turn_id=ctx.turn_id,
            command=command,
        )
    )
