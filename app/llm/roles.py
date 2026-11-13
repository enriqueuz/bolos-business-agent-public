"""Roles de modelo → instancias de Pydantic AI. Cambiar de modelo o proveedor = cambiar config.

El resto del código pide un ROL ("agent", "router"…), nunca un modelo concreto.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic_ai.models import Model, infer_model

from app.llm.thinking import thinking_settings


@dataclass(frozen=True)
class ModelRoles:
    router: Model
    fast: Model
    agent: Model
    onboarding: Model
    transcribe: Model
    vision: Model | None = None


def build_model(spec: str, api_key: str | None) -> Model:
    """'google:gemini-3.8-flash' → GoogleModel con la key de config. Otros prefijos: inferencia estándar de Pydantic AI
    (openai:, anthropic:, etc.), que lee sus keys del entorno."""
    if spec.startswith("google:"):
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(spec.split(":", 1)[1], provider=GoogleProvider(api_key=api_key))
    if spec.startswith("deepseek:"):
        from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
        from pydantic_ai.providers.deepseek import DeepSeekProvider

        # PISO: sin razonar. No es una preferencia — DeepSeek rechaza con 400 cualquier `tool_choice` forzado
        # mientras piensa ("Thinking mode does not support this tool_choice"), y el router y el onboarding usan
        # `output_type`, que Pydantic AI implementa exactamente así. Los roles que SÍ pueden razonar lo piden
        # explícitamente en los model_settings del run (hoy solo el carril agente; ver PydanticAIOrchestrator).
        # Sin `api_key`: el que recibe esta función es el de Gemini. DeepSeekProvider lee DEEPSEEK_API_KEY del entorno.
        return OpenAIChatModel(spec.split(":", 1)[1], provider=DeepSeekProvider(),
                               settings=OpenAIChatModelSettings(**thinking_settings("deepseek", "off")))
    return infer_model(spec)


def roles_from_settings(settings) -> ModelRoles:
    key = settings.gemini_api_key
    cache: dict[str, Model] = {}

    def get(spec: str) -> Model:
        if spec not in cache:
            cache[spec] = build_model(spec, key)
        return cache[spec]

    return ModelRoles(router=get(settings.llm_role_router), fast=get(settings.llm_role_fast), agent=get(settings.llm_role_agent),
                      onboarding=get(settings.llm_role_onboarding), transcribe=get(settings.llm_role_transcribe), vision=get(settings.llm_role_vision))
