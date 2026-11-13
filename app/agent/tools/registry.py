"""Registro de tools y ejecutor.

Las tools son adaptadores FINOS: arman un Command/Query con los args del modelo + lo inyectado, y delegan.
Toda la lógica de negocio vive en app/application. Este módulo se encarga de:
- visibilidad por turno (`prepare`): el modelo no ve tools que no puede usar
- errores de dominio → JSON que el modelo entiende y usa para corregirse
- confirmación en dos fases (PendingAction congelada)
- handles efímeros en vez de ids
- recibos deterministas y observabilidad (tool_calls)
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic_ai import RunContext, Tool
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset

from app.agent import receipts
from app.agent.deps import TurnDeps
from app.agent.money_evidence import validate_required_money_evidence
from app.agent.reconciliation import RECONCILIATION_TOOLS, finish_reconciliation
from app.application.audit import json_safe
from app.application.bus import Command, CommandResult
from app.application.commands.brokerage import BrokerCommand
from app.application.context import Actor, ExecContext
from app.application.conversation import BrokerMoneyDraft, ChoiceOption, Handle, PendingAction, PendingChoice
from app.application.queries.service import QueryResult
from app.domain.dates import local_today
from app.domain.enums import ActorType, BusinessVertical, Lane, LinkRelation, ToolOutcome
from app.domain.errors import (
    Ambiguous,
    DomainError,
    MissingBrokerBolivars,
    MissingBrokerPurchaseCustomer,
    NeedsChoice,
)
from app.domain.text import normalize_name
from app.logging import get_logger

log = get_logger(__name__)

# Registros que el botón "Deshacer" puede borrar justo después de crearlos (el primero que devuelve el comando).
UNDOABLE_KINDS = frozenset({"record_sale", "record_expense", "record_purchase", "create_receivable", "record_payment",
                            "record_exchange", "record_broker_account_payment", "record_broker_account_withdrawal", "record_broker_operation",
                            "record_broker_account_purchase", "record_pending_broker_purchase",
                            "record_broker_receivable_payment", "record_broker_customer_payout", "record_broker_purchase"})
# Escrituras que dejan al objeto fuera de alcance: su recibo no lleva enlace al panel (llevaría a un 404).
DELETING_KINDS = frozenset({"delete_record", "delete_product", "delete_broker_movement"})
AWAITING_CHOICE_INSTRUCTION = ("NO se guardó todavía. El sistema ya le mostró al dueño las opciones para elegir (botones o "
                               "números). No preguntes de nuevo ni las enumeres; como mucho, una frase corta o nada.")

HISTORY_RESULT_MAX = 400  # lo que de cada resultado viaja a la memoria corta

FAST_AND_AGENT = frozenset({Lane.FAST, Lane.AGENT})
AGENT_ONLY = frozenset({Lane.AGENT})
BROKER_OPERATION_KINDS = frozenset({"record_broker_operation", "record_broker_purchase",
                                    "record_broker_account_purchase", "record_pending_broker_purchase"})


async def remember_broker_money_draft(deps: TurnDeps, command: Command, *, question: str | None = None) -> None:
    previous = await deps.store.get_broker_money_draft(deps.scope)
    operations = previous.operations() if previous else []
    draft = BrokerMoneyDraft(command.kind, command.model_dump(mode="json"))
    selected_id = getattr(command, "draft_operation_id", None)
    if selected_id is not None and any(item.operation_id == str(selected_id) for item in operations):
        operations = [BrokerMoneyDraft(draft.command_kind, draft.command_payload, operation_id=item.operation_id)
                      if item.operation_id == str(selected_id) else item for item in operations]
    elif not any(item.command_kind == draft.command_kind and item.command_payload == draft.command_payload for item in operations):
        if selected_id is not None:
            draft.operation_id = str(selected_id)
        operations.append(draft)
    await deps.store.set_broker_money_draft(deps.scope, BrokerMoneyDraft.from_operations(operations))
    deps.question = question or MissingBrokerBolivars().message


async def forget_broker_money_draft(deps: TurnDeps, command: Command) -> None:
    draft = await deps.store.get_broker_money_draft(deps.scope)
    if draft is None:
        return
    monetary_fields = {"operation_amount_bs", "exchange_rate", "monetary_from_attachment", "attachment_upload_id", "money_evidence", "draft_operation_id"}
    payload = command.model_dump(mode="json")
    identity = {key: value for key, value in payload.items() if key not in monetary_fields}
    operation_id = getattr(command, "draft_operation_id", None)
    if operation_id is not None:
        # A continuation can now add its missing customer, not only money. Its stable
        # identity must remove that same draft even though the payload has changed.
        remaining = [item for item in draft.operations() if item.operation_id != str(operation_id)]
    else:
        remaining = [item for item in draft.operations() if item.command_kind != command.kind or
                     {key: value for key, value in item.command_payload.items() if key not in monetary_fields} != identity]
    if remaining:
        await deps.store.set_broker_money_draft(deps.scope, BrokerMoneyDraft.from_operations(remaining))
    else:
        await deps.store.clear_broker_money_draft(deps.scope)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    func: Callable[..., Awaitable[dict[str, Any]]]
    capability: str
    kind: str  # read | write | ui (interacción: no es una capacidad del negocio, no sale en el menú)
    lanes: frozenset[Lane]
    verticals: frozenset[BusinessVertical]


TOOLS: dict[str, ToolSpec] = {}


def tool(
    capability: str,
    *,
    kind: str,
    lanes: frozenset[Lane] = AGENT_ONLY,
    verticals: frozenset[BusinessVertical] = frozenset({BusinessVertical.RETAIL}),
):
    def decorator(func):
        TOOLS[func.__name__] = ToolSpec(func.__name__, func, capability, kind, lanes, verticals)
        return func

    return decorator


def _slim_node(node: Any) -> Any:
    """Quita `title` sin alterar los tipos aceptados por el schema.

    Conserva ``anyOf: [X, null]`` y ``default: null``. Omitir una propiedad y permitir JSON ``null`` son opciones
    formalmente distintas; al borrar la segunda, DeepSeek llegó a emitir la cadena ``"null"`` para un campo string.
    """
    if isinstance(node, list):
        return [_slim_node(v) for v in node]
    if not isinstance(node, dict):
        return node
    return {k: _slim_node(v) for k, v in node.items() if k != "title"}


def slim_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Adelgaza el schema de una tool sin cambiar lo que acepta.

    Quita lo que solo pesa: `title` (metadato que el modelo no usa) y la `description` de cada `$defs` (son
    docstrings escritos para desarrolladores —alguno hasta cita un archivo del repo— y no instrucciones para el
    modelo). Conserva nulabilidad y defaults porque sí cambian cómo el proveedor construye argumentos.

    Vale la pena porque el schema viaja SIN transformar al proveedor (`_function_declaration_from_tool` de
    pydantic_ai lo pasa tal cual), se manda ENTERO en cada llamada y un turno del carril agente hace dos.
    """
    out = _slim_node(schema)
    for definition in out.get("$defs", {}).values():
        if isinstance(definition, dict):
            definition.pop("description", None)
    return out


