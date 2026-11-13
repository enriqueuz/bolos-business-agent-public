"""Production regression: changing provenance must not turn invented money into a write."""

from decimal import Decimal

from sqlalchemy import func, select

from app.application.conversation import conversation_scope
from app.db.models import BrokerAttachment, BrokerDraftExecution, BrokerMovement
from tests.agent.test_brokerage_vertical import _receipt_image, _set_brokerage
from tests.agent.test_pipeline import PHONE, send


async def test_tony_retries_remain_draft_then_image_completes(env, script, business, session_factory):
    container, console = env
    await _set_brokerage(session_factory)
    script.steps = [
        {"tool": "record_pending_broker_purchase", "args": {"customer_name": "Tony", "amount": "330",
         "operation_amount_bs": "306900", "monetary_from_attachment": True, "text_money_sources": {"principal_usd": "330", 'operation_amount_bs': "306900"}}},
        {"tool": "record_pending_broker_purchase", "args": {"customer_name": "Tony", "amount": "330",
         "operation_amount_bs": "306900", "monetary_from_attachment": False, "text_money_sources": {"principal_usd": "330", 'operation_amount_bs': "306900"}}},
        {"text": ""},
    ]
    first = await send(container, "330$ Tony")
    assert "bolívares" in first.replies[0]["text"]
    scope = conversation_scope(business.id, PHONE)
    draft = await container.store.get_broker_money_draft(scope)
    assert len(draft.operations()) == 1
    assert draft.command_payload["operation_amount_bs"] is None
    assert draft.command_payload["exchange_rate"] is None
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 0

    async def download(_url):
        return _receipt_image(), "image/png"

    console.download_media = download
    script.steps = [{"tool": "resume_broker_operation", "args": {"extraction": {
        "amount_bs": "310200", "confidence": "clear"}}}]
    out = await send(container, "", kind="image", media_url="https://media.invalid/receipt")
    assert "pendiente o liquidada" in out.replies[0]["text"]
    out = await send(container, "Pendiente")
    if "tipo de cliente" in out.replies[0]["text"]:
        out = await send(container, "Global")
    assert "310.200" in out.replies[0]["text"]
    async with session_factory() as db:
        movement = (await db.scalars(select(BrokerMovement))).one()
        attachment = (await db.scalars(select(BrokerAttachment))).one()
        assert movement.operation_amount == 330
        assert movement.operation_amount_bs == Decimal("310200")
        assert movement.exchange_rate == Decimal("940")
        assert attachment.broker_movement_id == movement.id
        assert movement.monetary_provenance["operation_amount_bs"]["attachment_id"] == str(attachment.id)
        assert await db.scalar(select(func.count()).select_from(BrokerDraftExecution)) == 1
    assert await container.store.get_broker_money_draft(scope) is None


async def test_dollar_token_cannot_back_bolivars(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_operation", "args": {
        "operation_type": "sell", "operation_amount": "330", "operation_amount_bs": "330", "text_money_sources": {"principal_usd": "330", 'operation_amount_bs': "330"}}}, {"text": ""}]
    await send(container, "Vendí 330$")
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 0


async def test_valid_text_can_correct_bad_origin_hint(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_operation", "args": {
        "operation_type": "sell", "operation_amount": "330", "operation_amount_bs": "310200",
        "monetary_from_attachment": True, "text_money_sources": {"principal_usd": "330", 'operation_amount_bs': "310.200"}}}]
    await send(container, "Vendí 330$ por 310.200 Bs")
    async with session_factory() as db:
        row = (await db.scalars(select(BrokerMovement))).one()
        assert row.exchange_rate == 940
        assert row.monetary_provenance["operation_amount_bs"]["source"] == "explicit"


