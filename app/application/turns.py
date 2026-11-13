"""Bitácora de mensajes y turnos (observabilidad) + enlaces mensaje↔registro (correcciones por cita)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.audit import json_safe
from app.application.context import EntityRef
from app.db.models import AgentTurn, InboundMessage, LlmCall, MessageEntityLink, OutboundMessage, ToolCall
from app.domain.enums import EntityType, LinkRelation, MessageKind, TurnStatus


class TurnLog:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    # ------------------------------------------------------------------ mensajes
    async def save_inbound(self, **fields: Any) -> UUID | None:
        """Inserta el mensaje entrante. Devuelve None si el provider_message_id ya existía (duplicado)."""
        async with self._factory() as s, s.begin():
            stmt = insert(InboundMessage).values(**fields).on_conflict_do_nothing(index_elements=["provider_message_id"]).returning(
                InboundMessage.id)
            return (await s.execute(stmt)).scalar_one_or_none()

    async def get_inbound(self, inbound_id: UUID) -> InboundMessage | None:
        async with self._factory() as s:
            return await s.get(InboundMessage, inbound_id)

    async def unstarted_inbound(self, provider: str, message_id: str) -> UUID | None:
        """Recupera un mensaje persistido cuyo encolado falló, sin repetir turnos ya iniciados."""
        async with self._factory() as s:
            return (await s.execute(select(InboundMessage.id).where(
                InboundMessage.provider == provider, InboundMessage.provider_message_id == message_id,
                ~select(AgentTurn.id).where(AgentTurn.inbound_message_id == InboundMessage.id).exists(),
            ))).scalar_one_or_none()

    async def attach_inbound_business(self, inbound_id: UUID, business_id: UUID) -> None:
        async with self._factory() as s, s.begin():
            await s.execute(update(InboundMessage).where(InboundMessage.id == inbound_id).values(business_id=business_id))

    async def set_transcript(self, inbound_id: UUID, transcript: str) -> None:
        async with self._factory() as s, s.begin():
            await s.execute(update(InboundMessage).where(InboundMessage.id == inbound_id).values(transcript=transcript))

    async def save_outbound(self, *, business_id: UUID | None, turn_id: UUID | None, to_phone: str | None, provider: str,
                            provider_message_id: str | None, body: str, status: str = "sent", error: str | None = None,
                            interactive: dict[str, Any] | None = None, to_bsuid: str | None = None,
                            business_phone_number_id: str | None = None) -> UUID:
        async with self._factory() as s, s.begin():
            row = OutboundMessage(business_id=business_id, turn_id=turn_id, to_phone=to_phone, to_bsuid=to_bsuid,
                                  provider=provider, provider_message_id=provider_message_id, body=body, status=status,
                                  error=error, interactive=interactive, business_phone_number_id=business_phone_number_id)
            s.add(row)
            await s.flush()
            return row.id

    # ------------------------------------------------------------------ BSUID desconocido: mensajes en espera
    async def contact_requested_recently(self, bsuid: str, *, within: timedelta,
                                         business_phone_number_id: str | None = None) -> bool:
        """¿Ya se le pidió el contacto hace poco? Evita un botón por cada mensaje que mande mientras tanto."""
        async with self._factory() as s:
            return (await s.execute(select(
                select(OutboundMessage.id).where(OutboundMessage.to_bsuid == bsuid,
                                                 OutboundMessage.business_phone_number_id == business_phone_number_id,
                                                 OutboundMessage.status == "sent",
                                                 OutboundMessage.sent_at > func.now() - within).exists()
            ))).scalar_one()

    async def adopt_parked(self, bsuid: str, phone: str, *, within: timedelta) -> list[UUID]:
        """Asigna el teléfono ya verificado a los mensajes que quedaron en espera por ese BSUID y devuelve, en orden de
        llegada, los que hay que REPROCESAR. Solo los recientes: lo de hace días el dueño pudo registrarlo por otra vía.

        Un mensaje en espera cuyo turno aún no empezó NO se devuelve: su propio job lo procesará ya con teléfono.
        Reprocesarlo también lo registraría dos veces."""
        async with self._factory() as s, s.begin():
            adopted = (await s.execute(
                update(InboundMessage)
                .where(InboundMessage.from_bsuid == bsuid, InboundMessage.from_phone.is_(None),
                       InboundMessage.type != "contact_shared", InboundMessage.received_at > func.now() - within)
                .values(from_phone=phone)
                .returning(InboundMessage.id)
            )).scalars().all()
            if not adopted:
                return []
            return list((await s.execute(
                select(InboundMessage.id)
                .where(InboundMessage.id.in_(adopted),
                       select(AgentTurn.id).where(AgentTurn.inbound_message_id == InboundMessage.id).exists())
                .order_by(InboundMessage.received_at)
            )).scalars())

    async def link(self, business_id: UUID, message_kind: MessageKind, message_id: UUID, refs: list[EntityRef]) -> None:
        if not refs:
            return
        async with self._factory() as s, s.begin():
            for ref in refs:
                await s.execute(insert(MessageEntityLink).values(
                    business_id=business_id, message_kind=message_kind.value, message_id=message_id,
                    entity_type=ref.entity_type.value, entity_id=ref.entity_id, relation=ref.relation.value,
                ).on_conflict_do_nothing())

    async def quoted_message_refs(self, business_id: UUID, provider_message_id: str) -> tuple[str | None, list[tuple[EntityType, UUID]]]:
        """Busca el mensaje citado (saliente o entrante) y los registros enlazados a él."""
        async with self._factory() as s:
            out = (await s.execute(select(OutboundMessage).where(
                OutboundMessage.provider_message_id == provider_message_id, OutboundMessage.business_id == business_id))).scalar_one_or_none()
            if out is not None:
                kind, mid, body = MessageKind.OUTBOUND, out.id, out.body
            else:
                inb = (await s.execute(select(InboundMessage).where(
                    InboundMessage.provider_message_id == provider_message_id, InboundMessage.business_id == business_id))).scalar_one_or_none()
                if inb is None:
                    return None, []
                kind, mid, body = MessageKind.INBOUND, inb.id, (inb.body or inb.transcript or "")
            links = (await s.execute(select(MessageEntityLink).where(
                MessageEntityLink.message_kind == kind.value, MessageEntityLink.message_id == mid)
                .order_by(MessageEntityLink.id))).scalars().all()
            seen: dict[tuple[str, UUID], None] = {}
            for link in links:
                seen[(link.entity_type, link.entity_id)] = None
            return body, [(EntityType(t), i) for t, i in seen]

    # ------------------------------------------------------------------ turnos
    async def start_turn(self, *, business_id: UUID | None, inbound_message_id: UUID | None) -> UUID:
        async with self._factory() as s, s.begin():
            turn = AgentTurn(business_id=business_id, inbound_message_id=inbound_message_id, status=TurnStatus.RUNNING.value)
            s.add(turn)
            await s.flush()
            return turn.id

    async def finish_turn(self, turn_id: UUID, **fields: Any) -> None:
        async with self._factory() as s, s.begin():
            totals = (await s.execute(select(
                func.coalesce(func.sum(LlmCall.input_tokens), 0), func.coalesce(func.sum(LlmCall.output_tokens), 0),
                func.sum(LlmCall.cost_usd), func.count(LlmCall.id), func.count(LlmCall.cost_usd),
            ).where(LlmCall.turn_id == turn_id))).one()
            values = {**fields, "input_tokens": totals[0], "output_tokens": totals[1], "finished_at": datetime.now(UTC)}
            # el costo solo es confiable si TODAS las llamadas tienen precio conocido
            values["cost_usd"] = totals[2] if totals[3] and totals[3] == totals[4] else None
            await s.execute(update(AgentTurn).where(AgentTurn.id == turn_id).values(**values))

    async def record_llm_calls(self, turn_id: UUID, calls: list[dict[str, Any]]) -> None:
        if not calls:
            return
        async with self._factory() as s, s.begin():
            base = (await s.execute(select(func.count()).select_from(LlmCall).where(LlmCall.turn_id == turn_id))).scalar_one()
            for i, call in enumerate(calls):
                s.add(LlmCall(turn_id=turn_id, seq=base + i, **call))

    async def record_tool_call(self, turn_id: UUID, *, seq: int, tool_call_id: str | None, tool_name: str, args: Any,
                               injected_args: Any, result: Any, outcome: str, latency_ms: int) -> None:
        async with self._factory() as s, s.begin():
            s.add(ToolCall(turn_id=turn_id, seq=seq, tool_call_id=tool_call_id, tool_name=tool_name, args=json_safe(args),
                           injected_args=json_safe(injected_args), result=json_safe(result), outcome=outcome, latency_ms=latency_ms))

    # ------------------------------------------------------------------ panel / mantenimiento
    async def list_turns(self, business_id: UUID, limit: int = 50, before: datetime | None = None) -> list[dict[str, Any]]:
        async with self._factory() as s:
            stmt = (select(AgentTurn, InboundMessage).outerjoin(InboundMessage, InboundMessage.id == AgentTurn.inbound_message_id)
                    .where(AgentTurn.business_id == business_id))
            if before:
                stmt = stmt.where(AgentTurn.created_at < before)
            rows = (await s.execute(stmt.order_by(AgentTurn.created_at.desc()).limit(limit))).all()
            out = []
            for turn, inbound in rows:
                tools = (await s.execute(select(ToolCall.tool_name, ToolCall.outcome).where(ToolCall.turn_id == turn.id)
                                         .order_by(ToolCall.seq))).all()
                out.append({
                    "id": str(turn.id), "created_at": turn.created_at.isoformat(), "lane": turn.lane, "status": turn.status,
                    "user_text": (inbound.body or inbound.transcript) if inbound else None, "message_type": inbound.type if inbound else None,
                    "reply_text": turn.reply_text, "latency_ms": turn.latency_ms,
                    "cost_usd": str(turn.cost_usd) if turn.cost_usd is not None else None,
                    "tools": [{"name": n, "outcome": o} for n, o in tools], "grounding_ok": turn.grounding_ok, "error": turn.error,
                })
        return out

    async def chat_history(
        self,
        business_id: UUID,
        phone: str,
        *,
        limit: int = 50,
        before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Conversación del miembro, unificada entre WhatsApp y el panel."""
        async with self._factory() as s:
            stmt = (
                select(AgentTurn, InboundMessage)
                .join(InboundMessage, InboundMessage.id == AgentTurn.inbound_message_id)
                .where(
                    AgentTurn.business_id == business_id,
                    InboundMessage.from_phone == phone,
                )
            )
            if before:
                stmt = stmt.where(AgentTurn.created_at < before)
            rows = (
                await s.execute(stmt.order_by(AgentTurn.created_at.desc()).limit(limit))
            ).all()
            out = []
            for turn, inbound in reversed(rows):
                outbound = (
                    await s.execute(
                        select(OutboundMessage)
                        .where(OutboundMessage.turn_id == turn.id)
                        .order_by(OutboundMessage.sent_at)
                    )
                ).scalars().all()
                choices = []
                for message in outbound:
                    if message.interactive:
                        choices.extend(message.interactive.get("choices") or [])
                out.append(
                    {
                        "id": str(turn.id),
                        "created_at": turn.created_at.isoformat(),
                        "provider": inbound.provider,
                        "message_type": inbound.type,
                        "user_text": inbound.body or inbound.transcript or "",
                        "reply_text": turn.reply_text or "",
                        "status": turn.status,
                        "choices": choices,
                    }
                )
        return out

    async def turn_detail(self, business_id: UUID, turn_id: UUID) -> dict[str, Any] | None:
        async with self._factory() as s:
            turn = await s.get(AgentTurn, turn_id)
            if turn is None or turn.business_id != business_id:
                return None
            llm = (await s.execute(select(LlmCall).where(LlmCall.turn_id == turn_id).order_by(LlmCall.seq))).scalars().all()
            tools = (await s.execute(select(ToolCall).where(ToolCall.turn_id == turn_id).order_by(ToolCall.seq))).scalars().all()
            inbound = await s.get(InboundMessage, turn.inbound_message_id) if turn.inbound_message_id else None
        return {
            "id": str(turn.id), "lane": turn.lane, "route_reason": turn.route_reason, "status": turn.status, "stages": turn.stages,
            "user_text": (inbound.body or inbound.transcript) if inbound else None, "reply_text": turn.reply_text,
            "model_text": turn.model_text, "grounding_ok": turn.grounding_ok, "grounding_detail": turn.grounding_detail,
            "error": turn.error, "latency_ms": turn.latency_ms, "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens,
            "cost_usd": str(turn.cost_usd) if turn.cost_usd is not None else None, "created_at": turn.created_at.isoformat(),
            "llm_calls": [{"seq": c.seq, "role": c.role, "model": c.model, "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
                           "thinking_tokens": c.thinking_tokens, "cost_usd": str(c.cost_usd) if c.cost_usd is not None else None,
                           "finish_reason": c.finish_reason, "request": c.request, "response": c.response} for c in llm],
            "tool_calls": [{"seq": t.seq, "tool": t.tool_name, "args": t.args, "injected_args": t.injected_args, "result": t.result,
                            "outcome": t.outcome, "latency_ms": t.latency_ms} for t in tools],
        }

    async def purge_llm_payloads(self, older_than_days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        async with self._factory() as s, s.begin():
            res = await s.execute(update(LlmCall).where(LlmCall.created_at < cutoff, LlmCall.request.is_not(None))
                                  .values(request=None, response=None))
            return res.rowcount or 0


def cost_or_none(value: Decimal | None) -> Decimal | None:
    return value.quantize(Decimal("0.000001")) if value is not None else None


_ = LinkRelation
