"""Cuánto «piensa» el modelo, en un vocabulario neutro.

Cada proveedor lo pide a su manera y con su propia escala: Gemini con `thinking: "low"`, DeepSeek con
`extra_body.thinking` más un `reasoning_effort`. El resto del código pide un NIVEL y esta capa lo traduce, para que
cambiar de proveedor sea cambiar `LLM_ROLE_*` y nada más.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

from app.logging import get_logger

log = get_logger(__name__)

Effort = Literal["off", "low", "medium", "high"]
LEVELS: tuple[str, ...] = get_args(Effort)

# DeepSeek acepta minimal|low|medium|high|xhigh|max|ultra, pero por dentro solo tiene TRES niveles —low, high y max—
# y colapsa el resto (https://api-docs.deepseek.com/guides/thinking_mode/). Se mapea a esos tres para que cada
# escalón del vocabulario neutro signifique algo distinto de verdad y no queden dos niveles idénticos.
_DEEPSEEK_EFFORT = {"low": "low", "medium": "high", "high": "max"}


def normalize(value: str | None) -> Effort:
    """Nivel neutro a partir de la config. Un valor desconocido no revienta el arranque: cae en "off", que es el
    único seguro en todos los proveedores, y se avisa por log para que no pase inadvertido."""
    level = (value or "off").strip().lower()
    if level not in LEVELS:
        log.warning("llm.thinking_level_unknown", value=value, using="off")
        return "off"
    return level  # type: ignore[return-value]


def thinking_settings(system: str | None, effort: str | None) -> dict[str, Any]:
    """`model_settings` parciales para pedirle a ESE proveedor ese nivel de razonamiento.

    Un proveedor que no conocemos devuelve `{}`: corre con su default en vez de romper por una clave que no entiende.
    """
    level = normalize(effort)
    if system == "google":
        # Gemini no ofrece un apagado real; "minimal" es lo más cerca. Los otros niveles se llaman igual que aquí,
        # así que con Gemini esto devuelve exactamente lo que el orquestador mandaba antes de existir esta capa.
        return {"thinking": "minimal" if level == "off" else level}
    if system == "deepseek":
        # `thinking` DEBE viajar en extra_body, no al tope del body. `reasoning_effort` va por el mismo camino en vez
        # de por `openai_reasoning_effort`, que está tipado a low|medium|high y dejaría fuera el "max" de DeepSeek.
        if level == "off":
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {"extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": _DEEPSEEK_EFFORT[level]}}
    return {}
