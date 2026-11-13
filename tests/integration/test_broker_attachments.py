from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from PIL import Image
from sqlalchemy import func, select

from app.application.attachments import AttachmentService
from app.application.commands.brokerage import (
    AttachBrokerAttachment,
    DeleteBrokerMovement,
    RecordBrokerAccountPayment,
)
from app.application.context import Actor, ExecContext
from app.db.models import BrokerAttachment, BrokerMovement, Business, BusinessMember, InboundMessage
from app.domain.enums import ActorType, BusinessVertical, MemberRole, OnboardingState
from app.domain.errors import NotFound
from app.infra.storage.memory import MemoryAttachmentStore

NOW = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)


def _image(color: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (800, 500), color).save(out, "PNG")
    return out.getvalue()


async def _business(session_factory):
    async with session_factory() as session, session.begin():
        business = Business(
            name="Cambios Centro",
            vertical=BusinessVertical.BROKERAGE.value,
            onboarding_state=OnboardingState.DONE.value,
        )
        session.add(business)
        await session.flush()
        owner = BusinessMember(
            business_id=business.id, phone="584140000010", name="Enrique", role=MemberRole.OWNER.value
        )
        broker = BusinessMember(
            business_id=business.id, phone="584140000011", name="Samu", role=MemberRole.BROKER.value
        )
        other_broker = BusinessMember(
            business_id=business.id, phone="584140000012", name="Pedro", role=MemberRole.BROKER.value
        )
        session.add_all([owner, broker, other_broker])
        await session.flush()
    return business.id, owner.id, broker.id, other_broker.id


def _ctx(business_id, member_id):
    return ExecContext(
        business_id=business_id,
        actor=Actor(ActorType.PANEL, member_id),
        now=NOW,
    )


async def test_concurrent_source_message_retries_converge_on_one_upload(session_factory):
    business_id, _owner_id, broker_id, _other_broker_id = await _business(session_factory)
    async with session_factory() as session, session.begin():
        inbound = InboundMessage(
            business_id=business_id,
            provider="kapso",
            provider_message_id="wamid.same-image",
            from_phone="584140000011",
            type="image",
            raw={},
        )
        session.add(inbound)
        await session.flush()
        source_message_id = inbound.id
    service = AttachmentService(session_factory, MemoryAttachmentStore())

    first, second = await asyncio.gather(*(
        service.ingest(
            business_id=business_id,
            uploaded_by_member_id=broker_id,
            data=_image("white"),
            source_message_id=source_message_id,
        )
        for _ in range(2)
    ))

    assert first.id == second.id
    async with session_factory() as session:
        assert (
            await session.execute(
                select(func.count()).select_from(BrokerAttachment).where(
                    BrokerAttachment.source_message_id == source_message_id
                )
            )
        ).scalar_one() == 1


async def test_attachment_association_replacement_dedup_permissions_and_retention(bus, session_factory):
    business_id, owner_id, broker_id, other_broker_id = await _business(session_factory)
    store = MemoryAttachmentStore()
    service = AttachmentService(session_factory, store)
    first_upload = await service.ingest(
        business_id=business_id, uploaded_by_member_id=broker_id, data=_image("white"), original_filename="uno.png"
    )

    created = await bus.dispatch(
        RecordBrokerAccountPayment(amount=Decimal("100"), attachment_upload_id=first_upload.id),
        _ctx(business_id, broker_id),
    )
    movement_id = created.affected[0].entity_id
    assert created.data["attachment"] is True
    assert (await service.summary_for_movement(business_id, movement_id))["filename"] == "uno.png"

    replacement = await service.ingest(
        business_id=business_id, uploaded_by_member_id=broker_id, data=_image("blue"), original_filename="dos.png"
    )
    preview = await bus.dispatch(
        AttachBrokerAttachment(
            movement_id=movement_id,
            attachment_upload_id=replacement.id,
            expected_attachment_id=first_upload.id,
        ),
        _ctx(business_id, broker_id),
    )
    assert preview.needs_confirmation
    assert (await service.active_for_movement(business_id, movement_id)).id == first_upload.id

    await bus.dispatch(
        AttachBrokerAttachment(
            movement_id=movement_id,
            attachment_upload_id=replacement.id,
            expected_attachment_id=first_upload.id,
        ),
        _ctx(business_id, broker_id),
        confirmed=True,
    )
    async with session_factory() as session:
        old = await session.get(BrokerAttachment, first_upload.id)
        assert old.deleted_at == NOW
        assert old.purge_after == NOW + timedelta(days=90)

    reused = await service.ingest(
        business_id=business_id, uploaded_by_member_id=broker_id, data=_image("blue")
    )
    duplicate_preview = await bus.dispatch(
        RecordBrokerAccountPayment(amount=Decimal("50"), attachment_upload_id=reused.id),
        _ctx(business_id, broker_id),
    )
    assert duplicate_preview.needs_confirmation
    assert NOW.date().isoformat() in duplicate_preview.confirmation_reason
    assert "Samu" in duplicate_preview.confirmation_reason
    async with session_factory() as session:
        assert (await session.execute(select(func.count()).select_from(BrokerMovement))).scalar_one() == 1
    duplicate = await bus.dispatch(
        RecordBrokerAccountPayment(amount=Decimal("50"), attachment_upload_id=reused.id),
        _ctx(business_id, broker_id),
        confirmed=True,
    )
    assert duplicate.data["attachment"] is True

    own = await service.gallery(business_id=business_id, member_id=broker_id)
    all_rows = await service.gallery(business_id=business_id, member_id=owner_id)
    other = await service.gallery(business_id=business_id, member_id=other_broker_id)
    assert own["total"] == all_rows["total"] == 2
    assert other["total"] == 0
    assert all(item["broker"] == "Samu" for item in own["items"])

    signed = False
    original_signed_url = store.signed_url

    async def track_signed_url(key, *, expires_s):
        nonlocal signed
        signed = True
        return await original_signed_url(key, expires_s=expires_s)

    store.signed_url = track_signed_url
    with pytest.raises(NotFound):
        await service.detail(
            business_id=business_id,
            member_id=other_broker_id,
            attachment_id=replacement.id,
        )
    assert not signed

    await bus.dispatch(
        DeleteBrokerMovement(movement_id=movement_id), _ctx(business_id, broker_id), confirmed=True
    )
    async with session_factory() as session, session.begin():
        retired = await session.get(BrokerAttachment, replacement.id)
        assert retired.deleted_at == NOW
        assert retired.purge_after == NOW + timedelta(days=90)

        expired = await session.get(BrokerAttachment, first_upload.id)
        expired.purge_after = datetime.now(UTC) - timedelta(seconds=1)
    assert await service.purge_due() >= 1
    async with session_factory() as session:
        assert await session.get(BrokerAttachment, first_upload.id) is None
    assert not any(first_upload.blob.sha256 in key for key in store.objects)
