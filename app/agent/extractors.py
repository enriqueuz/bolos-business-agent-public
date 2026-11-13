"""Llamadas chicas al LLM fuera del agente: intérprete del onboarding y transcripción de voz de respaldo."""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.messages import BinaryContent

from app.agent.prompts.onboarding import onboarding_instructions
from app.application.onboarding import Interpretation, InterpretRequest, OnboardingUpdates
from app.application.turns import TurnLog
from app.domain.enums import ChargeRateMode, PaymentMethod
from app.domain.money import to_decimal
from app.llm.recorder import llm_call_rows
from app.llm.roles import ModelRoles
from app.logging import get_logger

log = get_logger(__name__)


class _Updates(BaseModel):
    name: str | None = None
    what_sells: str | None = None
    charge_rate_mode: ChargeRateMode | None = None
    custom_charge_rate: float | None = Field(default=None, gt=0)
    payment_methods: list[PaymentMethod] | None = None
    in_suggested_country: bool | None = None
    country: str | None = None


class _Interpretation(BaseModel):
    updates: _Updates = Field(default_factory=_Updates)
    confirmed: bool = False
    reply: str = ""


class LlmOnboardingInterpreter:
    def __init__(self, roles: ModelRoles, turns: TurnLog, *, price_overrides: str = "{}", request_timeout_s: float | None = None) -> None:
        self._roles = roles
        self._turns = turns
        self._prices = price_overrides
        self._settings = {"timeout": request_timeout_s} if request_timeout_s else None
        self._agent = Agent(output_type=_Interpretation, defer_model_check=True, retries=1)

    async def interpret(self, request: InterpretRequest) -> Interpretation | None:
        try:
            result = await self._agent.run(request.message or "(mensaje sin texto)", model=self._roles.onboarding,
                                           instructions=onboarding_instructions(request), model_settings=self._settings)
        except Exception:  # noqa: BLE001 — sin interpretación el flujo repite la pregunta sin cambiar nada
            log.exception("onboarding.interpret_failed")
            return None
        if request.turn_id is not None:
            await self._turns.record_llm_calls(request.turn_id, llm_call_rows("onboarding", result.new_messages(), self._prices))
        out = result.output
        u = out.updates
        return Interpretation(OnboardingUpdates(name=u.name, what_sells=u.what_sells, charge_rate_mode=u.charge_rate_mode,
                                                custom_charge_rate=to_decimal(u.custom_charge_rate) if u.custom_charge_rate else None,
                                                payment_methods=u.payment_methods, in_suggested_country=u.in_suggested_country,
                                                country=u.country), out.confirmed, out.reply.strip())


class LlmTranscriber:
    """Respaldo si el proveedor no trae la transcripción de la nota de voz."""

    def __init__(self, roles: ModelRoles, *, request_timeout_s: float = 45.0) -> None:
        self._roles = roles
        self._settings = {"timeout": request_timeout_s}
        self._agent = Agent(defer_model_check=True, instructions=(
            "Transcribe literalmente esta nota de voz en español (con la jerga local). Devuelve solo el texto, sin comentarios."))

    async def transcribe(self, audio: bytes, mime_type: str) -> str | None:
        try:
            result = await self._agent.run([BinaryContent(data=audio, media_type=(mime_type or "audio/ogg").split(";")[0])],
                                           model=self._roles.transcribe, model_settings=self._settings)
            return (result.output or "").strip() or None
        except Exception:  # noqa: BLE001
            log.exception("transcribe.failed")
            return None
