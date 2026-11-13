"""Puertos hacia el mundo exterior que la capa de aplicación necesita. Implementaciones en app/infra."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.domain.enums import RateKind


@dataclass(frozen=True, slots=True)
class FetchedRate:
    currency: str  # moneda local (ISO 4217); la tasa es unidades de esta moneda por 1 USD
    kind: RateKind
    rate: Decimal
    rate_date: datetime | None


class RateFetcher(Protocol):
    """Un proveedor de tasas de UN país (p. ej. dolarapi para VES). Se registra por CountryProfile.rate_provider."""

    async def fetch(self) -> list[FetchedRate]: ...


class Alerter(Protocol):
    async def alert(self, text: str) -> None: ...


class NullAlerter:
    async def alert(self, text: str) -> None:  # pragma: no cover - trivial
        return None


class AttachmentStore(Protocol):
    """Almacenamiento privado de comprobantes; ninguna clave se expone sin una URL firmada."""

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def signed_url(self, key: str, *, expires_s: int) -> str: ...
