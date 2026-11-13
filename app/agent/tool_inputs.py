"""Tipos reutilizables para normalizar entradas en la frontera de las tools."""

from typing import Annotated, Any

from pydantic import BeforeValidator


def _normalize_nullable_string(value: Any) -> Any:
    """Convierte el centinela textual exacto de ausencia antes de validar el tipo."""
    if isinstance(value, str) and value.strip().casefold() == "null":
        return None
    return value


NullableToolString = Annotated[str | None, BeforeValidator(_normalize_nullable_string)]
