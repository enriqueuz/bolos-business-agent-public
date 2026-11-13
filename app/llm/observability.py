"""Qué modelo y qué razonamiento corre de verdad en cada rol, dicho en voz alta al arrancar.

Desde fuera no había forma de saberlo: `llm_calls.thinking_tokens = 0` **no** distingue «razonamiento
apagado» de «encendido y sin nada que razonar» — el 60 % de las llamadas del carril agente, con el
razonamiento encendido sin discusión, también emiten 0. Esa ambigüedad ya costó una conclusión equivocada
leyendo la telemetría de un incidente; en medio del siguiente costaría más.
"""

from __future__ import annotations

from typing import Any

from app.llm.roles import ModelRoles
from app.llm.thinking import normalize, thinking_settings
from app.logging import get_logger

log = get_logger(__name__)


def _role_config(spec: str, model: Any, effort: str | None) -> dict[str, Any]:
    settings = thinking_settings(getattr(model, "system", None), effort) if effort else {}
    return {"model": spec, "thinking": normalize(effort) if effort else "piso del proveedor",
            "settings": settings or None}


def log_effective_llm_config(settings: Any, roles: ModelRoles) -> None:
    log.info(
        "llm.effective_config",
        router=_role_config(settings.llm_role_router, roles.router, None),
        fast=_role_config(settings.llm_role_fast, roles.fast, settings.llm_fast_thinking),
        agent=_role_config(settings.llm_role_agent, roles.agent, settings.llm_agent_thinking),
        onboarding=_role_config(settings.llm_role_onboarding, roles.onboarding, None),
        # El carril rápido solo fuerza `tool_choice` en el reintento de reparación: DeepSeek devuelve 400 si se
        # combina con razonamiento (ver `PydanticAIOrchestrator._repair` y `scripts/_probe_tool_choice.py`).
        tool_choice="forzado solo en el reintento de reparación del carril rápido",
    )
