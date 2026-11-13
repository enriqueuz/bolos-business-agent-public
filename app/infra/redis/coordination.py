"""Idempotencia y locks por conversación sobre Redis."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis
from redis.exceptions import LockError

DEDUP_TTL_S = 24 * 3600


async def claim_once(redis: Redis, key: str, ttl_s: int = DEDUP_TTL_S) -> bool:
    """True si es la primera vez que se ve `key`. Se reclama ANTES de procesar (un duplicado de un mensaje
    que maneja dinero es peor que un mensaje que el dueño tenga que reenviar)."""
    return bool(await redis.set(f"dedup:{key}", "1", nx=True, ex=ttl_s))


class ConversationBusy(Exception):
    pass


def chat_lock_scope(phone: str | None, bsuid: str | None = None) -> str:
    """Clave del lock por chat: solo el teléfono. No usa el negocio porque este se crea DENTRO del primer turno;
    con `conv:new:…` → `conv:{negocio}:…` dos mensajes seguidos del alta tomarían locks distintos y correrían a la vez.

    Sin teléfono (BSUID desconocido) el lock es por BSUID, con prefijo propio: quedarse con sus dígitos podría chocar
    con el de un teléfono."""
    if phone:
        return "chat:" + "".join(ch for ch in phone if ch.isdigit())
    return f"chat:bsuid:{bsuid}"


@asynccontextmanager
async def conversation_lock(redis: Redis, scope: str, *, timeout_s: int = 120, wait_s: float = 0.0) -> AsyncIterator[None]:
    """Serializa los turnos de un mismo chat entre todos los workers."""
    lock = redis.lock(f"lock:{scope}", timeout=timeout_s, blocking=wait_s > 0, blocking_timeout=wait_s or None)
    acquired = await lock.acquire()
    if not acquired:
        raise ConversationBusy(scope)
    try:
        yield
    finally:
        try:
            await lock.release()
        except LockError:
            pass
