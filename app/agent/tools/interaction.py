"""Tools de interacción: preguntas cerradas que el dueño responde con un toque (botones o lista de WhatsApp)."""

import time
from typing import Any

from pydantic_ai import RunContext

from app.agent.deps import TurnDeps
from app.agent.tools.registry import FAST_AND_AGENT, _record, tool
from app.application.conversation import ChoiceOption, PendingChoice
from app.channels.types import BUTTON_TITLE_MAX, ROW_DESCRIPTION_MAX
from app.domain.enums import BusinessVertical, ToolOutcome
from app.domain.text import normalize_name

MAX_OPTIONS = 10
ALL_VERTICALS = frozenset(BusinessVertical)
QUESTION_MAX_CHARS = 300
OPTION_MAX_CHARS = ROW_DESCRIPTION_MAX


@tool("Ofrecer opciones concretas para elegir con un toque", kind="ui", lanes=FAST_AND_AGENT,
      verticals=ALL_VERTICALS)
async def ask_choice(ctx: RunContext[TurnDeps], question: str, options: list[str]) -> dict[str, Any]:
    """Muestra una pregunta cerrada con opciones que el dueño elige con un toque (o respondiendo el número).

    Úsala cuando necesites que el dueño elija entre opciones CONCRETAS y conocidas: cuál de varios productos o registros,
    qué período, qué método de pago. NO la uses para confirmar un registro (el sistema ya pide sí/no), ni para preguntas
    abiertas ("¿a cuánto lo vendiste?"), ni si solo hay una opción. Después de llamarla NO repitas la pregunta ni las
    opciones en tu texto: el sistema las muestra. Cuando el dueño elija, su respuesta llega como un mensaje normal.

    Args:
        question: La pregunta, corta y clara (ej. "¿Cuál cadena vendiste?").
        options: De 2 a 10 opciones, sin numerarlas. Prefiere títulos breves (20 caracteres).
            Hasta 72 caracteres se conservan como descripción de lista; nunca recortes importes ni identidades.
    """
    deps = ctx.deps
    started = time.monotonic()
    args = {"question": question, "options": options}
    seen: set[str] = set()
    clean: list[str] = []
    for option in options:
        text = " ".join(str(option).split()).strip(" .")
        if len(text) > OPTION_MAX_CHARS:
            out = {"ok": False, "error": "invalid",
                   "message": f"Acorta cada opción a {OPTION_MAX_CHARS} caracteres sin omitir el dato que la distingue."}
            await _record(deps, ctx, "ask_choice", args, {}, out, ToolOutcome.REJECTED, started)
            return out
        key = normalize_name(text)
        if text and key not in seen:
            seen.add(key)
            clean.append(text)
    if not question.strip() or not 2 <= len(clean) <= MAX_OPTIONS:
        out = {"ok": False, "error": "invalid",
               "message": f"Necesito una pregunta y entre 2 y {MAX_OPTIONS} opciones distintas. Si no, pregunta en texto."}
        await _record(deps, ctx, "ask_choice", args, {}, out, ToolOutcome.REJECTED, started)
        return out
    deps.choice = PendingChoice(question=question.strip(), options=[
        ChoiceOption(title=option, description=option if len(option) > BUTTON_TITLE_MAX else None)
        for option in clean
    ])
    out = {"ok": True, "status": "awaiting_choice",
           "instruction": "El sistema ya le mostró la pregunta y las opciones. No las repitas; no escribas nada más o solo una "
                          "frase corta de contexto."}
    deps.tool_results.append(out)
    await _record(deps, ctx, "ask_choice", args, {}, out, ToolOutcome.OK, started)
    return out


@tool("Pedir UN dato que falta para poder registrar", kind="ui", lanes=FAST_AND_AGENT,
      verticals=ALL_VERTICALS)
async def need_more_info(ctx: RunContext[TurnDeps], question: str) -> dict[str, Any]:
    """Pregunta abierta y corta cuando falta un dato imprescindible para registrar (a cuánto lo vendió, en qué
    moneda pagó, de qué cliente es el fiado).

    Es el ÚNICO camino para pedir un dato: el sistema manda la pregunta tal cual y tú no escribes nada más. Si
    las respuestas posibles son concretas y conocidas (cuál producto, qué método de pago), usa `ask_choice`.

    Args:
        question: La pregunta, una sola frase (ej. "¿A cuánto vendiste el filtro?").
    """
    deps = ctx.deps
    started = time.monotonic()
    args = {"question": question}
    text = " ".join(str(question).split()).strip()
    if not text:
        out = {"ok": False, "error": "invalid", "message": "Necesito la pregunta."}
        await _record(deps, ctx, "need_more_info", args, {}, out, ToolOutcome.REJECTED, started)
        return out
    deps.question = text[:QUESTION_MAX_CHARS]
    out = {"ok": True, "status": "asked",
           "instruction": "El sistema ya le envió la pregunta al dueño. No la repitas ni escribas nada más."}
    deps.tool_results.append(out)
    await _record(deps, ctx, "need_more_info", args, {}, out, ToolOutcome.OK, started)
    return out