async def _prepare(ctx: RunContext[TurnDeps], tool_def: ToolDefinition) -> ToolDefinition | None:
    spec = TOOLS.get(tool_def.name)
    if tool_def.name == "cancel_balance_reconciliation" and ctx.deps.reconciliation is None:
        return None
    if ctx.deps.reconciliation is not None and spec and spec.kind == "write" and tool_def.name not in RECONCILIATION_TOOLS:
        return None
    if (spec is None or ctx.deps.lane not in spec.lanes or ctx.deps.business.vertical not in spec.verticals
            or (ctx.deps.read_only and spec.kind != "read")):
        return None
    return replace(tool_def, parameters_json_schema=slim_schema(tool_def.parameters_json_schema))


def _load_tools() -> None:
    from app.agent.tools import (  # noqa: F401  (registran las tools)
        brokerage,
        interaction,
        reads,
        reconciliation,
        writes,
    )


def build_toolset() -> FunctionToolset[TurnDeps]:
    _load_tools()

    return FunctionToolset([Tool(spec.func, takes_ctx=True, name=spec.name, prepare=_prepare, docstring_format="google",
                                sequential=spec.kind != "read")
                            for spec in TOOLS.values()])


def capabilities_text(vertical: BusinessVertical = BusinessVertical.RETAIL) -> str:
    _load_tools()
    return "\n".join(f"- {s.capability}" for s in TOOLS.values() if vertical in s.verticals and s.kind != "ui")


# ---------------------------------------------------------------------------------------------- ejecución


