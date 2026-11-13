"""Costo de una llamada al LLM cuando genai-prices no conoce el modelo (app/llm/recorder.py::response_cost).

Cubre el override de precio con y sin descuento de caché: la entrada en caché debe cobrarse a su propio
precio cuando el override lo trae, y como entrada normal (conservador) cuando no.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.usage import RequestUsage

from app.llm.recorder import response_cost

MODEL = "fictional-showcase-unknown-model"  # Intentionally absent from the pricing catalog


def _response(*, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0) -> ModelResponse:
    usage = RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read_tokens)
    return ModelResponse(parts=[TextPart("hola")], usage=usage, model_name=MODEL)


def test_no_override_returns_none() -> None:
    assert response_cost(_response(input_tokens=1000, output_tokens=100), overrides={}) is None


def test_unknown_model_in_overrides_returns_none() -> None:
    overrides = {"other-model": {"input": 1.0, "output": 2.0}}
    assert response_cost(_response(input_tokens=1000, output_tokens=100), overrides=overrides) is None


def test_without_cache_tokens_matches_flat_price() -> None:
    overrides = {MODEL: {"input": 0.75, "output": 3.75}}
    r = _response(input_tokens=1_000_000, output_tokens=1_000_000)
    assert response_cost(r, overrides) == Decimal("0.75") + Decimal("3.75")


def test_cache_read_without_its_own_price_is_charged_as_regular_input() -> None:
    """Sin precio de caché en el override: se cobra todo como entrada normal (conservador)."""
    overrides = {MODEL: {"input": 0.75, "output": 3.75}}
    r = _response(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=600_000)
    assert response_cost(r, overrides) == Decimal("0.75")


def test_cache_read_with_its_own_price_is_discounted() -> None:
    overrides = {MODEL: {"input": 0.75, "output": 3.75, "cache_read": 0.075}}
    r = _response(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=600_000)
    # 400k de entrada fresca a 0.75 + 600k en caché a 0.075 + 1M de salida a 3.75, todo por millón
    expected = (Decimal(400_000) * Decimal("0.75") + Decimal(600_000) * Decimal("0.075")) / Decimal(1_000_000) + Decimal("3.75")
    assert response_cost(r, overrides) == expected


def test_cache_read_tokens_never_exceed_input_tokens() -> None:
    """Un cache_read_tokens mayor al input_tokens (dato inconsistente del proveedor) no debe dar costo negativo."""
    overrides = {MODEL: {"input": 0.75, "output": 3.75, "cache_read": 0.075}}
    r = _response(input_tokens=1000, output_tokens=0, cache_read_tokens=5000)
    cost = response_cost(r, overrides)
    assert cost is not None and cost >= 0
    assert cost == Decimal(1000) * Decimal("0.075") / Decimal(1_000_000)
