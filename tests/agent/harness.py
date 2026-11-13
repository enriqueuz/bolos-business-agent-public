"""Arnés para probar el pipeline completo con modelos con guion (sin LLM real, sin patch()).

`Script` decide qué responde el "modelo" en cada llamada. Todo lo que pasa después del LLM (tools, CommandBus,
handles, confirmaciones, recibos, enlaces, observabilidad) corre de verdad contra Postgres y Redis.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.application.ports import FetchedRate
from app.channels.console import ConsoleProvider
from app.config import Settings
from app.container import Container, build_container
from app.domain.enums import RateKind
from app.llm.roles import ModelRoles

Step = dict[str, Any] | Callable[[list[ModelMessage], AgentInfo], ModelResponse]


class NoLLM(AssertionError):
    pass


@dataclass
class Script:
    """Pasos del modelo del agente. Cada llamada consume un paso."""

    steps: list[Step] = field(default_factory=list)
    lane: str = "agent"
    calls: int = 0
    seen_instructions: list[str] = field(default_factory=list)
    seen_tool_returns: list[Any] = field(default_factory=list)
    # Respuestas del intérprete de onboarding: dicts con la forma de su salida ({"updates": {...}, "confirmed", "reply"})
    onboarding: list[dict[str, Any]] = field(default_factory=list)
    onboarding_instructions: list[str] = field(default_factory=list)
    write_claim_assessments: list[dict[str, bool] | Exception] = field(default_factory=list)
    action_grounding_calls: int = 0

    def agent_fn(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        self.seen_instructions.append(info.instructions or "")
        last = messages[-1]
        for part in getattr(last, "parts", []):
            if isinstance(part, ToolReturnPart):
                self.seen_tool_returns.append(part.content)
        if not self.steps:
            raise NoLLM("El modelo fue llamado más veces de las previstas por el test")
        step = self.steps.pop(0)
        if callable(step):
            return step(messages, info)
        if "text" in step:
            return ModelResponse(parts=[TextPart(step["text"])])
        return ModelResponse(parts=[ToolCallPart(step["tool"], step.get("args", {}))])

    def onboarding_fn(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.onboarding_instructions.append(info.instructions or "")
        if not self.onboarding:
            raise NoLLM("El intérprete de onboarding fue llamado más veces de las previstas por el test")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, self.onboarding.pop(0))])

    def router_fn(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool = info.output_tools[0].name
        if "claims_current_turn_write" in info.output_tools[0].parameters_json_schema.get("properties", {}):
            self.action_grounding_calls += 1
            assessment = (self.write_claim_assessments.pop(0) if self.write_claim_assessments
                          else {"claims_current_turn_write": False, "uncertain": False})
            if isinstance(assessment, Exception):
                raise assessment
            return ModelResponse(parts=[ToolCallPart(tool, assessment)])
        return ModelResponse(parts=[ToolCallPart(tool, {"lane": self.lane, "reason": "test"})])


def handle_in_quoted_section(info: AgentInfo) -> str:
    text = info.instructions or ""
    section = text.split("EL DUEÑO ESTÁ RESPONDIENDO", 1)[1]
    return re.search(r"\b(R\d+) \|", section).group(1)


class FakeRates:
    async def fetch(self) -> list[FetchedRate]:
        return [FetchedRate("VES", RateKind.OFFICIAL, Decimal("800"), None),
                FetchedRate("VES", RateKind.MARKET, Decimal("900"), None)]


def transcribe_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise NoLLM("la transcripción no debería llamarse en este test")


def build_test_container(script: Script, *, allowed_phones: str = "584140000001",
                         system_admin_phones: str = "", system_admin_password_hash: str = "") -> tuple[Container, ConsoleProvider]:
    settings = Settings(database_url=os.environ["DATABASE_URL"], redis_url=os.environ["REDIS_URL"], app_env="test",
                        signup_mode="allowlist", signup_allowed_phones=allowed_phones,
                        system_admin_phones=system_admin_phones, system_admin_password_hash=system_admin_password_hash,
                        messaging_provider="console")
    console = ConsoleProvider()
    router = FunctionModel(script.router_fn, model_name="test-router")
    agent = FunctionModel(script.agent_fn, model_name="test-agent")
    onboarding = FunctionModel(script.onboarding_fn, model_name="test-onboarding")
    transcribe = FunctionModel(transcribe_fn, model_name="test-transcribe")
    roles = ModelRoles(router=router, fast=agent, agent=agent, onboarding=onboarding, transcribe=transcribe)
    container = build_container(settings, messaging=console, roles=roles, rate_fetchers={"dolarapi_ve": FakeRates()})
    return container, console