def _exec_ctx(deps: TurnDeps, *, target_is_quoted: bool = False) -> ExecContext:
    return ExecContext(business_id=deps.business_id, actor=Actor(ActorType.AGENT, deps.identity.member_id),
                       timezone=deps.business.timezone, now=deps.now, turn_id=deps.turn_id, source_message_id=deps.inbound_id,
                       target_is_quoted=target_is_quoted, require_money_evidence=True, require_purchase_settlement_choice=True,
                       reconciliation_operation_id=UUID(deps.reconciliation.operation_id) if deps.reconciliation else None)


def _fingerprint(name: str, args: dict[str, Any]) -> str:
    return hashlib.sha256(f"{name}:{json.dumps(json_safe(args), sort_keys=True)}".encode()).hexdigest()


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    """Quita del resultado lo que no aporta al modelo (tasas detalladas de cada pata) para ahorrar tokens."""
    # Reconciliation freezes these identifiers for the server-side approval. The
    # model receives names and ephemeral handles, never the resolved account IDs.
    return json_safe({key: value for key, value in data.items()
                      if key not in {"account_id", "portfolio_id", "account_version"}})


def _history_result(result: Any, receipt: str | None) -> str:
    """Lo que verá el modelo como resultado de esta tool en los turnos siguientes.

    El recibo determinista cuando lo hay: es la verdad de lo que el sistema le mostró al dueño, y en el
    historial tiene que colgar de SU tool call, no del texto del asistente (ver `history_from_window`).
    """
    if receipt:
        return receipt[:HISTORY_RESULT_MAX]
    if isinstance(result, dict):
        slim = {k: v for k, v in result.items() if k in ("ok", "error", "status", "message", "question")}
        return json.dumps(json_safe(slim or result), ensure_ascii=False)[:HISTORY_RESULT_MAX]
    return str(result)[:HISTORY_RESULT_MAX]


async def _record(deps: TurnDeps, ctx: RunContext[TurnDeps], name: str, args: dict[str, Any], injected: dict[str, Any],
                  result: Any, outcome: ToolOutcome, started: float, receipt: str | None = None) -> None:
    deps.calls.append({"tool": name, "args": json_safe(args), "result": _history_result(result, receipt)})
    try:
        await deps.turns.record_tool_call(deps.turn_id, seq=deps.next_seq(), tool_call_id=ctx.tool_call_id, tool_name=name,
                                          args=args, injected_args=injected, result=result, outcome=outcome.value,
                                          latency_ms=int((time.monotonic() - started) * 1000))
    except Exception:  # noqa: BLE001 — la observabilidad nunca debe tumbar un turno
        log.exception("tool_call.record_failed", tool=name)


