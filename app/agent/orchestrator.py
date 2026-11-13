"""Orquestador del agente.

El pipeline solo conoce el Protocol `AgentOrchestrator`. `PydanticAIOrchestrator` es la implementación de hoy;
un orquestador basado en LangGraph podría reemplazarla sin tocar comandos, métricas, tests de dominio ni evals.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel
from pydantic_ai import Agent, ModelHTTPError, RunContext, UsageLimitExceeded
from pydantic_ai.messages import BinaryContent, ModelMessage, UserContent
from pydantic_ai.usage import UsageLimits

from app.agent import grounding
from app.agent.action_grounding import ACTION_GROUNDING_INSTRUCTIONS, WriteClaimAssessment
from app.agent.context_builder import dynamic_instructions, history_from_window
from app.agent.deps import TurnDeps
from app.agent.prompts.brokerage import BROKERAGE_ROUTER_INSTRUCTIONS
from app.agent.prompts.system import ROUTER_INSTRUCTIONS, static_instructions
from app.agent.tools.registry import build_toolset
from app.domain.enums import BusinessVertical, Lane
from app.domain.text import emoji_only
from app.llm.recorder import llm_call_rows
from app.llm.roles import ModelRoles
from app.llm.thinking import thinking_settings
from app.logging import get_logger

log = get_logger(__name__)

EXHAUSTED_TEXT = "Esto me está tomando demasiados pasos 😅. ¿Qué operación o consulta del negocio necesitas resolver?"


class RouteDecision(BaseModel):
    lane: Literal["fast", "agent"]
    reason: str


@dataclass
class AgentResponse:
    text: str
    lane: Lane
    route_reason: str | None = None
    grounding: grounding.GroundingReport | None = None
    exhausted: bool = False
    stages: list[str] = field(default_factory=list)


class AgentOrchestrator(Protocol):
    async def handle(self, deps: TurnDeps, content: Sequence[UserContent], *, force_agent: bool = False) -> AgentResponse: ...


class PydanticAIOrchestrator:
    def __init__(self, roles: ModelRoles, *, agent_request_limit: int = 8, fast_request_limit: int = 4,
                 price_overrides: str = "{}", agent_thinking: str | None = None, fast_thinking: str | None = None,
                 request_timeout_s: float | None = None) -> None:
        self._roles = roles
        base = {"timeout": request_timeout_s} if request_timeout_s else {}
        # El ROUTER nunca razona y va aparte del carril rápido aunque hoy usen el mismo modelo: `output_type` lo hace
        # forzar `tool_choice`, y DeepSeek responde 400 a eso mientras piensa.
        # These roles select typed business actions, not creative prose. Reduce
        # sampling variation while retaining model interpretation of intent.
        self._router_settings = {**base, "temperature": 0}
        # El carril RÁPIDO sí puede (no fuerza `tool_choice`), pero es opt-in: sin `LLM_FAST_THINKING` se queda con el
        # piso de `build_model`, así que desplegar esto no le cambia el comportamiento a ningún proveedor.
        # `parallel_tool_calls`: que los varios registros de un mismo mensaje quepan en UNA respuesta. Importa
        # porque el loop se corta tras la primera vuelta de tools (ver `_run_agent`), así que lo que el modelo
        # dejara para una segunda vuelta se perdería. Medido contra DeepSeek: con razonamiento encendido y sin
        # forzar nada, "vendí 2 bujías a 3$ y pagué 20$ de luz" sale en dos tool calls paralelas.
        self._fast_settings = ({**base, "temperature": 0, "parallel_tool_calls": True,
                                **(thinking_settings(roles.fast.system, fast_thinking) if fast_thinking else {})}) or None
        # Cuánto piensa el CARRIL AGENTE, en vocabulario neutro: `thinking_settings` lo traduce al dialecto del
        # proveedor. Los demás roles se quedan con el piso de `build_model` — el router y el onboarding DEBEN
        # quedarse sin razonar porque fuerzan `tool_choice` y DeepSeek lo rechaza con 400.
        # Ojo: `merge_model_settings` fusiona PLANO (`base | overrides`), así que este `extra_body` reemplaza entero
        # al del modelo. Si algún día se le agrega otra clave allá, hay que repetirla acá o se pierde en silencio.
        self._vision_settings = base or None
        self._agent_settings = {**base, "temperature": 0,
                                **thinking_settings(roles.agent.system, agent_thinking)}
        self._agent_limit = agent_request_limit
        self._fast_limit = fast_request_limit
        self._prices = price_overrides
        self._action_grounding = Agent(output_type=WriteClaimAssessment, instructions=ACTION_GROUNDING_INSTRUCTIONS,
                                       defer_model_check=True, retries=1)
        self._routers = {
            BusinessVertical.RETAIL: Agent(output_type=RouteDecision, instructions=ROUTER_INSTRUCTIONS,
                                           defer_model_check=True, retries=1),
            BusinessVertical.BROKERAGE: Agent(output_type=RouteDecision, instructions=BROKERAGE_ROUTER_INSTRUCTIONS,
                                               defer_model_check=True, retries=1),
        }
        toolset = build_toolset()
        self._agents: dict[BusinessVertical, Agent[TurnDeps, str]] = {}
        for vertical in BusinessVertical:
            agent: Agent[TurnDeps, str] = Agent(
                deps_type=TurnDeps, instructions=static_instructions(vertical), toolsets=[toolset],
                defer_model_check=True, retries=2,
            )

            @agent.instructions
            async def _dynamic(ctx: RunContext[TurnDeps]) -> str:
                return await dynamic_instructions(ctx.deps)

            self._agents[vertical] = agent
        # Alias conservado para pruebas e integraciones que inspeccionan el agente retail.
        self.business_agent = self._agents[BusinessVertical.RETAIL]

    async def route(self, deps: TurnDeps, text: str) -> RouteDecision:
        try:
            router = self._routers[deps.business.vertical]
            route_input = text
            if deps.business.vertical == BusinessVertical.BROKERAGE:
                route_input = f"Rol autenticado: {deps.identity.member_role.value}\nMensaje: {text}"
            result = await router.run(route_input, model=self._roles.router, model_settings=self._router_settings)
            await deps.turns.record_llm_calls(deps.turn_id, llm_call_rows("router", result.new_messages(), self._prices))
            return result.output
        except Exception as exc:  # noqa: BLE001 — si el router falla, el carril seguro es el agente
            log.warning("router.failed", error=str(exc))
            return RouteDecision(lane="agent", reason=f"router_error: {type(exc).__name__}")

    async def _run_agent(self, deps: TurnDeps, content: Sequence[UserContent], *, agent: Agent[TurnDeps, str],
                         model: Any, history: list[ModelMessage],
                         limit: int, settings: Any) -> tuple[str, list[ModelMessage]]:
        """Corre el agente y corta el loop cuando el recibo determinista ya dice todo.

        Un turno de registro hace DOS llamadas al modelo: la primera pide la tool y la segunda escribe un texto
        que `TurnPipeline._compose` descarta, porque el recibo ya lo cuenta. Esa segunda llamada reenvía el
        prompt entero (instrucciones + esquemas de las 25 tools + contexto), así que es la mitad del costo del
        turno y no cambia una coma de lo que ve el dueño.

        El corte va antes del SEGUNDO `ModelRequestNode`: los nodos se emiten antes de ejecutarse, así que al
        verlo las tools del primero ya corrieron y lo único que falta por hacer es pedir ese texto.
        """
        model_requests = 0
        async with agent.iter(list(content), deps=deps, model=model, message_history=history,
                                            usage_limits=UsageLimits(request_limit=limit),
                                            model_settings=settings) as run:
            async for node in run:
                if not Agent.is_model_request_node(node):
                    continue
                model_requests += 1
                if model_requests > 1 and deps.turn_is_answered:
                    log.info("agent.stopped_early", lane=deps.lane.value, writes=deps.simple_writes)
                    return "", list(run.ctx.state.message_history[len(history):])
            result = run.result
        if result is None:  # el loop terminó sin salida (p. ej. se agotaron los reintentos): que responda el recibo
            return "", list(run.ctx.state.message_history[len(history):])
        return (result.output or "").strip(), result.new_messages()

    async def _repair(self, deps: TurnDeps, content: Sequence[UserContent], *, agent: Agent[TurnDeps, str],
                      model: Any, history: list[ModelMessage],
                      new_messages: list[ModelMessage]) -> tuple[str, list[ModelMessage]]:
        """El carril rápido cerró sin llamar NINGUNA herramienta: se repite el turno obligando a que llame una.

        El router ya se comprometió a que este mensaje es «uno o varios registros nuevos y simples», así que un
        turno sin herramienta es una violación del contrato, no una respuesta. Sin esto, el modelo puede
        contestar «✅ Venta registrada» sin haber registrado nada (incidente del 25-09-2026).

        `tool_choice` forzado y el razonamiento de DeepSeek son incompatibles —400 «Thinking mode does not
        support this tool_choice», medido con `scripts/_probe_tool_choice.py`—, así que el reintento apaga el
        razonamiento. Es el ÚNICO lugar donde se apaga: en el camino normal manda el razonamiento, aquí manda
        la garantía, porque este turno ya demostró que el modelo no está llamando la herramienta.

        Pydantic AI rechaza un `tool_choice` estático de `"required"` (el run nunca podría terminar) pero acepta
        un `model_settings` callable que lo varíe por paso: forzado en la primera llamada, libre después.
        """
        log.warning("agent.contract_violation", lane=deps.lane.value, stage="fast_no_tool")
        deps.contract_violations += 1
        deps.reset_turn_output()
        forced = {**thinking_settings(self._roles.fast.system, "off"), "parallel_tool_calls": True}

        def settings(ctx: Any) -> dict[str, Any]:
            return {**forced, **({"tool_choice": "required"} if ctx.run_step <= 1 else {})}

        try:
            output, repair_messages = await self._run_agent(deps, content, agent=agent, model=model, history=history,
                                                            limit=self._fast_limit, settings=settings)
        except (UsageLimitExceeded, ModelHTTPError) as exc:
            log.warning("agent.repair_failed", error=f"{type(exc).__name__}: {exc}")
            return "", new_messages
        log.info("agent.repaired", used_a_tool=deps.used_a_tool, writes=deps.simple_writes)
        return output, [*new_messages, *repair_messages]

    async def _check_grounding(self, deps: TurnDeps, output: str) -> grounding.GroundingReport:
        def build_grounding_report(*, claims_current_turn_write: bool = False,
                                   action_check_failed: bool = False,
                                   presentation_violation: bool = False) -> grounding.GroundingReport:
            report = grounding.check(output, deps.tool_results, deps.user_text, deps.receipts + [deps.context_text],
                                   wrote_something=deps.wrote_something,
                                   claims_current_turn_write=claims_current_turn_write,
                                   action_check_failed=action_check_failed)
            if presentation_violation:
                report.ok = False
                report.presentation_violation = True
            return report

        report = build_grounding_report()
        # Deterministic receipts/presentations and a bare acknowledgement need no
        # semantic review. Invalid numbers already require a rewrite or fallback.
        if (not report.ok or deps.wrote_something or not output.strip()
                or deps.read_presentation == output or emoji_only(output) == "👍"):
            return report
        try:
            summary_rules_apply = deps.business.vertical == BusinessVertical.BROKERAGE and any(
                isinstance(result, dict) and result.get("presentation_rules") for result in deps.tool_results
            )
            result = await self._action_grounding.run(
                json.dumps({"user_message": deps.user_text, "assistant_response": output,
                            "summary_rules_apply": summary_rules_apply}, ensure_ascii=False),
                model=self._roles.router, model_settings=self._router_settings,
                usage_limits=UsageLimits(request_limit=2))
            await deps.turns.record_llm_calls(deps.turn_id, llm_call_rows("action_grounding", result.new_messages(), self._prices))
            presentation_violation = summary_rules_apply and result.output.violates_house_presentation
            presentation_quote = result.output.house_presentation_quote
            unsupported_presentation_assessment = presentation_violation and (
                not presentation_quote or presentation_quote not in output
            )
            return build_grounding_report(claims_current_turn_write=result.output.claims_current_turn_write,
                                          action_check_failed=result.output.uncertain or unsupported_presentation_assessment,
                                          presentation_violation=presentation_violation and not unsupported_presentation_assessment)
        except Exception as exc:
            log.warning("grounding.action_verification_failed", error_type=type(exc).__name__)
            return build_grounding_report(action_check_failed=True)

    async def handle(self, deps: TurnDeps, content: Sequence[UserContent], *, force_agent: bool = False) -> AgentResponse:
        text = " ".join(c for c in content if isinstance(c, str))
        has_media = any(isinstance(c, BinaryContent) for c in content)
        if force_agent or has_media or not text:
            decision = RouteDecision(lane="agent", reason="forced" if force_agent else "media_or_empty")
        else:
            decision = await self.route(deps, text)
        deps.lane = Lane.FAST if decision.lane == "fast" else Lane.AGENT
        model = self._roles.fast if deps.lane == Lane.FAST else self._roles.agent
        limit = self._fast_limit if deps.lane == Lane.FAST else self._agent_limit
        settings = self._agent_settings if deps.lane == Lane.AGENT else self._fast_settings
        if has_media and self._roles.vision is not None:
            model, settings = self._roles.vision, self._vision_settings
        agent = self._agents[deps.business.vertical]
        window = await deps.store.get_window(deps.scope)
        history = history_from_window(window)

        try:
            output, new_messages = await self._run_agent(deps, content, agent=agent, model=model, history=history, limit=limit,
                                                         settings=settings)
            if deps.lane == Lane.FAST and not deps.used_a_tool:
                output, new_messages = await self._repair(deps, content, agent=agent, model=model, history=history,
                                                          new_messages=new_messages)
        except UsageLimitExceeded:
            log.warning("agent.usage_limit", lane=deps.lane.value)
            return AgentResponse(EXHAUSTED_TEXT, deps.lane, decision.reason, exhausted=True)
        await deps.turns.record_llm_calls(deps.turn_id, llm_call_rows(deps.lane.value, new_messages, self._prices))

        # Verifica la respuesta que se enviará, también si la vista estándar reemplaza el texto final.
        output = deps.read_presentation or output
        report = await self._check_grounding(deps, output)
        # Si ya se gastó un reintento reparando el turno, no se gasta otro puliendo un texto que el pipeline va
        # a descartar igual: `TurnPipeline._compose` bloquea lo que suene a recibo cuando no se escribió nada.
        if not report.ok and (deps.contract_violations or report.action_check_failed):
            log.info("grounding.rewrite_skipped", reasons=report.failure_reasons,
                     contract_violations=deps.contract_violations)
        elif not report.ok:
            log.info("grounding.retry", ungrounded=report.ungrounded, claimed_write=report.claimed_write)
            fix = ("Revisa tu respuesta: dices haber registrado, guardado, corregido o borrado algo, y en este turno "
                   "no llamaste ninguna herramienta de escritura, así que NO se guardó nada. Reescríbela sin "
                   "atribuirte esa acción. Responde solo con el texto final para el dueño."
                   if report.claimed_write else
                   f"Revisa tu respuesta: estas cifras no aparecen en los resultados de las herramientas: "
                   f"{', '.join(str(n) for n in report.ungrounded)}. Reescribe la respuesta usando SOLO cifras de las herramientas "
                   f"(puedes volver a consultar). Responde solo con el texto final para el dueño.")
            if report.presentation_violation:
                fix = ("Reescribe el reporte con los mismos datos: Casa se presenta únicamente con operaciones y cartera. "
                       "Los balances y comisiones corresponden exclusivamente a corredores con nombre. "
                       "Omite comentarios sobre conceptos que no aplican a Casa, también dentro de las filas de movimientos. "
                       "Conserva el resumen, los movimientos y los clientes solicitados. Responde con el reporte corregido.")
            try:
                deps.read_only = True
                retry = await agent.run(fix, deps=deps, model=model, message_history=[*history, *new_messages],
                                                      usage_limits=UsageLimits(request_limit=3), model_settings=settings)
                await deps.turns.record_llm_calls(deps.turn_id, llm_call_rows(f"{deps.lane.value}_grounding", retry.new_messages(),
                                                                              self._prices))
                retried = (retry.output or "").strip()
                if retried:
                    report2 = await self._check_grounding(deps, retried)
                    output, report = retried, report2
                # An empty rewrite has verified nothing. Preserve the rejection
                # instead of turning a failed consultation into "Listo 👍".
            except Exception as exc:
                # Commands may already be committed. Keep the original failed
                # report so the pipeline preserves receipts and blocks the prose.
                log.warning("grounding.revision_failed", error_type=type(exc).__name__)
            finally:
                deps.read_only = False
        return AgentResponse(output, deps.lane, decision.reason, report)
