"""Herramientas del agente para la vertical de corredores."""

import time
from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field
from pydantic_ai import RunContext

from app.agent.deps import TurnDeps
from app.agent.money_evidence import validate_required_money_evidence
from app.agent.receipts import render_broker_balance_summary
from app.agent.tool_inputs import NullableToolString
from app.agent.tools.registry import (
    FAST_AND_AGENT,
    _record,
    execute_command,
    execute_query,
    resolve_handle,
    tool,
)
from app.application.bus import load_command
from app.application.commands.brokerage import (
    CompleteBrokerReceipt,
    DeleteBrokerMovement,
    EditBrokerMovement,
    RecordBrokerAccountPayment,
    RecordBrokerAccountPurchase,
    RecordBrokerAccountWithdrawal,
    RecordBrokerCustomerPayout,
    RecordBrokerOperation,
    RecordBrokerPurchase,
    RecordBrokerReceivablePayment,
    RecordPendingBrokerPurchase,
)
from app.application.context import EntityRef
from app.application.conversation import BrokerMoneyDraft
from app.application.queries.service import QueryResult
from app.channels.types import OutboundImage
from app.domain.brokerage import parse_broker_decimal
from app.domain.dates import PeriodName
from app.domain.enums import BusinessVertical, EntityType, MemberRole, ToolOutcome
from app.domain.errors import ValidationFailed

BROKERAGE = frozenset({BusinessVertical.BROKERAGE})
PHYSICAL_SALE_NOTICE = (
    "No registré nada. El modo corredores maneja movimientos, operaciones de divisas y cartera en USD; "
    "no registra productos, inventario ni ventas de artículos físicos."
)


async def _query_account(
    ctx: RunContext[TurnDeps],
    account_scope: Literal["default", "house", "all", "broker"],
    broker_name: str | None,
) -> tuple[str, UUID | None, dict[str, Any] | None]:
    """Convierte nombres visibles en un alcance autorizado sin exponer ids al modelo."""
    if ctx.deps.identity.member_role == MemberRole.BROKER:
        if account_scope == "house" or broker_name:
            return "broker", None, {
                "message": "Solo puedes consultar tu propia cuenta.", "candidates": []
            }
        return "broker", ctx.deps.identity.member_id, None
    if broker_name:
        members = await ctx.deps.brokerage_queries.members(
            ctx.deps.business_id, ctx.deps.identity.member_id
        )
        matches = [row for row in members if broker_name.casefold().strip() in row["name"].casefold()]
        if len(matches) != 1:
            return "broker", None, {
                "message": "No pude identificar un único corredor.",
                "candidates": [row["name"] for row in matches],
            }
        return "broker", UUID(matches[0]["id"]), None
    resolved = "all" if account_scope == "default" else account_scope
    if resolved == "broker":
        return resolved, None, {"message": "Indica el corredor.", "candidates": []}
    return resolved, None, None


def _operation_attachment(ctx: RunContext[TurnDeps], use_previous_receipt: bool) -> UUID | None:
    if not use_previous_receipt:
        return ctx.deps.attachment_upload_id
    if ctx.deps.attachment_upload_id or len(ctx.deps.preceding_receipt_ids) != 1:
        raise ValidationFailed("No hay un único comprobante anterior disponible. Reenvíalo con la descripción de la operación.")
    return ctx.deps.preceding_receipt_ids[0]


class BrokerMovementChanges(BaseModel):
    """Campos seguros que pueden corregirse sin cambiar la naturaleza del movimiento."""

    amount: str | None = None
    received_amount: str | None = None
    operation_amount_bs: str | None = None
    exchange_rate: str | None = None
    customer_classification: Literal["global", "personal"] | None = None
    customer_name: str | None = Field(default=None, max_length=160)
    description: NullableToolString = Field(default=None, max_length=300)
    business_date: date | None = None

    def command_payload(self) -> dict[str, Any]:
        payload = self.model_dump(exclude_unset=True)
        for key in ("amount", "received_amount", "operation_amount_bs", "exchange_rate"):
            if payload.get(key) is not None:
                payload[key] = parse_broker_decimal(payload[key])
        return payload


class MoneyTextSources(BaseModel):
    """Exact, unique quotes from the current user message; one number per quote.

    Declare the semantic field explicitly. Include currency/context to distinguish repeated numbers.
    Do not quote history or model-calculated values. Omit sources that the user did not provide.
    """

    principal_usd: str | None = Field(default=None, description=(
        "Exact current-message quote containing ONLY ONE complete number: the USD principal. "
        "For 'Venta pendiente 100 a 950', use '100', never '100 a 950'. "
        "If the value repeats, include currency or surrounding words to make this occurrence unique."))
    operation_amount_bs: str | None = Field(default=None, description=(
        "Unique literal quote with ONLY the explicitly supplied total Bs number, including currency/context "
        "if needed. Include this source even when the user also supplies an explicit exchange rate."))
    exchange_rate: str | None = Field(default=None, description=(
        "Unique literal quote with ONLY the explicitly supplied rate number. For '1000 dólares a tasa 1000', "
        "use 'tasa 1000', not ambiguous '1000'. Include this source even if total Bs is also supplied."))


class ReceiptExtraction(BaseModel):
    amount_bs: str | None = None
    confidence: Literal["clear", "ambiguous", "unreadable"] = "ambiguous"
    bank_reference: str | None = Field(default=None, max_length=120)
    date: str | None = Field(default=None, max_length=80)
    sender_bank: str | None = Field(default=None, max_length=160)
    receiver_bank: str | None = Field(default=None, max_length=160)
    beneficiary: str | None = Field(default=None, max_length=160)


