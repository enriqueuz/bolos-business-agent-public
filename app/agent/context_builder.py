"""Context Builder: arma el bloque dinámico del turno. No reconstruye todo el historial.

Orden (lo estable primero para aprovechar cache de prompt): reglas estáticas → negocio/fecha/tasas → caja de hoy →
memoria relevante → registros referenciables → mensaje citado / confirmación descartada.
"""

from __future__ import annotations

import json

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from app.agent.deps import TurnDeps
from app.agent.prompts.system import FAST_LANE_NOTE, history_hint
from app.application.conversation import WindowEntry
from app.domain.countries import CountryProfile, profile_for_country_name
from app.domain.dates import fmt_date, local_now
from app.domain.enums import BusinessVertical, Lane, MemberRole, PaymentMethod
from app.logging import get_logger

log = get_logger(__name__)


def country_block(profile: CountryProfile) -> list[str]:
    """Todo lo específico del país que el modelo necesita: moneda local, tasas, jerga y nombres de métodos de pago."""
    lines = [
        "\n# País y monedas",
        f"País: {profile.name}. Moneda local: {profile.local_currency} ({profile.local.plural}, símbolo {profile.local.symbol}). "
        f"Monedas permitidas en registros: {profile.local_currency} y USD.",
        f"Tasa oficial: {profile.official_rate_label}."
        + (f" Tasa de mercado: {profile.market_rate_label}." if profile.has_market_rate else " No hay tasa de mercado separada.")
        + " El negocio cobra con una de ellas o con su tasa propia (su \"tasa para cobrar\").",
    ]
    if profile.slang:
        lines.append("Jerga de moneda: " + "; ".join(profile.slang) + ".")
    lines.append("Nombres locales de métodos de pago: "
                 + ", ".join(f"{m.value} = {profile.payment_label(m.value)}" for m in PaymentMethod) + ".")
    lines.extend(profile.extra_prompt_notes)
    return lines


def unsupported_country_note(declared: str | None, profile: CountryProfile) -> list[str]:
    """Negocio en un país que Bolos aún no maneja: su moneda local no existe en el sistema, así que registra en USD."""
    if not declared or profile_for_country_name(declared) is not None:
        return []
    return [f"OJO: el negocio está en {declared}, país que Bolos aún no maneja. Registra todo en USD (currency=USD). "
            f"Si da un monto en la moneda de {declared}, NO lo registres como {profile.local_currency}: pídele el "
            "equivalente en dólares."]


