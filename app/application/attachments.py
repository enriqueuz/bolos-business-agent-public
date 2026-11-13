"""Ingesta, consulta y ciclo de vida de comprobantes de corredores."""

from __future__ import annotations

import hashlib
import io
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import joinedload, selectinload

from app.application import audit
from app.application.common import bump
from app.application.context import ExecContext
from app.application.ports import AttachmentStore
from app.db.models import (
    AgentTurn,
    AttachmentBlob,
    AttachmentObjectDeletion,
    BrokerAttachment,
    BrokerMovement,
    Business,
    BusinessMember,
    Customer,
    InboundMessage,
)
from app.domain.enums import AuditAction, BusinessVertical, EntityType, MemberRole
from app.domain.errors import Conflict, NotFound, Stale, ValidationFailed

MAX_ATTACHMENT_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
THUMBNAIL_SIZE = (480, 480)
UNATTACHED_TTL = timedelta(hours=24)
RETAIN_DELETED = timedelta(days=90)
SUPPORTED_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@dataclass(frozen=True, slots=True)
class PreparedImage:
    original: bytes
    thumbnail: bytes
    sha256: str
    mime_type: str
    extension: str
    width: int
    height: int


def prepare_image(data: bytes) -> PreparedImage:
    if not data or len(data) > MAX_ATTACHMENT_BYTES:
        raise ValidationFailed("El comprobante debe pesar como máximo 16 MB.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").upper()
            mime = SUPPORTED_FORMATS.get(fmt)
            if mime is None:
                raise ValidationFailed("El comprobante debe ser una imagen JPEG, PNG o WebP.")
            if getattr(image, "n_frames", 1) != 1:
                raise ValidationFailed("Los comprobantes animados no están permitidos.")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValidationFailed("La imagen es demasiado grande para procesarla.")
            thumb = ImageOps.exif_transpose(image).convert("RGB")
            thumb.thumbnail(THUMBNAIL_SIZE)
            out = io.BytesIO()
            thumb.save(out, "WEBP", quality=78, method=4)
    except ValidationFailed:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValidationFailed("No pude leer esa imagen. Envía un JPEG, PNG o WebP válido.") from exc
    extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[fmt]
    return PreparedImage(data, out.getvalue(), hashlib.sha256(data).hexdigest(), mime, extension, width, height)


def _attachment_summary(row: BrokerAttachment) -> dict:
    return {
        "id": str(row.id),
        "mime_type": row.blob.mime_type,
        "byte_size": row.blob.byte_size,
        "width": row.blob.width,
        "height": row.blob.height,
        "filename": row.original_filename,
        "created_at": row.created_at.isoformat(),
        "version": row.version,
    }