@tool("Completar Bs, tasa o cliente de una operación sin guardar", kind="write",
      lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def resume_broker_operation(
    ctx: RunContext[TurnDeps], operation_amount_bs: str | None = None,
    exchange_rate: str | None = None, extraction: ReceiptExtraction | None = None,
    operation_number: int | None = None,
    text_money_sources: MoneyTextSources | None = None,
    customer_name: NullableToolString = None,
) -> dict[str, Any]:
    """Retoma exclusivamente la operación que espera bolívares, conservando todos sus otros datos.

    Una cifra sola que responde a la solicitud de Bs es operation_amount_bs, no una nueva compra propia.
    Tasa explícita va en exchange_rate. Para imagen actual, usa extraction con monto transferido y confianza.
    Para una respuesta «Tasa 950», envía exchange_rate="950" y
    text_money_sources={"exchange_rate": "950"} en la MISMA llamada.
    Si la extracción es ambigua o ilegible, no se registra: se vuelve a pedir el dato. No uses esta herramienta
    para completar un movimiento ya guardado ni para una operación nueva e independiente.
    Si hay varias operaciones esperando Bs, pregunta cuál corresponde y pasa su operation_number del contexto.
    También completa una compra cuya modalidad Pendiente ya eligió el usuario y espera cliente:
    pasa customer_name, sin reconstruir montos, modalidad ni comprobante.
    Si el contexto dice awaiting=customer_name y el usuario responde «Jessica», llama esta herramienta
    con customer_name="Jessica". NO uses record_broker_purchase ni copies Bs/tasa anteriores.
    """
    draft = await ctx.deps.store.get_broker_money_draft(ctx.deps.scope)
    args = dict(operation_amount_bs=operation_amount_bs, exchange_rate=exchange_rate,
                extraction=extraction.model_dump() if extraction else None,
                text_money_sources=text_money_sources.model_dump() if text_money_sources else None,
                operation_number=operation_number, customer_name=customer_name)

    if draft is not None and len(draft.operations()) > 1 and operation_number is None:
        probe = load_command(draft.command_kind, {**draft.command_payload,
                             "operation_amount_bs": operation_amount_bs, "exchange_rate": exchange_rate,
                             "attachment_upload_id": ctx.deps.attachment_upload_id})
        money_check = validate_required_money_evidence(probe, ctx.deps, args, is_draft_continuation=True)
        verified_command = money_check.prepared_command
        if not money_check.needs_money_evidence and verified_command.money_evidence:
            draft.pending_completion = {field: getattr(verified_command, field, None) for field in
                                        ("operation_amount_bs", "exchange_rate", "attachment_upload_id", "monetary_from_attachment")}
            draft.pending_completion["money_evidence"] = verified_command.money_evidence
            from app.application.audit import json_safe

            draft.pending_completion = json_safe(draft.pending_completion)
            await ctx.deps.store.set_broker_money_draft(ctx.deps.scope, draft)

    def command():
        if draft is None:
            raise ValidationFailed("No hay una operación esperando bolívares. Indica la compra o venta completa.")
        operations = draft.operations()
        if len(operations) > 1 and operation_number is None:
            raise ValidationFailed("Hay varias operaciones sin guardar. Pregunta a cuál corresponden estos bolívares o capture.")
        index = (operation_number or 1) - 1
        if operation_number is not None and not 1 <= operation_number <= len(operations):
            raise ValidationFailed("Selecciona una de las operaciones sin guardar del contexto actual.")
        selected = operations[index]
        if customer_name is not None:
            if not (selected.command_payload.get("purchase_settlement_confirmed")
                    and selected.command_payload.get("settlement") == "pending"
                    and not (selected.command_payload.get("customer_name") or selected.command_payload.get("customer_id"))):
                raise ValidationFailed("Esta operación no está esperando el nombre de un cliente.")
            if operation_amount_bs is not None or exchange_rate is not None or extraction is not None:
                raise ValidationFailed("Indica solo el cliente; esta compra ya conserva sus datos monetarios.")
            return load_command(selected.command_kind, {**selected.command_payload, "customer_name": customer_name,
                                                       "draft_operation_id": selected.operation_id})
        if extraction is not None:
            if not ctx.deps.attachment_upload_id:
                raise ValidationFailed("Adjunta el comprobante para leer el monto.")
            if extraction.confidence != "clear" or extraction.amount_bs is None:
                # The uncertainty is already a typed extraction result. Deliver
                # its deterministic question rather than asking the model to
                # compose another explanation from unverified image amounts.
                ctx.deps.question = (
                    "No pude identificar con seguridad el monto del capture. "
                    "Dime la cantidad de bolívares o envíame un capture más claro."
                )
                raise ValidationFailed(ctx.deps.question)
        payload = {**selected.command_payload, "operation_amount_bs": operation_amount_bs,
                   "exchange_rate": exchange_rate, "monetary_from_attachment": False,
                   "draft_operation_id": selected.operation_id, "money_evidence": {}}
        if draft.pending_completion and extraction is None:
            if draft.pending_completion.get("attachment_upload_id"):
                payload["attachment_upload_id"] = draft.pending_completion["attachment_upload_id"]
            for field in ("operation_amount_bs", "exchange_rate"):
                stored = draft.pending_completion.get(field)
                if payload[field] is None or stored is not None and parse_broker_decimal(payload[field]) == parse_broker_decimal(stored):
                    payload[field] = stored
                    if field in draft.pending_completion.get("money_evidence", {}):
                        payload["money_evidence"][field] = draft.pending_completion["money_evidence"][field]
        if extraction is not None:
            payload.update(operation_amount_bs=extraction.amount_bs, monetary_from_attachment=True,
                           attachment_upload_id=str(ctx.deps.attachment_upload_id))
        return load_command(selected.command_kind, payload)

    return await execute_command(ctx, "resume_broker_operation", args, command)


@tool("Descartar la operación sin guardar que esperaba bolívares", kind="ui",
      lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def cancel_broker_operation(ctx: RunContext[TurnDeps], operation_number: int | None = None) -> dict[str, Any]:
    """Úsala solo si el usuario cancela explícitamente la operación que espera Bs. No borra movimientos."""
    started = time.monotonic()
    draft = await ctx.deps.store.get_broker_money_draft(ctx.deps.scope)
    operations = draft.operations() if draft else []
    if (len(operations) > 1 and operation_number is None or
            operation_number is not None and not 1 <= operation_number <= len(operations)):
        ctx.deps.question = "¿Cuál de las operaciones sin guardar quieres descartar?"
        out = {"ok": False, "status": "asked", "instruction": "El sistema pregunta cuál operación descartar. Usa su operation_number."}
    else:
        if operations:
            operations.pop((operation_number or 1) - 1)
        if operations:
            await ctx.deps.store.set_broker_money_draft(ctx.deps.scope, BrokerMoneyDraft.from_operations(operations))
        else:
            await ctx.deps.store.clear_broker_money_draft(ctx.deps.scope)
        ctx.deps.notices.append("Operación descartada. No se registró ningún movimiento.")
        out = {"ok": True, "status": "cancelled", "instruction": "El sistema ya informó que descartó la operación."}
    ctx.deps.tool_results.append(out)
    await _record(ctx.deps, ctx, "cancel_broker_operation", {"operation_number": operation_number}, {}, out, ToolOutcome.OK, started)
    return out


@tool("Extraer Bs de un comprobante y completar la operación existente sin otra comisión",
      kind="write", lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def complete_broker_receipt(
    ctx: RunContext[TurnDeps], extraction: ReceiptExtraction, handle: str | None = None,
) -> dict[str, Any]:
    """Use for a receipt image referring to an existing operation, including image-only messages.

    Extract transferred Bs, not balances, references, accounts or phones. Mark uncertain amounts ambiguous;
    unreadable means no usable amount (amount_bs=null); ambiguous means the transferred amount cannot be
    identified safely among several amounts. Preserve that confidence for replacements too; backend explains it.
    Quoted receipt takes precedence over handle/caption and temporal matching.
    If an UNRECORDED operation is waiting for Bs, use resume_broker_operation with extraction instead.
    No handle: backend chooses only one recent compatible recorded operation by the same actor, or asks a selection.
    Never use this to create an operation. Caption describing a new purchase/sale uses its recording tool instead.
    """
    quoted = [h for h in ctx.deps.quoted_handles if h.entity_type == EntityType.BROKER_MOVEMENT]
    target = quoted[0] if len(quoted) == 1 else await resolve_handle(ctx, handle) if handle else None
    if isinstance(target, dict):
        ctx.deps.tool_results.append(target)
        return target
    def command():
        if len(quoted) > 1 or target is not None and target.entity_type != EntityType.BROKER_MOVEMENT:
            raise ValidationFailed("Selecciona un único movimiento para el comprobante.")
        if not ctx.deps.attachment_upload_id:
            raise ValidationFailed("Adjunta un comprobante para completar esta operación.")
        return CompleteBrokerReceipt(
            movement_id=target.entity_id if target else None,
            expected_version=target.version if target else None,
            attachment_upload_id=ctx.deps.attachment_upload_id,
            operation_amount_bs=extraction.amount_bs if extraction.confidence != "unreadable" else None,
            extracted_data=extraction.model_dump(), extraction_ambiguous=extraction.confidence == "ambiguous",
        )
    return await execute_command(ctx, "complete_broker_receipt", {"extraction": extraction.model_dump(), "handle": handle},
                                 command, target_handle=target)


@tool("Explicar que una venta de artículos físicos no pertenece al modo corredores", kind="ui",
      lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def reject_physical_sale(ctx: RunContext[TurnDeps]) -> dict[str, Any]:
    """Declara, sin escribir, que una venta inequívoca de artículos físicos está fuera de esta vertical.

    Úsala cuando el usuario describa inequívocamente una venta retail de productos o artículos, por ejemplo al
    expresar el producto junto con su cantidad o precio. No la uses solo porque aparezca «vendí»: «vendí 1000 y
    entregué 990» es una operación de corredor. Si podría haber más de una interpretación financiera o falta un
    dato, usa `need_more_info` en vez de rechazar o registrar.
    """
    deps = ctx.deps
    started = time.monotonic()
    if PHYSICAL_SALE_NOTICE not in deps.notices:
        deps.notices.append(PHYSICAL_SALE_NOTICE)
    out = {
        "ok": True,
        "status": "unsupported_physical_sale",
        "message": PHYSICAL_SALE_NOTICE,
        "instruction": "El sistema ya explicó el límite y que no registró nada. No añadas ni repitas texto.",
    }
    deps.tool_results.append(out)
    await _record(deps, ctx, "reject_physical_sale", {}, {}, out, ToolOutcome.OK, started)
    return out


@tool("Registrar un abono a cuenta o sumar un importe al balance explícitamente, sin cliente ni comisión", kind="write",
      lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def record_broker_account_payment(
    ctx: RunContext[TurnDeps],
    amount: str,
    broker_name: NullableToolString = None,
    description: NullableToolString = None,
    business_date: date | None = None,
) -> dict[str, Any]:
    """«Súmale 500$ a mi balance» o el abono existente «Pagué 500 a mi cuenta/al negocio».

    Suma exactamente el monto, con cualquier saldo previo y para cualquier corredor.
    Sin Bs/tasa, cliente ni comisión. «Recibí 500» o «Recibí de Casa» solos son ambiguos:
    pregunta si quiere sumar o restar antes de escribir. Conserva compras y pagos de cliente
    identificados por el contexto. Sumar un importe NO es reconciliar un saldo objetivo.

    Args:
        amount: Monto positivo en USD.
        broker_name: Nombre del corredor. Obligatorio para el dueño porque Casa no tiene abonos a cuenta.
        description: Concepto corto dicho por el usuario.
        business_date: Fecha del movimiento; por defecto hoy.
    """
    args = dict(amount=amount, broker_name=broker_name, description=description,
                business_date=business_date)
    return await execute_command(ctx, "record_broker_account_payment", args, lambda: RecordBrokerAccountPayment(
        amount=parse_broker_decimal(amount), broker_name=broker_name,
        description=description, business_date=business_date,
        attachment_upload_id=ctx.deps.attachment_upload_id,
    ))


@tool("Restar un importe del balance por pedido explícito, sin cliente, operación ni comisión", kind="write",
      lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def record_broker_account_withdrawal(
    ctx: RunContext[TurnDeps], amount: str,
    broker_name: NullableToolString = None, description: NullableToolString = None,
    business_date: date | None = None,
) -> dict[str, Any]:
    """«Réstale 500$ a mi balance» o «Resta 500 de mi saldo por la entrega a Sandra».

    Resta exactamente el monto con cualquier saldo previo; amount es positivo. No pide Bs ni tasa.
    Sin clientes ni cuentas por cobrar; un destinatario puede quedar en description.
    «Entregué 500» o «Entregué de mi saldo» solos NO ordenan una resta: pregunta si quiere
    sumar o restar, sin escribir. «Réstalo» puede completar esa aclaración con el monto anterior.
    «Entregué a Sandra» como pago de cliente usa record_broker_customer_payout.
    «Entregué al negocio» conserva record_broker_account_payment (delta positivo).
    «Saqué 500», monto solo sin otro pedido pendiente o abreviación conservan compra propia con comisión.
    broker_name selecciona corredor solo para el dueño; el corredor usa su propia cuenta.
    """
    args = dict(amount=amount, broker_name=broker_name, description=description, business_date=business_date)
    return await execute_command(ctx, "record_broker_account_withdrawal", args,
        lambda: RecordBrokerAccountWithdrawal(amount=parse_broker_decimal(amount), broker_name=broker_name,
            description=description, business_date=business_date, attachment_upload_id=ctx.deps.attachment_upload_id))


@tool("Registrar venta liquidada/pendiente o proponer compra que requiere elegir estado", kind="write",
      lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def record_broker_operation(
    ctx: RunContext[TurnDeps],
    operation_type: Literal["buy", "sell"],
    operation_amount: str,
    settlement: Literal["settled", "pending"] = "settled",
    received_amount: str | None = None,
    customer_name: NullableToolString = None,
    broker_name: NullableToolString = None,
    description: NullableToolString = None,
    business_date: date | None = None,
    operation_amount_bs: str | None = None, exchange_rate: str | None = None,
    monetary_from_attachment: bool = False, use_previous_receipt: bool = False,
    customer_classification: Literal["global", "personal"] | None = None,
    extraction: ReceiptExtraction | None = None,
    text_money_sources: MoneyTextSources | None = None,
) -> dict[str, Any]:
    """Registra venta liquidada/pendiente. Las compras SIEMPRE esperan elección Pendiente/Liquidada.
    Para compras prefiere record_broker_purchase; esta herramienta tampoco puede evitar esa elección.

    Ventas sin deuda explícita: settlement="settled". «Pendiente», «no le he pagado»,
    «todavía no envié los dólares» o «queda pendiente de pago»: settlement="pending" y cliente obligatorio.
    El backend calcula el saldo y distingue cartera global/personal; no calcules efectos financieros.
    settlement="pending" solo aplica a ventas; compras pendientes usan record_broker_purchase.

    «Vendí 100$» usa SIEMPRE esta herramienta con operation_type="sell", no record_broker_purchase.
    Vendí/vendimos/venta describen venta; compré/compramos/compra describen compra.

    Toda venta de corredor acredita SOLO comisión, sin descontar principal, sea liquidada o pendiente,
    personal, global o sin cliente. Casa mantiene delta/comisión cero. Ventas liquidadas no crean obligación.
    Interpreta la frase completa: «pendiente» solo no define venta. «Compra 900 pendiente» es compra;
    «Pendiente 900» requiere aclarar intención. Ventas son liquidadas por defecto, sin preguntar modalidad.
    «Vendí 200, entregué 195» requiere need_more_info para aclarar el nominal antes de escribir.
    Nunca pidas USD entregados ni registres excedentes de ventas. Compras usan received_amount para USD
    recibidos explícitos; el excedente y la comisión sobre el nominal se calculan en backend.
    No uses montos de artículos físicos; esos van con `reject_physical_sale`.
    Si el usuario proporciona USD, total Bs Y tasa, envía AMBOS campos con sus fuentes. Nunca descartes
    un dato expreso para hacer que cuadren: el backend valida la triple y rechaza contradicciones.

    Args:
        operation_type: buy para compra de dólares; sell para venta de dólares.
        operation_amount: Monto total comprado o vendido, en USD.
        received_amount: Solo compra: USD recibidos explícitos, independientes de abonos y del principal.
        customer_name: Cliente expresamente indicado, si existe. Otros nombres van en description.
        broker_name: Nombre del corredor si la operación pertenece a uno. Omitirlo hace que el dueño opere Casa.
        description: Concepto corto dicho por el usuario.
        business_date: Fecha de la operación; por defecto hoy.
        operation_amount_bs: Monto TOTAL en Bs, no tasa. «100$ 80000bs», «100$ por 80000bs» o «100$ 80000»
            indican "80000" aquí. Omite exchange_rate solo si no fue declarado. También admite Bs de la imagen.
        exchange_rate: Tasa por dólar declarada: «100$ a 800», «100$ tasa 800», «100$ a tasa de 800»
            indican "800" aquí. Omite operation_amount_bs solo si no fue declarado. Nunca pongas aquí el total Bs.
            USD solo: omite ambos campos; el sistema conserva la operación sin guardar y pide Bs o capture. No calcules derivados.
        monetary_from_attachment: true si Bs provienen de la imagen actual, nunca para una referencia externa.
        customer_classification: Solo una elección o clasificación explícita, fuera del nombre del cliente.
            Para el cliente llamado «QA V2 Global Samu», omítelo si no eligió clasificación por separado;
            «Global» dentro de ese nombre no clasifica al cliente. El backend preguntará si es nuevo.
        use_previous_receipt: true solo si el único comprobante inmediatamente anterior pertenece inequívocamente
            a esta operación. No lo uses para otra operación; ante ambigüedad pide aclaración.
    """
    args = dict(customer_classification=customer_classification,operation_amount_bs=operation_amount_bs, exchange_rate=exchange_rate,
                monetary_from_attachment=monetary_from_attachment,
                extraction=extraction.model_dump() if extraction else None,
                text_money_sources=text_money_sources.model_dump() if text_money_sources else None,
        operation_type=operation_type, settlement=settlement,
        operation_amount=operation_amount,
        received_amount=received_amount,
        customer_name=customer_name,
        broker_name=broker_name,
        description=description,
        business_date=business_date,
    )
    return await execute_command(ctx, "record_broker_operation", {**args, "use_previous_receipt": use_previous_receipt}, lambda: RecordBrokerOperation(customer_classification=customer_classification,
                                     operation_amount_bs=operation_amount_bs, exchange_rate=exchange_rate,
                                     monetary_from_attachment=monetary_from_attachment,
        operation_type=operation_type, settlement=settlement,
        operation_amount=parse_broker_decimal(operation_amount),
        received_amount=parse_broker_decimal(received_amount) if received_amount is not None else None,
        customer_name=customer_name,
        broker_name=broker_name,
        description=description,
        business_date=business_date,
        attachment_upload_id=_operation_attachment(ctx, use_previous_receipt),
    ))


@tool("Proponer compra de divisas; siempre pregunta Pendiente/Liquidada antes de guardar",
      kind="write", lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def record_broker_purchase(
    ctx: RunContext[TurnDeps], amount: str, customer_name: NullableToolString = None,
    settlement: Literal["settled", "pending"] | None = None,
    broker_name: NullableToolString = None, description: NullableToolString = None,
    business_date: date | None = None, operation_amount_bs: str | None = None,
    exchange_rate: str | None = None, monetary_from_attachment: bool = False, use_previous_receipt: bool = False,
    customer_classification: Literal["global", "personal"] | None = None,
    received_amount: str | None = None,
    extraction: ReceiptExtraction | None = None,
    text_money_sources: MoneyTextSources | None = None,
) -> dict[str, Any]:
    """For purchases of USD, including contextual dollars received, never sales (vendí/venta).
    «Vendí 100$» must use record_broker_operation(operation_type="sell", operation_amount="100").
    ALWAYS proposes an unrecorded purchase: backend asks Pendiente/Liquidada before saving,
    even with an explicit settlement, a receipt, 'de contado', or USD received. Never replace this
    deterministic question with ask_choice. Omit settlement unless explicitly stated; it is only a proposal.
    «Compré 987 a Gamal, recibí 1000» supplies received_amount="1000" but still waits for that choice.
    «100$ Jessica zelle recibido» and name+amount shortcuts are purchases requiring the same choice.
    Zelle is context, not a stored field. A debt payment or Bs received for a sale is not a new purchase.
    Amounts are decimal strings. Bs/rate come exclusively from user text, voice or current image.
    Text Bs/rate REQUIRE text_money_sources in the SAME call; without its literal quotes the backend
    discards those proposed numbers. «Jessica 100$ a tasa 950» supplies amount="100", exchange_rate="950",
    text_money_sources={"principal_usd": "100$", "exchange_rate": "950"}.
    «Compré 100$ por 95000 Bs» supplies amount="100", operation_amount_bs="95000",
    text_money_sources={"principal_usd": "100$", "operation_amount_bs": "95000 Bs"}.
    «100$ 80000bs» or «100$ 80000»: operation_amount_bs="80000", omit exchange_rate.
    «100$ a 800», «100$ tasa 800», «100$ a tasa de 800»: exchange_rate="800", omit operation_amount_bs.
    USD only: omit both; backend keeps an unrecorded draft and asks for Bs or a receipt before any settlement choice. Never supply a derived value.
    Classification is supplied only when explicitly stated outside the customer name, never guessed.
    A customer named «QA V2 Global Samu» does not provide a classification: omit customer_classification
    unless the user separately chooses global/personal. Backend asks classification for new identities.
    use_previous_receipt: true solo si el único comprobante inmediatamente anterior pertenece inequívocamente
    a esta operación. No lo uses para otra operación; ante ambigüedad pide aclaración.
    """
    args = dict(received_amount=received_amount, amount=amount, customer_name=customer_name, settlement=settlement, broker_name=broker_name,
                description=description, business_date=business_date, operation_amount_bs=operation_amount_bs,
                exchange_rate=exchange_rate, monetary_from_attachment=monetary_from_attachment,
                extraction=extraction.model_dump() if extraction else None,
                text_money_sources=text_money_sources.model_dump() if text_money_sources else None,
                customer_classification=customer_classification)
    return await execute_command(ctx, "record_broker_purchase", {**args, "use_previous_receipt": use_previous_receipt},
                                 lambda: RecordBrokerPurchase(**{key: value for key, value in args.items() if key not in {"extraction", "text_money_sources"}}, attachment_upload_id=_operation_attachment(ctx, use_previous_receipt)))


@tool("Registrar una compra propia del corredor en su balance, con comisión y sin crear cliente ni deuda",
      kind="write", lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def record_broker_account_purchase(
    ctx: RunContext[TurnDeps], amount: str, description: NullableToolString = None,
    broker_name: NullableToolString = None,
    business_date: date | None = None,
    operation_amount_bs: str | None = None, exchange_rate: str | None = None,
    monetary_from_attachment: bool = False,
    received_amount: str | None = None,
    extraction: ReceiptExtraction | None = None,
    text_money_sources: MoneyTextSources | None = None,
) -> dict[str, Any]:
    """Registra fondos usados por el corredor para sí mismo como una compra en cuenta.

    Úsala cuando el corredor autenticado diga «Saqué 40», escriba solo «40»/«40$», use su abreviación como
    «SB 40», o agregue un concepto como «SB 40 Farmatodo». La comisión se acredita de inmediato y el principal
    queda en su balance; no crea cliente ni cuenta por cobrar. El dueño debe identificar al corredor.

    Bs/tasa escritos exigen text_money_sources en esta misma llamada. «SB 45.66 Farmatodo, tasa 800»:
    amount="45.66", description="Farmatodo", exchange_rate="800",
    text_money_sources={"principal_usd": "45.66", "exchange_rate": "800"}.
    Sin Bs/tasa el backend conserva un borrador; la continuación usa resume_broker_operation.

    Args:
        amount: Principal usado por el corredor, en USD.
        description: Concepto adicional expreso, por ejemplo Farmatodo; nunca la abreviación del corredor.
        broker_name: Nombre del corredor cuando escribe el dueño; el corredor autenticado usa su propia cuenta.
        business_date: Fecha de la operación; por defecto hoy.
    """
    args = dict(operation_amount_bs=operation_amount_bs, exchange_rate=exchange_rate,
                monetary_from_attachment=monetary_from_attachment,
                extraction=extraction.model_dump() if extraction else None,
                text_money_sources=text_money_sources.model_dump() if text_money_sources else None,
        received_amount=received_amount,
        amount=amount,
        description=description,
        broker_name=broker_name,
        business_date=business_date,
    )
    return await execute_command(ctx, "record_broker_account_purchase", args,
                                 lambda: RecordBrokerAccountPurchase(
                                     operation_amount_bs=operation_amount_bs, exchange_rate=exchange_rate,
                                     monetary_from_attachment=monetary_from_attachment,
                                     received_amount=parse_broker_decimal(received_amount) if received_amount is not None else None,
        amount=parse_broker_decimal(amount), description=description,
                                     broker_name=broker_name,
                                     business_date=business_date,
                                     attachment_upload_id=ctx.deps.attachment_upload_id,
                                 ))


@tool("Proponer compra pendiente; siempre espera elección Pendiente/Liquidada antes de registrar",
      kind="write", lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def record_pending_broker_purchase(
    ctx: RunContext[TurnDeps], customer_name: str, amount: str,
    broker_name: NullableToolString = None, description: NullableToolString = None,
    business_date: date | None = None, force_new_customer: bool = False,
    operation_amount_bs: str | None = None, exchange_rate: str | None = None,
    monetary_from_attachment: bool = False, use_previous_receipt: bool = False,
    customer_classification: Literal["global", "personal"] | None = None,
    received_amount: str | None = None,
    extraction: ReceiptExtraction | None = None,
    text_money_sources: MoneyTextSources | None = None,
) -> dict[str, Any]:
    """Compatibilidad de compras pendientes. Para proponer una compra NUEVA prefiere
    record_broker_purchase(settlement="pending"); conserva el mismo flujo Pendiente/Liquidada.

    Con USD y cliente conocidos, LLAMA esta herramienta aunque no haya Bs/tasa ni fuentes textuales.
    Omite esos campos: el backend pide el dato y conserva la operación SIN escribir dinero.
    No reemplaces esa llamada por need_more_info, porque una pregunta sola no conserva el borrador.

    Úsala solo si el usuario dejó claro que el negocio ya entregó fondos y ese cliente quedó debiendo los USD.
    Esta herramienta también espera SIEMPRE la elección Pendiente/Liquidada antes de guardar.
    Prefiere record_broker_purchase para las compras, incluidos atajos nombre+monto y comprobantes.
    Tras elegir Pendiente, la compra queda en el historial y la comisión se reconoce al entregar esos fondos, aunque el cliente no haya
    entregado todavía las divisas.
    «Entregamos los fondos a Orlando y quedó debiendo 197.77 dólares» es un ejemplo inequívoco.
    Cuando escribe un corredor, los atajos «Orlando 100» y «Orlando 100$» también significan una compra pendiente:
    el nombre es el cliente y el monto es el principal en USD. En esta vertical, omitir la moneda significa USD.
    Añadir Bs, tasa, comprobante o USD recibidos nunca evita la elección obligatoria del usuario.
    Una deuda genérica todavía requiere aclaración.

    Args:
        customer_name: Cliente que quedó debiendo la compra.
        amount: Principal pendiente en USD.
        broker_name: Corredor responsable; omitirlo hace que el dueño use la cartera de Casa.
        description: Concepto expreso opcional.
        business_date: Fecha; por defecto hoy.
        force_new_customer: true solo tras confirmar que es un cliente nuevo.
        operation_amount_bs: Total en Bs: «100$ 80000bs» o «100$ 80000» indica "80000"; omite tasa.
        exchange_rate: Tasa por USD explícita: «100$ a 800» o «100$ tasa 800» indica "800"; omite Bs.
        customer_classification: Solo una elección explícita de clasificación, separada del nombre.
            El cliente «QA V2 Global Samu» no indica clasificación: omite este campo hasta que el usuario
            elija Global/Personal por separado. No deduzcas la clasificación de palabras del nombre.
    use_previous_receipt: true solo si el único comprobante inmediatamente anterior pertenece inequívocamente
    a esta operación. No lo uses para otra operación; ante ambigüedad pide aclaración.
    """
    args = dict(received_amount=received_amount, customer_classification=customer_classification,operation_amount_bs=operation_amount_bs, exchange_rate=exchange_rate,
                monetary_from_attachment=monetary_from_attachment,
                extraction=extraction.model_dump() if extraction else None,
                text_money_sources=text_money_sources.model_dump() if text_money_sources else None,customer_name=customer_name, amount=amount, broker_name=broker_name,
                description=description, business_date=business_date,
                force_new_customer=force_new_customer or None)
    return await execute_command(ctx, "record_pending_broker_purchase", {**args, "use_previous_receipt": use_previous_receipt},
                                 lambda: RecordPendingBrokerPurchase(received_amount=received_amount, customer_classification=customer_classification,
                                     operation_amount_bs=operation_amount_bs, exchange_rate=exchange_rate,
                                     monetary_from_attachment=monetary_from_attachment,
                                     customer_name=customer_name, amount=parse_broker_decimal(amount),
                                     broker_name=broker_name, description=description,
                                     business_date=business_date, force_new_customer=force_new_customer,
                                     attachment_upload_id=_operation_attachment(ctx, use_previous_receipt),
                                 ))


@tool("Registrar el abono USD de un cliente: reduce la deuda y, para corredor, aumenta su saldo",
      kind="write", lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def record_broker_receivable_payment(
    ctx: RunContext[TurnDeps], customer_name: str, amount: str,
    broker_name: NullableToolString = None, description: NullableToolString = None,
    business_date: date | None = None,
) -> dict[str, Any]:
    """Registra atómicamente un pago a la cartera administrada por el corredor.

    Cuando un corredor dice «Carlos pagó 50», aplica 50 USD por FIFO a las deudas de Carlos en su propia cartera
    y suma 50 USD al saldo del corredor. No vuelve a calcular ni acreditar comisión. Si escribe el dueño sin
    identificar corredor, aplica el pago por FIFO exclusivamente a la cartera de Casa y no modifica balances.

    Args:
        customer_name: Cliente que abonó.
        amount: Monto pagado en USD; toda cifra de esta vertical se entiende en USD.
        broker_name: Corredor responsable; omitirlo hace que el dueño use la cartera de Casa.
        description: Concepto expreso opcional.
        business_date: Fecha; por defecto hoy.
    """
    args = dict(customer_name=customer_name, amount=amount, broker_name=broker_name,
                description=description, business_date=business_date)
    return await execute_command(ctx, "record_broker_receivable_payment", args,
                                 lambda: RecordBrokerReceivablePayment(
                                     customer_name=customer_name, amount=parse_broker_decimal(amount),
                                     broker_name=broker_name, description=description,
                                     business_date=business_date,
                                     attachment_upload_id=ctx.deps.attachment_upload_id,
                                 ))


@tool("Consultar balances y los totales operados hoy por Casa, corredor o todo el negocio", kind="read",
      verticals=BROKERAGE)
async def get_broker_balances(
    ctx: RunContext[TurnDeps],
    account_scope: Literal["default", "house", "all", "broker"] = "default",
    broker_name: NullableToolString = None,
    period: PeriodName = PeriodName.TODAY,
    date_from: date | None = None,
    date_to: date | None = None,
    presentation: Literal["data", "summary"] = "data",
) -> dict[str, Any]:
    """Consulta métricas del período y balance/cartera actuales, sin escribir.

    Args:
        account_scope: default/all consolida el negocio para el dueño; house es Casa o su cuenta propia.
            Un corredor siempre queda limitado a su cuenta autenticada.
        broker_name: Corredor concreto elegido por el dueño; prevalece sobre account_scope.
        period: Período de operaciones, hoy si no se solicita otro. Balance y cartera son actuales.
        date_from: Inicio de un período custom.
        date_to: Fin de un período custom.
        presentation: summary solo si se solicita una vista estándar completa de negocio/cuenta.
            data para cifras concretas, comparaciones o análisis que necesitan combinar herramientas.
            El sistema elige una sola presentación al terminar el turno; sigue consultando si hace falta.
    """

    scope, broker_id, error = await _query_account(ctx, account_scope, broker_name)
    args = dict(account_scope=account_scope, broker_name=broker_name, period=period,
                date_from=date_from, date_to=date_to, presentation=presentation)
    if error:
        async def invalid() -> QueryResult:
            raise ValidationFailed(error["message"], candidates=error["candidates"])
        return await execute_query(ctx, "get_broker_balances", args, invalid)

    async def run() -> QueryResult:
        data = await ctx.deps.brokerage_queries.summary(
            ctx.deps.business_id,
            ctx.deps.identity.member_id,
            period=period, date_from=date_from, date_to=date_to,
            account_scope=scope,
            broker_id=broker_id,
        )
        clean_balances = [{k: v for k, v in row.items() if k != "member_id"} for row in data["balances"]]
        return QueryResult({
            "date": data["date"], "period": data["period"], "account_scope": data["account_scope"],
            "balances": clean_balances, "totals": data["totals"], "multicurrency": data["multicurrency"],
            "period_label": "Hoy" if period == PeriodName.TODAY else data["period"]["label"],
            "presentation_rules": (
                "Para presentar un resumen, el apartado de balances enumera exactamente las filas de balances. "
                "Si la lista está vacía, omite ese apartado. Las otras cuentas se presentan en operaciones y cartera. "
                "Finaliza al terminar los datos solicitados; las explicaciones conceptuales corresponden a preguntas explícitas."
            ),
        }, [])

    out = await execute_query(ctx, "get_broker_balances", args, run)
    if out.get("ok") and presentation == "summary":
        ctx.deps.renders.append(render_broker_balance_summary(
            out,
            account_scope=scope,
            viewer_is_broker=ctx.deps.identity.member_role == MemberRole.BROKER,
        ))
    return out


async def _customer_history_query(
    ctx, *, customer_name, account_scope, broker_name, portfolio_scope,
    account_filter_explicitly_requested, date_from=None, date_to=None, limit=20,
    include_movements=True,
):
    """Separate customer identity/visibility from an explicit execution-account filter."""
    # This typed flag is the model's interpretation of the CURRENT request;
    # never infer semantic intent with keyword matching in deterministic code.
    selected_scope = account_scope if account_filter_explicitly_requested else "default"
    selected_broker = broker_name if account_filter_explicitly_requested else None
    scope, broker_id, error = await _query_account(ctx, selected_scope, selected_broker)
    if error:
        raise ValidationFailed(error["message"], candidates=error["candidates"])
    data = await ctx.deps.brokerage_queries.customer_accounts(
        ctx.deps.business_id, ctx.deps.identity.member_id,
        account_scope="all", portfolio_scope=portfolio_scope,
        customer_name=customer_name, status="all", limit=30,
    )
    refs = []
    records = [{k: v for k, v in row.items() if k not in {"customer_id", "member_id", "version"}}
               for row in data["items"]]
    if data["total"] == 1 and include_movements:
        history_scope = scope if account_filter_explicitly_requested else "all"
        history = await ctx.deps.brokerage_queries.customer_account(
            ctx.deps.business_id, ctx.deps.identity.member_id, UUID(data["items"][0]["customer_id"]),
            date_from=date_from, date_to=date_to, limit=limit,
            account_scope=history_scope, broker_id=broker_id if account_filter_explicitly_requested else None,
            include_balance_steps=not (date_from or date_to or history_scope != "all"
                                      or (broker_id and account_filter_explicitly_requested)),
        )
        # Shared global history must not leak another broker's balance, commission,
        # phone or identity. Keep only the customer's monetary history.
        fields = {"business_date", "occurred_at", "logical_type", "customer_name", "amount",
                  "operation_amount_bs", "exchange_rate", "original_settlement",
                  "customer_balance_delta", "customer_balance_before", "customer_balance_after"}
        records[0].update(
            movements=[{k: v for k, v in row.items() if k in fields} for row in history["items"]],
            history_total=history["total"], history_complete=history["total"] <= limit,
            history_scope=history_scope, balance_scope="customer_portfolio",
            balance_basis="Saldo actual de la cartera del cliente; historial filtrado solo por petición explícita o fechas.",
        )
        for row in history["items"]:
            refs.append(EntityRef(EntityType(row.get("entry_type", "broker_movement")), UUID(row["id"]), version=row["version"],
                summary=f"{row['logical_type']} | {history['customer']} | USD {row['amount']} | {row['business_date']}"))
    return data, records, refs


@tool("Buscar movimientos por cliente o por cuenta de corredores", kind="read", verticals=BROKERAGE)
async def find_broker_movements(
    ctx: RunContext[TurnDeps], broker_name: NullableToolString = None,
    account_scope: Literal["default", "house", "all", "broker"] = "default",
    date_from: date | None = None, date_to: date | None = None, limit: int = 10,
    portfolio_scope: Literal["all", "global", "personal"] = "all",
    customer_name: str | None = None,
    account_filter_explicitly_requested: bool = False,
) -> dict[str, Any]:
    """Busca movimientos visibles. default/all consolida para el dueño; house limita a Casa.
    Sin cliente, el corredor solo ve su cuenta. Con cliente global, el historial
    compartido incluye todos los ejecutores sin revelar sus balances o comisiones.
    El dueño puede filtrar por cuenta solo si lo solicita expresamente.

    Args:
        broker_name: Corredor a buscar; omitir para todos los visibles.
        date_from: Fecha inicial.
        date_to: Fecha final.
        limit: Máximo de resultados, de 1 a 15.
        customer_name: Cliente nombrado; devuelve su historial autorizado completo por defecto, incluido global compartido.
        account_filter_explicitly_requested: True solo si el mensaje actual pide limitar a Casa o a un corredor.
            No heredar esa restricción de registros o consultas anteriores. Con cliente, False ignora account_scope/broker_name.
    """
    args = {"portfolio_scope": portfolio_scope,"broker_name": broker_name, "account_scope": account_scope,
            "date_from": date_from, "date_to": date_to, "limit": limit,
            "customer_name": customer_name, "account_filter_explicitly_requested": account_filter_explicitly_requested}

    async def run() -> QueryResult:
        if customer_name:
            data, records, refs = await _customer_history_query(
                ctx, customer_name=customer_name, account_scope=account_scope, broker_name=broker_name,
                portfolio_scope=portfolio_scope, account_filter_explicitly_requested=account_filter_explicitly_requested,
                date_from=date_from, date_to=date_to, limit=max(1, min(limit, 15)),
            )
            if data["total"] != 1:
                return QueryResult({"customer_matches": data["total"], "candidates": [r["customer"] for r in records],
                                    "movements": [], "needs_customer_selection": True}, refs)
            return QueryResult({**records[0], "account_scope": records[0]["history_scope"],
                                "total": records[0]["history_total"]}, refs)
        scope, broker_id, error = await _query_account(ctx, account_scope, broker_name)
        if error:
            raise ValidationFailed(error["message"], candidates=error["candidates"])
        data = await ctx.deps.brokerage_queries.movements(
            ctx.deps.business_id, ctx.deps.identity.member_id, broker_id=broker_id,
            account_scope=scope, portfolio_scope=portfolio_scope,
            date_from=date_from, date_to=date_to, limit=max(1, min(limit, 15)),
        )
        refs: list[EntityRef] = []
        records: list[dict[str, Any]] = []
        for row in data["items"]:
            labels = {
                "adjustment": "Ajuste de saldo",
                "customer_payout": "pago entregado",
                "account_payment": "abono a cuenta",
                "account_withdrawal": "salida de cuenta",
                "receivable_payment": "abono de cliente",
                "buy": "compra",
                "sell": "venta",
                "account": "compra propia",
                "pending": "compra pendiente",
            }
            label = labels[row["logical_type"]]
            summary = f"{label} | {row['broker']} | USD {row['amount']} | {row['business_date']}"
            refs.append(EntityRef(EntityType.BROKER_MOVEMENT, UUID(row["id"]), version=row["version"], summary=summary))
            record = {k: v for k, v in row.items() if k not in {"id", "version", "member_id", "customer_id", "kind", "portfolio_member_id", "monetary_provenance"}}
            if row["account_kind"] == "house":
                for key in ("balance_delta", "balance_delta_display", "commission", "commission_rate"):
                    record.pop(key, None)  # Casa no tiene estos conceptos; no ofrecer campos irrelevantes al modelo.
            record["tipo"] = label
            records.append(record)
        return QueryResult({"account_scope": scope, "total": data["total"],
                            "totals": data["totals"], "movements": records}, refs)

    return await execute_query(ctx, "find_broker_movements", args, run)


@tool("Consultar saldos netos por cliente: a favor, por cobrar o saldados", kind="read", verticals=BROKERAGE)
async def find_broker_receivables(
    ctx: RunContext[TurnDeps], broker_name: NullableToolString = None,
    account_scope: Literal["default", "house", "all", "broker"] = "default",
    customer_name: str | None = None, limit: int = 10,
    portfolio_scope: Literal["all", "global", "personal"] = "all",
    include_movements: bool = False,
    account_filter_explicitly_requested: bool = False,
) -> dict[str, Any]:
    """Consulta cuentas por cliente. Positivo: Casa debe al cliente; negativo: cliente debe.

    Incluye cuentas saldadas al buscar un cliente. Nunca sumes compras individuales
    a este saldo: ya incluye compras, ventas pendientes y ambos sentidos de pago.
    Global es compartido; personal ajeno no es visible. No expone IDs al modelo.
    Para explicar un saldo u operación, pasa customer_name e include_movements=true:
    incluye hasta 20 movimientos recientes de ese cliente con saldo anterior, variación y posterior.
    Si hay varios candidatos, pide precisar el nombre. El saldo actual puede diferir del posterior a una operación.
    Con cliente nombrado, account_filter_explicitly_requested=True solo si el usuario pide expresamente
    limitar el historial a Casa o un corredor en el mensaje actual. Por defecto no heredes filtros anteriores.
    """
    args = dict(portfolio_scope=portfolio_scope, broker_name=broker_name, account_scope=account_scope,
                customer_name=customer_name, limit=limit, include_movements=include_movements,
                account_filter_explicitly_requested=account_filter_explicitly_requested)

    async def run() -> QueryResult:
        if include_movements and not customer_name:
            raise ValidationFailed("Indica el cliente para consultar la composición de su saldo.")
        if customer_name:
            data, records, refs = await _customer_history_query(
                ctx, customer_name=customer_name, account_scope=account_scope, broker_name=broker_name,
                portfolio_scope=portfolio_scope, account_filter_explicitly_requested=account_filter_explicitly_requested,
                include_movements=include_movements,
            )
            return QueryResult({"account_scope": records[0].get("history_scope", "all") if len(records) == 1 else "all",
                                "total": data["total"], "total_to_collect": data["total_to_collect"],
                                "total_to_pay": data["total_to_pay"], "customer_accounts": records}, refs)
        scope, broker_id, error = await _query_account(ctx, account_scope, broker_name)
        if error:
            raise ValidationFailed(error["message"], candidates=error["candidates"])
        data = await ctx.deps.brokerage_queries.customer_accounts(
            ctx.deps.business_id, ctx.deps.identity.member_id, broker_id=broker_id,
            account_scope=scope, portfolio_scope=portfolio_scope, customer_name=customer_name,
            status="all" if customer_name else "open", limit=max(1, min(limit, 30)))
        records = [{k: v for k, v in row.items() if k not in {"customer_id", "member_id", "version"}}
                   for row in data["items"]]
        refs = []
        return QueryResult({"account_scope": scope, "total": data["total"],
                            "total_to_collect": data["total_to_collect"],
                            "total_to_pay": data["total_to_pay"], "customer_accounts": records}, refs)

    return await execute_query(ctx, "find_broker_receivables", args, run)


@tool("Reenviar por WhatsApp el comprobante de un movimiento usando su referencia R", kind="read", verticals=BROKERAGE)
async def show_broker_attachment(ctx: RunContext[TurnDeps], handle: str) -> dict[str, Any]:
    """Envía una sola imagen con caption; nunca expone URL, UUID ni clave de almacenamiento al modelo.

    Args:
        handle: Referencia exacta del movimiento, por ejemplo R3.
    """
    started = time.monotonic()
    target = await resolve_handle(ctx, handle)
    if isinstance(target, dict):
        ctx.deps.tool_results.append(target)
        return target
    if target.entity_type != EntityType.BROKER_MOVEMENT:
        out = {"ok": False, "error": "invalid", "message": "Esa referencia no es un movimiento de corredor."}
    else:
        # La consulta del movimiento aplica tenant/rol antes de que se firme cualquier URL privada.
        movement = await ctx.deps.brokerage_queries.movement(
            ctx.deps.business_id, ctx.deps.identity.member_id, target.entity_id
        )
        attachment = await ctx.deps.attachments.summary_for_movement(
            ctx.deps.business_id, target.entity_id, urls=True
        )
        if attachment is None:
            out = {"ok": False, "error": "not_found", "message": "Ese movimiento no tiene comprobante."}
        else:
            labels = {
                "account_payment": "Abono a cuenta",
                "account_withdrawal": "Salida de cuenta",
                "receivable_payment": "Abono de cliente",
                "buy": "Compra",
                "sell": "Venta",
                "account": "Compra propia",
                "pending": "Compra pendiente",
            }
            caption = " · ".join(
                part for part in (
                    labels.get(movement["logical_type"], "Movimiento"),
                    movement["business_date"],
                    movement["broker"],
                    movement.get("customer_name"),
                ) if part
            )
            ctx.deps.outbound_image = OutboundImage(attachment["original_url"], caption)
            ctx.deps.shown_refs.append(
                EntityRef(target.entity_type, target.entity_id, version=target.version, summary=target.summary)
            )
            out = {
                "ok": True,
                "status": "attachment_ready",
                "message": "El sistema enviará el comprobante como una sola imagen. No añadas texto.",
            }
    ctx.deps.tool_results.append(out)
    await _record(ctx.deps, ctx, "show_broker_attachment", {"handle": handle}, {}, out,
                  ToolOutcome.OK if out.get("ok") else ToolOutcome.REJECTED, started)
    return out


@tool("Borrar un movimiento de corredor usando su referencia R", kind="write", verticals=BROKERAGE)
async def delete_broker_movement(ctx: RunContext[TurnDeps], handle: str) -> dict[str, Any]:
    """Borra un movimiento identificado por una referencia devuelta por `find_broker_movements`.

    Args:
        handle: Referencia exacta, por ejemplo R3.
    """
    target = await resolve_handle(ctx, handle)
    if isinstance(target, dict):
        ctx.deps.tool_results.append(target)
        return target
    if target.entity_type != EntityType.BROKER_MOVEMENT:
        out = {"ok": False, "error": "invalid", "message": "Esa referencia no es un movimiento de corredor."}
        ctx.deps.tool_results.append(out)
        return out
    return await execute_command(ctx, "delete_broker_movement", {"handle": handle}, lambda: DeleteBrokerMovement(
        movement_id=target.entity_id, expected_version=target.version), target_handle=target)


@tool("Corregir un movimiento de corredor y sus registros vinculados usando su referencia R", kind="write",
      verticals=BROKERAGE)
async def edit_broker_movement(
    ctx: RunContext[TurnDeps], handle: str, changes: BrokerMovementChanges,
    text_money_sources: MoneyTextSources | None = None,
) -> dict[str, Any]:
    """Corrige un movimiento existente sin borrarlo ni registrarlo de nuevo.

    Usa una referencia R del contexto o de `find_broker_movements` y pasa únicamente los campos que cambian.
    «Es Diego Example» como respuesta a una venta citada aclara el cliente de ESA venta:
    usa changes.customer_name="Diego Example", conservando sus importes y tasas. Reasigna al cliente
    existente; no uses edit_customer_profile para renombrar el perfil anterior ni su historial.
    En una compra pendiente, `amount` actualiza atómicamente el principal, la deuda del cliente, la comisión y el
    balance del corredor. No uses `delete_broker_movement` cuando el usuario diga «eran 1000», «corrige a 1000» o
    señale que un dato estaba equivocado.

    Args:
        handle: Referencia exacta del movimiento, por ejemplo R3.
        changes: Solo campos expresamente corregidos. «Fueron 81000bs» cambia operation_amount_bs;
            «la tasa era 810» cambia exchange_rate. Omite el otro campo; el backend recalcula el derivado.
    """
    target = await resolve_handle(ctx, handle)
    args = {"handle": handle, "changes": changes.model_dump(exclude_unset=True),
            "text_money_sources": text_money_sources.model_dump() if text_money_sources else None}
    if isinstance(target, dict):
        ctx.deps.tool_results.append(target)
        return target
    if target.entity_type != EntityType.BROKER_MOVEMENT:
        out = {"ok": False, "error": "invalid", "message": "Esa referencia no es un movimiento de corredor."}
        ctx.deps.tool_results.append(out)
        return out
    return await execute_command(
        ctx,
        "edit_broker_movement",
        args,
        lambda: EditBrokerMovement(
            movement_id=target.entity_id,
            expected_version=target.version,
            **changes.command_payload(),
        ),
        target_handle=target,
    )


@tool("Registrar USD entregados por Casa al cliente, sin crear una compra ni un abono recibido",
      kind="write", lanes=FAST_AND_AGENT, verticals=BROKERAGE)
async def record_broker_customer_payout(
    ctx: RunContext[TurnDeps], customer_name: str, amount: str,
    broker_name: NullableToolString = None, description: NullableToolString = None,
    business_date: date | None = None,
) -> dict[str, Any]:
    """«Pagué 1324 a Hum» o «enviamos 100 dólares a Hum»: dinero entregado al cliente.

    Usa cliente destinatario explícito. «Hum pagó 100» es pago recibido y usa
    record_broker_receivable_payment. «Pagué 20 a mi cuenta/al negocio» conserva
    record_broker_account_payment; sin destinatario inequívoco aclara la intención.
    «Entregué 500» sin contexto o «Entregué 500 de mi saldo a Sandra Contreras» son ambiguos:
    aunque Sandra exista, NO uses esta herramienta. Pide con need_more_info si suma o resta
    al balance del corredor, sin escribir. El destinatario no resuelve esa mezcla de cuentas.
    «Réstale 500 a mi balance por Sandra» usa record_broker_account_withdrawal
    y no modifica al cliente. Una suma/resta explícita de balance no es un pago a cliente.
    Solo USD; no exige tasa/Bs. El backend permite pagos parciales o excedentes.
    No crees una venta, compra ni CustomerPayment para representar esta entrega.
    """
    args = dict(customer_name=customer_name, amount=amount, broker_name=broker_name,
                description=description, business_date=business_date)
    return await execute_command(ctx, "record_broker_customer_payout", args,
        lambda: RecordBrokerCustomerPayout(**args, attachment_upload_id=ctx.deps.attachment_upload_id))
