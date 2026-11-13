"""Tasas de cambio por moneda local. Una sola fuente de verdad (`effective`) que NUNCA lanza al leer para el prompt.

Todas las tasas son "unidades de moneda local por 1 USD".
- Oficial y mercado: las automáticas del país, siempre como referencia. La oficial es la última persistida en
  `exchange_rates` para esa moneda: último valor bueno, sobrevive reinicios y caídas del proveedor, así nunca se escribe
  una tasa 0. En países sin tasa de mercado propia (perfil `has_market_rate=False`), la de mercado es igual a la oficial.
- Aplicada (tasa para cobrar): la que elige el negocio en `charge_rate_mode` (oficial, mercado o su tasa propia).
  No hay tasas especiales por operación: todo movimiento usa la tasa para cobrar vigente.
- Real (funds): la tasa a la que el negocio obtuvo sus bolívares (último cambio registrado o la que dijo). Solo sirve
  para el costo real de lo que paga en moneda local.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports import Alerter, NullAlerter, RateFetcher
from app.db.models import Business, CustomRate, ExchangeRate
from app.domain.countries import country_profile
from app.domain.enums import ChargeRateMode, CustomRateSource, RateAppliedSource, RateKind, RatePurpose
from app.domain.errors import NotFound
from app.domain.money import EffectiveRates
from app.logging import get_logger

log = get_logger(__name__)

# Una tasa nueva que difiere más de esto de la anterior se considera sospechosa y no se guarda.
MAX_JUMP_RATIO = Decimal("0.35")


@dataclass(frozen=True, slots=True)
class RatesSnapshot:
    local_currency: str
    official: Decimal | None
    official_as_of: datetime | None
    market_auto: Decimal | None
    market_auto_as_of: datetime | None
    custom: Decimal | None
    custom_label: str | None
    custom_set_at: datetime | None
    charge_rate_mode: ChargeRateMode = ChargeRateMode.OFFICIAL
    charge_rate_confirmed_on: date | None = None
    funds: Decimal | None = None
    funds_set_at: datetime | None = None
    funds_source: CustomRateSource | None = None

    @property
    def market(self) -> Decimal | None:
        return self.market_auto or self.official

    def applied(self) -> tuple[Decimal, RateAppliedSource, datetime | None, str | None]:
        """Tasa para cobrar según el modo del negocio. Si falta la del modo (sin tasa propia, sin mercado), cae a la oficial."""
        if self.charge_rate_mode == ChargeRateMode.CUSTOM and self.custom:
            return self.custom, RateAppliedSource.CUSTOM, self.custom_set_at, self.custom_label
        if self.charge_rate_mode == ChargeRateMode.MARKET and self.market_auto:
            return self.market_auto, RateAppliedSource.MARKET, self.market_auto_as_of, None
        return self.official or Decimal(0), RateAppliedSource.OFFICIAL, self.official_as_of, None

    def effective(self) -> EffectiveRates:
        official = self.official or Decimal(0)
        applied, source, set_at, label = self.applied()
        return EffectiveRates(local_currency=self.local_currency, official=official, market=self.market or Decimal(0),
                              applied=applied, applied_source=source, official_as_of=self.official_as_of,
                              market_as_of=self.market_auto_as_of or self.official_as_of, applied_set_at=set_at,
                              applied_label=label, funds=self.funds, funds_set_at=self.funds_set_at)


async def load_snapshot(session: AsyncSession, business_id: UUID) -> RatesSnapshot:
    """Tasas vigentes de un negocio, leídas en la sesión dada (sirve dentro de comandos y consultas)."""
    business = await session.get(Business, business_id)
    if business is None:
        raise NotFound("Negocio no encontrado.")
    currency = business.local_currency

    async def latest(kind: RateKind) -> ExchangeRate | None:
        return (await session.execute(
            select(ExchangeRate).where(ExchangeRate.currency == currency, ExchangeRate.kind == kind.value)
            .order_by(ExchangeRate.fetched_at.desc()).limit(1)
        )).scalar_one_or_none()

    official = await latest(RateKind.OFFICIAL)
    market = await latest(RateKind.MARKET) if country_profile(business.country_code).has_market_rate else official
    custom = await RateService.current_custom(session, business_id, RatePurpose.CHARGE)
    funds = await RateService.current_custom(session, business_id, RatePurpose.FUNDS)
    return RatesSnapshot(
        local_currency=currency,
        official=official.rate if official else None,
        official_as_of=(official.rate_date or official.fetched_at) if official else None,
        market_auto=market.rate if market else None,
        market_auto_as_of=(market.rate_date or market.fetched_at) if market else None,
        custom=custom.rate if custom else None,
        custom_label=custom.label if custom else None,
        custom_set_at=custom.set_at if custom else None,
        charge_rate_mode=ChargeRateMode(business.charge_rate_mode),
        charge_rate_confirmed_on=business.charge_rate_confirmed_on,
        funds=funds.rate if funds else None,
        funds_set_at=funds.set_at if funds else None,
        funds_source=CustomRateSource(funds.source) if funds else None,
    )


class RateService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], fetchers: dict[str, RateFetcher] | None = None,
                 alerter: Alerter | None = None) -> None:
        """`fetchers`: proveedor (CountryProfile.rate_provider) → RateFetcher."""
        self._factory = session_factory
        self._fetchers = fetchers or {}
        self._alerter = alerter or NullAlerter()

    async def snapshot(self, business_id: UUID, session: AsyncSession | None = None) -> RatesSnapshot:
        if session is not None:
            return await self._snapshot(session, business_id)
        async with self._factory() as s:
            return await self._snapshot(s, business_id)

    async def _snapshot(self, session: AsyncSession, business_id: UUID) -> RatesSnapshot:
        return await load_snapshot(session, business_id)

    async def effective(self, session: AsyncSession, business_id: UUID) -> EffectiveRates:
        return (await self._snapshot(session, business_id)).effective()

    async def safe_snapshot(self, business_id: UUID) -> RatesSnapshot | None:
        """Para el prompt: nunca lanza."""
        try:
            return await self.snapshot(business_id)
        except Exception:  # noqa: BLE001
            log.exception("rates.snapshot_failed")
            return None

    @staticmethod
    async def current_custom(session: AsyncSession, business_id: UUID,
                             purpose: RatePurpose = RatePurpose.CHARGE) -> CustomRate | None:
        return (await session.execute(
            select(CustomRate).where(CustomRate.business_id == business_id, CustomRate.purpose == purpose.value,
                                     CustomRate.cleared_at.is_(None))
            .order_by(CustomRate.set_at.desc()).limit(1)
        )).scalar_one_or_none()

    async def refresh(self) -> dict[str, str]:
        """Trae tasas automáticas de los países que tienen negocios (o todos los proveedores si aún no hay).

        Llamado por el cron del worker. Solo persiste valores sanos.
        """
        async with self._factory() as session:
            countries = (await session.execute(select(distinct(Business.country_code)))).scalars().all()
        providers = {country_profile(c).rate_provider for c in countries} or set(self._fetchers)
        saved: dict[str, str] = {}
        for provider in sorted(providers):
            fetcher = self._fetchers.get(provider)
            if fetcher is None:
                log.warning("rates.no_fetcher_for_provider", provider=provider)
                continue
            try:
                fetched = await fetcher.fetch()
            except Exception as exc:  # noqa: BLE001
                log.warning("rates.fetch_failed", provider=provider, error=str(exc))
                saved[f"{provider}"] = f"fetch_failed: {exc}"
                continue
            async with self._factory() as session, session.begin():
                for item in fetched:
                    if not item.rate.is_finite() or item.rate <= 0:
                        continue
                    previous = (await session.execute(
                        select(ExchangeRate).where(ExchangeRate.currency == item.currency, ExchangeRate.kind == item.kind.value)
                        .order_by(ExchangeRate.fetched_at.desc()).limit(1)
                    )).scalar_one_or_none()
                    if previous and abs(item.rate - previous.rate) / previous.rate > MAX_JUMP_RATIO:
                        msg = f"Tasa {item.currency}/{item.kind.value} sospechosa: {previous.rate} → {item.rate}; no se guardó."
                        log.warning("rates.suspicious_jump", currency=item.currency, kind=item.kind.value,
                                    previous=str(previous.rate), new=str(item.rate))
                        await self._alerter.alert("⚠️ " + msg)
                        continue
                    session.add(ExchangeRate(currency=item.currency, kind=item.kind.value, provider=provider, rate=item.rate,
                                             rate_date=item.rate_date))
                    saved[f"{item.currency}:{item.kind.value}"] = str(item.rate)
        return {"status": "ok", **saved}
