"""Convierte los mensajes de una ejecución de Pydantic AI en filas de `llm_calls` (independiente del proveedor)."""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache
from typing import Any

from pydantic_ai import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse

from app.logging import get_logger

log = get_logger(__name__)

MAX_PAYLOAD_CHARS = 60_000


@lru_cache(maxsize=8)
def _price_overrides(raw: str) -> dict[str, dict[str, float]]:
    """JSON inválido = se avisa UNA vez, no en cada llamada.

    Invalid overrides can silently lose cost attribution; validate_price_overrides rejects them at startup.
    """
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        log.warning("llm.price_overrides_invalid", raw=raw[:120])
        return {}


def validate_price_overrides(raw: str) -> None:
    """Falla al arrancar si `LLM_PRICE_OVERRIDES` no es JSON: sin esto el costo se pierde en silencio."""
    try:
        json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM_PRICE_OVERRIDES no es JSON válido: {exc}") from exc


def response_cost(response: ModelResponse, overrides: dict[str, dict[str, float]]) -> Decimal | None:
    try:
        return Decimal(str(response.cost().total_price))
    except Exception:  # noqa: BLE001 — genai-prices no conoce el modelo
        price = overrides.get(response.model_name or "")
        if not price:
            return None
        u = response.usage
        # Tokens de entrada que releyó del caché (más baratos): si el override no trae su precio, se
        # cobran como entrada normal (conservador: nunca subestima el costo real).
        cached = min(u.cache_read_tokens or 0, u.input_tokens or 0)
        fresh = (u.input_tokens or 0) - cached
        cache_price = Decimal(str(price.get("cache_read", price["input"])))
        input_cost = Decimal(fresh) * Decimal(str(price["input"])) + Decimal(cached) * cache_price
        output_cost = Decimal(u.output_tokens or 0) * Decimal(str(price["output"]))
        return (input_cost + output_cost) / Decimal(1_000_000)


def _dump(messages: list[ModelMessage]) -> Any:
    data = ModelMessagesTypeAdapter.dump_python(messages, mode="json")
    text = json.dumps(data, ensure_ascii=False)
    if len(text) > MAX_PAYLOAD_CHARS:
        return {"truncated": True, "preview": text[:MAX_PAYLOAD_CHARS]}
    return data


def llm_call_rows(role: str, new_messages: list[ModelMessage], price_overrides_raw: str = "{}") -> list[dict[str, Any]]:
    overrides = _price_overrides(price_overrides_raw)
    rows: list[dict[str, Any]] = []
    pending_request: list[ModelMessage] = []
    previous_ts = None
    for msg in new_messages:
        if isinstance(msg, ModelRequest):
            pending_request.append(msg)
            previous_ts = previous_ts or getattr(msg, "timestamp", None)
            continue
        if isinstance(msg, ModelResponse):
            usage = msg.usage
            thinking = int((usage.details or {}).get("thoughts_tokens", 0) or (usage.details or {}).get("reasoning_tokens", 0) or 0)
            latency = None
            if previous_ts and msg.timestamp:
                latency = max(0, int((msg.timestamp - previous_ts).total_seconds() * 1000))
            rows.append({
                "role": role, "model": msg.model_name, "provider": msg.provider_name,
                "request": _dump(pending_request) if pending_request else None, "response": _dump([msg]),
                "input_tokens": usage.input_tokens or 0, "output_tokens": usage.output_tokens or 0, "thinking_tokens": thinking,
                "cost_usd": response_cost(msg, overrides), "finish_reason": msg.finish_reason, "latency_ms": latency,
            })
            pending_request, previous_ts = [], msg.timestamp
    return rows