async def associate_attachment(
    session: AsyncSession,
    ctx: ExecContext,
    attachment_id: UUID | None,
    movement: BrokerMovement,
    *,
    expected_attachment_id: UUID | None = None,
    command: str,
) -> tuple[BrokerAttachment | None, str | None]:
    """Asocia dentro de la transacción financiera y devuelve un posible motivo de confirmación."""
    if attachment_id is None:
        return None, None
    candidate = (
        await session.execute(
            select(BrokerAttachment)
            .options(selectinload(BrokerAttachment.blob))
            .where(BrokerAttachment.id == attachment_id)
            .with_for_update(of=BrokerAttachment)
        )
    ).scalar_one_or_none()
    if candidate is None or candidate.business_id != ctx.business_id or candidate.deleted_at is not None:
        raise NotFound("La carga del comprobante ya no está disponible.")
    if candidate.blob.status != "ready":
        raise ValidationFailed("El comprobante todavía no terminó de cargarse.")
    if ctx.actor.member_id and candidate.uploaded_by_member_id != ctx.actor.member_id:
        raise ValidationFailed("Esa carga pertenece a otra sesión.")
    if candidate.broker_movement_id and candidate.broker_movement_id != movement.id:
        raise Conflict("Ese archivo ya fue consumido por otro movimiento.")
    money_source = (movement.monetary_provenance or {}).get("operation_amount_bs", {})
    if money_source.get("attachment_id") == str(candidate.id) and money_source.get("extraction"):
        candidate.extracted_data = money_source["extraction"]
    if candidate.broker_movement_id == movement.id and candidate.attached_at is not None:
        return candidate, None

    current = (
        await session.execute(
            select(BrokerAttachment)
            .options(selectinload(BrokerAttachment.blob))
            .where(
                BrokerAttachment.broker_movement_id == movement.id,
                BrokerAttachment.deleted_at.is_(None),
                BrokerAttachment.id != candidate.id,
            )
            .with_for_update(of=BrokerAttachment)
        )
    ).scalar_one_or_none()
    if expected_attachment_id is not None and (current is None or current.id != expected_attachment_id):
        raise Stale("El comprobante de este movimiento cambió. Actualiza el detalle antes de reemplazarlo.")
    if current and current.blob_id == candidate.blob_id:
        candidate.deleted_at = ctx.now
        candidate.purge_after = ctx.now + UNATTACHED_TTL
        bump(candidate)
        return current, None

    duplicate = (
        await session.execute(
            select(BrokerAttachment)
            .join(BrokerMovement, BrokerMovement.id == BrokerAttachment.broker_movement_id)
            .options(joinedload(BrokerAttachment.movement).joinedload(BrokerMovement.broker))
            .where(
                BrokerAttachment.business_id == ctx.business_id,
                BrokerAttachment.blob_id == candidate.blob_id,
                BrokerAttachment.deleted_at.is_(None),
                BrokerAttachment.broker_movement_id.is_not(None),
                BrokerAttachment.broker_movement_id != movement.id,
                BrokerMovement.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    reasons = []
    if current:
        reasons.append("Este movimiento ya tiene un comprobante y será reemplazado")
    if duplicate:
        existing = duplicate.movement
        visible = (existing.broker_member_id == ctx.actor.member_id or
                   existing.customer is not None and existing.customer.classification == "global")
        actor = await session.get(BusinessMember, ctx.actor.member_id)
        if actor and actor.role == "owner":
            visible = True
        if visible:
            detail = f" del {existing.business_date.isoformat()} de {existing.broker.name if existing.broker else 'Casa'}"
            if existing.customer:
                detail += f" con {existing.customer.display_name}"
            reasons.append(f"La misma imagen ya respalda el movimiento{detail}")
        else:
            reasons.append("La misma imagen ya respalda otro movimiento")

    now = ctx.now.astimezone(UTC)
    if current:
        before = audit.snapshot(current)
        current.deleted_at = now
        current.purge_after = now + RETAIN_DELETED
        bump(current)
        audit.record(session, ctx, entity_type=EntityType.BROKER_ATTACHMENT, entity_id=current.id,
                     action=AuditAction.DELETE, before=before, after=audit.snapshot(current), command=command)
        # El índice parcial solo admite un soporte activo. Fuerza primero la baja lógica para que
        # PostgreSQL no dependa del orden en que el ORM agrupe los dos UPDATE del reemplazo.
        await session.flush()
    candidate.broker_movement_id = movement.id
    candidate.attached_at = now
    candidate.purge_after = None
    bump(candidate)
    audit.record(session, ctx, entity_type=EntityType.BROKER_ATTACHMENT, entity_id=candidate.id,
                 action=AuditAction.CREATE, before=None, after=audit.snapshot(candidate), command=command)
    return candidate, ". ".join(reasons) + ". ¿Quieres continuar?" if reasons else None


async def retire_movement_attachment(session: AsyncSession, ctx: ExecContext, movement_id: UUID, *, command: str) -> None:
    row = (
        await session.execute(
            select(BrokerAttachment).where(
                BrokerAttachment.broker_movement_id == movement_id,
                BrokerAttachment.deleted_at.is_(None),
            ).with_for_update(of=BrokerAttachment)
        )
    ).scalar_one_or_none()
    if row:
        before = audit.snapshot(row)
        row.deleted_at = ctx.now
        row.purge_after = ctx.now + RETAIN_DELETED
        bump(row)
        audit.record(session, ctx, entity_type=EntityType.BROKER_ATTACHMENT, entity_id=row.id,
                     action=AuditAction.DELETE, before=before, after=audit.snapshot(row), command=command)


class AttachmentService:
    def __init__(self, factory: async_sessionmaker[AsyncSession], store: AttachmentStore, *, signed_url_ttl_s: int = 900) -> None:
        self._factory = factory
        self.store = store
        self.signed_url_ttl_s = signed_url_ttl_s

    async def ingest(
        self,
        *,
        business_id: UUID,
        uploaded_by_member_id: UUID,
        data: bytes,
        original_filename: str | None = None,
        source_message_id: UUID | None = None,
    ) -> BrokerAttachment:
        prepared = prepare_image(data)
        if source_message_id:
            async with self._factory() as session:
                existing = (
                    await session.execute(
                        select(BrokerAttachment)
                        .options(selectinload(BrokerAttachment.blob))
                        .where(BrokerAttachment.source_message_id == source_message_id)
                    )
                ).scalar_one_or_none()
                if existing:
                    if existing.blob.sha256 != prepared.sha256:
                        raise Conflict("Ese mensaje ya contiene otra imagen.")
                    if existing.blob.status != "ready":
                        await self.store.put(existing.blob.original_key, prepared.original, prepared.mime_type)
                        await self.store.put(existing.blob.thumbnail_key, prepared.thumbnail, "image/webp")
                        async with self._factory() as ready_session, ready_session.begin():
                            blob = await ready_session.get(AttachmentBlob, existing.blob_id, with_for_update=True)
                            if blob:
                                blob.status = "ready"
                    return existing
        prefix = f"{business_id}/{prepared.sha256}"
        original_key = f"{prefix}/original.{prepared.extension}"
        thumbnail_key = f"{prefix}/thumbnail.webp"
        async with self._factory() as session, session.begin():
            blob_id = (
                await session.execute(
                    insert(AttachmentBlob)
                    .values(
                        id=uuid.uuid4(), business_id=business_id, sha256=prepared.sha256,
                        original_key=original_key, thumbnail_key=thumbnail_key,
                        mime_type=prepared.mime_type, byte_size=len(prepared.original),
                        width=prepared.width, height=prepared.height, status="pending",
                    )
                    .on_conflict_do_nothing()
                    .returning(AttachmentBlob.id)
                )
            ).scalar_one_or_none()
            if blob_id is None:
                blob_id = (
                    await session.execute(
                        select(AttachmentBlob.id).where(
                            AttachmentBlob.business_id == business_id,
                            AttachmentBlob.sha256 == prepared.sha256,
                        )
                    )
                ).scalar_one()
            filename = Path(original_filename).name[:180] if original_filename else None
            if source_message_id is None:
                row = BrokerAttachment(
                    business_id=business_id,
                    blob_id=blob_id,
                    uploaded_by_member_id=uploaded_by_member_id,
                    original_filename=filename,
                )
                session.add(row)
                await session.flush()
                attachment_id = row.id
            else:
                # Dos entregas concurrentes del mismo webhook esperan en el índice y convergen en
                # una sola carga; no se filtra un IntegrityError fuera de la saga.
                attachment_id = (
                    await session.execute(
                        insert(BrokerAttachment)
                        .values(
                            id=uuid.uuid4(),
                            business_id=business_id,
                            blob_id=blob_id,
                            uploaded_by_member_id=uploaded_by_member_id,
                            source_message_id=source_message_id,
                            original_filename=filename,
                        )
                        .on_conflict_do_nothing(
                            index_elements=["source_message_id"],
                            index_where=BrokerAttachment.source_message_id.is_not(None),
                        )
                        .returning(BrokerAttachment.id)
                    )
                ).scalar_one_or_none()
                if attachment_id is None:
                    existing = (
                        await session.execute(
                            select(BrokerAttachment).where(
                                BrokerAttachment.source_message_id == source_message_id
                            )
                        )
                    ).scalar_one()
                    if existing.blob_id != blob_id:
                        raise Conflict("Ese mensaje ya contiene otra imagen.")
                    attachment_id = existing.id
        await self.store.put(original_key, prepared.original, prepared.mime_type)
        await self.store.put(thumbnail_key, prepared.thumbnail, "image/webp")
        async with self._factory() as session, session.begin():
            blob = await session.get(AttachmentBlob, blob_id, with_for_update=True)
            if blob is None:
                raise NotFound("La carga del comprobante ya no está disponible.")
            blob.status = "ready"
        async with self._factory() as session:
            return (
                await session.execute(
                    select(BrokerAttachment)
                    .options(selectinload(BrokerAttachment.blob))
                    .where(BrokerAttachment.id == attachment_id)
                )
            ).scalar_one()

    async def bytes_for_vision(self, attachment_id: UUID) -> tuple[bytes, str]:
        async with self._factory() as session:
            row = await session.get(BrokerAttachment, attachment_id)
            if row is None or row.deleted_at is not None or row.blob.status != "ready":
                raise NotFound("Comprobante no encontrado.")
            return await self.store.get(row.blob.original_key), row.blob.mime_type

    async def bind_source(
        self, *, business_id: UUID, uploaded_by_member_id: UUID, attachment_id: UUID, source_message_id: UUID
    ) -> BrokerAttachment:
        """Vincula una carga del panel al mensaje que la consumirá, sin aceptar cargas ajenas."""
        async with self._factory() as session, session.begin():
            row = (
                await session.execute(
                    select(BrokerAttachment)
                    .options(selectinload(BrokerAttachment.blob))
                    .where(BrokerAttachment.id == attachment_id)
                    .with_for_update(of=BrokerAttachment)
                )
            ).scalar_one_or_none()
            if (
                row is None
                or row.business_id != business_id
                or row.uploaded_by_member_id != uploaded_by_member_id
                or row.deleted_at is not None
                or row.broker_movement_id is not None
            ):
                raise NotFound("La carga del comprobante ya no está disponible.")
            if row.source_message_id not in {None, source_message_id}:
                raise Conflict("Esa carga ya fue usada en otro mensaje.")
            row.source_message_id = source_message_id
            await session.flush()
            return row

    async def preceding_receipts(
        self, business_id: UUID, member_id: UUID, inbound_id: UUID, *, now: datetime,
    ) -> list[BrokerAttachment]:
        """Only the immediately preceding run of uncaptioned, unquoted images is eligible.

        Return up to two: multiple candidates must never silently become a single choice.
        No new temporary store; inbound chronology and the existing uploads are authoritative.
        """
        async with self._factory() as session:
            current = await session.get(InboundMessage, inbound_id)
            business = await session.get(Business, business_id)
            if (current is None or business is None or current.business_id != business_id or not current.from_phone
                    or current.type != "text" or current.reply_to_provider_message_id):
                return []
            minutes = min(max(int((business.settings or {}).get("broker_receipt_window_minutes", 10)), 1), 60)
            previous = (await session.scalars(select(InboundMessage).where(
                InboundMessage.business_id == business_id,
                InboundMessage.from_phone == current.from_phone,
                InboundMessage.received_at < current.received_at,
                InboundMessage.received_at >= now - timedelta(minutes=minutes),
            ).order_by(InboundMessage.received_at.desc()).limit(2))).all()
            candidates = []
            for message in previous:
                if (message.provider != current.provider or message.type != "image"
                        or (message.body or "").strip() or message.reply_to_provider_message_id):
                    break
                # Only images explicitly left without a destination may move forward. An image
                # already offered to an earlier operation/draft must not acquire a new destination.
                contextless = await session.scalar(select(AgentTurn.id).where(
                    AgentTurn.inbound_message_id == message.id,
                    AgentTurn.business_id == business_id,
                    AgentTurn.stages.contains(["image:needs_context"]),
                ).limit(1))
                if contextless is None:
                    break
                attachment = (await session.scalars(select(BrokerAttachment).where(
                    BrokerAttachment.business_id == business_id,
                    BrokerAttachment.uploaded_by_member_id == member_id,
                    BrokerAttachment.source_message_id == message.id,
                    BrokerAttachment.deleted_at.is_(None),
                    BrokerAttachment.broker_movement_id.is_(None),
                ))).one_or_none()
                if attachment is None or attachment.blob.status != "ready":
                    break
                used = await session.scalar(select(BrokerAttachment.id).where(
                    BrokerAttachment.business_id == business_id,
                    BrokerAttachment.blob_id == attachment.blob_id,
                    BrokerAttachment.broker_movement_id.is_not(None),
                ).limit(1))
                if used:
                    break
                candidates.append(attachment)
            return candidates

    async def for_source(self, business_id: UUID, source_message_id: UUID) -> BrokerAttachment | None:
        async with self._factory() as session:
            return (
                await session.execute(
                    select(BrokerAttachment).options(selectinload(BrokerAttachment.blob)).where(
                        BrokerAttachment.business_id == business_id,
                        BrokerAttachment.source_message_id == source_message_id,
                        BrokerAttachment.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()

    async def active_for_movement(self, business_id: UUID, movement_id: UUID) -> BrokerAttachment | None:
        async with self._factory() as session:
            return (
                await session.execute(
                    select(BrokerAttachment).options(selectinload(BrokerAttachment.blob)).where(
                        BrokerAttachment.business_id == business_id,
                        BrokerAttachment.broker_movement_id == movement_id,
                        BrokerAttachment.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()

    async def summary_for_movement(self, business_id: UUID, movement_id: UUID, *, urls: bool = False) -> dict | None:
        row = await self.active_for_movement(business_id, movement_id)
        if row is None:
            return None
        out = _attachment_summary(row)
        if urls:
            out["thumbnail_url"] = await self.store.signed_url(row.blob.thumbnail_key, expires_s=self.signed_url_ttl_s)
            out["original_url"] = await self.store.signed_url(row.blob.original_key, expires_s=self.signed_url_ttl_s)
        return out

    async def gallery(
        self,
        *,
        business_id: UUID,
        member_id: UUID,
        broker_id: UUID | None = None,
        account_scope: str = "all",
        logical_type: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        from app.application.queries.brokerage import BrokerageQueries

        async with self._factory() as session:
            member = await session.get(BusinessMember, member_id)
            if member is None or not member.is_active or member.business_id != business_id or member.role not in {"owner", "broker"}:
                raise NotFound("Comprobantes no encontrados.")
            filters = [
                BrokerAttachment.business_id == business_id,
                BrokerAttachment.deleted_at.is_(None),
                BrokerMovement.deleted_at.is_(None),
            ]
            if member.role == "broker":
                if account_scope == "house" or (broker_id and broker_id != member_id):
                    raise ValidationFailed("Solo puedes consultar tus propios comprobantes.")
                filters.append(or_(BrokerMovement.broker_member_id == member_id, BrokerMovement.customer.has(Customer.classification == "global")))
            elif broker_id:
                if account_scope == "house":
                    raise ValidationFailed("No combines Casa con un corredor.")
                filters.append(BrokerMovement.broker_member_id == broker_id)
            elif account_scope == "house":
                filters.append(BrokerMovement.broker_member_id.is_(None))
            elif account_scope == "broker":
                raise ValidationFailed("Indica el corredor.")
            elif account_scope != "all":
                raise ValidationFailed("Ámbito de cuenta inválido.")
            if logical_type:
                filters.append(BrokerageQueries._logical_type_filter(logical_type))
            if date_from:
                filters.append(BrokerMovement.business_date >= date_from)
            if date_to:
                filters.append(BrokerMovement.business_date <= date_to)
            total = (
                await session.execute(
                    select(func.count()).select_from(BrokerAttachment).join(BrokerMovement).where(*filters)
                )
            ).scalar_one()
            rows = (
                await session.execute(
                    select(BrokerAttachment)
                    .join(BrokerMovement)
                    .options(
                        joinedload(BrokerAttachment.blob),
                        joinedload(BrokerAttachment.movement).joinedload(BrokerMovement.broker),
                    )
                    .where(*filters)
                    .order_by(BrokerMovement.business_date.desc(), BrokerAttachment.created_at.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).scalars().all()
        items = []
        for row in rows:
            item = _attachment_summary(row)
            item.update(
                {
                    "movement_id": str(row.movement.id),
                    "business_date": row.movement.business_date.isoformat(),
                    "broker": row.movement.broker.name if row.movement.broker else "Casa",
                    "account_kind": "broker" if row.movement.broker_member_id else "house",
                    "logical_type": BrokerageQueries.logical_type(row.movement),
                    "customer": row.movement.customer.display_name if row.movement.customer else None,
                    "thumbnail_url": await self.store.signed_url(
                        row.blob.thumbnail_key, expires_s=self.signed_url_ttl_s
                    ),
                }
            )
            items.append(item)
        return {"total": total, "items": items, "limit": limit, "offset": offset}

    async def detail(self, *, business_id: UUID, member_id: UUID, attachment_id: UUID) -> dict:
        async with self._factory() as session:
            member = await session.get(BusinessMember, member_id)
            row = (
                await session.execute(
                    select(BrokerAttachment)
                    .options(
                        joinedload(BrokerAttachment.blob),
                        joinedload(BrokerAttachment.movement).joinedload(BrokerMovement.broker),
                    )
                    .where(
                        BrokerAttachment.id == attachment_id,
                        BrokerAttachment.business_id == business_id,
                        BrokerAttachment.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if (
                member is None
                or not member.is_active
                or member.business_id != business_id
                or member.role not in {"owner", "broker"}
                or row is None
                or row.movement is None
                or row.movement.deleted_at is not None
                or (member.role == "broker" and row.movement.broker_member_id != member_id
                    and (row.movement.customer is None or row.movement.customer.classification != "global"))
            ):
                raise NotFound("Comprobante no encontrado.")
        out = _attachment_summary(row)
        out.update(
            {
                "movement_id": str(row.movement.id),
                "business_date": row.movement.business_date.isoformat(),
                "broker": row.movement.broker.name if row.movement.broker else "Casa",
                "account_kind": "broker" if row.movement.broker_member_id else "house",
                "customer": row.movement.customer.display_name if row.movement.customer else None,
                "original_url": await self.store.signed_url(row.blob.original_key, expires_s=self.signed_url_ttl_s),
            }
        )
        return out

    async def purge_due(self) -> int:
        now = datetime.now(UTC)
        async with self._factory() as session, session.begin():
            rows = (
                await session.execute(
                    select(BrokerAttachment).where(
                        or_(
                            BrokerAttachment.purge_after <= now,
                            (BrokerAttachment.broker_movement_id.is_(None))
                            & (BrokerAttachment.created_at <= now - UNATTACHED_TTL),
                        )
                    )
                )
            ).scalars().all()
            blob_ids = {row.blob_id for row in rows}
            for row in rows:
                await session.delete(row)
        deleted = len(rows)
        for blob_id in blob_ids:
            async with self._factory() as session, session.begin():
                blob = await session.get(AttachmentBlob, blob_id, with_for_update=True)
                if blob is None:
                    continue
                refs = (
                    await session.execute(
                        select(func.count()).select_from(BrokerAttachment).where(BrokerAttachment.blob_id == blob_id)
                    )
                ).scalar_one()
                if refs:
                    continue
                await session.execute(
                    insert(AttachmentObjectDeletion)
                    .values([{"object_key": blob.original_key}, {"object_key": blob.thumbnail_key}])
                    .on_conflict_do_nothing(index_elements=["object_key"])
                )
                await session.delete(blob)
        async with self._factory() as session:
            pending = (
                await session.execute(
                    select(AttachmentObjectDeletion).where(AttachmentObjectDeletion.completed_at.is_(None)).limit(100)
                )
            ).scalars().all()
        for item in pending:
            await self.store.delete(item.object_key)
            async with self._factory() as session, session.begin():
                current = await session.get(AttachmentObjectDeletion, item.id)
                if current:
                    current.completed_at = now
        return deleted + len(pending)

    async def ensure_brokerage_member(self, business_id: UUID, member_id: UUID) -> BusinessMember:
        async with self._factory() as session:
            member = await session.get(BusinessMember, member_id)
            if (
                member is None or member.business_id != business_id
                or member.business.vertical != BusinessVertical.BROKERAGE.value
                or member.role not in {MemberRole.OWNER.value, MemberRole.BROKER.value}
            ):
                raise ValidationFailed("El módulo de corredores no está habilitado para tu cuenta.")
            return member
