"""API del panel para la vertical brokerage."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select

from app.api.deps import get_container
from app.api.panel.auth import PanelSession, current_session
from app.application.brokerage_money import BrokerDecimal
from app.application.commands.brokerage import (
    AttachBrokerAttachment,
    CreateBrokerMember,
    DeleteBrokerMovement,
    EditBrokerMovement,
    RecordBrokerAccountPayment,
    RecordBrokerAccountPurchase,
    RecordBrokerAccountWithdrawal,
    RecordBrokerCustomerPayout,
    RecordBrokerOperation,
    RecordBrokerReceivablePayment,
    RecordPendingBrokerPurchase,
    SetBrokerMemberActive,
    UpdateBrokerMember,
)
from app.application.commands.customers import UpsertCustomer
from app.application.exports.brokerage import BrokerageWorkbook, workbook_filename
from app.application.queries.brokerage import BrokerageQueries
from app.application.queries.panel import PanelQueries
from app.container import Container
from app.db.models import (
    AgentTurn,
    AuditLog,
    BrokerDraftExecution,
    BrokerMovement,
    BusinessMember,
    Customer,
    CustomerPayment,
    InboundMessage,
    ToolCall,
)
from app.domain.brokerage import q_money
from app.domain.customers import normalize_nickname
from app.domain.dates import PeriodName, local_today
from app.domain.enums import BusinessVertical, EntityType, MemberRole
from app.domain.errors import NeedsConfirmation

router = APIRouter(prefix="/brokerage", tags=["panel-brokerage"])
Session = Annotated[PanelSession, Depends(current_session)]


def container_dep(request: Request) -> Container:
    return get_container(request)


C = Annotated[Container, Depends(container_dep)]


def _require_brokerage(session: PanelSession) -> None:
    if session.vertical != BusinessVertical.BROKERAGE or session.role not in {
        MemberRole.OWNER,
        MemberRole.BROKER,
    }:
        raise HTTPException(403, "El módulo de corredores no está habilitado para tu cuenta.")


def _require_owner(session: PanelSession) -> None:
    _require_brokerage(session)
    if session.role != MemberRole.OWNER:
        raise HTTPException(403, "Solo el dueño puede hacer esta acción.")


def _result(res) -> dict:
    from app.application.audit import json_safe

    return {
        "ok": True,
        "data": json_safe(res.data),
        "affected": [
            {"entity_type": row.entity_type.value, "entity_id": str(row.entity_id), "version": row.version}
            for row in res.affected
        ],
    }


class AccountPaymentBody(BaseModel):
    amount: BrokerDecimal = Field(gt=0)
    broker_id: UUID | None = None
    description: str | None = Field(default=None, max_length=300)
    business_date: date | None = None
    attachment_upload_id: UUID | None = None
    idempotency_key: UUID | None = None


class OperationBody(BaseModel):
    operation_type: Literal["buy", "sell"]
    settlement: Literal["settled", "pending"] = "settled"
    operation_amount: BrokerDecimal = Field(gt=0)
    delivered_amount: BrokerDecimal | None = Field(default=None, gt=0)
    customer_name: str | None = Field(default=None, max_length=160)
    customer_id: UUID | None = None
    force_new_customer: bool = False
    broker_id: UUID | None = None
    description: str | None = Field(default=None, max_length=300)
    business_date: date | None = None
    attachment_upload_id: UUID | None = None

    received_amount: BrokerDecimal | None = Field(default=None, gt=0)
    operation_amount_bs: BrokerDecimal | None = Field(default=None, gt=0)
    exchange_rate: BrokerDecimal | None = Field(default=None, gt=0)

    customer_classification: Literal["global", "personal"] | None = None


class AccountPurchaseBody(BaseModel):
    amount: BrokerDecimal = Field(gt=0)
    broker_id: UUID | None = None
    description: str | None = Field(default=None, max_length=300)
    business_date: date | None = None
    attachment_upload_id: UUID | None = None

    received_amount: BrokerDecimal | None = Field(default=None, gt=0)
    operation_amount_bs: BrokerDecimal | None = Field(default=None, gt=0)
    exchange_rate: BrokerDecimal | None = Field(default=None, gt=0)


class PendingPurchaseBody(BaseModel):
    customer_name: str = Field(min_length=1, max_length=160)
    customer_id: UUID | None = None
    amount: BrokerDecimal = Field(gt=0)
    broker_id: UUID | None = None
    description: str | None = Field(default=None, max_length=300)
    business_date: date | None = None
    force_new_customer: bool = False
    attachment_upload_id: UUID | None = None

    received_amount: BrokerDecimal | None = Field(default=None, gt=0)
    operation_amount_bs: BrokerDecimal | None = Field(default=None, gt=0)
    exchange_rate: BrokerDecimal | None = Field(default=None, gt=0)

    customer_classification: Literal["global", "personal"] | None = None


class ReceivablePaymentBody(BaseModel):
    customer_name: str = Field(min_length=1, max_length=160)
    customer_id: UUID | None = None
    amount: BrokerDecimal = Field(gt=0)
    broker_id: UUID | None = None
    description: str | None = Field(default=None, max_length=300)
    business_date: date | None = None
    attachment_upload_id: UUID | None = None


class EditMovementBody(BaseModel):
    expected_version: int = Field(ge=1)
    amount: BrokerDecimal | None = Field(default=None, gt=0)
    delivered_amount: BrokerDecimal | None = Field(default=None, gt=0)
    customer_name: str | None = Field(default=None, max_length=160)
    customer_id: UUID | None = None
    force_new_customer: bool = False
    description: str | None = Field(default=None, max_length=300)
    business_date: date | None = None

    received_amount: BrokerDecimal | None = Field(default=None, gt=0)
    operation_amount_bs: BrokerDecimal | None = Field(default=None, gt=0)
    exchange_rate: BrokerDecimal | None = Field(default=None, gt=0)

    customer_classification: Literal["global", "personal"] | None = None


class BrokerBody(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    phone: str = Field(min_length=8, max_length=20)
    abbreviation: str | None = Field(default=None, max_length=12)


class BrokerUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=160)
    abbreviation: str | None = Field(default=None, min_length=1, max_length=12)


class BrokerAbbreviationBody(BaseModel):
    abbreviation: str = Field(min_length=1, max_length=12)


class BrokerActiveBody(BaseModel):
    is_active: bool


class AttachBody(BaseModel):
    attachment_upload_id: UUID
    expected_attachment_id: UUID | None = None


async def _duplicate(
    c: Container,
    session: PanelSession,
    *,
    broker_id: UUID | None,
    logical_type: str,
    amount: Decimal,
    business_date: date | None,
    customer_name: str | None,
) -> dict | None:
    """Advertencia heurística; nunca bloquea cuando el usuario confirma."""
    effective_broker = session.member_id if session.role == MemberRole.BROKER else broker_id
    value_column = (
        BrokerMovement.operation_amount
        if logical_type in {"buy", "sell", "account", "pending"}
        else select(CustomerPayment.amount).where(CustomerPayment.id == BrokerMovement.related_payment_id).scalar_subquery() if logical_type == "receivable_payment"
        else BrokerMovement.customer_payout_amount if logical_type == "customer_payout"
        else func.abs(BrokerMovement.balance_delta)
    )
    filters = [
        BrokerMovement.business_id == session.business_id,
        BrokerMovement.broker_member_id.is_not_distinct_from(effective_broker),
        BrokerMovement.deleted_at.is_(None),
        BrokerMovement.created_at >= datetime.now(UTC) - timedelta(minutes=10),
        value_column == q_money(amount),
        BrokerageQueries._logical_type_filter(logical_type),
    ]
    filters.append(BrokerMovement.business_date == (business_date or local_today(session.timezone)))
    if customer_name:
        filters.append(
            BrokerMovement.customer.has(or_(func.lower(Customer.name) == customer_name.strip().casefold(), Customer.nickname == normalize_nickname(customer_name)))
        )
    async with c.session_factory() as db:
        row = (
            await db.execute(
                select(BrokerMovement).where(*filters).order_by(BrokerMovement.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
    return BrokerageQueries._movement(row) if row else None


def _warn_duplicate(duplicate: dict | None) -> None:
    if duplicate:
        raise NeedsConfirmation(
            "Encontré un movimiento igual registrado en los últimos 10 minutos. ¿Quieres registrarlo otra vez?",
            duplicate=duplicate,
        )


@router.get("/customers")
async def search_customers(session: Session, c: C, q: str = Query("", max_length=160)) -> list[dict]:
    _require_brokerage(session)
    return await c.brokerage_queries.customers(session.business_id, session.member_id, q)


class CustomerProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=160)
    nickname: str | None = Field(default=None, max_length=160)


@router.patch("/customers/{customer_id}")
async def edit_customer_profile(customer_id: UUID, body: CustomerProfileBody, session: Session, c: C) -> dict:
    _require_brokerage(session)
    if not body.model_fields_set & {"name", "nickname"}:
        raise HTTPException(422, "Indica el nombre o apodo que quieres cambiar.")
    res = await c.bus.dispatch(UpsertCustomer(customer_id=customer_id, **body.model_dump(exclude_unset=True)), session.ctx())
    return _result(res)


@router.get("/summary")
async def summary(
    session: Session,
    c: C,
    period: PeriodName = PeriodName.THIS_MONTH,
    date_from: date | None = None,
    date_to: date | None = None,
    broker_id: UUID | None = None,
    account_scope: Literal["all", "house", "broker"] = "all",
) -> dict:
    _require_brokerage(session)
    return await c.brokerage_queries.summary(
        session.business_id,
        session.member_id,
        period=period,
        date_from=date_from,
        date_to=date_to,
        broker_id=broker_id,
        account_scope=account_scope,
    )


@router.get("/reference-rates")
async def reference_rates(session: Session, c: C) -> dict:
    _require_brokerage(session)
    history = await PanelQueries(c.session_factory).rates_history(session.business_id)
    return {
        "current": await c.queries.rates_info(session.business_id, None),
        "auto": history["auto"],
        "custom": [],
        "funds": [],
    }


@router.get("/members")
async def members(session: Session, c: C) -> list[dict]:
    _require_brokerage(session)
    return await c.brokerage_queries.members(session.business_id, session.member_id)


@router.get("/accounts")
async def accounts(session: Session, c: C) -> list[dict]:
    _require_brokerage(session)
    return await c.brokerage_queries.accounts(session.business_id, session.member_id)


@router.post("/members")
async def create_member(body: BrokerBody, session: Session, c: C) -> dict:
    _require_owner(session)
    res = await c.bus.dispatch(CreateBrokerMember(**body.model_dump()), session.ctx())
    return res.data


@router.patch("/members/{member_id}")
async def update_member(member_id: UUID, body: BrokerUpdateBody, session: Session, c: C) -> dict:
    _require_owner(session)
    res = await c.bus.dispatch(
        UpdateBrokerMember(member_id=member_id, **body.model_dump(exclude_unset=True)), session.ctx()
    )
    return res.data


@router.patch("/members/{member_id}/abbreviation")
async def update_member_abbreviation(
    member_id: UUID, body: BrokerAbbreviationBody, session: Session, c: C
) -> dict:
    _require_brokerage(session)
    if session.role == MemberRole.BROKER and member_id != session.member_id:
        raise HTTPException(403, "Solo puedes cambiar tu propia abreviación.")
    res = await c.bus.dispatch(
        UpdateBrokerMember(member_id=member_id, abbreviation=body.abbreviation), session.ctx()
    )
    return res.data


@router.patch("/members/{member_id}/active")
async def set_member_active(
    member_id: UUID,
    body: BrokerActiveBody,
    session: Session,
    c: C,
    confirmed: bool = Query(False),
) -> dict:
    _require_owner(session)
    res = await c.bus.dispatch(
        SetBrokerMemberActive(member_id=member_id, is_active=body.is_active),
        session.ctx(),
        confirmed=confirmed,
    )
    if res.needs_confirmation:
        raise NeedsConfirmation(res.confirmation_reason or "¿Desactivar este acceso?")
    return res.data


@router.post("/attachments/uploads")
async def upload_attachment(
    session: Session,
    c: C,
    file: UploadFile = File(...),
) -> dict:
    _require_brokerage(session)
    assert session.business_id is not None and session.member_id is not None
    data = await file.read(16 * 1024 * 1024 + 1)
    row = await c.attachments.ingest(
        business_id=session.business_id,
        uploaded_by_member_id=session.member_id,
        data=data,
        original_filename=file.filename,
    )
    return {"attachment_upload_id": str(row.id), "attachment": await c.attachments.summary_for_movement(
        session.business_id, row.broker_movement_id
    ) if row.broker_movement_id else {"id": str(row.id), "filename": row.original_filename}}


@router.get("/attachments")
async def attachments(
    session: Session,
    c: C,
    broker_id: UUID | None = None,
    account_scope: Literal["all", "house", "broker"] = "all",
    logical_type: str | None = Query(None, alias="type"),
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
) -> dict:
    _require_brokerage(session)
    assert session.business_id is not None and session.member_id is not None
    return await c.attachments.gallery(
        business_id=session.business_id,
        member_id=session.member_id,
        broker_id=broker_id,
        account_scope=account_scope,
        logical_type=logical_type,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.get("/attachments/{attachment_id}")
async def attachment_detail(attachment_id: UUID, session: Session, c: C) -> dict:
    _require_brokerage(session)
    assert session.business_id is not None and session.member_id is not None
    return await c.attachments.detail(
        business_id=session.business_id, member_id=session.member_id, attachment_id=attachment_id
    )


@router.get("/movements")
async def movements(
    session: Session,
    c: C,
    broker_id: UUID | None = None,
    account_scope: Literal["all", "house", "broker"] = "all",
    portfolio_scope: Literal["all", "global", "personal"] = "all",
    completeness: Literal["complete", "incomplete"] | None = None,
    logical_type: str | None = Query(None, alias="type"),
    settlement: str | None = None,
    q: str | None = Query(None, max_length=120),
    customer_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
) -> dict:
    _require_brokerage(session)
    result = await c.brokerage_queries.movements(
        session.business_id,
        session.member_id,
        broker_id=broker_id,
        account_scope=account_scope,
        portfolio_scope=portfolio_scope,
        completeness=completeness,
        logical_type=logical_type,
        settlement=settlement,
        q=q,
        customer_id=customer_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    assert session.business_id is not None
    for item in result["items"]:
        item["attachment"] = await c.attachments.summary_for_movement(
            session.business_id, UUID(item["id"])
        )
    return result


@router.get("/movements/{movement_id}")
async def movement_detail(movement_id: UUID, session: Session, c: C) -> dict:
    _require_brokerage(session)
    result = await c.brokerage_queries.movement(session.business_id, session.member_id, movement_id)
    result["attachment"] = await c.attachments.summary_for_movement(
        session.business_id, movement_id, urls=True
    )
    return result


@router.put("/movements/{movement_id}/attachment")
async def put_movement_attachment(
    movement_id: UUID,
    body: AttachBody,
    session: Session,
    c: C,
    confirmed: bool = Query(False),
) -> dict:
    _require_brokerage(session)
    res = await c.bus.dispatch(
        AttachBrokerAttachment(
            movement_id=movement_id,
            attachment_upload_id=body.attachment_upload_id,
            expected_attachment_id=body.expected_attachment_id,
        ),
        session.ctx(),
        confirmed=confirmed,
    )
    if res.needs_confirmation:
        raise NeedsConfirmation(res.confirmation_reason or "¿Reemplazar este comprobante?")
    return _result(res)


@router.patch("/movements/{movement_id}")
async def edit_movement(movement_id: UUID, body: EditMovementBody, session: Session, c: C) -> dict:
    _require_brokerage(session)
    payload = body.model_dump(exclude_unset=True)
    payload.pop("expected_version", None)
    res = await c.bus.dispatch(
        EditBrokerMovement(movement_id=movement_id, expected_version=body.expected_version, **payload),
        session.ctx(),
    )
    return _result(res)


@router.get("/receivables")
async def receivables(
    session: Session,
    c: C,
    broker_id: UUID | None = None,
    account_scope: Literal["all", "house", "broker"] = "all",
    portfolio_scope: Literal["all", "global", "personal"] = "all",
    q: str | None = Query(None, max_length=120),
    status: str = "open",
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(50, ge=1, le=250),
    offset: int = Query(0, ge=0),
) -> dict:
    _require_brokerage(session)
    return await c.brokerage_queries.receivables(
        session.business_id, session.member_id, broker_id=broker_id, account_scope=account_scope, portfolio_scope=portfolio_scope,
        customer_name=q, status=status,
        date_from=date_from, date_to=date_to, limit=limit, offset=offset,
    )


@router.get("/receivables/{receivable_id}")
async def receivable_detail(receivable_id: UUID, session: Session, c: C) -> dict:
    _require_brokerage(session)
    return await c.brokerage_queries.receivable(session.business_id, session.member_id, receivable_id)


@router.get("/export.xlsx")
async def export_xlsx(
    session: Session,
    c: C,
    broker_id: UUID | None = None,
    account_scope: Literal["all", "house", "broker"] = "all",
    date_from: date | None = None,
    date_to: date | None = None,
) -> StreamingResponse:
    """Copia editable del ledger y cartera al corte operativo, solo para el dueño."""
    _require_owner(session)
    if account_scope == "broker" and broker_id is None:
        raise HTTPException(422, "Indica un corredor para exportar esa cuenta.")
    today = local_today(session.timezone)
    date_from = date_from or today.replace(day=1)
    date_to = date_to or today
    if date_from > date_to:
        raise HTTPException(422, "La fecha inicial no puede ser posterior a la fecha final.")
    if date_from > today or date_to > today:
        raise HTTPException(422, "Las fechas de exportación no pueden ser futuras.")
    content = await BrokerageWorkbook(c.session_factory).build(
        session.business_id, date_from=date_from, date_to=date_to, broker_id=broker_id, account_scope=account_scope,
    )
    filename = workbook_filename(date_from, date_to)
    return StreamingResponse(
        iter([content]), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store"},
    )


@router.post("/account-payments")
async def create_account_payment(
    body: AccountPaymentBody, session: Session, c: C, confirmed: bool = Query(False),
    confirmed_duplicate: bool = Query(False),
) -> dict:
    return await _create_direct_account_movement(body, session, c, RecordBrokerAccountPayment,
                                                confirmed=confirmed, confirmed_duplicate=confirmed_duplicate)


@router.post("/account-withdrawals")
async def create_account_withdrawal(
    body: AccountPaymentBody, session: Session, c: C, confirmed: bool = Query(False),
    confirmed_duplicate: bool = Query(False),
) -> dict:
    return await _create_direct_account_movement(body, session, c, RecordBrokerAccountWithdrawal,
                                                confirmed=confirmed, confirmed_duplicate=confirmed_duplicate)


async def _create_direct_account_movement(
    body: AccountPaymentBody, session: PanelSession, c: Container,
    command_type: type[RecordBrokerAccountPayment] | type[RecordBrokerAccountWithdrawal], *,
    confirmed: bool, confirmed_duplicate: bool,
) -> dict:
    _require_brokerage(session)
    is_idempotent_replay = False
    if body.idempotency_key is not None:
        async with c.session_factory() as db:
            is_idempotent_replay = await db.get(BrokerDraftExecution, (session.business_id, body.idempotency_key)) is not None
    if not confirmed_duplicate and not is_idempotent_replay:
        _warn_duplicate(
            await _duplicate(
                c,
                session,
                broker_id=body.broker_id,
                logical_type="account_withdrawal" if command_type is RecordBrokerAccountWithdrawal else "account_payment",
                amount=body.amount,
                business_date=body.business_date,
                customer_name=None,
            )
        )
    res = await c.bus.dispatch(
        command_type(
            amount=body.amount,
            broker_id=body.broker_id,
            description=body.description,
            business_date=body.business_date,
            attachment_upload_id=body.attachment_upload_id,
            draft_operation_id=body.idempotency_key,
        ),
        session.ctx(),
        confirmed=confirmed,
    )
    if res.needs_confirmation:
        raise NeedsConfirmation(res.confirmation_reason or "¿Guardar este comprobante?")
    return _result(res)


@router.post("/operations")
async def create_operation(
    body: OperationBody, session: Session, c: C, confirmed: bool = Query(False),
    confirmed_duplicate: bool = Query(False),
) -> dict:
    _require_brokerage(session)
    if not confirmed_duplicate:
        _warn_duplicate(
            await _duplicate(
                c,
                session,
                broker_id=body.broker_id,
                logical_type=body.operation_type,
                amount=body.operation_amount,
                business_date=body.business_date,
                customer_name=body.customer_name,
            )
        )
    res = await c.bus.dispatch(
        RecordBrokerOperation(**body.model_dump()),
        session.ctx(),
        confirmed=confirmed,
    )
    if res.needs_confirmation:
        raise NeedsConfirmation(res.confirmation_reason or "¿Guardar este comprobante?")
    return _result(res)


@router.post("/account-purchases")
async def create_account_purchase(
    body: AccountPurchaseBody, session: Session, c: C, confirmed: bool = Query(False),
    confirmed_duplicate: bool = Query(False),
) -> dict:
    _require_brokerage(session)
    if not confirmed_duplicate:
        _warn_duplicate(
            await _duplicate(
                c,
                session,
                broker_id=body.broker_id,
                logical_type="account",
                amount=body.amount,
                business_date=body.business_date,
                customer_name=None,
            )
        )
    res = await c.bus.dispatch(
        RecordBrokerAccountPurchase(**body.model_dump()),
        session.ctx(),
        confirmed=confirmed,
    )
    if res.needs_confirmation:
        raise NeedsConfirmation(res.confirmation_reason or "¿Guardar este comprobante?")
    return _result(res)


@router.post("/pending-purchases")
async def create_pending_purchase(
    body: PendingPurchaseBody, session: Session, c: C, confirmed: bool = Query(False),
    confirmed_duplicate: bool = Query(False),
) -> dict:
    _require_brokerage(session)
    if not confirmed_duplicate:
        _warn_duplicate(await _duplicate(
            c, session, broker_id=body.broker_id, logical_type="pending", amount=body.amount,
            business_date=body.business_date, customer_name=body.customer_name,
        ))
    res = await c.bus.dispatch(RecordPendingBrokerPurchase(**body.model_dump()), session.ctx(), confirmed=confirmed)
    if res.needs_confirmation:
        raise NeedsConfirmation(res.confirmation_reason or "¿Seguro?")
    return _result(res)


@router.post("/receivable-payments")
async def create_receivable_payment(
    body: ReceivablePaymentBody, session: Session, c: C, confirmed: bool = Query(False),
    confirmed_duplicate: bool = Query(False),
) -> dict:
    _require_brokerage(session)
    if not confirmed_duplicate:
        _warn_duplicate(await _duplicate(
            c, session, broker_id=body.broker_id, logical_type="receivable_payment", amount=body.amount,
            business_date=body.business_date, customer_name=body.customer_name,
        ))
    res = await c.bus.dispatch(RecordBrokerReceivablePayment(
        customer_name=body.customer_name, customer_id=body.customer_id, amount=body.amount, broker_id=body.broker_id,
        description=body.description, business_date=body.business_date,
        attachment_upload_id=body.attachment_upload_id,
    ), session.ctx(), confirmed=confirmed)
    if res.needs_confirmation:
        raise NeedsConfirmation(res.confirmation_reason or "¿Seguro?")
    return _result(res)


@router.delete("/movements/{movement_id}")
async def delete_movement(
    movement_id: UUID, session: Session, c: C, expected_version: int | None = None,
    confirmed: bool = Query(False),
) -> dict:
    _require_brokerage(session)
    res = await c.bus.dispatch(
        DeleteBrokerMovement(movement_id=movement_id, expected_version=expected_version),
        session.ctx(), confirmed=confirmed,
    )
    if res.needs_confirmation:
        raise NeedsConfirmation(res.confirmation_reason or "¿Seguro?")
    return _result(res)


@router.get("/activity")
async def activity(session: Session, c: C, limit: int = Query(50, ge=1, le=200)) -> list[dict]:
    _require_owner(session)
    command_labels = {
        "reconcile_account_balance": "Registró un ajuste de saldo",
        "delete_balance_adjustment": "Eliminó un ajuste de saldo",
        "record_broker_account_payment": "Registró un abono a cuenta",
        "record_broker_account_withdrawal": "Registró una salida de cuenta",
        "record_broker_operation": "Registró una compra o venta",
        "record_broker_account_purchase": "Registró una compra propia",
        "record_pending_broker_purchase": "Registró una compra pendiente",
        "record_broker_receivable_payment": "Registró un abono de cliente",
        "record_broker_customer_payout": "Registró un pago entregado al cliente",
        "edit_broker_movement": "Corrigió un movimiento",
        "delete_broker_movement": "Eliminó un movimiento",
    }
    tool_labels = {
        **command_labels,
        "reconcile_balance": "Preparó una reconciliación de saldo",
        "get_broker_balances": "Consultó los saldos",
        "list_broker_movements": "Consultó movimientos",
    }
    async with c.session_factory() as db:
        # Los turnos representan lo conversado con Bolos (panel y WhatsApp). Los audits sin turno
        # completan la actividad hecha directamente desde formularios, sin filtrar argumentos internos.
        turn_rows = (
            await db.execute(
                select(AgentTurn, InboundMessage, BusinessMember.name)
                .join(InboundMessage, InboundMessage.id == AgentTurn.inbound_message_id)
                .outerjoin(
                    BusinessMember,
                    and_(
                        BusinessMember.business_id == session.business_id,
                        BusinessMember.phone == InboundMessage.from_phone,
                    ),
                )
                .where(AgentTurn.business_id == session.business_id)
                .order_by(AgentTurn.created_at.desc())
                .limit(limit)
            )
        ).all()
        turn_ids = [turn.id for turn, _inbound, _name in turn_rows]
        tools_by_turn: dict[UUID, list[ToolCall]] = {turn_id: [] for turn_id in turn_ids}
        audits_by_turn: dict[UUID, list[AuditLog]] = {turn_id: [] for turn_id in turn_ids}
        if turn_ids:
            for call in (
                await db.execute(
                    select(ToolCall).where(ToolCall.turn_id.in_(turn_ids)).order_by(ToolCall.seq)
                )
            ).scalars():
                tools_by_turn[call.turn_id].append(call)
            for log in (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.business_id == session.business_id,
                        AuditLog.turn_id.in_(turn_ids),
                    ).order_by(AuditLog.at)
                )
            ).scalars():
                audits_by_turn[log.turn_id].append(log)
        direct_rows = (
            await db.execute(
                select(AuditLog, BusinessMember.name)
                .outerjoin(BusinessMember, BusinessMember.id == AuditLog.actor_id)
                .where(
                    AuditLog.business_id == session.business_id,
                    AuditLog.turn_id.is_(None),
                    AuditLog.entity_type == EntityType.BROKER_MOVEMENT.value,
                )
                .order_by(AuditLog.at.desc())
                .limit(limit)
            )
        ).all()

    entries: list[dict] = []
    for turn, inbound, member_name in turn_rows:
        calls = tools_by_turn[turn.id]
        readable = list(dict.fromkeys(tool_labels[call.tool_name] for call in calls if call.tool_name in tool_labels))
        related = next(
            (log for log in audits_by_turn[turn.id] if log.entity_type == "broker_movement"),
            audits_by_turn[turn.id][0] if audits_by_turn[turn.id] else None,
        )
        outcomes = {call.outcome for call in calls}
        if turn.status == "error" or "error" in outcomes or "rejected" in outcomes:
            result = "No completado"
        elif "needs_confirmation" in outcomes:
            result = "Esperando confirmación"
        elif related:
            result = "Completado"
        else:
            result = "Consulta respondida"
        entries.append({
            "id": str(turn.id),
            "at": turn.created_at.isoformat(),
            "member": member_name or inbound.contact_name or "Miembro",
            "message": inbound.body or inbound.transcript or "Nota de voz",
            "action": " · ".join(readable[:2]) if readable else "Conversó con Bolos",
            "result": result,
            "entity_type": related.entity_type if related else None,
            "entity_id": str(related.entity_id) if related else None,
        })
    for log, member_name in direct_rows:
        entries.append({
            "id": f"audit-{log.id}",
            "at": log.at.isoformat(),
            "member": member_name or "Panel",
            "message": "Acción realizada desde el panel",
            "action": command_labels.get(log.command, "Actualizó un registro"),
            "result": "Completado",
            "entity_type": log.entity_type,
            "entity_id": str(log.entity_id),
        })
    entries.sort(key=lambda item: item["at"], reverse=True)
    return entries[:limit]


@router.post("/customer-payouts")
async def create_customer_payout(body: ReceivablePaymentBody, session: Session, c: C,
                                 confirmed: bool = Query(False), confirmed_duplicate: bool = Query(False)) -> dict:
    _require_brokerage(session)
    if not confirmed_duplicate:
        _warn_duplicate(await _duplicate(c, session, broker_id=body.broker_id, logical_type="customer_payout",
                                         amount=body.amount, business_date=body.business_date,
                                         customer_name=body.customer_name))
    res = await c.bus.dispatch(RecordBrokerCustomerPayout(**body.model_dump()), session.ctx(), confirmed=confirmed)
    if res.needs_confirmation:
        raise NeedsConfirmation(res.confirmation_reason or "¿Guardar este pago entregado?")
    return _result(res)


@router.get("/customer-accounts")
async def customer_accounts(session: Session, c: C, broker_id: UUID | None = None,
                            account_scope: Literal["all", "house", "broker"] = "all",
                            portfolio_scope: Literal["all", "global", "personal"] = "all",
                            q: str | None = Query(None, max_length=120),
                            status: Literal["open", "all", "customer_owes", "house_owes", "settled"] = "open",
                            limit: int = Query(50, ge=1, le=250), offset: int = Query(0, ge=0)) -> dict:
    _require_brokerage(session)
    return await c.brokerage_queries.customer_accounts(session.business_id, session.member_id,
        broker_id=broker_id, account_scope=account_scope, portfolio_scope=portfolio_scope,
        customer_name=q, status=status, limit=limit, offset=offset)


@router.get("/customer-accounts/{customer_id}")
async def customer_account(customer_id: UUID, session: Session, c: C, date_from: date | None = None,
                           date_to: date | None = None, limit: int = Query(50, ge=1, le=250),
                           offset: int = Query(0, ge=0)) -> dict:
    _require_brokerage(session)
    return await c.brokerage_queries.customer_account(session.business_id, session.member_id, customer_id,
        date_from=date_from, date_to=date_to, limit=limit, offset=offset)
