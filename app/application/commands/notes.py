"""Memoria del negocio: hechos estables que el dueño cuenta y que no son transacciones."""

from __future__ import annotations

from typing import ClassVar

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import audit
from app.application.bus import Command, CommandDeps, CommandResult, command_handler
from app.application.context import EntityRef, ExecContext
from app.application.resolvers import _candidates, _pick
from app.db.models import BusinessNote, Customer, Product
from app.domain.enums import AuditAction, EntityType, NoteKind
from app.domain.errors import Ambiguous
from app.domain.text import clean_display_name


class RememberNote(Command):
    kind: ClassVar[str] = "remember_note"

    text: str = Field(min_length=3, max_length=1000)
    note_kind: NoteKind = NoteKind.GENERAL
    subject_name: str | None = Field(default=None, max_length=160)
    replaces_previous: bool = False


async def _resolve_subject(session: AsyncSession, ctx: ExecContext, kind: NoteKind, name: str | None):
    if not name:
        return None, None, None
    model = {NoteKind.CUSTOMER: Customer, NoteKind.PRODUCT: Product}.get(kind)
    if model is None:
        return None, None, clean_display_name(name)
    try:
        match = _pick(await _candidates(session, model, ctx.business_id, name), "cliente" if model is Customer else "producto", name)
    except Ambiguous:
        match = None
    if match is None:
        return None, None, clean_display_name(name)
    etype = EntityType.CUSTOMER if model is Customer else EntityType.PRODUCT
    return etype, match.entity.id, match.entity.name


@command_handler(RememberNote)
async def remember_note(cmd: RememberNote, ctx: ExecContext, session: AsyncSession, deps: CommandDeps) -> CommandResult:
    etype, eid, subject = await _resolve_subject(session, ctx, cmd.note_kind, cmd.subject_name)
    superseded = 0
    if cmd.replaces_previous and subject:
        previous = (
            await session.execute(
                select(BusinessNote).where(
                    BusinessNote.business_id == ctx.business_id, BusinessNote.kind == cmd.note_kind.value,
                    BusinessNote.subject_name == subject, BusinessNote.superseded_at.is_(None),
                    BusinessNote.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        for note in previous:
            note.superseded_at = ctx.now
            superseded += 1
    note = BusinessNote(
        business_id=ctx.business_id, kind=cmd.note_kind.value, subject_entity_type=etype.value if etype else None,
        subject_entity_id=eid, subject_name=subject, text=cmd.text.strip(), source_message_id=ctx.source_message_id,
        created_by_actor_type=ctx.actor.type.value,
    )
    session.add(note)
    await session.flush()
    audit.record(session, ctx, entity_type=EntityType.NOTE, entity_id=note.id, action=AuditAction.CREATE, before=None,
                 after=audit.snapshot(note), command=cmd.kind)
    return CommandResult(
        kind=cmd.kind,
        data={"note": note.text, "subject": subject, "kind": cmd.note_kind.value, "superseded_previous": superseded},
        affected=[EntityRef(EntityType.NOTE, note.id, version=note.version, summary=f"nota | {note.text[:80]}")],
    )
