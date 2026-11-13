"""Perfil de producto resuelto por negocio.

El catálogo es estático: habilita composición modular sin discovery dinámico ni
configuración ejecutable en base de datos.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import BusinessVertical, Capability, MemberRole


@dataclass(frozen=True, slots=True)
class BusinessProfile:
    vertical: BusinessVertical
    capabilities: frozenset[Capability]


PROFILES = {
    BusinessVertical.RETAIL: BusinessProfile(
        BusinessVertical.RETAIL,
        frozenset({Capability.RETAIL_OPERATIONS, Capability.RECEIVABLES}),
    ),
    BusinessVertical.BROKERAGE: BusinessProfile(
        BusinessVertical.BROKERAGE,
        frozenset({Capability.BROKER_LEDGER}),
    ),
}


def profile_for(vertical: str | BusinessVertical) -> BusinessProfile:
    return PROFILES[BusinessVertical(vertical)]


def panel_capabilities(vertical: BusinessVertical, role: MemberRole) -> list[str]:
    profile = profile_for(vertical)
    capabilities = {c.value for c in profile.capabilities}
    if vertical == BusinessVertical.BROKERAGE and role == MemberRole.BROKER:
        # El corredor puede usar el panel, pero las consultas quedan acotadas a su miembro.
        capabilities.discard(Capability.EXPORTS.value)
    return sorted(capabilities)
