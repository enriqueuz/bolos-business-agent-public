"""Perfiles de país: TODO lo que cambia entre Venezuela, Colombia, Argentina… vive aquí y en nada más.

Agregar un país:
  1. su moneda en currencies.py
  2. un CountryProfile en PROFILES (+ prefijo telefónico). El prefijo es solo una SUGERENCIA: el onboarding siempre
     pregunta "¿Tu negocio está en <país>?" antes del primer registro (un venezolano con línea +57 no es de Colombia).
  3. un RateFetcher para sus tasas en app/infra/rates/ registrado por `rate_provider`
  4. casos de eval con su jerga
Esquema, comandos, métricas, correcciones y panel no cambian.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal

from app.domain.currencies import USD, CurrencyInfo, currency_info
from app.domain.enums import PAYMENT_METHOD_LABELS, CurrencyView, PaymentMethod, RateAppliedSource
from app.domain.text import normalize_name


@dataclass(frozen=True, slots=True)
class CountryProfile:
    code: str  # ISO 3166-1 alfa-2
    name: str
    local_currency: str  # ISO 4217
    timezone: str
    phone_prefixes: tuple[str, ...]
    official_rate_label: str  # "BCV", "TRM", "oficial"
    market_rate_label: str  # "mercado", "blue"
    # False en países donde la tasa de mercado ≈ oficial: la pata de mercado se iguala a la oficial y la UI la oculta.
    has_market_rate: bool
    rate_provider: str  # clave del RateFetcher (app/infra/rates)
    # Montos sin moneda explícita por debajo de esto se asumen en USD (heurística por magnitud).
    assume_usd_below: Decimal
    # Marcas de moneda local en el lenguaje del dueño (regex, texto normalizado en minúsculas)
    local_currency_markers: tuple[str, ...]
    usd_markers: tuple[str, ...] = (r"\$", r"\bd[oó]lar(es)?\b", r"\bverdes?\b", r"\busd\b", r"\bdivisas?\b")
    # Glosario de jerga monetaria para el prompt
    slang: tuple[str, ...] = ()
    onboarding_example_product: str = "productos"
    locale: str = "es"
    extra_prompt_notes: tuple[str, ...] = field(default_factory=tuple)
    # Nombres locales de los métodos de pago (sobre las etiquetas neutras de enums.py)
    payment_method_labels: dict[str, str] = field(default_factory=dict)

    def payment_label(self, method: str | None) -> str | None:
        if not method:
            return None
        return self.payment_method_labels.get(method) or PAYMENT_METHOD_LABELS.get(PaymentMethod(method), method)

    def view_label(self, view: CurrencyView) -> str:
        """Nombre largo de la vista de reportes: 'dólares a tasa BCV', 'bolívares'…"""
        return {
            CurrencyView.LOCAL: self.local.plural,
            CurrencyView.USD_APPLIED: "dólares a tu tasa para cobrar",
            CurrencyView.USD_OFFICIAL: f"dólares a tasa {self.official_rate_label}",
            CurrencyView.USD_MARKET: f"dólares a tasa de {self.market_rate_label}",
        }[view]

    def view_title(self, view: CurrencyView) -> str:
        """Título corto para resúmenes: 'en Bs', 'en $ a tasa BCV'."""
        return {
            CurrencyView.LOCAL: f"en {self.local.symbol}",
            CurrencyView.USD_APPLIED: "en $ a tu tasa para cobrar",
            CurrencyView.USD_OFFICIAL: f"en $ a tasa {self.official_rate_label}",
            CurrencyView.USD_MARKET: f"en $ a tasa de {self.market_rate_label}",
        }[view]

    def applied_rate_label(self, source: RateAppliedSource) -> str:
        """Nombre de la tasa para cobrar según de dónde salió: 'BCV', 'mercado', 'tu tasa propia'…"""
        return {
            RateAppliedSource.OFFICIAL: self.official_rate_label,
            RateAppliedSource.MARKET: self.market_rate_label,
            RateAppliedSource.CUSTOM: "tu tasa propia",
            RateAppliedSource.INLINE: "la tasa de ese movimiento",
            RateAppliedSource.EXCHANGE: "la tasa del cambio",
        }[source]

    def to_public(self) -> dict[str, object]:
        """Lo que necesita el panel para mostrar monedas y tasas sin saber de qué país se trata."""
        return {
            "country": self.code, "country_name": self.name, "local_currency": self.local_currency,
            "local_symbol": self.local.symbol, "local_symbol_after": self.local.symbol_after, "local_decimals": self.local.decimals,
            "local_currency_name": self.local.plural, "official_rate_label": self.official_rate_label,
            "market_rate_label": self.market_rate_label, "has_market_rate": self.has_market_rate,
            "payment_method_labels": {m.value: self.payment_label(m.value) for m in PaymentMethod},
        }

    @property
    def local(self) -> CurrencyInfo:
        return currency_info(self.local_currency)

    @property
    def allowed_currencies(self) -> tuple[str, ...]:
        return (self.local_currency, USD)

    def currency_hint(self, text: str) -> str | None:
        """Moneda si el texto trae UNA sola marca inequívoca (local o USD); None si no trae o trae ambas."""
        t = text.lower()
        usd = any(re.search(p, t) for p in self.usd_markers)
        local = any(re.search(p, t) for p in self.local_currency_markers)
        if usd == local:
            return None
        return USD if usd else self.local_currency


VENEZUELA = CountryProfile(
    code="VE",
    name="Venezuela",
    local_currency="VES",
    timezone="America/Caracas",
    phone_prefixes=("58",),
    official_rate_label="BCV",
    market_rate_label="mercado",
    has_market_rate=True,
    rate_provider="dolarapi_ve",
    assume_usd_below=Decimal("1000"),
    local_currency_markers=(r"\bbs\.?\b", r"\bbol[ií]var(es)?\b", r"\bbolos\b", r"\blucas?\b", r"\bpalos?\b", r"\bves\b"),
    slang=('"Bs", "bolos", "bolívares" = VES', '"lucas" = miles de Bs', '"palos" = millones de Bs',
           '"$", "dólares", "verdes", "divisas" = USD'),
    onboarding_example_product="productos",
    locale="es-VE",
    extra_prompt_notes=('Di "tasa de mercado", "tasa para cobrar" o "tasa real de tus bolívares"; nunca "paralelo" ni "paralela".',),
    payment_method_labels={"card": "Punto", "crypto": "Binance", "mobile_payment": "Pago móvil"},
)

PROFILES: dict[str, CountryProfile] = {p.code: p for p in (VENEZUELA,)}
DEFAULT_COUNTRY = "VE"


def country_profile(code: str | None) -> CountryProfile:
    return PROFILES.get((code or DEFAULT_COUNTRY).upper(), PROFILES[DEFAULT_COUNTRY])


def profile_for_phone(phone: str, default: str = DEFAULT_COUNTRY) -> CountryProfile:
    """País SUGERIDO por el prefijo (gana el más largo: '1809' antes que '1'). El onboarding lo confirma con el dueño."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    matches = [(len(prefix), profile) for profile in PROFILES.values() for prefix in profile.phone_prefixes
               if digits.startswith(prefix)]
    return max(matches, key=lambda m: m[0])[1] if matches else country_profile(default)


def profile_for_country_name(text: str) -> CountryProfile | None:
    """'venezuela', 'Venezuela 🇻🇪', 've' → perfil soportado; None si Bolos aún no maneja ese país."""
    t = normalize_name(text)
    for profile in PROFILES.values():
        if t in (normalize_name(profile.name), profile.code.lower()):
            return profile
    return None


def profile_for_currency(local_currency: str) -> CountryProfile:
    for profile in PROFILES.values():
        if profile.local_currency == local_currency:
            return profile
    return country_profile(DEFAULT_COUNTRY)


# Perfil "de visualización" del contexto actual (como un locale de i18n): lo fija cada operación (CommandBus, turno del
# agente, request del panel) con el país del negocio, para que resúmenes y etiquetas usen los nombres locales sin
# pasar el perfil por cada función. Si no hay uno fijado, se usan etiquetas neutras.
_display_profile: ContextVar[CountryProfile | None] = ContextVar("display_profile", default=None)


def use_display_profile(profile: CountryProfile) -> None:
    _display_profile.set(profile)


def display_profile() -> CountryProfile | None:
    return _display_profile.get()
