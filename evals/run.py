"""Runner de evals contra los modelos REALES configurados (usa la base de desarrollo; cada caso crea su negocio).

  docker compose exec api python -m evals.run                  # todos
  docker compose exec api python -m evals.run --only sale_     # casos cuyo id contiene 'sale_'
  docker compose exec api python -m evals.run --repeat 3       # estabilidad

Escribe evals/results/<fecha>.json y .md con acierto por categoría, latencia y costo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from sqlalchemy import func, select

from app.agent.tools.registry import TOOLS
from app.application.commands.brokerage import CreateBrokerMember
from app.application.context import Actor, ExecContext
from app.application.conversation import WindowEntry, conversation_scope
from app.channels.console import ConsoleProvider
from app.config import get_settings
from app.container import build_container
from app.db.models import AgentTurn, Business, BusinessMember, LlmCall, ToolCall
from app.domain.enums import ActorType, OnboardingState
from app.logging import configure_logging
from evals.cases import CASES, Case
from evals.conversation import visible_reply_text


async def run_case(container, case: Case) -> dict[str, Any]:
    phone = "5899" + uuid.uuid4().hex[:8].translate(str.maketrans("abcdef", "123456"))
    async with container.session_factory() as s, s.begin():
        b = Business() if case.onboarding else Business(
            vertical=case.vertical.value,
            name="Eval Repuestos" if case.vertical.value == "retail" else "Eval Corredores",
            what_sells="repuestos de moto" if case.vertical.value == "retail" else "divisas",
            onboarding_state=OnboardingState.DONE.value, welcomed=True,
            payment_methods=["cash", "mobile_payment"])
        s.add(b)
        await s.flush()
        member = BusinessMember(
            business_id=b.id,
            phone=phone,
            name=case.member_name,
            role=case.member_role.value,
            abbreviation=case.member_abbreviation,
        )
        s.add(member)
        await s.flush()
        member_id = member.id
    ctx = ExecContext(business_id=b.id, actor=Actor(ActorType.PANEL, member_id))
    for cmd in case.seed:
        if isinstance(cmd, CreateBrokerMember):
            # El teléfono es único globalmente: aislar seeds también entre casos/repeticiones concurrentes.
            cmd = cmd.model_copy(update={"phone": "58414" + str(uuid.uuid4().int % 10**7).zfill(7)})
        ctx.now = datetime.now(UTC)  # each seed has a real chronology, even within the same business date
        await container.bus.dispatch(cmd, ctx)

    if case.prior_assistant_reply is not None:
        await container.store.append_window(conversation_scope(b.id, phone),
            WindowEntry(user="Explícame las ventas pendientes de María y Juan.", assistant=case.prior_assistant_reply))

    turn_ids, replies, last_reply_id = [], [], None
    turn_errors = []
    turn_replies = []
    started = time.monotonic()
    for i, text in enumerate(case.messages):
        reply_to = last_reply_id if (case.quote_last_reply_on_final and i == len(case.messages) - 1) else None
        inbound_id = await container.turns.save_inbound(
            provider="console", provider_message_id=f"eval-{uuid.uuid4()}", from_phone=phone, contact_name="Eval", type="image" if i in case.image_inputs else "text",
            body=text, transcript=None, media_url=None, media_mime_type="image/png" if i in case.image_inputs else None, reply_to_provider_message_id=reply_to, raw={})
        if i in case.image_inputs:
            data = (Path(__file__).parent / "fixtures" / case.image_inputs[i]).read_bytes()
            await container.attachments.ingest(business_id=b.id, uploaded_by_member_id=member_id,
                                               data=data, source_message_id=inbound_id)
        outcome = await container.pipeline.process(inbound_id)
        if outcome.status != "ok":
            turn_errors.append(f"turno {i + 1}: {outcome.status}")
        turn_ids.append(outcome.turn_id)
        replies.extend(visible_reply_text(r) for r in outcome.replies)
        turn_replies.append("\n\n".join(visible_reply_text(r) for r in outcome.replies))
        if outcome.replies:
            last_reply_id = outcome.replies[-1]["provider_message_id"]
    elapsed = time.monotonic() - started

    final_turn = turn_ids[-1]
    async with container.session_factory() as s:
        calls = (await s.execute(select(ToolCall).where(ToolCall.turn_id == turn_ids[case.tools_turn]).order_by(ToolCall.seq))).scalars().all()
        turn = await s.get(AgentTurn, final_turn)
        all_costs = (await s.execute(select(AgentTurn.cost_usd).where(AgentTurn.id.in_(turn_ids)))).scalars().all()

    ok_calls = [c for c in calls if c.outcome in ("ok", "needs_confirmation")]
    names = [c.tool_name for c in ok_calls]
    failures = list(turn_errors)
    for tool in case.expect_tools:
        if tool not in names:
            failures.append(f"falta {tool} (llamó: {[c.tool_name + ':' + c.outcome for c in calls]})")
    for tool in case.expect_attempted_tools:
        if not any(call.tool_name == tool for call in calls):
            failures.append(f"no propuso {tool} (llamó: {[c.tool_name + ':' + c.outcome for c in calls]})")
    for tool in case.forbid_tools:
        if tool in names:
            failures.append(f"no debía llamar {tool}")
    for tool in case.forbid_attempted_tools:
        if any(call.tool_name == tool for call in calls):
            failures.append(f"no debía proponer {tool}, ni siquiera si el comando fue rechazado")
    if case.expect_lane and (turn is None or turn.lane != case.expect_lane):
        failures.append(f"carril esperado {case.expect_lane}, obtuvo {turn.lane if turn else None}")
    if case.no_writes:
        # Las tools se registran al construir el container, después de importar este módulo.
        wrote = [c.tool_name for c in calls if c.tool_name in TOOLS and TOOLS[c.tool_name].kind == "write" and c.outcome == "ok"]
        if wrote:
            failures.append(f"no debía escribir: {wrote}")
    for tool, check in case.arg_checks.items():
        matching = [c for c in (calls if tool in case.expect_attempted_tools else ok_calls) if c.tool_name == tool]
        try:
            if matching and not any(check(c.args or {}) for c in matching):
                failures.append(f"args de {tool} no cumplen: {[c.args for c in matching]}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"check de {tool} falló: {exc!r} args={[c.args for c in matching]}")
    if case.db_check and not await case.db_check(container, b.id):
        failures.append("db_check falló")
    if case.reply_check and not case.reply_check(turn_replies):
        failures.append("reply_check falló: respuesta sin el contexto o alcance de negocio esperado")
    costs = [c for c in all_costs if c is not None]
    return {"id": case.id, "category": case.category, "passed": not failures, "failures": failures, "latency_s": round(elapsed, 2),
            "lane": turn.lane if turn else None, "grounding_ok": turn.grounding_ok if turn else None,
            "cost_usd": float(sum(costs)) if costs and len(costs) == len(all_costs) else None,
            "turn_ids": [str(t) for t in turn_ids],
            "tools": [f"{c.tool_name}:{c.outcome}" for c in calls], "reply": replies[-1] if replies else None,
            "turn_replies": turn_replies, "notes": case.notes}


async def llm_breakdown(container, turn_ids: list[str]) -> list[dict[str, Any]]:
    """Llamadas, tokens y gasto por modelo en toda la corrida.

    Va en el reporte y no solo en el JSON porque al comparar proveedores el costo es la mitad de la decisión.
    `sin_precio` distingue los dos motivos por los que el costo sale vacío: el modelo corrió pero no está en
    LLM_PRICE_OVERRIDES, o directamente no llegó a correr (la tabla sale vacía).
    """
    if not turn_ids:
        return []
    async with container.session_factory() as s:
        rows = (await s.execute(
            select(LlmCall.model, func.count().label("calls"), func.sum(LlmCall.input_tokens), func.sum(LlmCall.output_tokens),
                   func.sum(LlmCall.cost_usd), func.count(LlmCall.cost_usd))
            .where(LlmCall.turn_id.in_([uuid.UUID(t) for t in turn_ids]))
            .group_by(LlmCall.model).order_by(func.count().desc()))).all()
    return [{"model": model or "?", "calls": calls, "input_tokens": int(tin or 0), "output_tokens": int(tout or 0),
             "cost_usd": float(cost or 0), "unpriced_calls": calls - priced} for model, calls, tin, tout, cost, priced in rows]


def _cost_headline(llm: list[dict[str, Any]], cases: int) -> str:
    """Lo que costó la corrida, sumando las llamadas y no los turnos.

    Sumar `agent_turns.cost_usd` subestima: ese campo queda NULL cuando el turno no llamó al modelo (una respuesta
    determinista), y entonces el caso entero se descarta aunque sí se sepa lo que costó. Un turno sin llamadas cuesta 0,
    no «desconocido»; solo una llamada SIN precio vuelve parcial el total, y se dice cuál para poder arreglarlo.
    """
    if not llm:
        return "sin datos — no se registró NINGUNA llamada al modelo: los turnos fallaron antes de llegar al proveedor"
    spent = sum(r["cost_usd"] for r in llm)
    unpriced = sum(r["unpriced_calls"] for r in llm)
    calls = sum(r["calls"] for r in llm)
    if unpriced:
        missing = ", ".join(sorted({r["model"] for r in llm if r["unpriced_calls"]}))
        return (f"${spent:.5f} PARCIAL — {unpriced}/{calls} llamadas sin precio ({missing}): "
                "falta LLM_PRICE_OVERRIDES (ver docs/evaluation.md)")
    return f"${spent:.5f} en {calls} llamadas · ${spent / cases:.5f} por caso"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only")
    parser.add_argument("--case", action="append", dest="case_ids",
                        help="Exact case id; repeat this option to reproduce selected failures")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    selected_cases = [c for c in CASES if (not args.only or args.only in c.id)
                      and (not args.case_ids or c.id in args.case_ids)]
    unknown_case_ids = set(args.case_ids or ()) - {c.id for c in CASES}
    if unknown_case_ids:
        parser.error(f"Unknown case ids: {', '.join(sorted(unknown_case_ids))}")
    cases = selected_cases * args.repeat
    settings = get_settings()
    configure_logging("WARNING", json=False)
    container = build_container(settings, messaging=ConsoleProvider())
    await container.rates.refresh()
    sem = asyncio.Semaphore(args.concurrency)

    async def guarded(case: Case) -> dict[str, Any]:
        async with sem:
            try:
                return await run_case(container, case)
            except Exception as exc:  # noqa: BLE001
                return {"id": case.id, "category": case.category, "passed": False, "failures": [f"excepción: {exc!r}"]}

    results = await asyncio.gather(*(guarded(c) for c in cases))
    llm = await llm_breakdown(container, [t for r in results for t in r.get("turn_ids", [])])
    await container.close()

    by_cat: dict[str, list[bool]] = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["passed"])
    total = sum(r["passed"] for r in results)
    latencies = [r["latency_s"] for r in results if r.get("latency_s")]
    costs = [r["cost_usd"] for r in results if r.get("cost_usd") is not None]
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    summary = {"when": stamp, "models": {"router": settings.llm_role_router, "fast": settings.llm_role_fast, "agent": settings.llm_role_agent,
                                        "onboarding": settings.llm_role_onboarding, "vision": settings.llm_role_vision},
               "passed": total, "total": len(results), "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in by_cat.items()},
               "latency_median_s": median(latencies) if latencies else None, "latency_max_s": max(latencies) if latencies else None,
               "cost_known_usd": round(sum(costs), 5) if costs else None,
               "cost_run_usd": round(sum(r["cost_usd"] for r in llm), 5) if llm else None,
               "unpriced_calls": sum(r["unpriced_calls"] for r in llm), "llm_by_model": llm, "results": results}
    (out_dir / f"{stamp}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    # El costo va en el reporte, no solo en el JSON: es la mitad de la decisión al comparar modelos.
    costed = _cost_headline(llm, len(results))
    lines = [f"# Evals {stamp}", "", f"Modelos: {summary['models']}", "",
             f"**{total}/{len(results)} casos OK** · latencia mediana {summary['latency_median_s']}s · máx {summary['latency_max_s']}s",
             f"· costo {costed}", "",
             "| Categoría | OK |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in summary["by_category"].items()]
    if llm:
        lines += ["", "## Costo por modelo", "",
                  "| Modelo | Llamadas | Entrada | Salida | Costo | Sin precio |", "|---|---:|---:|---:|---:|---:|"]
        lines += [f"| {r['model']} | {r['calls']:,} | {r['input_tokens']:,} | {r['output_tokens']:,} | "
                  f"${r['cost_usd']:.4f} | {r['unpriced_calls'] or ''} |" for r in llm]
    lines += ["", "## Fallos", ""]
    for r in results:
        if not r["passed"]:
            lines.append(f"- **{r['id']}**: {'; '.join(r['failures'])}\n  - respuesta: {(r.get('reply') or '')[:300]!r}")
    (out_dir / f"{stamp}.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    asyncio.run(main())
