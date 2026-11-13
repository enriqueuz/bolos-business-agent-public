"""Thin adapters for the isolated historical balance flow."""
import time
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic_ai import RunContext

from app.agent.deps import TurnDeps
from app.agent.reconciliation import ensure_reconciliation
from app.agent.tools.registry import FAST_AND_AGENT, _record, execute_command, resolve_handle, tool
from app.application.commands.reconciliation import DeleteBalanceAdjustment, ReconcileAccountBalance
from app.application.reconciliation import historical_date
from app.domain.brokerage import parse_broker_decimal, q_money
from app.domain.dates import local_today
from app.domain.enums import BusinessVertical, EntityType, ToolOutcome
from app.domain.errors import ValidationFailed

BROKERAGE = frozenset({BusinessVertical.BROKERAGE})


async def _question(ctx, message):
    ctx.deps.question = message
    result = {"ok": False, "status": "asked", "question": message}
    ctx.deps.tool_results.append(result)
    await _record(ctx.deps, ctx, "reconcile_balance", {}, {}, result, ToolOutcome.REJECTED, time.monotonic())
    return result


@tool("Ajustar el saldo acumulado histórico de una cuenta", kind="write", lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def reconcile_balance(ctx: RunContext[TurnDeps], holder_name: str | None = None,
        account_type: Literal["auto", "broker", "customer"] | None = None,
        historical_day: int | None = None, historical_month: int | None = None, historical_year: int | None = None,
        amount: str | None = None, direction: Literal["debt", "credit"] | None = None, new_request: bool = False) -> dict:
    """Prepara el saldo OBJETIVO al corte, nunca un delta calculado por el modelo.

    Úsala cuando el usuario pide ajustar saldo (ajuste saldo, ajusta el saldo, quiero ajustar el saldo).
    Requiere expresar la acción de ajustar saldo. «Alexandra debía 900 al 31/10» NO pide ajustar:
    usa need_more_info para aclarar la intención. Fecha+monto+deuda solos no autorizan esta propuesta.
    Una consulta o explicación del comando tampoco solicita un ajuste.
    Extrae el nombre y fecha del usuario.
    Usa account_type=auto salvo que el usuario distinga explícitamente Cliente/Corredor.
    deuda, debía y signo negativo: direction=debt. favor/a favor y signo positivo: credit.
    Sin dirección expresa, omítela para preguntar. Cero explícito no necesita dirección.
    No inventes fecha ni año: historical_year se omite si no fue expresado en el flujo.
    Si inicia otra solicitud de ajuste, usa new_request=true para descartar datos del borrador anterior.
    En continuaciones usa new_request=false y aporta solo los datos nuevos; el backend conserva los anteriores.
    El backend calcula y muestra la confirmación. Nunca registres compras, ventas o abonos para ajustar.
    """
    deps = ctx.deps
    await ensure_reconciliation(deps, new_request=new_request)
    provided = {"holder_name": holder_name, "account_type": account_type, "historical_day": historical_day,
                "historical_month": historical_month, "historical_year": historical_year, "amount": amount, "direction": direction}
    deps.reconciliation.fields.update({k: v for k, v in provided.items() if v is not None})
    await deps.store.set_reconciliation(deps.scope, deps.reconciliation)
    fields = deps.reconciliation.fields
    if not fields.get("holder_name"):
        return await _question(ctx, "¿De quién es la cuenta que deseas reconciliar?")
    if not fields.get("historical_day") or not fields.get("historical_month"):
        return await _question(ctx, "¿A qué fecha corresponde el saldo histórico?")
    if fields.get("amount") is None:
        return await _question(ctx, "¿Cuál era el saldo a esa fecha?")
    try:
        cutoff = historical_date(fields["historical_day"], fields["historical_month"], fields.get("historical_year"),
                                 local_today(deps.business.timezone, deps.now))
        value = parse_broker_decimal(fields["amount"])
        sign = fields["amount"].strip()[:1]
        inferred = "debt" if sign == "-" else "credit" if sign == "+" else None
        chosen_direction = fields.get("direction") or inferred
        if inferred and fields.get("direction") and inferred != fields["direction"]:
            raise ValidationFailed("El signo y la dirección se contradicen. ¿Era deuda o saldo a favor?")
        if value != 0 and chosen_direction is None:
            return await _question(ctx, "¿Ese saldo era deuda con la casa o dinero a favor de la persona?")
        declared = q_money(abs(value) * (Decimal("-1") if chosen_direction == "debt" else Decimal("1")))
    except (ValidationFailed, ValueError) as exc:
        return await _question(ctx, exc.message if isinstance(exc, ValidationFailed) else str(exc))
    return await execute_command(ctx, "reconcile_balance", provided, lambda: ReconcileAccountBalance(
        holder_name=fields["holder_name"], account_type=fields.get("account_type", "auto"),
        business_date=cutoff, declared_balance=declared, draft_operation_id=UUID(deps.reconciliation.operation_id),
        origin_message_id=UUID(deps.reconciliation.origin_message_id)))


@tool("Eliminar un ajuste histórico por referencia", kind="write", lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def delete_balance_adjustment(ctx: RunContext[TurnDeps], handle: str) -> dict:
    """Elimina únicamente un ajuste de saldo. Requiere petición explícita de eliminar ese ajuste y confirmación.
    Solo dueño o autor; personales ajenos permanecen prohibidos a corredores.
    """
    await ensure_reconciliation(ctx.deps)
    target = await resolve_handle(ctx, handle)
    if isinstance(target, dict):
        return target
    if target.entity_type not in {EntityType.BROKER_MOVEMENT, EntityType.CUSTOMER_BALANCE_ADJUSTMENT}:
        return await _question(ctx, "Esa referencia no corresponde a un ajuste de saldo.")
    draft = ctx.deps.reconciliation
    return await execute_command(ctx, "delete_balance_adjustment", {"handle": handle}, lambda: DeleteBalanceAdjustment(
        entity_type=target.entity_type.value, entity_id=target.entity_id, expected_version=target.version,
        draft_operation_id=UUID(draft.operation_id), origin_message_id=UUID(draft.origin_message_id)), target_handle=target)


@tool("Cancelar la reconciliación pendiente", kind="ui", lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def cancel_balance_reconciliation(ctx: RunContext[TurnDeps]) -> dict:
    """Cancela si el usuario abandona el ajuste o cambia de intención. No registra operaciones."""
    await ctx.deps.store.clear_reconciliation(ctx.deps.scope)
    await ctx.deps.store.pop_pending(ctx.deps.scope)
    await ctx.deps.store.pop_choice(ctx.deps.scope)
    ctx.deps.reconciliation = None
    ctx.deps.notices.append("Reconciliación cancelada. No se registró ningún ajuste.")
    result = {"ok": True, "status": "cancelled"}
    await _record(ctx.deps, ctx, "cancel_balance_reconciliation", {}, {}, result, ToolOutcome.OK, time.monotonic())
    return result