async def execute_command(
        ctx: RunContext[TurnDeps],
        name: str,
        args: dict[str, Any],
        build: Callable[[], Command],
        target_handle: Handle | None = None,
) -> dict[str, Any]:
    deps = ctx.deps
    if deps.read_only:
        return {
            "ok": False,
            "error": "read_only",
            "message": "Solo revisa la respuesta; no registres ni cambies movimientos."
        }
    started = time.monotonic()
    injected = {
        "business_id": str(deps.business_id),
        "member_id": str(deps.identity.member_id),
        "turn_id": str(deps.turn_id),
        "target_is_quoted": bool(target_handle and target_handle.quoted),
    }
    if target_handle:
        injected.update({"entity_type": target_handle.entity_type.value, "entity_id": str(target_handle.entity_id),
                         "expected_version": target_handle.version})
    fp = _fingerprint(name, args)
    if fp in deps.call_fingerprints:
        out = {"ok": False, "error": "duplicate_call",
               "message": "Ya ejecutaste exactamente esta acción en este turno. No la repitas; responde al dueño."}
        await _record(deps, ctx, name, args, injected, out, ToolOutcome.REJECTED, started)
        return out
    command: Command | None = None
    try:
        command = build()
        if deps.reconciliation is not None and name not in RECONCILIATION_TOOLS:
            raise DomainError("Primero completa o cancela la reconciliación; no se registró ninguna operación comercial.")
        if isinstance(command, BrokerCommand):
            frozen = {"origin_message_id": command.origin_message_id or deps.inbound_id}
            if command.kind in {"record_broker_account_payment", "record_broker_account_withdrawal"}:
                # Same inbound/action survives a worker retry without a second ledger entry.
                frozen["draft_operation_id"] = uuid5(NAMESPACE_URL, f"broker-direct:{deps.inbound_id}:{fp}")
            if command.kind in {"record_broker_account_payment", "record_broker_account_withdrawal", "record_broker_operation", "record_broker_purchase",
                                 "record_broker_account_purchase", "record_pending_broker_purchase", "record_broker_receivable_payment", "record_broker_customer_payout"}:
                frozen["business_date"] = command.business_date or local_today(deps.business.timezone, deps.now)
            command = command.model_copy(update=frozen)
            money_check = validate_required_money_evidence(command, deps, args, is_draft_continuation=name == "resume_broker_operation")
            command = money_check.prepared_command
            if money_check.needs_money_evidence:
                if command.kind in BROKER_OPERATION_KINDS:
                    # Never keep the rejected money in the draft or reuse it on the next attempt.
                    command = command.model_copy(update={"operation_amount_bs": None, "exchange_rate": None,
                                                         "money_evidence": {}, "monetary_from_attachment": False})
                    raise MissingBrokerBolivars()
                raise DomainError("El monto o la tasa no tienen respaldo en el mensaje o comprobante actual. Indica el dato o envía el capture.")
        result: CommandResult = await deps.bus.dispatch(command, _exec_ctx(deps, target_is_quoted=bool(target_handle and target_handle.quoted)))
    except DomainError as exc:
        if isinstance(exc, MissingBrokerPurchaseCustomer) and command is not None:
            await remember_broker_money_draft(deps, command, question=exc.message)
            out = {**exc.to_dict(), "status": "awaiting_customer",
                   "instruction": "No se guardó nada. Retoma con resume_broker_operation y customer_name cuando indique el cliente."}
            deps.tool_results.append(out)
            await _record(deps, ctx, name, args, injected, out, ToolOutcome.REJECTED, started)
            return out
        if isinstance(exc, MissingBrokerBolivars) and command is not None and command.kind in BROKER_OPERATION_KINDS:
            await remember_broker_money_draft(deps, command)
            out = {**exc.to_dict(), "status": "awaiting_bolivars",
                   "instruction": "No se guardó nada. El sistema pide Bs o capture; retoma con resume_broker_operation cuando llegue el dato."}
            deps.tool_results.append(out)
            await _record(deps, ctx, name, args, injected, out, ToolOutcome.REJECTED, started)
            return out
        if isinstance(exc, NeedsChoice):
            if command is not None and command.kind in BROKER_OPERATION_KINDS:
                await forget_broker_money_draft(deps, command)
            deps.choice = choice_from_error(exc, target_is_quoted=bool(target_handle and target_handle.quoted))
            out = {"ok": False, "status": "awaiting_choice", "question": exc.message, "instruction": AWAITING_CHOICE_INSTRUCTION}
            deps.tool_results.append(out)
            await _record(deps, ctx, name, args, injected, out, ToolOutcome.NEEDS_CONFIRMATION, started)
            return out
        if (choice := entity_choice_from_error(
            exc, command, target_is_quoted=bool(target_handle and target_handle.quoted)
        )) is not None:
            if command is not None and command.kind in BROKER_OPERATION_KINDS:
                await forget_broker_money_draft(deps, command)
            deps.choice = choice
            out = {"ok": False, "status": "awaiting_choice", "candidates": exc.details.get("candidates"),
                   "instruction": AWAITING_CHOICE_INSTRUCTION}
            deps.tool_results.append(out)
            await _record(deps, ctx, name, args, injected, out, ToolOutcome.NEEDS_CONFIRMATION, started)
            return out
        out = exc.to_dict()
        await _record(deps, ctx, name, args, injected, out, ToolOutcome.REJECTED, started)
        deps.tool_results.append(out)
        return out
    except ValueError as exc:  # validación de Pydantic al construir el comando
        out = {"ok": False, "error": "invalid", "message": str(exc)}
        await _record(deps, ctx, name, args, injected, out, ToolOutcome.REJECTED, started)
        return out
    except Exception as exc:  # noqa: BLE001
        log.exception("tool.internal_error", tool=name)
        deps.internal_errors.append(f"{name}: {type(exc).__name__}: {exc}")
        deps.internal_exceptions.append((exc, name))
        out = {"ok": False, "error": "internal",
               "message": "Falló el sistema al ejecutar esto. NO se guardó nada. Díselo al dueño y pídele que lo intente de nuevo."}
        await _record(deps, ctx, name, args, injected, {**out, "exception": repr(exc)}, ToolOutcome.ERROR, started)
        return out

    if command.kind in BROKER_OPERATION_KINDS:
        await forget_broker_money_draft(deps, command)
    deps.call_fingerprints.add(fp)
    if result.needs_confirmation:
        preview = receipts.render_preview(result)
        deps.pending = PendingAction(command_kind=command.kind, command_payload=result.command_payload or {},
                                     reason=result.confirmation_reason or "Necesito tu confirmación.", preview_text=preview,
                                     target_is_quoted=bool(target_handle and target_handle.quoted))
        await deps.store.set_pending(deps.scope, deps.pending)
        out = {"ok": False, "status": "needs_confirmation", "reason": result.confirmation_reason, "preview": _compact(result.data),
               "instruction": ("NO se guardó todavía. El sistema ya le mostró al dueño el detalle y le preguntó si confirma "
                               "(sí/no). No repitas el detalle ni digas que quedó registrado.")}
        deps.tool_results.append(out)
        await _record(deps, ctx, name, args, injected, out, ToolOutcome.NEEDS_CONFIRMATION, started)
        return out

    await finish_reconciliation(deps, result)
    deps.receipts.append(receipts.render(result))
    # Un registro nuevo lo cuenta entero el recibo; una corrección o un borrado puede necesitar que el modelo
    # añada algo. De esa distinción depende que el turno se pueda cerrar sin una segunda llamada al modelo.
    if result.affected and command.kind not in DELETING_KINDS and command.kind not in {"reconcile_account_balance", "delete_balance_adjustment"}:
        deps.linkable.append(result.affected[0])
    if command.kind in UNDOABLE_KINDS or command.kind in {"complete_broker_receipt", "attach_broker_attachment", "reconcile_account_balance", "delete_balance_adjustment"}:
        deps.simple_writes += 1
        if result.affected and command.kind in UNDOABLE_KINDS:
            deps.undoable.append(result.affected[0])
    else:
        deps.other_writes += 1
    handles = []
    for ref in result.affected:
        deps.changed_refs.append(ref)
        if ref.summary or ref.relation == LinkRelation.UPDATED:
            h = await deps.store.register_handle(deps.scope, ref.entity_type, ref.entity_id, ref.version,
                                                 ref.summary or (target_handle.summary if target_handle else ""),
                                                 quoted=bool(target_handle and target_handle.quoted and target_handle.entity_id == ref.entity_id))
            handles.append(h.line())
    out = {"ok": True, **_compact(result.data), "handles": handles,
           "note": "El sistema ya le envió al dueño el recibo con el detalle. No lo repitas."}
    deps.tool_results.append(out)
    await _record(deps, ctx, name, args, injected, out, ToolOutcome.OK, started, receipt=deps.receipts[-1])
    return out


