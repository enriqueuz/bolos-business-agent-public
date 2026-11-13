"""Identidad: número de WhatsApp → miembro → negocio. Alta de negocios nuevos según la política de acceso.

El teléfono es la identidad. El BSUID (id de usuario por negocio que manda Meta cuando oculta el número de alguien
con username) es solo un alias de canal que se traduce a teléfono antes de llegar aquí (ver app/api/webhooks.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Business, BusinessMember, WhatsAppIdentity
from app.domain.countries import DEFAULT_COUNTRY, CountryProfile, country_profile, profile_for_phone
from app.domain.enums import BusinessVertical, CurrencyView, MemberRole, OnboardingState
from app.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BusinessSnapshot:
    id: UUID
    vertical: BusinessVertical
    country_code: str
    local_currency: str
    name: str | None
    what_sells: str | None
    timezone: str
    display_currency: CurrencyView
    payment_methods: list[str]
    onboarding_state: OnboardingState
    version: int
    welcomed: bool
    onboarding_draft: dict[str, Any] = field(default_factory=dict)
    declared_country: str | None = None

    @property
    def onboarded(self) -> bool:
        return self.onboarding_state == OnboardingState.DONE

    @property
    def profile(self) -> CountryProfile:
        return country_profile(self.country_code)


@dataclass(frozen=True, slots=True)
class Identity:
    member_id: UUID
    member_name: str | None
    member_role: MemberRole
    phone: str
    business: BusinessSnapshot


def snapshot(b: Business) -> BusinessSnapshot:
    return BusinessSnapshot(b.id, BusinessVertical(b.vertical), b.country_code, b.local_currency, b.name, b.what_sells, b.timezone, CurrencyView(b.display_currency), list(b.payment_methods or []),
                            OnboardingState(b.onboarding_state), b.version, b.welcomed, dict(b.onboarding_draft or {}),
                            b.declared_country)


def normalize_phone(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


class IdentityService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, signup_mode: str, allowed_phones: set[str],
                 default_country: str = DEFAULT_COUNTRY) -> None:
        self._default_country = default_country
        self._factory = session_factory
        self._mode = signup_mode
        self._allowed = {normalize_phone(p) for p in allowed_phones}

    async def resolve(self, phone: str, contact_name: str | None = None) -> Identity | None:
        """Devuelve la identidad, creando negocio+dueño si la política lo permite. None = sin acceso."""
        phone = normalize_phone(phone)
        async with self._factory() as s, s.begin():
            # El alta por panel y por WhatsApp puede llegar a la vez para el mismo número.
            await s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:phone, 0))"), {"phone": phone})
            member = (await s.execute(select(BusinessMember).where(BusinessMember.phone == phone)
                                      .order_by(BusinessMember.created_at).limit(1))).scalar_one_or_none()
            if member is not None:
                if not member.is_active:
                    return None
                return Identity(member.id, member.name, MemberRole(member.role), phone, snapshot(member.business))
            if not self.can_signup(phone):
                return None
            profile = profile_for_phone(phone, self._default_country)
            business = Business(onboarding_state=OnboardingState.IN_PROGRESS.value, country_code=profile.code,
                                local_currency=profile.local_currency, timezone=profile.timezone)
            s.add(business)
            await s.flush()
            member = BusinessMember(business_id=business.id, phone=phone, name=contact_name)
            s.add(member)
            await s.flush()
            await s.refresh(business)
            return Identity(member.id, member.name, MemberRole(member.role), phone, snapshot(business))

    def can_signup(self, phone: str) -> bool:
        if self._mode == "open":
            return True
        if self._mode == "allowlist":
            return phone in self._allowed
        return False

    async def refresh_business(self, business_id: UUID) -> BusinessSnapshot:
        async with self._factory() as s:
            return snapshot(await s.get_one(Business, business_id))

    async def member_by_phone(self, phone: str) -> Identity | None:
        phone = normalize_phone(phone)
        async with self._factory() as s:
            member = (await s.execute(select(BusinessMember).where(BusinessMember.phone == phone)
                                      .order_by(BusinessMember.created_at).limit(1))).scalar_one_or_none()
            return (
                Identity(member.id, member.name, MemberRole(member.role), phone, snapshot(member.business))
                if member and member.is_active
                else None
            )

    # ------------------------------------------------------------------ BSUID (usernames de WhatsApp)
    async def remember_bsuid(self, bsuid: str, phone: str, source: str = "webhook") -> None:
        """Enlaza BSUID → teléfono. Solo escribe si es nuevo o cambió (llega en casi todos los mensajes)."""
        phone = normalize_phone(phone)
        stmt = insert(WhatsAppIdentity).values(bsuid=bsuid, phone=phone, source=source)
        stmt = stmt.on_conflict_do_update(
            index_elements=[WhatsAppIdentity.bsuid],
            set_={"phone": stmt.excluded.phone, "source": stmt.excluded.source, "updated_at": func.now()},
            where=WhatsAppIdentity.phone.is_distinct_from(stmt.excluded.phone),
        ).returning(WhatsAppIdentity.bsuid)
        async with self._factory() as s, s.begin():
            if (await s.execute(stmt)).scalar_one_or_none() is not None:
                log.info("identity.bsuid_linked", source=source)

    async def phone_for_bsuid(self, bsuid: str) -> str | None:
        async with self._factory() as s:
            return (await s.execute(select(WhatsAppIdentity.phone).where(WhatsAppIdentity.bsuid == bsuid))).scalar_one_or_none()
