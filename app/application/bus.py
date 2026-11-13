"""CommandBus: el ÚNICO camino de escritura del sistema.

Lo usan igual el agente (vía tools), el panel web (vía REST) y la confirmación de acciones pendientes.

Flujo de `dispatch`:
  1. abre una transacción
  2. ejecuta el handler (validación de dominio + mutación + auditoría)
  3. consulta la política de riesgo con el resultado REAL ya calculado
  4. si hace falta confirmación y el actor es el agente → ROLLBACK y devuelve un preview exacto
     (lo que se mostrará es literalmente lo que se ejecutaría) + el comando serializado para congelarlo.
     Excepción: `guard_reason` (lo pone el handler, p. ej. una tasa sospechosa) pide confirmación a CUALQUIER actor,
     también al panel, que reenvía con confirmed=True si el dueño dice que sí.
  5. si no → COMMIT
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal
from typing import Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from app.application.context import EntityRef, ExecContext
from app.application.rates import RateService
from app.db.models import BrokerDraftExecution, Business
from app.domain.countries import country_profile, use_display_profile
from app.domain.enums import ActorType, EntityType, LinkRelation
from app.domain.errors import Conflict, DomainError, Stale, ValidationFailed
from app.logging import get_logger

log = get_logger(__name__)

_LOCK_NOT_AVAILABLE = "55P03"  # PostgreSQL: lock_not_available (venció lock_timeout)


class Command(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    kind: ClassVar[str]


@dataclass(slots=True)
class CommandResult:
    kind: str
    data: dict[str, Any]
    affected: list[EntityRef] = field(default_factory=list)
    status: Literal["ok", "needs_confirmation"] = "ok"
    confirmation_reason: str | None = None
    command_payload: dict[str, Any] | None = None
    # Motivo para pedir confirmación sin importar el actor (agente o panel). Lo decide el handler.
    guard_reason: str | None = None

    @property
    def needs_confirmation(self) -> bool:
        return self.status == "needs_confirmation"


@dataclass(frozen=True, slots=True)
class CommandDeps:
    rates: RateService


Handler = Callable[[Any, ExecContext, AsyncSession, CommandDeps], Awaitable[CommandResult]]
# Devuelve el motivo si hace falta confirmación, o None.
RiskPolicy = Callable[[Any, ExecContext, CommandResult], str | None]


@dataclass(frozen=True, slots=True)
class _Registration:
    command_type: type[Command]
    handler: Handler
    risk: RiskPolicy | None


_REGISTRY: dict[str, _Registration] = {}


def command_handler(command_type: type[Command], risk: RiskPolicy | None = None) -> Callable[[Handler], Handler]:
    def decorator(fn: Handler) -> Handler:
        if command_type.kind in _REGISTRY:
            raise RuntimeError(f"Comando duplicado: {command_type.kind}")
        _REGISTRY[command_type.kind] = _Registration(command_type, fn, risk)
        return fn

    return decorator


def always_confirm(reason: str) -> RiskPolicy:
    return lambda _cmd, _ctx, _res: reason


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


def load_command(kind: str, payload: dict[str, Any]) -> Command:
    reg = _REGISTRY.get(kind)
    if reg is None:
        raise ValidationFailed(f"Comando desconocido: {kind}")
    return reg.command_type.model_validate(payload)


class CommandBus:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], deps: CommandDeps) -> None:
        self._factory = session_factory
        self._deps = deps

    async def dispatch(self, command: Command, ctx: ExecContext, *, confirmed: bool = False) -> CommandResult:
        reg = _REGISTRY.get(command.kind)
        if reg is None:
            raise ValidationFailed(f"Comando sin handler: {command.kind}")
        async with self._factory() as session:
            tx = await session.begin()
            try:
                business = await session.get(Business, ctx.business_id)
                if business is not None:
                    use_display_profile(country_profile(business.country_code))
                if command.kind in {"reconcile_account_balance", "delete_balance_adjustment"}:
                    from app.application.reconciliation import authorize_reconciliation

                    await authorize_reconciliation(session, ctx, command.draft_operation_id)
                operation_id = getattr(command, "draft_operation_id", None)
                execution_command_kind = getattr(command, "draft_origin_command_kind", None) or command.kind
                if operation_id is not None:
                    # Serialize even the first execution, when no result row exists yet.
                    lock_key = int.from_bytes(hashlib.sha256(f"{ctx.business_id}:{operation_id}".encode()).digest()[:8], signed=True)
                    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})
                    saved = await session.get(BrokerDraftExecution, (ctx.business_id, operation_id))
                    if saved is not None:
                        if saved.actor_id != ctx.actor.member_id or saved.command_kind != execution_command_kind:
                            raise ValidationFailed("La operación pendiente pertenece a otro contexto.")
                        if command.kind in {"record_broker_account_payment", "record_broker_account_withdrawal"}:
                            from app.application.access import (
                                effective_brokerage_account,
                                resolve_brokerage_account,
                            )
                            from app.db.models import BrokerMovement
                            from app.domain.brokerage import q_money

                            movement = await session.get(BrokerMovement, UUID(saved.result["affected"][0]["entity_id"]))
                            if movement is None or movement.business_id != ctx.business_id:
                                raise ValidationFailed("El movimiento original no está disponible.")
                            if command.broker_id is not None:
                                _, account = await effective_brokerage_account(session, ctx, command.broker_id, allow_house=False)
                            else:
                                _, account = await resolve_brokerage_account(session, ctx, command.broker_name, allow_house=False)
                            original_amount = saved.result["data"]["movement"].get("amount")
                            if (account.broker_member_id != movement.broker_member_id or
                                    original_amount is not None and q_money(command.amount) != q_money(Decimal(original_amount))):
                                raise Conflict("Esa solicitud ya registró otro importe o cuenta. Revisa el movimiento original antes de volver a registrar.")
                        if command.kind in {"reconcile_account_balance", "delete_balance_adjustment"}:
                            from app.application.reconciliation import authorize_customer
                            from app.db.models import CustomerBalanceAdjustment

                            _, current_actor = await authorize_reconciliation(session, ctx, operation_id)
                            for reference in saved.result["affected"]:
                                if reference["entity_type"] == EntityType.CUSTOMER_BALANCE_ADJUSTMENT.value:
                                    adjustment = await session.get(CustomerBalanceAdjustment, UUID(reference["entity_id"]))
                                    if adjustment is not None:
                                        authorize_customer(adjustment.customer, ctx, current_actor)
                        payload = saved.result
                        refs = [EntityRef(entity_type=EntityType(ref["entity_type"]), entity_id=UUID(ref["entity_id"]),
                                          relation=LinkRelation(ref["relation"]), version=ref["version"], summary=ref["summary"])
                                for ref in payload["affected"]]
                        await tx.rollback()
                        return CommandResult(kind=payload["kind"], data=payload["data"], affected=refs)
                if ctx.require_money_evidence and hasattr(command, "money_evidence"):
                    for field_name in ("operation_amount_bs", "exchange_rate"):
                        value = getattr(command, field_name, None)
                        if field_name in command.model_fields_set and value is not None:
                            evidence = getattr(command, "money_evidence", {}).get(field_name, {})
                            if evidence.get("value") != str(value) or not evidence.get("message_id"):
                                raise ValidationFailed("Bs o tasa sin evidencia verificada; indica el dato o envía el capture.")
                result = await reg.handler(command, replace(ctx, command_confirmed=confirmed), session, self._deps)
                # Read-only preparation may request confirmation, including a refreshed
                # snapshot after a stale approval. Never flush or persist that preview.
                if result.needs_confirmation:
                    await tx.rollback()
                    return result
                await session.flush()
                if session.info.get("created_customers"):
                    result.data["created_customers"] = session.info["created_customers"]
                reason = None
                if not confirmed:
                    reason = result.guard_reason
                    if not reason and reg.risk and ctx.actor.type == ActorType.AGENT:
                        reason = reg.risk(command, ctx, result)
                if reason:
                    await tx.rollback()
                    result.status = "needs_confirmation"
                    result.confirmation_reason = reason
                    result.command_payload = result.command_payload or command.model_dump(mode="json")
                    log.info("command.needs_confirmation", kind=command.kind, reason=reason)
                    return result
                if operation_id is not None and not result.data.get("no_change"):
                    from app.application.audit import json_safe

                    session.add(BrokerDraftExecution(business_id=ctx.business_id, operation_id=operation_id,
                                actor_id=ctx.actor.member_id, command_kind=execution_command_kind,
                                result=json_safe({"kind": result.kind, "data": result.data,
                                                  "affected": [asdict(ref) for ref in result.affected]})))
                await tx.commit()
                log.info("command.ok", kind=command.kind, actor=ctx.actor.type.value, affected=len(result.affected))
                return result
            except StaleDataError as exc:
                # Bloqueo optimista a nivel de base de datos: otra transacción cambió el registro entre que lo leímos y
                # que lo escribimos (el `expected_version` solo cubre lo que pasó ANTES de leerlo).
                if tx.is_active:
                    await tx.rollback()
                log.warning("command.stale_write", kind=command.kind, actor=ctx.actor.type.value, business_id=str(ctx.business_id),
                            reason=str(exc))
                raise Stale("Este registro cambió mientras lo editabas (quizás desde el panel o WhatsApp). "
                            "Vuelve a buscarlo antes de modificarlo.") from exc
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                    if tx.is_active:
                        await tx.rollback()
                    log.error("command.failed", kind=command.kind, actor=ctx.actor.type.value, business_id=str(ctx.business_id),
                              error=type(exc).__name__, reason=str(exc))
                    raise
                # Otra operación tuvo el registro (p. ej. el mismo cliente) más de `lock_timeout` segundos: se avisa, no se cuelga.
                if tx.is_active:
                    await tx.rollback()
                log.warning("command.lock_timeout", kind=command.kind, actor=ctx.actor.type.value, business_id=str(ctx.business_id))
                raise Conflict("Estoy procesando otro movimiento de este cliente. Intenta de nuevo en unos segundos.") from exc
            except DomainError as exc:
                # Rechazos esperados (ambiguo, versión vieja, validación…): sin traceback, pero con el porqué completo.
                if tx.is_active:
                    await tx.rollback()
                log.warning("command.rejected", kind=command.kind, actor=ctx.actor.type.value, business_id=str(ctx.business_id),
                            error=exc.code, reason=exc.message, details=exc.details, payload=command.model_dump(mode="json"))
                raise
            except Exception as exc:
                if tx.is_active:
                    await tx.rollback()
                log.error("command.failed", kind=command.kind, actor=ctx.actor.type.value, business_id=str(ctx.business_id),
                          error=type(exc).__name__, reason=str(exc))
                raise
            except BaseException:
                if tx.is_active:
                    await tx.rollback()
                raise


# Importar los módulos de comandos registra sus handlers.
def load_all_commands() -> None:
    from app.application.commands import (  # noqa: F401
        brokerage,
        business,
        cash,
        catalog,
        customers,
        exchanges,
        expenses,
        notes,
        purchases,
        rates,
        receivables,
        reconciliation,
        records,
        sales,
        system_businesses,
    )
