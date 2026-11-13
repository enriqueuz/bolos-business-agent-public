"""La traducción de «cuánto piensa» al dialecto de cada proveedor (app/llm/thinking.py).

Es una tabla, así que se prueba como tabla. Lo que de verdad protege esto es que cambiar de proveedor NO cambie lo
que ve el otro: el caso `google` fija byte a byte lo que el orquestador mandaba antes de que existiera la capa.
"""

from __future__ import annotations

import pytest

from app.llm.thinking import LEVELS, thinking_settings


@pytest.mark.parametrize("level", ["low", "medium", "high"])
def test_google_passes_the_level_through_unchanged(level):
    """Con Gemini el nivel neutro y el del proveedor se llaman igual: nada que traducir, nada que romper."""
    assert thinking_settings("google", level) == {"thinking": level}


def test_google_off_is_minimal_because_gemini_has_no_real_off():
    assert thinking_settings("google", "off") == {"thinking": "minimal"}


def test_deepseek_off_disables_thinking():
    """El piso de DeepSeek. Sin esto, el router y el onboarding (que fuerzan tool_choice) responden 400."""
    assert thinking_settings("deepseek", "off") == {"extra_body": {"thinking": {"type": "disabled"}}}


@pytest.mark.parametrize(("level", "effort"), [("low", "low"), ("medium", "high"), ("high", "max")])
def test_deepseek_uses_its_three_real_tiers(level, effort):
    """DeepSeek acepta más nombres de los que distingue: colapsa todo en low/high/max. Cada escalón neutro tiene que
    caer en uno distinto, o tendríamos dos niveles que hacen exactamente lo mismo."""
    assert thinking_settings("deepseek", level) == {
        "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": effort}}


def test_deepseek_never_uses_the_typed_openai_field():
    """`openai_reasoning_effort` está tipado a low|medium|high y dejaría fuera el "max" de DeepSeek."""
    assert "openai_reasoning_effort" not in thinking_settings("deepseek", "high")


@pytest.mark.parametrize("level", LEVELS)
def test_unknown_provider_is_inert(level):
    """Un proveedor que no conocemos corre con su default en vez de romper por una clave que no entiende."""
    assert thinking_settings("anthropic", level) == {}
    assert thinking_settings(None, level) == {}


@pytest.mark.parametrize("value", [None, "", "  ", "altísimo", "true"])
def test_a_bad_config_value_falls_back_to_off_instead_of_crashing(value):
    """Un typo en LLM_AGENT_THINKING no puede tumbar el arranque; cae en el único nivel seguro en todos lados."""
    assert thinking_settings("deepseek", value) == {"extra_body": {"thinking": {"type": "disabled"}}}
    assert thinking_settings("google", value) == {"thinking": "minimal"}


def test_levels_are_case_and_space_insensitive():
    assert thinking_settings("google", " LOW ") == {"thinking": "low"}


class TestOrchestratorWiring:
    """Qué ajustes termina recibiendo cada rol. Lo que protege esto es que el ROUTER nunca razone —fuerza
    `tool_choice` y DeepSeek responde 400— y que encender el carril rápido no se lo contagie."""

    @pytest.fixture(autouse=True)
    def provider_credentials(self, monkeypatch):
        # These tests only construct models; they never call the provider and
        # must not depend on a developer's real key being configured.
        monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-placeholder")

    @staticmethod
    def _build(spec: str, **kwargs):
        from app.agent.orchestrator import PydanticAIOrchestrator
        from app.llm.roles import ModelRoles, build_model

        m = build_model(spec, "no-usada-por-deepseek")
        roles = ModelRoles(router=m, fast=m, agent=m, onboarding=m, transcribe=m)
        return PydanticAIOrchestrator(roles, request_timeout_s=45.0, **kwargs)

    def test_router_never_reasons_even_with_both_lanes_on(self):
        o = self._build("deepseek:deepseek-flash", agent_thinking="low", fast_thinking="low")
        assert o._router_settings == {"timeout": 45.0, "temperature": 0}

    def test_fast_lane_is_opt_in_and_untouched_by_default(self):
        """Sin LLM_FAST_THINKING el carril rápido se queda con el piso de razonamiento del modelo.

        `parallel_tool_calls` va aparte y siempre: los varios registros de un mensaje tienen que caber en UNA
        respuesta, porque el loop se corta tras la primera vuelta de tools.
        """
        o = self._build("deepseek:deepseek-flash", agent_thinking="low")
        assert o._fast_settings == {"timeout": 45.0, "temperature": 0, "parallel_tool_calls": True}

    def test_fast_lane_reasons_when_asked(self):
        o = self._build("deepseek:deepseek-flash", agent_thinking="low", fast_thinking="low")
        assert o._fast_settings == {"timeout": 45.0, "temperature": 0, "parallel_tool_calls": True,
                                    "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}}

    def test_gemini_preserves_thinking_with_low_variation_action_settings(self):
        """Solo el carril agente razona; los roles de texto comparten temperatura cero."""
        o = self._build("google:gemini-3.8-flash", agent_thinking="low")
        assert o._agent_settings == {"timeout": 45.0, "temperature": 0, "thinking": "low"}
        assert o._fast_settings == {"timeout": 45.0, "temperature": 0, "parallel_tool_calls": True}
        assert o._router_settings == {"timeout": 45.0, "temperature": 0}
