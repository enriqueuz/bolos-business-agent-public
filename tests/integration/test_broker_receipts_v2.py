from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.application.attachments import AttachmentService
from app.application.commands.brokerage import CompleteBrokerReceipt, RecordBrokerOperation
from app.db.models import BrokerAttachment, BrokerMovement
from app.domain.errors import NeedsChoice, ValidationFailed
from app.infra.storage.memory import MemoryAttachmentStore
from tests.integration.test_broker_attachments import _image
from tests.integration.test_brokerage import _brokerage, _ctx
from tests.integration.test_brokerage_required_bs import seed_legacy_incomplete


async def test_receipt_completion_is_atomic_idempotent_and_audited(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    created = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", operation_type="buy", operation_amount="100"), ctx)
    mid = created.affected[0].entity_id
    service = AttachmentService(session_factory, MemoryAttachmentStore())
    upload = await service.ingest(business_id=business, uploaded_by_member_id=broker, data=_image("red"))
    command = CompleteBrokerReceipt(movement_id=mid, expected_version=1, attachment_upload_id=upload.id,
                                    operation_amount_bs="80000", extracted_data={"bank_reference": "1234"})
    await bus.dispatch(command, ctx)
    await bus.dispatch(command, ctx)  # The original version is stale, but this exact upload was already applied.
    async with session_factory() as session:
        row = await session.get(BrokerMovement, mid)
        assert row.exchange_rate == Decimal("800")
        assert row.commission == Decimal("1") and row.balance_delta == Decimal("1")
        assert row.version == 2
        assert row.monetary_provenance["operation_amount_bs"]["source"] == "attachment"
        assert (await session.get(BrokerAttachment, upload.id)).extracted_data["bank_reference"] == "1234"


async def test_recent_multiple_candidates_ask_and_expired_candidates_do_not_match(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    first = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", operation_type="buy", operation_amount="100"), ctx)
    second = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", operation_type="sell", operation_amount="200"), ctx)
    for mid in [first.affected[0].entity_id, second.affected[0].entity_id]:
        await seed_legacy_incomplete(session_factory, mid)
    async with session_factory() as session, session.begin():
        for mid in [first.affected[0].entity_id, second.affected[0].entity_id]:
            row = await session.get(BrokerMovement, mid)
            row.created_at = ctx.now - timedelta(minutes=1)
    service = AttachmentService(session_factory, MemoryAttachmentStore())
    upload = await service.ingest(business_id=business, uploaded_by_member_id=broker, data=_image("blue"))
    command = CompleteBrokerReceipt(attachment_upload_id=upload.id, operation_amount_bs="80000")
    with pytest.raises(NeedsChoice) as choice:
        await bus.dispatch(command, ctx)
    assert len(choice.value.options) == 2
    with pytest.raises(ValidationFailed, match="reciente compatible"):
        await bus.dispatch(command, replace(ctx, now=ctx.now + timedelta(minutes=11)))


async def test_single_recent_candidate_completes_the_original_operation(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    created = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", operation_type="sell", operation_amount="100"), ctx)
    await seed_legacy_incomplete(session_factory, created.affected[0].entity_id)
    async with session_factory() as session, session.begin():
        row = await session.get(BrokerMovement, created.affected[0].entity_id)
        row.created_at = ctx.now - timedelta(minutes=1)
    service = AttachmentService(session_factory, MemoryAttachmentStore())
    upload = await service.ingest(business_id=business, uploaded_by_member_id=broker, data=_image("yellow"))
    result = await bus.dispatch(CompleteBrokerReceipt(attachment_upload_id=upload.id, operation_amount_bs="80000"), ctx)
    assert result.affected[0].entity_id == created.affected[0].entity_id
    assert result.data["exchange_rate"] == "800.00"


async def test_ambiguous_extraction_confirmation_rolls_back_money_and_attachment(bus, session_factory):
    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    created = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", operation_type="sell", operation_amount="100"), ctx)
    service = AttachmentService(session_factory, MemoryAttachmentStore())
    upload = await service.ingest(business_id=business, uploaded_by_member_id=broker, data=_image("green"))
    command = CompleteBrokerReceipt(movement_id=created.affected[0].entity_id, expected_version=1,
                                    attachment_upload_id=upload.id, operation_amount_bs="80000", extraction_ambiguous=True)
    preview = await bus.dispatch(command, ctx)
    assert preview.needs_confirmation
    async with session_factory() as session:
        assert (await session.get(BrokerMovement, created.affected[0].entity_id)).operation_amount_bs == Decimal("80000")
        assert (await session.get(BrokerAttachment, upload.id)).broker_movement_id is None
    await bus.dispatch(command, ctx, confirmed=True)


async def test_global_support_is_readable_but_not_writable_by_other_broker(bus, session_factory):
    business, _, one, two = await _brokerage(session_factory)
    ctx = _ctx(business, one)
    service = AttachmentService(session_factory, MemoryAttachmentStore())
    upload = await service.ingest(business_id=business, uploaded_by_member_id=one, data=_image("white"))
    result = await bus.dispatch(RecordBrokerOperation(exchange_rate="800", operation_type="buy", operation_amount="100",
                                customer_name="Juan", customer_classification="global", attachment_upload_id=upload.id), ctx)
    assert (await service.detail(business_id=business, member_id=two, attachment_id=upload.id))["movement_id"] == str(result.affected[0].entity_id)
    another = await service.ingest(business_id=business, uploaded_by_member_id=two, data=_image("black"))
    with pytest.raises(ValidationFailed, match="propia cuenta"):
        await bus.dispatch(CompleteBrokerReceipt(movement_id=result.affected[0].entity_id, expected_version=1,
                                                 attachment_upload_id=another.id, operation_amount_bs="80000"), _ctx(business, two))


@pytest.mark.parametrize("confidence, amount, phrase", [
    ("unreadable", None, "No pude leer el monto"),
    ("ambiguous", "81000", "identificar con seguridad"),
    ("ambiguous", None, "identificar con seguridad"),
])
async def test_uncertain_replacement_explains_preview_and_preserves_original_until_confirmed(
    bus, session_factory, confidence, amount, phrase,
):
    from sqlalchemy import func, select

    from app.agent.receipts import render, render_preview

    business, _, broker, _ = await _brokerage(session_factory)
    ctx = _ctx(business, broker)
    service = AttachmentService(session_factory, MemoryAttachmentStore())
    original = await service.ingest(business_id=business, uploaded_by_member_id=broker, data=_image("red"))
    operation = await bus.dispatch(RecordBrokerOperation(operation_type="sell", operation_amount="100",
                                   operation_amount_bs="80000", attachment_upload_id=original.id), ctx)
    mid = operation.affected[0].entity_id
    replacement = await service.ingest(business_id=business, uploaded_by_member_id=broker, data=_image("green"))
    command = CompleteBrokerReceipt(movement_id=mid, expected_version=1, attachment_upload_id=replacement.id,
                                    operation_amount_bs=amount, extraction_ambiguous=confidence == "ambiguous",
                                    extracted_data={"confidence": confidence, "amount_bs": amount})
    preview = await bus.dispatch(command, ctx)
    assert preview.needs_confirmation
    assert phrase in render_preview(preview)
    assert "Sin guardar" in render_preview(preview)
    assert "Comprobante guardado" not in render_preview(preview)
    # Cancelling means never applying the frozen command: original data and support remain unchanged.
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, mid)
        assert movement.operation_amount_bs == Decimal("80000") and movement.exchange_rate == Decimal("800")
        assert movement.commission == Decimal("1") and movement.version == 1
        assert (await session.get(BrokerAttachment, original.id)).broker_movement_id == mid
        assert (await session.get(BrokerAttachment, original.id)).deleted_at is None
        assert (await session.get(BrokerAttachment, replacement.id)).broker_movement_id is None
    result = await bus.dispatch(command, ctx, confirmed=True)
    assert "Comprobante guardado" in render(result) and phrase in render(result)
    async with session_factory() as session:
        movement = await session.get(BrokerMovement, mid)
        assert movement.operation_amount_bs == Decimal(amount or "80000")
        assert movement.commission == Decimal("1") and movement.balance_delta == Decimal("1")
        assert (await session.execute(select(func.count()).select_from(BrokerMovement))).scalar_one() == 1
        assert (await session.get(BrokerAttachment, replacement.id)).broker_movement_id == mid
