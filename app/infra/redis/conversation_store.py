"""Implementación Redis de ConversationStore. N workers comparten estado; los deploys no cortan el hilo."""

from __future__ import annotations

import json
from uuid import UUID

from redis.asyncio import Redis

from app.application.conversation import (
    HANDLES_TTL_S,
    PENDING_TTL_S,
    UNDO_TTL_S,
    WELCOME_LINK_TTL_S,
    WINDOW_SIZE,
    WINDOW_TTL_S,
    BrokerMoneyDraft,
    Handle,
    PendingAction,
    PendingChoice,
    ReconciliationDraft,
    UndoEntry,
    WindowEntry,
)
from app.domain.enums import EntityType


class RedisConversationStore:
    def __init__(self, redis: Redis) -> None:
        self._r = redis

    async def set_reconciliation(self, scope: str, draft: ReconciliationDraft) -> None:
        await self._r.set(f"{scope}:reconciliation", json.dumps(draft.to_dict()), ex=PENDING_TTL_S)

    async def get_reconciliation(self, scope: str) -> ReconciliationDraft | None:
        raw = await self._r.get(f"{scope}:reconciliation")
        return ReconciliationDraft.from_dict(json.loads(raw)) if raw else None

    async def clear_reconciliation(self, scope: str) -> None:
        await self._r.delete(f"{scope}:reconciliation")

    async def set_broker_money_draft(self, scope: str, draft: BrokerMoneyDraft) -> None:
        await self._r.set(f"{scope}:broker_money_draft", json.dumps(draft.to_dict()), ex=PENDING_TTL_S)

    async def get_broker_money_draft(self, scope: str) -> BrokerMoneyDraft | None:
        raw = await self._r.get(f"{scope}:broker_money_draft")
        return BrokerMoneyDraft.from_dict(json.loads(raw)) if raw else None

    async def clear_broker_money_draft(self, scope: str) -> None:
        await self._r.delete(f"{scope}:broker_money_draft")

    # ---------------------------------------------------------------- window
    async def get_window(self, scope: str) -> list[WindowEntry]:
        raw = await self._r.lrange(f"{scope}:window", 0, -1)
        return [WindowEntry(**json.loads(x)) for x in raw]

    async def append_window(self, scope: str, entry: WindowEntry) -> None:
        key = f"{scope}:window"
        async with self._r.pipeline(transaction=True) as p:
            p.rpush(key, json.dumps(entry.to_dict(), ensure_ascii=False))
            p.ltrim(key, -WINDOW_SIZE, -1)
            p.expire(key, WINDOW_TTL_S)
            await p.execute()

    # ---------------------------------------------------------------- handles
    async def register_handle(self, scope: str, entity_type: EntityType, entity_id: UUID, version: int | None,
                              summary: str, quoted: bool = False) -> Handle:
        hkey, ikey, ckey = f"{scope}:handles", f"{scope}:handle_index", f"{scope}:handle_seq"
        entity_key = f"{entity_type.value}:{entity_id}"
        existing = await self._r.hget(ikey, entity_key)
        raw_existing = await self._r.hget(hkey, existing) if existing else None
        if raw_existing:
            handle = Handle.from_dict(json.loads(raw_existing))
            if handle:
                handle.version, handle.summary = version, summary
                handle.quoted = handle.quoted or quoted
                await self._r.hset(hkey, handle.handle, json.dumps(handle.to_dict(), ensure_ascii=False))
                await self._touch(scope)
                return handle
        seq = await self._r.incr(ckey)
        handle = Handle(f"R{seq}", entity_type, entity_id, version, summary, quoted)
        async with self._r.pipeline(transaction=True) as p:
            p.hset(hkey, handle.handle, json.dumps(handle.to_dict(), ensure_ascii=False))
            p.hset(ikey, entity_key, handle.handle)
            await p.execute()
        await self._touch(scope)
        return handle

    async def _touch(self, scope: str) -> None:
        for suffix in ("handles", "handle_index", "handle_seq"):
            await self._r.expire(f"{scope}:{suffix}", HANDLES_TTL_S)

    async def get_handle(self, scope: str, handle: str) -> Handle | None:
        raw = await self._r.hget(f"{scope}:handles", handle.strip().upper())
        return Handle.from_dict(json.loads(raw)) if raw else None

    async def list_handles(self, scope: str) -> list[Handle]:
        raw = await self._r.hgetall(f"{scope}:handles")
        handles = [Handle.from_dict(json.loads(v)) for v in raw.values()]
        return sorted(handles, key=lambda h: int(h.handle[1:]))

    async def update_handle_version(self, scope: str, handle: str, version: int | None, summary: str | None = None) -> None:
        h = await self.get_handle(scope, handle)
        if h:
            h.version = version
            if summary:
                h.summary = summary
            await self._r.hset(f"{scope}:handles", h.handle, json.dumps(h.to_dict(), ensure_ascii=False))

    async def clear_quoted(self, scope: str) -> None:
        for h in await self.list_handles(scope):
            if h.quoted:
                h.quoted = False
                await self._r.hset(f"{scope}:handles", h.handle, json.dumps(h.to_dict(), ensure_ascii=False))

    # ---------------------------------------------------------------- pending
    async def set_pending(self, scope: str, action: PendingAction) -> None:
        await self._r.set(f"{scope}:pending", json.dumps(action.to_dict(), ensure_ascii=False, default=str), ex=PENDING_TTL_S)

    async def pop_pending(self, scope: str) -> PendingAction | None:
        raw = await self._r.getdel(f"{scope}:pending")
        return PendingAction.from_dict(json.loads(raw)) if raw else None

    async def peek_pending(self, scope: str) -> PendingAction | None:
        raw = await self._r.get(f"{scope}:pending")
        return PendingAction.from_dict(json.loads(raw)) if raw else None

    # ---------------------------------------------------------------- choice
    async def set_choice(self, scope: str, choice: PendingChoice) -> None:
        # Dos copias: la ACTIVA del hilo (la consume el siguiente mensaje) y una por TOKEN que sobrevive a lo
        # que el dueño escriba mientras tanto. Sin la segunda, cualquier mensaje intermedio dejaba el botón
        # muerto — así se perdió una venta el 25-09-2026: tocó la opción 44 s después de escribir otra cosa.
        blob = json.dumps(choice.to_dict(), ensure_ascii=False, default=str)
        async with self._r.pipeline(transaction=True) as p:
            p.set(f"{scope}:choice", blob, ex=PENDING_TTL_S)
            p.set(f"{scope}:choice:{choice.token}", blob, ex=UNDO_TTL_S)
            await p.execute()

    async def choice_by_token(self, scope: str, token: str) -> PendingChoice | None:
        raw = await self._r.get(f"{scope}:choice:{token}")
        return PendingChoice.from_dict(json.loads(raw)) if raw else None

    async def pop_choice(self, scope: str) -> PendingChoice | None:
        raw = await self._r.getdel(f"{scope}:choice")
        return PendingChoice.from_dict(json.loads(raw)) if raw else None

    async def peek_choice(self, scope: str) -> PendingChoice | None:
        raw = await self._r.get(f"{scope}:choice")
        return PendingChoice.from_dict(json.loads(raw)) if raw else None

    # ---------------------------------------------------------------- undo (uno por recibo; de un solo uso)
    async def set_undo(self, scope: str, entry: UndoEntry) -> None:
        await self._r.set(f"{scope}:undo:{entry.token}", json.dumps(entry.to_dict(), ensure_ascii=False, default=str), ex=UNDO_TTL_S)

    async def pop_undo(self, scope: str, token: str) -> UndoEntry | None:
        raw = await self._r.getdel(f"{scope}:undo:{token}")
        return UndoEntry.from_dict(json.loads(raw)) if raw else None

    async def mark_token_used(self, scope: str, token: str) -> None:
        await self._r.set(f"{scope}:used:{token}", "1", ex=UNDO_TTL_S)

    async def token_used(self, scope: str, token: str) -> bool:
        return bool(await self._r.exists(f"{scope}:used:{token}"))

    # ------------------------------------------------------- enlace de bienvenida
    async def arm_welcome_link(self, scope: str) -> None:
        await self._r.set(f"{scope}:welcome_link", "1", ex=WELCOME_LINK_TTL_S)

    async def take_welcome_link(self, scope: str) -> bool:
        return bool(await self._r.getdel(f"{scope}:welcome_link"))
