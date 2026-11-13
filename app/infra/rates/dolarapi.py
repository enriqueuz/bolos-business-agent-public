"""Proveedor de tasas de Venezuela: ve.dolarapi.com (oficial BCV y paralelo/mercado).

Otros países tendrán su propio fetcher en este paquete, registrado en `build_rate_fetchers` con la clave de
su CountryProfile.rate_provider.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx

from app.application.ports import FetchedRate, RateFetcher
from app.domain.enums import RateKind
from app.logging import get_logger

log = get_logger(__name__)

DOLARAPI_VE_OFFICIAL = "https://ve.dolarapi.com/v1/dolares/oficial"
DOLARAPI_VE_MARKET = "https://ve.dolarapi.com/v1/dolares/paralelo"


class DolarApiVeFetcher:
    currency = "VES"

    def __init__(self, official_url: str = DOLARAPI_VE_OFFICIAL, market_url: str = DOLARAPI_VE_MARKET,
                 http: httpx.AsyncClient | None = None) -> None:
        self._urls = {RateKind.OFFICIAL: official_url, RateKind.MARKET: market_url}
        self._http = http or httpx.AsyncClient(timeout=10)

    async def fetch(self) -> list[FetchedRate]:
        out = []
        for kind, url in self._urls.items():
            try:
                resp = await self._http.get(url)
                resp.raise_for_status()
                data = resp.json()
                value = data.get("promedio") or data.get("venta") or data.get("compra")
                rate = Decimal(str(value))
                if not rate.is_finite() or rate <= 0:
                    raise ValueError("Tasa inválida")
                ts = data.get("fechaActualizacion")
                rate_date = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
                out.append(FetchedRate(self.currency, kind, rate, rate_date))
            except (httpx.HTTPError, ValueError, InvalidOperation, AttributeError, TypeError) as exc:
                # La caída de mercado no debe descartar la tasa oficial que sí está disponible.
                log.warning("rates.endpoint_failed", kind=kind.value, error=type(exc).__name__)
        return out


def build_rate_fetchers() -> dict[str, RateFetcher]:
    """Proveedor (CountryProfile.rate_provider) → fetcher. Agregar un país = agregar su entrada aquí."""
    return {"dolarapi_ve": DolarApiVeFetcher()}