async def dynamic_instructions(deps: TurnDeps) -> str:
    b = deps.business
    now_local = local_now(b.timezone, deps.now)
    parts = ["# Contexto de este turno", f"Negocio: {b.name or 'sin nombre'}.",
             f"Miembro: {deps.identity.member_name or 'sin nombre'} ({deps.identity.member_role.value}).",
             f"Fecha y hora local: {fmt_date(now_local.date())} ({now_local.date().isoformat()}), {now_local:%H:%M}."]
    if deps.reconciliation is not None:
        parts.append("RECONCILIACIÓN EN CURSO iniciada por una solicitud de ajuste de saldo. Usa reconcile_balance para interpretar "
                     "el saldo objetivo histórico, sin calcular delta ni registrar compras/ventas/abonos. "
                     "Llama reconcile_balance incluso si el historial muestra un ajuste idéntico: el backend resuelve delta cero. "
                     "Puede ajustar cuentas de otros corredores; clientes solo globales o personales propios si es corredor. "
                     "Usa auto salvo tipo de cuenta explícito. Datos ya aportados en este flujo: "
                     + json.dumps(deps.reconciliation.fields, ensure_ascii=False))
    if b.vertical == BusinessVertical.RETAIL:
        parts.extend([
            f"Actividad: {b.what_sells or 'no indicada'}.",
            f"Moneda preferida para reportes: {b.profile.view_label(b.display_currency)}.",
            *country_block(b.profile),
            *unsupported_country_note(b.declared_country, b.profile),
        ])
        if b.payment_methods:
            parts.append("Métodos de pago que usa: " + ", ".join(b.profile.payment_label(m) or m for m in b.payment_methods) + ".")
    try:
        if b.vertical == BusinessVertical.BROKERAGE:
            scope = (
                "Cuenta autorizada y predeterminada: su propia cuenta de corredor. No puede seleccionar Casa ni otro corredor."
                if deps.identity.member_role == MemberRole.BROKER
                else "Cuenta predeterminada para operar: Casa. Puede seleccionar un corredor por nombre o consultar el consolidado."
            )
            if deps.reconciliation is not None:
                scope = "Para esta reconciliación puede elegir cualquier corredor del negocio; clientes personales ajenos siguen prohibidos a corredores."
            parts.append(f"Vertical: corredores. Ledger en USD. {scope}")
            accounts = await deps.brokerage_queries.accounts(b.id, deps.identity.member_id)
            parts.append("Cuentas autorizadas: " + ", ".join(row["name"] for row in accounts) + ".")
            parts.append(await deps.brokerage_queries.balances_line(b.id, deps.identity.member_id))
        else:
            rates = await deps.queries.rates_info(b.id, deps.now)
            if rates.get("available"):
                applied = rates["applied"]
                line = f"Tasas ({rates['unit']}). Tasa para cobrar: {applied['label']} {applied['rate']}"
                if rates.get("custom") and applied["mode"] == "custom":
                    c = rates["custom"]
                    line += f"{' (' + c['label'] + ')' if c.get('label') else ''}, fijada {c['set']}"
                    line += f", confirmada {applied['confirmed']}" if applied.get("confirmed") else ""
                line += f". Referencias: {rates['official_label']} {rates['official']['rate']} ({rates['official']['as_of']})"
                if rates.get("market_auto"):
                    line += f"; {rates['market_label']} {rates['market_auto']['rate']} ({rates['market_auto']['as_of']})"
                if funds := rates.get("funds"):
                    origin = "de un cambio" if funds.get("source") == "exchange" else "la dijo el dueño"
                    line += f". Tasa real de sus bolívares: {funds['rate']} ({origin}, {funds['set']})"
                parts.append(line + ". Nunca le pidas al dueño una tasa que ya tienes.")
            else:
                parts.append("Tasas: NO disponibles ahora mismo (si hace falta convertir, dile que fije su tasa para cobrar).")
            parts.append(await deps.queries.today_line(b.id, deps.now))
            policies = await deps.queries.policy_notes(b.id)
            if policies:
                parts.append("Reglas del negocio que el dueño contó: " + " · ".join(policies))
            subjects = (await deps.queries.known_names(b.id))["subjects"]
            mentioned = [s for s in subjects if s and s.lower() in deps.user_text.lower()]
            if mentioned:
                notes = await deps.queries.notes_for_subjects(b.id, mentioned)
                if notes:
                    parts.append("Memoria sobre lo mencionado: " + " · ".join(f"{n['subject']}: {n['text']}" for n in notes))
    except Exception:  # noqa: BLE001 — el contexto degrada, nunca rompe el turno
        log.exception("context.partial_failure")

    handles = await deps.store.list_handles(deps.scope)
    if deps.quoted_text is not None:
        parts.append("\n# EL DUEÑO ESTÁ RESPONDIENDO (CITANDO) ESTE MENSAJE")
        parts.append(f'"{deps.quoted_text[:600]}"')
        parts.append("La cita aporta contexto, no una orden de escritura. La intención y el cliente nombrado "
                     "explícitamente en el mensaje actual tienen prioridad sobre el sujeto de la cita. "
                     "Una explicación anterior del asistente puede ser incorrecta: contrástala con las consultas.")
        parts.append("Interpreta las aclaraciones junto con el registro citado. Si identifica a quién corresponde "
                     "la operación, resuelve esa identidad y corrige solo el cliente mediante la herramienta existente "
                     "cuando la reasignación sea inequívoca. Si falta confirmar la intención, pregunta por esa operación "
                     "y ese cliente concretos; no vuelvas a preguntar por montos o tasas que no cuestionó. "
                     "Una consulta o explicación sobre el cliente no solicita cambios.")
        parts.append("Reasignar el cliente de la operación citada usa edit_broker_movement con changes.customer_name. "
                     "No uses edit_customer_profile para esa reasignación: renombraría el perfil y todo su historial. "
                     "Si el cliente indicado ya existe, reutilízalo sin cambiar su perfil ni el del cliente anterior.")
        if deps.quoted_handles:
            parts.append("Registros vinculados a ese mensaje (puedes consultarlos; corrige solo si el usuario lo pide explícitamente):")
            parts.extend(h.line() for h in deps.quoted_handles)
        elif deps.quoted_deleted:
            parts.append("Los registros de ese mensaje YA FUERON BORRADOS: " + " · ".join(deps.quoted_deleted) + ". Díselo al dueño.")
        else:
            parts.append("Ese mensaje no tiene registros vinculados; úsalo solo como contexto.")
    other = [h for h in handles if h not in deps.quoted_handles][-12:]
    if other:
        parts.append("\n# Registros recientes referenciables (usa la referencia exacta para corregir/borrar)")
        parts.extend(h.line() for h in other)
    if deps.choice_note:
        parts.append(f"\n{deps.choice_note}")
    if b.vertical == BusinessVertical.BROKERAGE:
        draft = await deps.store.get_broker_money_draft(deps.scope)
        if draft is not None:
            visible = [{"operation_number": index, "command": item.command_kind,
                        "awaiting": "customer_name" if (item.command_payload.get("purchase_settlement_confirmed")
                            and item.command_payload.get("settlement") == "pending"
                            and not (item.command_payload.get("customer_name") or item.command_payload.get("customer_id"))) else "bolivars",
                        "data": {key: value for key, value in item.command_payload.items()
                                 if value is not None and not key.endswith("_id")
                                 and key not in {"money_evidence", "purchase_settlement_confirmed", "draft_origin_command_kind"}}}
                       for index, item in enumerate(draft.operations(), 1)]
            parts.append("\n# Operaciones SIN GUARDAR que esperan bolívares o cliente\n"
                         + json.dumps(visible, ensure_ascii=False)
                         + "\nSi responde con monto Bs, tasa o capture, usa resume_broker_operation. Una cifra sola responde Bs, "
                         "no inicia una compra propia. No reconstruyas ni repitas el registro original. "
                         "Si una compra pendiente ya tiene Bs/tasa y le pedimos el cliente, usa resume_broker_operation "
                         "con customer_name; conserva los demás datos y la modalidad elegida. "
                         "awaiting=customer_name significa que el único dato pedido es el cliente: envía solo "
                         "customer_name y operation_number si hace falta. NO uses record_broker_purchase para esa respuesta. "
                         "Si cancela, usa cancel_broker_operation. Si describe otra operación explícita, usa su herramienta normal. "
                         "Si hay varias operaciones esperando Bs, pregunta cuál corresponde y pasa operation_number. "
                         "Una imagen citada de un movimiento existente conserva su destino; si hay ambigüedad, pregunta.")
    if deps.discarded_choice:
        parts.append(f"\nNota: le habías ofrecido opciones («{deps.discarded_choice.question}») y el dueño escribió otra cosa; "
                     "esas opciones ya no valen. Si su mensaje las retoma, vuelve a ofrecerlas.")
    if deps.discarded_pending:
        parts.append(f"\nNota: había una confirmación pendiente que el dueño no respondió con sí/no y se descartó: "
                     f"{deps.discarded_pending.reason}. Si su mensaje la retoma, vuelve a proponerla.")
    if deps.lane == Lane.FAST:
        parts.append(FAST_LANE_NOTE)
    deps.context_text = "\n".join(parts)
    return deps.context_text