def choice_from_error(exc: NeedsChoice, *, target_is_quoted: bool = False) -> PendingChoice:
    """Pregunta de un comando (`NeedsChoice`) → elección pendiente: cada opción ejecuta su comando sin LLM o vuelve al agente."""
    return PendingChoice(question=exc.message, options=[ChoiceOption(**option) for option in exc.options],
                         target_is_quoted=target_is_quoted)


def entity_choice_from_error(
    exc: DomainError, command: Command | None, *, target_is_quoted: bool = False,
) -> PendingChoice | None:
    """Ambigüedad de cliente o producto → comando congelado por opción, sin reinterpretación del LLM."""
    if not isinstance(exc, Ambiguous) or command is None:
        return None
    if exc.details.get("entity") == "producto":
        return _product_choice(exc, command, target_is_quoted=target_is_quoted)
    if exc.details.get("entity") != "cliente":
        return None
    payload = command.model_dump(mode="json")
    candidates = [c for c in exc.details.get("candidates") or [] if c][:9]
    candidate_ids = [
        c for c in exc.details.get("candidate_ids")
        or [] if c
    ][:len(candidates)]
    descriptions = list(exc.details.get("candidate_descriptions") or [])[:len(candidates)]
    if "customer_name" not in payload or len(candidates) < 1:
        return None
    options = [
        ChoiceOption(
            title=c,
            description=descriptions[index] if index < len(descriptions) else None,
            action="command",
            command_kind=command.kind,
            command_payload={**payload, "customer_id": UUID(candidate_ids[index])} if index < len(candidate_ids)
            else {**payload, "customer_name": c},
        )
        for index, c in enumerate(candidates)
    ]
    if "force_new_customer" in payload:
        options.append(ChoiceOption(title="Es un cliente nuevo", action="command", command_kind=command.kind, confirmed=True,
                                    command_payload={**payload, "force_new_customer": True}))
    name = exc.details.get("name") or payload["customer_name"]
    return PendingChoice(question=f"¿Quién es «{name}»? Tengo varios clientes parecidos.", options=options,
                         target_is_quoted=target_is_quoted)