async def test_stale_draft_replay_does_not_duplicate(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_operation", "args": {"operation_type": "sell", "operation_amount": "100"}}]
    await send(container, "Vendí 100$")
    scope = conversation_scope(business.id, PHONE)
    stale = await container.store.get_broker_money_draft(scope)
    for _ in range(2):
        await container.store.set_broker_money_draft(scope, stale)
        script.steps = [{"tool": "resume_broker_operation", "args": {"exchange_rate": "940", "text_money_sources": {'exchange_rate': "940"}}}]
        await send(container, "Tasa 940")
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 1
        assert await db.scalar(select(func.count()).select_from(BrokerDraftExecution)) == 1
    assert await container.store.get_broker_money_draft(scope) is None


async def test_correction_cannot_invent_rate(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_operation", "args": {
        "operation_type": "sell", "operation_amount": "330", "exchange_rate": "940", "text_money_sources": {"principal_usd": "330", 'exchange_rate': "940"}}}]
    await send(container, "Vendí 330$ a tasa 940")
    script.steps = [{"tool": "edit_broker_movement", "args": {"handle": "R1", "changes": {"exchange_rate": "930"}, "text_money_sources": {"exchange_rate": "930"}}},
                    {"tool": "need_more_info", "args": {"question": "¿Cuál fue la tasa?"}}]
    await send(container, "Corrige la tasa")
    async with session_factory() as db:
        row = (await db.scalars(select(BrokerMovement))).one()
        assert row.exchange_rate == 940 and row.operation_amount_bs == 310200


async def test_unverified_caption_does_not_gain_authority_from_attachment(env, script, business, session_factory):
    container, console = env
    await _set_brokerage(session_factory)

    async def download(_url):
        return _receipt_image(), "image/png"

    console.download_media = download
    script.steps = [{"tool": "record_broker_operation", "args": {
        "operation_type": "sell", "operation_amount": "330", "operation_amount_bs": "306900",
        "monetary_from_attachment": True, "text_money_sources": {"principal_usd": "330", 'operation_amount_bs': "306900"}}}, {"text": ""}]
    await send(container, "Vendí 330$", kind="image", media_url="https://media.invalid/receipt")
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 0
        attachment = (await db.scalars(select(BrokerAttachment))).one()
        assert attachment.broker_movement_id is None


async def test_concurrent_draft_completion_commits_one_result(env, script, business, session_factory):
    import asyncio
    from uuid import UUID

    from app.application.bus import load_command
    from app.application.context import Actor, ExecContext
    from app.db.models import BusinessMember
    from app.domain.enums import ActorType

    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_operation", "args": {"operation_type": "sell", "operation_amount": "100"}}]
    await send(container, "Vendí 100$")
    scope = conversation_scope(business.id, PHONE)
    draft = await container.store.get_broker_money_draft(scope)
    command = load_command(draft.command_kind, {**draft.command_payload, "draft_operation_id": draft.operation_id,
                                               "exchange_rate": "940"})
    async with session_factory() as db:
        member = (await db.scalars(select(BusinessMember))).one()
    ctx = ExecContext(business_id=business.id, actor=Actor(ActorType.PANEL, member.id))
    results = await asyncio.gather(*(container.bus.dispatch(command, ctx) for _ in range(4)))
    assert len({result.affected[0].entity_id for result in results}) == 1
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 1
        assert await db.scalar(select(func.count()).select_from(BrokerDraftExecution)) == 1
        saved = (await db.scalars(select(BrokerDraftExecution))).one()
        assert saved.operation_id == UUID(draft.operation_id)


async def test_money_in_message_still_requires_a_declared_source(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_broker_operation", "args": {
        "operation_type": "sell", "operation_amount": "330", "exchange_rate": "940"}}, {"text": ""}]
    await send(container, "Vendí 330$ a tasa 940")
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 0


async def test_bare_principal_cannot_be_reused_as_bolivars(env, script, business, session_factory):
    container, _ = env
    await _set_brokerage(session_factory)
    script.steps = [{"tool": "record_pending_broker_purchase", "args": {
        "customer_name": "Tony", "amount": "330", "operation_amount_bs": "330",
        "text_money_sources": {"principal_usd": "330", "operation_amount_bs": "330"}}}, {"text": ""}]
    await send(container, "Tony 330")
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(BrokerMovement)) == 0