def history_from_window(window: list[WindowEntry]) -> list[ModelMessage]:
    """Reconstruye la memoria de corto plazo como mensajes neutrales (independiente del proveedor).

    Las herramientas del turno se replican como lo que fueron —`ToolCallPart` del asistente y `ToolReturnPart`
    de vuelta—, nunca como prosa. Un historial que aplana las tool calls en texto le enseña al modelo que su
    trabajo es *narrar* la acción en vez de *ejecutarla*; es un modo de fallo conocido, y es exactamente lo que
    pasó el 25-09-2026: el recibo determinista se guardaba como `assistant` y a los pocos turnos DeepSeek
    empezó a escribir «✅ Venta registrada» sin llamar `record_sale`.
    """
    messages: list[ModelMessage] = []
    for i, entry in enumerate(window):
        messages.append(ModelRequest(parts=[UserPromptPart(content=entry.user or "(mensaje sin texto)")]))
        calls = [c for c in (entry.calls or []) if c.get("tool")]
        if calls:
            ids = [f"h{i}_{n}" for n in range(len(calls))]
            messages.append(ModelResponse(parts=[
                ToolCallPart(tool_name=c["tool"], args=c.get("args") or {}, tool_call_id=tid)
                for c, tid in zip(calls, ids, strict=True)]))
            messages.append(ModelRequest(parts=[
                ToolReturnPart(tool_name=c["tool"], content=c.get("result") or "ok", tool_call_id=tid)
                for c, tid in zip(calls, ids, strict=True)]))
        # El texto del modelo va aparte y solo si lo hubo: en un turno de registro no escribe nada, y meter ahí
        # el recibo del sistema es justo la mentira que este módulo evita.
        if text := (entry.assistant or "").strip():
            messages.append(ModelResponse(parts=[TextPart(content=text)]))
        elif not calls:
            messages.append(ModelResponse(parts=[TextPart(content="(sin respuesta)")]))
        if hint := history_hint(entry.actions):
            messages.append(ModelRequest(parts=[UserPromptPart(content=hint)]))
    return messages