def _product_payload(payload: dict[str, Any], original: str, selected: str, *, force_new: bool) -> dict[str, Any] | None:
    """Cambia solo el producto ambiguo y conserva intactos precio, cantidad, pago y fecha del comando original."""
    updated = dict(payload)
    if isinstance(payload.get("items"), list):
        items = [dict(item) for item in payload["items"]]
        positions = [i for i, item in enumerate(items) if normalize_name(str(item.get("product", ""))) == normalize_name(original)]
        if len(positions) != 1:
            return None  # no adivinar qué renglón cambiar si el comando es inconsistente
        item = items[positions[0]]
        item["product"] = selected
        item["force_new_product"] = force_new
        updated["items"] = items
        return updated
    if normalize_name(str(payload.get("product_name", ""))) == normalize_name(original):
        updated["product_name"] = selected
        updated["force_new_product"] = force_new
        return updated
    return None


def _product_choice(exc: Ambiguous, command: Command, *, target_is_quoted: bool = False) -> PendingChoice | None:
    """Cada botón conserva el comando original y distingue candidato existente de producto nuevo.

    Esta asociación evita que un toque tardío en «Es un producto nuevo» termine ejecutando otra sugerencia:
    el nombre, precio y cantidad originales viajan dentro de la opción, no en el historial del modelo.
    """
    payload = command.model_dump(mode="json")
    original = str(exc.details.get("name") or "").strip()
    candidates = [str(candidate) for candidate in exc.details.get("candidates") or [] if candidate][:9]
    if not original or not candidates:
        return None
    options: list[ChoiceOption] = []
    for candidate in candidates:
        candidate_payload = _product_payload(payload, original, candidate, force_new=False)
        if candidate_payload is None:
            return None
        options.append(ChoiceOption(title=candidate, action="command", command_kind=command.kind,
                                    command_payload=candidate_payload))
    new_payload = _product_payload(payload, original, original, force_new=True)
    if new_payload is None:
        return None
    options.append(ChoiceOption(title="Es un producto nuevo", action="command", command_kind=command.kind,
                                command_payload=new_payload))
    return PendingChoice(question=f"No estoy seguro de cuál es «{original}». ¿Cuál de estos es?", options=options,
                         target_is_quoted=target_is_quoted)


async def execute_query(ctx: RunContext[TurnDeps], name: str, args: dict[str, Any],
                        run: Callable[[], Awaitable[QueryResult]]) -> dict[str, Any]:
    deps = ctx.deps
    started = time.monotonic()
    injected = {"business_id": str(deps.business_id)}
    try:
        res = await run()
    except DomainError as exc:
        out = exc.to_dict()
        deps.tool_results.append(out)
        await _record(deps, ctx, name, args, injected, out, ToolOutcome.REJECTED, started)
        return out
    except Exception as exc:  # noqa: BLE001
        log.exception("tool.internal_error", tool=name)
        deps.internal_errors.append(f"{name}: {type(exc).__name__}: {exc}")
        deps.internal_exceptions.append((exc, name))
        out = {"ok": False, "error": "internal", "message": "No pude consultar eso por un error del sistema. Díselo al dueño."}
        await _record(deps, ctx, name, args, injected, {**out, "exception": repr(exc)}, ToolOutcome.ERROR, started)
        return out
    out: dict[str, Any] = {"ok": True, **res.data}
    if res.refs:
        lines = []
        for ref in res.refs:
            h = await deps.store.register_handle(deps.scope, ref.entity_type, ref.entity_id, ref.version, ref.summary or "")
            lines.append(h.line())
            deps.shown_refs.append(ref)
        out["records"] = lines
    deps.tool_results.append(out)
    await _record(deps, ctx, name, args, injected, out, ToolOutcome.OK, started)
    return out


async def resolve_handle(ctx: RunContext[TurnDeps], handle: str) -> Handle | dict[str, Any]:
    h = await ctx.deps.store.get_handle(ctx.deps.scope, handle)
    if h is None:
        valid = [x.line() for x in await ctx.deps.store.list_handles(ctx.deps.scope)]
        return {"ok": False, "error": "unknown_handle",
                "message": f"'{handle}' no es una referencia válida. Usa find_records para buscar el registro; nunca inventes referencias.",
                "valid_handles": valid[-15:]}
    return h
