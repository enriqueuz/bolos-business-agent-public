"""API REST del panel del dueño; las excepciones son las pocas operaciones internas de administración del sistema."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, or_, select, text

from app.api.deps import get_container
from app.api.panel.auth import (
    COOKIE,
    PanelSession,
    check_panel_origin,
    current_session,
    limit_admin_login_requests,
    limit_login_requests,
    request_code,
    verify_code,
    verify_link_token,
    verify_system_admin_password,
)
from app.application.bus import Command, CommandResult
from app.application.business_profile import panel_capabilities
from app.application.commands.brokerage import UpdateStaffMember
from app.application.commands.business import UpdateBusiness
from app.application.commands.cash import CloseCash
from app.application.commands.catalog import DeleteProduct, UpsertProduct
from app.application.commands.customers import DeleteCustomer, MergeCustomers, UpsertCustomer
from app.application.commands.exchanges import RecordExchange
from app.application.commands.expenses import RecordExpense
from app.application.commands.notes import RememberNote
from app.application.commands.purchases import RecordPurchase
from app.application.commands.rates import SetChargeRateMode, SetRate
from app.application.commands.receivables import CreateReceivable, RecordPayment
from app.application.commands.records import DeleteRecord, EditRecord, RecordChanges
from app.application.commands.sales import RecordSale
from app.application.commands.system_businesses import (
    SystemCreateMember,
    SystemRenameBusiness,
    SystemUpdateMember,
    member_data,
)
from app.application.context import Actor, ExecContext
from app.application.identity import normalize_phone
from app.application.metrics.compiler import MetricsQuery
from app.application.queries.cash import cash_summary
from app.application.queries.panel import PanelQueries
from app.container import Container
from app.db.models import (
    AgentTurn,
    AttachmentBlob,
    AttachmentObjectDeletion,
    AuditLog,
    BrokerMovement,
    Business,
    BusinessMember,
    BusinessNote,
    CashSession,
    CurrencyExchange,
    Customer,
    CustomerPayment,
    Expense,
    InboundMessage,
    MessageEntityLink,
    OutboundMessage,
    PaymentAllocation,
    Product,
    Purchase,
    Receivable,
    Sale,
)
from app.domain.brokerage import (
    DEFAULT_COMMISSION_RATE,
    normalize_broker_abbreviation,
)
from app.domain.countries import country_profile
from app.domain.dates import local_today
from app.domain.enums import (
    ActorType,
    BusinessVertical,
    ChargeRateMode,
    CurrencyView,
    EntityType,
    MemberRole,
    OnboardingState,
    RateScope,
)
from app.domain.errors import NeedsConfirmation

router = APIRouter(prefix="/api/panel/v1", tags=["panel"], dependencies=[Depends(check_panel_origin)])
Session = Annotated[PanelSession, Depends(current_session)]


def container_dep(request: Request) -> Container:
    return get_container(request)


C = Annotated[Container, Depends(container_dep)]


async def require_system_admin(session: Session, c: C) -> PanelSession:
    """La UI puede ocultar el enlace, pero el permiso siempre se comprueba en el servidor."""
    if not session.system_authenticated or not c.settings.is_system_admin(session.phone):
        raise HTTPException(403, "No tienes permiso para administrar el sistema.")
    return session


SystemAdmin = Annotated[PanelSession, Depends(require_system_admin)]


async def require_retail_owner(session: Session) -> PanelSession:
    if not session.has_business or session.vertical != BusinessVertical.RETAIL or session.role != MemberRole.OWNER:
        raise HTTPException(403, "Esta sección no está habilitada para tu cuenta.")
    return session


RetailSession = Annotated[PanelSession, Depends(require_retail_owner)]


async def require_customer_owner(session: Session) -> PanelSession:
    """Clientes es una entidad compartida por Retail y Corredores; solo la administra el dueño."""
    if not session.has_business or session.role != MemberRole.OWNER:
        raise HTTPException(403, "Solo el dueño puede administrar clientes.")
    return session


CustomerSession = Annotated[PanelSession, Depends(require_customer_owner)]


def panel_queries(c: Container) -> PanelQueries:
    return PanelQueries(c.session_factory)


def _result(res: CommandResult) -> dict[str, Any]:
    from app.application.audit import json_safe

    return {"ok": True, "data": json_safe(res.data),
            "affected": [{"entity_type": r.entity_type.value, "entity_id": str(r.entity_id), "version": r.version} for r in res.affected]}


async def _dispatch(c: Container, session: PanelSession, command: Command, *, confirmed: bool = False) -> dict[str, Any]:
    if not session.has_business or session.vertical != BusinessVertical.RETAIL or session.role != MemberRole.OWNER:
        raise HTTPException(403, "Esta acción no está habilitada para tu cuenta.")
    res = await c.bus.dispatch(command, session.ctx(), confirmed=confirmed)
    if res.needs_confirmation:
        raise NeedsConfirmation(res.confirmation_reason or "¿Seguro?")
    return _result(res)


async def _dispatch_customer(c: Container, session: PanelSession, command: Command) -> dict[str, Any]:
    await require_customer_owner(session)
    res = await c.bus.dispatch(command, session.ctx())
    return _result(res)


# `?confirmed=true`: el dueño ya dijo que sí en el diálogo de confirmación del panel.
Confirmed = Annotated[bool, Query()]


# ------------------------------------------------------------------------------------------------ auth


class CodeRequest(BaseModel):
    phone: str = Field(min_length=6, max_length=20)

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[+0-9 ()-]+", value):
            raise ValueError("Indica un número de WhatsApp válido.")
        phone = re.sub(r"\D", "", value)
        if not 8 <= len(phone) <= 15 or phone.startswith("0"):
            raise ValueError("Incluye el código de país en tu número de WhatsApp.")
        return phone


class CodeVerify(CodeRequest):
    code: str = Field(pattern=r"^[0-9]{6}$")


class SystemAdminLogin(CodeRequest):
    password: str = Field(min_length=1, max_length=200)


@router.post("/auth/request-code", dependencies=[Depends(limit_login_requests)])
async def auth_request_code(body: CodeRequest, c: C) -> dict[str, Any]:
    code = await request_code(c, body.phone)
    out: dict[str, Any] = {"sent": True}  # misma respuesta exista o no el número (no revela quién usa Bolos)
    if code and not c.settings.is_production and c.messaging.name == "console":
        out["dev_code"] = code
    return out


@router.post("/auth/verify")
async def auth_verify(body: CodeVerify, response: Response, c: C) -> dict[str, Any]:
    session = await verify_code(c, body.phone, body.code, response)
    return {"business_id": str(session.business_id), "phone": session.phone}


class LinkToken(BaseModel):
    token: str = Field(min_length=1, max_length=512)


@router.post("/auth/link", dependencies=[Depends(limit_admin_login_requests)])
async def auth_link(body: LinkToken, response: Response, c: C) -> dict[str, Any]:
    """Entrada por el enlace de bienvenida que Bolos mandó por WhatsApp (un solo uso, 15 minutos)."""
    session = await verify_link_token(c, body.token, response)
    return {"business_id": str(session.business_id), "phone": session.phone}


@router.post("/auth/system-login", dependencies=[Depends(limit_admin_login_requests)])
async def auth_system_login(body: SystemAdminLogin, response: Response, c: C) -> dict[str, Any]:
    session = await verify_system_admin_password(c, body.phone, body.password, response)
    return {"business_id": str(session.business_id) if session.business_id else None,
            "phone": session.phone, "system_admin": True}


@router.post("/auth/logout")
async def auth_logout(response: Response, c: C) -> dict[str, bool]:
    response.delete_cookie(COOKIE, path="/", httponly=True, secure=c.settings.is_production,
                           samesite="none" if c.settings.is_production else "lax")
    return {"ok": True}


@router.get("/me")
async def me(session: Session, c: C) -> dict[str, Any]:
    if not session.has_business:
        return {"phone": session.phone, "member": None, "capabilities": [], "business": None,
                "system_admin": c.settings.is_system_admin(session.phone)}
    return {"phone": session.phone,
            "member": {"id": str(session.member_id), "role": session.role.value},
            "capabilities": panel_capabilities(session.vertical, session.role),
            "business": await panel_queries(c).business(session.business_id),
            "system_admin": c.settings.is_system_admin(session.phone)}


# ------------------------------------------------------------------------------------------------ administración del sistema


async def _clear_business_data(c: Container, business_id: UUID) -> dict[str, int]:
    """Devuelve un negocio al final de onboarding sin tocar su ficha, miembros ni preferencias.

    Las tasas personalizadas se conservan: son una preferencia de configuración. Al borrar cambios de moneda,
    PostgreSQL deja su ``exchange_id`` en NULL (la FK está definida con SET NULL).
    """
    tables = [
        (BrokerMovement, "movimientos de corredores"),
        (AttachmentBlob, "archivos de comprobantes"),
        (CustomerPayment, "abonos"),
        (Receivable, "fiados"),
        (Sale, "ventas"),
        (Expense, "gastos"),
        (Purchase, "compras"),
        (CurrencyExchange, "cambios"),
        (CashSession, "cierres de caja"),
        (BusinessNote, "notas"),
        (Product, "productos"),
        (Customer, "clientes"),
        (MessageEntityLink, "vínculos de mensajes"),
        (AgentTurn, "turnos de Bolos"),
        (InboundMessage, "mensajes recibidos"),
        (OutboundMessage, "mensajes enviados"),
        (AuditLog, "historial"),
    ]
    deleted: dict[str, int] = {}
    async with c.session_factory() as s, s.begin():
        keys = (
            await s.execute(
                select(AttachmentBlob.original_key, AttachmentBlob.thumbnail_key).where(
                    AttachmentBlob.business_id == business_id
                )
            )
        ).all()
        requested = {key for pair in keys for key in pair}
        existing = set()
        if requested:
            existing = set(
                (await s.execute(
                    select(AttachmentObjectDeletion.object_key).where(
                        AttachmentObjectDeletion.object_key.in_(requested)
                    )
                )).scalars()
            )
        s.add_all(AttachmentObjectDeletion(object_key=key) for key in requested - existing)
        # PaymentAllocation cuelga de un abono y un fiado, no directamente del negocio. Debe salir antes
        # de ambos para respetar sus FK y no tocar asignaciones de ningún otro negocio.
        payment_ids = select(CustomerPayment.id).where(CustomerPayment.business_id == business_id)
        receivable_ids = select(Receivable.id).where(Receivable.business_id == business_id)
        result = await s.execute(delete(PaymentAllocation).where(or_(
            PaymentAllocation.payment_id.in_(payment_ids),
            PaymentAllocation.receivable_id.in_(receivable_ids),
        )))
        deleted["asignaciones de pago"] = result.rowcount or 0
        for model, label in tables:
            result = await s.execute(delete(model).where(model.business_id == business_id))
            deleted[label] = result.rowcount or 0
    if requested:
        await c.attachments.purge_due()
    return deleted


@router.get("/system/businesses")
async def system_businesses(admin: SystemAdmin, c: C, q: str | None = Query(None, max_length=100),
                            limit: int = Query(50, ge=1, le=100)) -> list[dict[str, Any]]:
    del admin  # deja claro que esta ruta exige el permiso, aunque no necesite su identidad después.
    phones = func.array_agg(BusinessMember.phone).filter(BusinessMember.phone.is_not(None)).label("phones")
    stmt = select(Business, phones).outerjoin(BusinessMember).group_by(Business.id).order_by(Business.created_at.desc()).limit(limit)
    if q and (term := q.strip()):
        digits = "".join(char for char in term if char.isdigit())
        filters = [Business.name.ilike(f"%{term}%")]
        if digits:
            filters.append(BusinessMember.phone.like(f"%{digits}%"))
        stmt = stmt.where(or_(*filters))
    async with c.session_factory() as s:
        rows = (await s.execute(stmt)).all()
    return [{"id": str(business.id), "name": business.name, "phones": phones or [],
             "created_at": business.created_at.isoformat(), "onboarding_state": business.onboarding_state,
             "country_code": business.country_code, "vertical": business.vertical} for business, phones in rows]


class SystemBootstrapMember(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    phone: str = Field(min_length=8, max_length=20)
    # Owner comparte este payload pero no usa abreviación; la obligatoriedad se valida solo para brokers al crear.
    abbreviation: str | None = Field(default=None, max_length=12)

    @field_validator("phone")
    @classmethod
    def valid_phone(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[+0-9 ()-]+", value):
            raise ValueError("Indica un número de WhatsApp válido.")
        phone = normalize_phone(value)
        if not 8 <= len(phone) <= 15 or phone.startswith("0"):
            raise ValueError("Incluye el código de país en tu número de WhatsApp.")
        return phone


class SystemBrokerageBusinessBody(BaseModel):
    """Alta preparada para el piloto: una casa de cambio venezolana y sus miembros iniciales."""

    name: str = Field(min_length=1, max_length=160)
    owner: SystemBootstrapMember
    brokers: list[SystemBootstrapMember] = Field(default_factory=list, max_length=20)


def _brokerage_welcome(member: BusinessMember, business: Business) -> str:
    first_name = (member.name or "").split(" ")[0] or ""
    greeting = f"¡Hola {first_name}! 👋" if first_name else "¡Hola! 👋"
    role = "dueño" if member.role == MemberRole.OWNER.value else "corredor"
    examples = (
        "• _Compré 450 a María Pérez_\n"
        "• _Juan López 100_\n"
        "• _Carlos Mora pagó 50_"
    )
    return (
        f"{greeting} Ya tienes acceso como *{role}* en *{business.name}*.\n\n"
        f"Puedes escribirle a Bolos de forma natural, por ejemplo:\n\n{examples}\n\n"
        "También puedes preguntar _¿cuánto debo?_ cuando quieras ver tu cuenta."
    )


@router.get("/system/signup-phones")
async def system_signup_phones(admin: SystemAdmin, c: C) -> list[dict[str, Any]]:
    """Números que la configuración permite usar en el piloto, con su disponibilidad actual."""
    del admin
    allowed = sorted(c.settings.allowed_phones)
    if not allowed:
        return []
    async with c.session_factory() as s:
        occupied = set((await s.execute(
            select(BusinessMember.phone).where(BusinessMember.phone.in_(allowed))
        )).scalars())
    return [{"phone": phone, "available": phone not in occupied} for phone in allowed]


@router.post("/system/businesses")
async def system_create_brokerage_business(
    body: SystemBrokerageBusinessBody, admin: SystemAdmin, c: C,
) -> dict[str, Any]:
    """Crea una casa de cambio operativa sin pasar por el onboarding retail.

    Es una operación interna y atómica: los tres roles se validan contra la misma allowlist que protege el
    acceso por WhatsApp, para que un POST directo no pueda agregar un teléfono no autorizado.
    """
    del admin
    allowed = c.settings.allowed_phones
    if not allowed:
        raise HTTPException(422, "SIGNUP_ALLOWED_PHONES no tiene números configurados.")
    business_name = body.name.strip()
    if not business_name:
        raise HTTPException(422, "El negocio necesita un nombre.")

    raw_members = [
        (body.owner, MemberRole.OWNER),
        *((broker, MemberRole.BROKER) for broker in body.brokers)
    ]
    prepared: list[tuple[SystemBootstrapMember, MemberRole, str, str | None]] = []
    for member, role in raw_members:
        phone = normalize_phone(member.phone)
        if not 8 <= len(phone) <= 15:
            raise HTTPException(422, "Indica números de WhatsApp válidos con código de país.")
        if phone not in allowed:
            raise HTTPException(422, "Cada teléfono debe estar en SIGNUP_ALLOWED_PHONES.")
        name = member.name.strip()
        if len(name) < 2:
            raise HTTPException(422, "Cada miembro necesita un nombre.")
        abbreviation = None
        if role == MemberRole.BROKER:
            if not member.abbreviation or not member.abbreviation.strip():
                raise HTTPException(422, "Cada corredor necesita una abreviación.")
            abbreviation = normalize_broker_abbreviation(member.abbreviation)
        prepared.append((member, role, phone, abbreviation))

    phones = [phone for _member, _role, phone, _abbreviation in prepared]
    if len(set(phones)) != len(phones):
        raise HTTPException(422, "Un teléfono solo puede ocupar un rol en este negocio.")
    abbreviations = [abbreviation.casefold() for _member, role, _phone, abbreviation in prepared
                     if role == MemberRole.BROKER and abbreviation]
    if len(set(abbreviations)) != len(abbreviations):
        raise HTTPException(422, "Cada corredor necesita una abreviación distinta.")

    profile = country_profile("VE")
    async with c.session_factory() as s, s.begin():
        # Comparte la misma exclusión por teléfono con IdentityService para no duplicar una alta simultánea desde WhatsApp.
        for phone in sorted(phones):
            await s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:phone, 0))"), {"phone": phone})
        existing = list((await s.execute(
            select(BusinessMember.phone).where(BusinessMember.phone.in_(phones))
        )).scalars())
        if existing:
            raise HTTPException(409, "Uno de esos teléfonos ya pertenece a un negocio.")

        business = Business(
            vertical=BusinessVertical.BROKERAGE.value,
            name=business_name,
            country_code=profile.code,
            local_currency=profile.local_currency,
            timezone=profile.timezone,
            display_currency=CurrencyView.USD_APPLIED.value,
            charge_rate_mode=ChargeRateMode.OFFICIAL.value,
            payment_methods=[],
            onboarding_state=OnboardingState.DONE.value,
            welcomed=True,
            settings={"broker_commission_rate": str(DEFAULT_COMMISSION_RATE)},
        )
        s.add(business)
        await s.flush()
        members = [
            BusinessMember(
                business_id=business.id,
                phone=phone,
                name=member.name.strip(),
                role=role.value,
                abbreviation=abbreviation,
            )
            for member, role, phone, abbreviation in prepared
        ]
        s.add_all(members)
        await s.flush()

        # El alta interna no tiene actor miembro del nuevo negocio; conserva la creación de la ficha en su auditoría.
        from app.application import audit
        from app.application.context import Actor, ExecContext
        from app.domain.enums import ActorType, AuditAction

        audit.record(
            s,
            ExecContext(business_id=business.id, actor=Actor(ActorType.SYSTEM), timezone=profile.timezone),
            entity_type=EntityType.BUSINESS,
            entity_id=business.id,
            action=AuditAction.CREATE,
            before=None,
            after=audit.snapshot(business),
            command="system_create_brokerage_business",
        )

        return {
            "id": str(business.id),
            "name": business.name,
            "vertical": business.vertical,
            "country_code": business.country_code,
            "timezone": business.timezone,
            "commission_rate": str(DEFAULT_COMMISSION_RATE),
            "members": [
                {
                    "id": str(member.id),
                    "name": member.name,
                    "phone": member.phone,
                    "role": member.role,
                    "abbreviation": member.abbreviation,
                    "welcome_message": _brokerage_welcome(member, business),
                }
                for member in members
            ],
        }


async def _system_business(c: Container, business_id: UUID) -> Business:
    async with c.session_factory() as s:
        business = await s.get(Business, business_id)
        if business is None:
            raise HTTPException(404, "Negocio no encontrado.")
        if business.vertical != BusinessVertical.BROKERAGE.value:
            raise HTTPException(422, "Esta edición solo está disponible para casas de cambio.")
        return business


async def _system_dispatch(
    c: Container, admin: PanelSession, business_id: UUID, command: Command, *, confirmed: bool = False,
) -> dict[str, Any]:
    business = await _system_business(c, business_id)
    ctx = ExecContext(business_id=business_id, actor=Actor(ActorType.SYSTEM, admin.member_id),
                      timezone=business.timezone)
    result = await c.bus.dispatch(command, ctx, confirmed=confirmed)
    if result.needs_confirmation:
        raise NeedsConfirmation(result.confirmation_reason or "¿Desactivar este acceso?")
    return result.data


@router.get("/system/businesses/{business_id}")
async def system_business_detail(business_id: UUID, admin: SystemAdmin, c: C) -> dict[str, Any]:
    business = await _system_business(c, business_id)
    async with c.session_factory() as s:
        members = (await s.execute(select(BusinessMember).where(
            BusinessMember.business_id == business_id,
        ).order_by(BusinessMember.role.desc(), BusinessMember.created_at, BusinessMember.id))).scalars().all()
    return {"id": str(business.id), "name": business.name, "vertical": business.vertical,
            "members": [member_data(member) for member in members]}


@router.patch("/system/businesses/{business_id}")
async def system_rename_business(
    business_id: UUID, body: SystemRenameBusiness, admin: SystemAdmin, c: C,
) -> dict[str, Any]:
    return await _system_dispatch(c, admin, business_id, body)


class SystemCreateMemberBody(SystemBootstrapMember):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    role: MemberRole


@router.post("/system/businesses/{business_id}/members")
async def system_add_member(
    business_id: UUID, body: SystemCreateMemberBody, admin: SystemAdmin, c: C,
) -> dict[str, Any]:
    if body.phone not in c.settings.allowed_phones:
        raise HTTPException(422, "Cada teléfono debe estar en SIGNUP_ALLOWED_PHONES.")
    business = await _system_business(c, business_id)
    data = await _system_dispatch(c, admin, business_id, SystemCreateMember(**body.model_dump()))
    data["welcome_message"] = _brokerage_welcome(BusinessMember(name=data["name"], role=data["role"]), business)
    return data


class SystemUpdateMemberBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=2, max_length=160)
    abbreviation: str | None = Field(default=None, min_length=1, max_length=12)
    is_active: bool | None = None


@router.patch("/system/businesses/{business_id}/members/{member_id}")
async def system_edit_member(
    business_id: UUID, member_id: UUID, body: SystemUpdateMemberBody, admin: SystemAdmin, c: C,
    confirmed: bool = Query(False),
) -> dict[str, Any]:
    command = SystemUpdateMember(member_id=member_id, **body.model_dump(exclude_unset=True))
    return await _system_dispatch(c, admin, business_id, command, confirmed=confirmed)


class SystemVerticalBody(BaseModel):
    vertical: BusinessVertical


@router.patch("/system/businesses/{business_id}/vertical")
async def system_business_vertical(
    business_id: UUID, body: SystemVerticalBody, admin: SystemAdmin, c: C,
) -> dict[str, Any]:
    """Activa una vertical solo en un negocio sin operación; evita mezclar semánticas históricas."""
    del admin
    transactional = (Sale, Expense, Purchase, Receivable, CustomerPayment, CurrencyExchange, BrokerMovement)
    async with c.session_factory() as s, s.begin():
        business = await s.get(Business, business_id, with_for_update=True)
        if business is None:
            raise HTTPException(404, "Negocio no encontrado.")
        if business.vertical == body.vertical.value:
            return {"ok": True, "vertical": business.vertical}
        for model in transactional:
            exists = (await s.execute(select(model.id).where(model.business_id == business_id).limit(1))).first()
            if exists:
                raise HTTPException(409, "Limpia los datos operativos antes de cambiar la vertical.")
        incompatible_member = (await s.execute(
            select(BusinessMember.id).where(
                BusinessMember.business_id == business_id,
                BusinessMember.role != MemberRole.OWNER.value,
            ).limit(1)
        )).first()
        if incompatible_member:
            raise HTTPException(409, "El negocio tiene miembros de su vertical actual y no puede cambiarse todavía.")
        business.vertical = body.vertical.value
        business.version += 1
        await s.flush()
        return {"ok": True, "vertical": business.vertical}


@router.post("/system/businesses/{business_id}/clean")
async def system_clean_business(business_id: UUID, admin: SystemAdmin, c: C) -> dict[str, Any]:
    raise HTTPException(403, "Destructive system maintenance is disabled in this distribution.")
    del admin
    async with c.session_factory() as s:
        if await s.get(Business, business_id) is None:
            raise HTTPException(404, "Negocio no encontrado.")
    return {"ok": True, "deleted": await _clear_business_data(c, business_id)}


@router.delete("/system/businesses/{business_id}")
async def system_delete_business(business_id: UUID, admin: SystemAdmin, c: C) -> dict[str, bool]:
    raise HTTPException(403, "Destructive system maintenance is disabled in this distribution.")
    del admin
    # Primero limpiamos referencias operativas que no dependen de CASCADE entre sí; después la ficha y sus
    # miembros/identidad se eliminan mediante sus FK hacia businesses. La cookie de ese negocio queda inválida.
    await _clear_business_data(c, business_id)
    async with c.session_factory() as s, s.begin():
        result = await s.execute(delete(Business).where(Business.id == business_id))
        if not result.rowcount:
            raise HTTPException(404, "Negocio no encontrado.")
    return {"ok": True}


# ------------------------------------------------------------------------------------------------ dashboard


@router.get("/dashboard")
async def dashboard(session: RetailSession, c: C, day: date | None = None) -> dict[str, Any]:
    business = await panel_queries(c).business(session.business_id)
    view = CurrencyView(business["display_currency"])
    d = day or local_today(business["timezone"])
    async with c.session_factory() as s:
        today = await cash_summary(s, session.business_id, d, view)
    month = await c.queries.metrics(session.business_id, MetricsQuery(metrics=["sales_total", "sales_count", "gross_margin",
                                                                              "expenses_total", "purchases_total"],
                                                                     period="this_month", compare_to="previous_period"), None)
    top = await c.queries.metrics(session.business_id, MetricsQuery(metrics=["item_revenue", "units_sold"], group_by=["product"],
                                                                    period="last_30_days", limit=5), None)
    receivables = await c.queries.list_receivables(session.business_id, limit=5)
    rates = await c.queries.rates_info(session.business_id, None)
    return {"business": business, "today": today, "month": month.data, "top_products": top.data, "receivables": receivables.data,
            "rates": rates}


@router.post("/metrics")
async def metrics(body: MetricsQuery, session: RetailSession, c: C) -> dict[str, Any]:
    return (await c.queries.metrics(session.business_id, body, None)).data


# ------------------------------------------------------------------------------------------------ movimientos


@router.get("/movements")
async def movements(session: RetailSession, c: C, date_from: date | None = None, date_to: date | None = None, kind: str | None = None,
                    q: str | None = None, customer_id: UUID | None = None, limit: int = Query(50, ge=1, le=200),
                    offset: int = Query(0, ge=0)) -> dict[str, Any]:
    return await panel_queries(c).movements(session.business_id, date_from=date_from, date_to=date_to, kind=kind, q=q,
                                            limit=limit, offset=offset, customer_id=customer_id)


@router.get("/records/{entity_type}/{entity_id}")
async def record_detail(entity_type: EntityType, entity_id: UUID, session: RetailSession, c: C) -> dict[str, Any]:
    return await panel_queries(c).record(session.business_id, entity_type, entity_id)


class EditBody(BaseModel):
    expected_version: int | None = None
    changes: RecordChanges
    item_changes: list[RecordChanges] = Field(default_factory=list, max_length=50)


@router.patch("/records/{entity_type}/{entity_id}")
async def record_edit(entity_type: EntityType, entity_id: UUID, body: EditBody, session: Session, c: C) -> dict[str, Any]:
    return await _dispatch(c, session, EditRecord(entity_type=entity_type, entity_id=entity_id, expected_version=body.expected_version,
                                                  changes=body.changes, item_changes=body.item_changes))


@router.delete("/records/{entity_type}/{entity_id}")
async def record_delete(entity_type: EntityType, entity_id: UUID, session: Session, c: C,
                        expected_version: int | None = None) -> dict[str, Any]:
    return await _dispatch(c, session, DeleteRecord(entity_type=entity_type, entity_id=entity_id, expected_version=expected_version))


@router.post("/sales")
async def create_sale(body: RecordSale, session: Session, c: C, confirmed: Confirmed = False) -> dict[str, Any]:
    return await _dispatch(c, session, body, confirmed=confirmed)


@router.post("/expenses")
async def create_expense(body: RecordExpense, session: Session, c: C, confirmed: Confirmed = False) -> dict[str, Any]:
    return await _dispatch(c, session, body, confirmed=confirmed)


@router.post("/purchases")
async def create_purchase(body: RecordPurchase, session: Session, c: C, confirmed: Confirmed = False) -> dict[str, Any]:
    return await _dispatch(c, session, body, confirmed=confirmed)


@router.post("/receivables")
async def create_receivable(body: CreateReceivable, session: Session, c: C, confirmed: Confirmed = False) -> dict[str, Any]:
    return await _dispatch(c, session, body, confirmed=confirmed)


@router.post("/payments")
async def create_payment(body: RecordPayment, session: Session, c: C, confirmed: Confirmed = False) -> dict[str, Any]:
    return await _dispatch(c, session, body, confirmed=confirmed)


@router.get("/receivables")
async def receivables(session: RetailSession, c: C) -> dict[str, Any]:
    return (await c.queries.list_receivables(session.business_id, limit=100)).data


# ------------------------------------------------------------------------------------------------ catálogo y clientes


@router.get("/products")
async def products(session: RetailSession, c: C, q: str | None = None) -> list[dict[str, Any]]:
    return await panel_queries(c).products(session.business_id, q)


@router.post("/products")
async def upsert_product(body: UpsertProduct, session: Session, c: C) -> dict[str, Any]:
    return await _dispatch(c, session, body)


@router.delete("/products/{product_id}")
async def delete_product(product_id: UUID, session: Session, c: C, expected_version: int | None = None) -> dict[str, Any]:
    return await _dispatch(c, session, DeleteProduct(product_id=product_id, expected_version=expected_version))


@router.get("/customers")
async def customers(session: CustomerSession, c: C, q: str | None = None) -> list[dict[str, Any]]:
    return await panel_queries(c).customers(session.business_id, q)


@router.get("/customers/{customer_id}")
async def customer_detail(customer_id: UUID, session: CustomerSession, c: C) -> dict[str, Any]:
    return await panel_queries(c).customer_detail(session.business_id, customer_id)


@router.post("/customers")
async def upsert_customer(body: UpsertCustomer, session: Session, c: C) -> dict[str, Any]:
    return await _dispatch_customer(c, session, body)


@router.post("/customers/merge")
async def merge_customers(body: MergeCustomers, session: Session, c: C) -> dict[str, Any]:
    return await _dispatch_customer(c, session, body)


@router.delete("/customers/{customer_id}")
async def delete_customer(customer_id: UUID, session: Session, c: C, expected_version: int | None = None) -> dict[str, Any]:
    return await _dispatch_customer(c, session, DeleteCustomer(customer_id=customer_id, expected_version=expected_version))


# ------------------------------------------------------------------------------------------------ tasas, caja, notas, negocio


@router.get("/rates")
async def rates(session: RetailSession, c: C) -> dict[str, Any]:
    return {"current": await c.queries.rates_info(session.business_id, None), **await panel_queries(c).rates_history(session.business_id)}


class CustomRateBody(BaseModel):
    rate: Decimal = Field(gt=0)
    label: str | None = Field(default=None, max_length=60)


# Tasa propia para cobrar (lo que hoy usa el panel). La API completa de tasas (propósito, modo, cambios) llega en la Parte 9.
@router.post("/rates/custom")
async def set_rate(body: CustomRateBody, session: Session, c: C, confirmed: Confirmed = False) -> dict[str, Any]:
    return await _dispatch(c, session, SetRate(rate=body.rate, label=body.label, purpose=RateScope.CHARGE), confirmed=confirmed)


@router.delete("/rates/custom")
async def clear_rate(session: Session, c: C) -> dict[str, Any]:
    """Dejar de cobrar con la tasa propia: vuelve a la de mercado (o a la oficial si el país no tiene mercado)."""
    business = await panel_queries(c).business(session.business_id)
    mode = ChargeRateMode.MARKET if business["profile"]["has_market_rate"] else ChargeRateMode.OFFICIAL
    return await _dispatch(c, session, SetChargeRateMode(mode=mode))


class RateBody(BaseModel):
    rate: Decimal = Field(gt=0)
    purpose: RateScope
    label: str | None = Field(default=None, max_length=60)


@router.post("/rates")
async def set_rate_with_purpose(body: RateBody, session: Session, c: C, confirmed: Confirmed = False) -> dict[str, Any]:
    """Tasa para cobrar, tasa real de los bolívares, o ambas."""
    return await _dispatch(c, session, SetRate(rate=body.rate, purpose=body.purpose, label=body.label), confirmed=confirmed)


@router.put("/rates/charge-mode")
async def set_charge_mode(body: SetChargeRateMode, session: Session, c: C) -> dict[str, Any]:
    return await _dispatch(c, session, body)


@router.post("/exchanges")
async def create_exchange(body: RecordExchange, session: Session, c: C, confirmed: Confirmed = False) -> dict[str, Any]:
    return await _dispatch(c, session, body, confirmed=confirmed)


@router.get("/cash-sessions")
async def cash_sessions(session: RetailSession, c: C) -> list[dict[str, Any]]:
    return await panel_queries(c).cash_sessions(session.business_id)


@router.post("/cash/close")
async def close_cash(body: CloseCash, session: Session, c: C) -> dict[str, Any]:
    return await _dispatch(c, session, body)


@router.get("/notes")
async def notes(session: RetailSession, c: C) -> list[dict[str, Any]]:
    return await panel_queries(c).notes(session.business_id)


@router.post("/notes")
async def create_note(body: RememberNote, session: Session, c: C) -> dict[str, Any]:
    return await _dispatch(c, session, body)


@router.get("/business")
async def business(session: RetailSession, c: C) -> dict[str, Any]:
    return await panel_queries(c).business(session.business_id)


@router.patch("/business")
async def update_business(body: UpdateBusiness, session: Session, c: C) -> dict[str, Any]:
    return await _dispatch(c, session, body)


# ------------------------------------------------------------------------------------------------ nota de voz

# El audio viaja en base64 dentro del JSON (como el simulador con imágenes): así el backend no necesita
# python-multipart y el panel no arma multipart a mano.
MAX_AUDIO_BYTES = 6 * 1024 * 1024
AUDIO_PREFIX = "audio/"


class VoiceNote(BaseModel):
    audio_base64: str = Field(min_length=16, max_length=4 * ((MAX_AUDIO_BYTES + 2) // 3))
    mime_type: str = Field(default="audio/ogg", max_length=80)


class VoiceReply(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    button_id: str | None = Field(default=None, max_length=256)  # botón u opción tocada en la respuesta anterior


async def _panel_turn(c: Container, session: PanelSession, *, kind: str, text: str | None = None,
                      transcript: str | None = None, mime_type: str | None = None, button_id: str | None = None,
                      attachment_upload_id: UUID | None = None,
                      reply_to_provider_message_id: str | None = None) -> dict[str, Any]:
    """Corre el MISMO turno que WhatsApp, pero la respuesta se muestra en el panel en vez de enviarse."""
    from app.agent.pipeline import PANEL_PROVIDER
    from app.infra.redis.coordination import ConversationBusy, chat_lock_scope, conversation_lock

    inbound_id = await c.turns.save_inbound(
        provider=PANEL_PROVIDER, provider_message_id=f"panel-{uuid4()}", business_id=session.business_id,
        from_phone=session.phone, contact_name=None, type=kind, body=text, transcript=transcript,
        media_url=None, media_mime_type=mime_type, reply_to_provider_message_id=reply_to_provider_message_id,
        interactive_reply_id=button_id,
        raw={"source": "panel"},
    )
    if inbound_id is None:
        raise HTTPException(500, "No pude guardar el mensaje.")
    if attachment_upload_id is not None:
        assert session.business_id is not None and session.member_id is not None
        await c.attachments.bind_source(
            business_id=session.business_id,
            uploaded_by_member_id=session.member_id,
            attachment_id=attachment_upload_id,
            source_message_id=inbound_id,
        )
    try:
        async with conversation_lock(c.redis, chat_lock_scope(session.phone), timeout_s=180, wait_s=20):
            outcome = await c.pipeline.process(inbound_id, deliver=False)
    except ConversationBusy as exc:
        raise HTTPException(409, "Bolos está procesando otro mensaje tuyo. Intenta en unos segundos.") from exc
    messages = []
    for reply in outcome.replies:
        provider_message_id = f"panel-out-{uuid4()}"
        # Con ``deliver=False`` el pipeline ya persiste exactamente una salida ``panel`` con sus
        # opciones. Aquí solo damos al cliente un id efímero para identificar la burbuja actual.
        messages.append(
            {
                "text": reply["text"],
                "choices": reply.get("choices") or [],
                "provider_message_id": provider_message_id,
            }
        )
    return {"turn_id": str(outcome.turn_id) if outcome.turn_id else None, "status": outcome.status, "lane": outcome.lane,
            "transcript": transcript or text, "replies": [r["text"] for r in outcome.replies], "reactions": outcome.reactions,
            "messages": messages}


@router.post("/voice")
async def voice_note(body: VoiceNote, session: Session, c: C) -> dict[str, Any]:
    """Nota de voz grabada en el panel: se transcribe y se procesa como si la hubieras mandado por WhatsApp."""
    import base64
    import binascii

    if not body.mime_type.startswith(AUDIO_PREFIX):
        raise HTTPException(415, "Solo acepto audio.")
    try:
        audio = base64.b64decode(body.audio_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "El audio llegó dañado.") from exc
    if not audio:
        raise HTTPException(400, "El audio llegó vacío.")
    if len(audio) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "La nota de voz es muy larga. Grábala en partes más cortas.")

    transcriber = c.pipeline.transcriber
    if transcriber is None:
        raise HTTPException(503, "La transcripción no está disponible ahora mismo.")
    # Gemini espera el tipo base, sin los parámetros de códec que agrega MediaRecorder.
    transcript = await transcriber.transcribe(audio, body.mime_type.split(";")[0].strip())
    if not transcript:
        raise HTTPException(422, "No entendí el audio. Vuelve a grabarlo o escríbelo.")
    return await _panel_turn(c, session, kind="voice", transcript=transcript, mime_type=body.mime_type)


@router.post("/voice/reply")
async def voice_note_reply(body: VoiceReply, session: Session, c: C) -> dict[str, Any]:
    """Seguimiento en texto del mismo hilo: confirmar, corregir o responder lo que Bolos preguntó."""
    return await _panel_turn(c, session, kind="text", text=body.text.strip(), button_id=body.button_id)


# ------------------------------------------------------------------------------------------------ observabilidad


@router.get("/turns")
async def turns(session: RetailSession, c: C, limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    return await c.turns.list_turns(session.business_id, limit)


@router.get("/turns/{turn_id}")
async def turn_detail(turn_id: UUID, session: RetailSession, c: C) -> dict[str, Any]:
    detail = await c.turns.turn_detail(session.business_id, turn_id)
    if detail is None:
        raise HTTPException(404)
    return detail


# ------------------------------------------------------------------------------------------------ simulador (no producción)


class PanelChat(BaseModel):
    text: str | None = None
    reply_to_provider_message_id: str | None = None
    attachment_upload_id: UUID | None = None
    button_id: str | None = Field(default=None, max_length=256)


@router.post("/chat")
async def panel_chat(body: PanelChat, session: Session, c: C) -> dict[str, Any]:
    """Habla con Bolos usando el mismo pipeline, permisos y bloqueo de WhatsApp."""
    text = (body.text or "").strip()
    if not text and not body.button_id and body.attachment_upload_id is None:
        raise HTTPException(422, "Escribe un mensaje.")
    return await _panel_turn(
        c,
        session,
        kind="image" if body.attachment_upload_id else "text",
        text=text,
        button_id=body.button_id,
        attachment_upload_id=body.attachment_upload_id,
        reply_to_provider_message_id=body.reply_to_provider_message_id,
    )


@router.get("/chat/history")
async def panel_chat_history(
    session: Session,
    c: C,
    limit: int = Query(50, ge=1, le=100),
    before: datetime | None = None,
) -> list[dict[str, Any]]:
    return await c.turns.chat_history(
        session.business_id, session.phone, limit=limit, before=before
    )


from app.api.panel.brokerage import router as brokerage_router  # noqa: E402

router.include_router(brokerage_router)


async def require_staff_owner(session: Session) -> PanelSession:
    if not session.has_business or session.role != MemberRole.OWNER:
        raise HTTPException(403, "Solo el dueño puede administrar el equipo.")
    return session


StaffSession = Annotated[PanelSession, Depends(require_staff_owner)]


@router.get("/staff")
async def staff(session: StaffSession, c: C) -> list[dict]:
    return await panel_queries(c).staff(session.business_id)


class StaffUpdateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=2, max_length=160)
    abbreviation: str | None = Field(default=None, min_length=1, max_length=12)


@router.patch("/staff/{member_id}")
async def update_staff(member_id: UUID, body: StaffUpdateBody, session: StaffSession, c: C) -> dict:
    result = await c.bus.dispatch(
        UpdateStaffMember(member_id=member_id, **body.model_dump(exclude_unset=True)), session.ctx()
    )
    return result.data
