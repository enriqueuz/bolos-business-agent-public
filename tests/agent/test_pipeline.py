"""Pipeline completo con modelo con guion: registro, confirmación sin LLM, corrección por cita, grounding, onboarding."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.db.models import AgentTurn, Business, LlmCall, MessageEntityLink, Receivable, Sale, ToolCall
from app.domain.enums import OnboardingState
from tests.agent.harness import handle_in_quoted_section

PHONE = "584140000001"


async def test_parallel_duplicate_tool_calls_only_create_one_sale(env, script, business, session_factory):
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    args = {"items": [{"product": "Bujía", "quantity": 2, "unit_price": 3, "currency": "USD"}]}
    def duplicate_calls(messages, info):
        return ModelResponse(parts=[ToolCallPart("record_sale", args, tool_call_id="one"),
                                    ToolCallPart("record_sale", args, tool_call_id="two")])
    script.steps = [duplicate_calls, {"text": "Listo"}]
    c, _ = env
    out = await send(c, "Vendí 2 bujías a 3 dólares")
    assert out.status == "ok"
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Sale))).scalar_one() == 1


async def test_grounding_revision_has_no_write_tools(env, script, business, session_factory):
    """La revisión de grounding corre en modo solo-lectura: no puede registrar nada.

    Se usa una CONSULTA y no un registro porque un turno que solo registra se cierra sin pedirle texto al modelo
    (ver tests/agent/test_single_model_call.py), y sin texto no hay nada que revisar.
    """
    from pydantic_ai.messages import ModelResponse, TextPart

    def revise(messages, info):
        assert all(tool.name not in {"record_sale", "record_expense", "delete_record"} for tool in info.function_tools)
        return ModelResponse(parts=[TextPart("Hoy no has vendido nada.")])
    script.steps = [{"tool": "query_metrics", "args": {"metrics": ["sales_total"], "period": "today"}},
                    {"text": "Vendiste 987654 dólares."}, revise]
    c, _ = env
    out = await send(c, "¿cuánto vendí hoy?")
    assert out.status == "ok" and script.calls == 3
    assert "987654" not in out.replies[0]["text"]


async def send(container, text: str | None, *, phone: str = PHONE, reply_to: str | None = None, name: str = "Enrique",
               button_id: str | None = None, kind: str = "text", media_url: str | None = None,
               media_mime_type: str | None = None):
    inbound_id = await container.turns.save_inbound(
        provider="console", provider_message_id=f"in-{uuid.uuid4()}", from_phone=phone, contact_name=name, type=kind, body=text,
        transcript=None, media_url=media_url, media_mime_type=media_mime_type, reply_to_provider_message_id=reply_to,
        interactive_reply_id=button_id, raw={})
    return await container.pipeline.process(inbound_id)


def interp(reply: str = "", confirmed: bool = False, **updates) -> dict:
    return {"updates": updates, "confirmed": confirmed, "reply": reply}


async def onboard(container, script):
    script.onboarding = [interp(), interp(name="Repuestos El Rayo"), interp(what_sells="repuestos de moto"),
                         interp(charge_rate_mode="official"), interp(payment_methods=["cash", "mobile_payment"])]
    for text in ("hola", "Repuestos El Rayo", "repuestos de moto", "sí", "1", "efectivo en dólares y pago móvil"):
        last = await send(container, text)
    assert "¿Lo guardo así?" in last.replies[0]["text"]
    return await send(container, "sí")


async def business_row(session_factory) -> Business:
    async with session_factory() as s:
        return (await s.execute(select(Business))).scalar_one()


async def test_onboarding_form_saves_only_after_confirmation(env, script, session_factory):
    container, console = env
    script.onboarding = [interp(), interp(name="Repuestos El Rayo"), interp(what_sells="repuestos de moto"),
                         interp(charge_rate_mode="official"), interp(payment_methods=["cash", "mobile_payment"])]
    first = await send(container, "hola")
    assert "Soy *Bolos*" in first.replies[0]["text"] and "¿Cómo se llama tu negocio?" in first.replies[0]["text"]
    for text in ("Repuestos El Rayo", "repuestos de moto"):
        await send(container, text)
    country = await send(container, "sí")  # "¿Tu negocio está en Venezuela?": el sí no pasa por el LLM
    assert "¿qué tasa usas para calcular cuánto cobrar?" in country.replies[0]["text"]
    rate = await send(container, "1")
    assert "Usaré la tasa BCV vigente para calcular cuánto cobrar" in rate.replies[0]["text"]
    summary = await send(container, "efectivo en dólares y pago móvil")
    text = summary.replies[0]["text"]
    assert "Repuestos El Rayo" in text and "*Tasa para cobrar:* tasa BCV" in text and "Pago móvil" in text
    b = await business_row(session_factory)
    assert b.name is None and b.onboarding_state == OnboardingState.IN_PROGRESS.value  # nada guardado sin confirmar

    done = await send(container, "sí")
    assert "Ya puedes usarme" in done.replies[0]["text"] and done.lane == "deterministic"  # el "sí" no pasa por el LLM
    b = await business_row(session_factory)
    assert b.name == "Repuestos El Rayo" and b.what_sells == "repuestos de moto" and b.onboarding_state == OnboardingState.DONE.value
    assert b.display_currency == "usd_applied" and b.charge_rate_mode == "official"
    assert b.payment_methods == ["cash", "mobile_payment"]
    assert b.country_code == "VE" and b.declared_country == "Venezuela"
    async with session_factory() as s:
        roles = (await s.execute(select(LlmCall.role))).scalars().all()
    assert roles == ["onboarding"] * 5


async def test_onboarding_correction_of_an_earlier_answer(env, script, session_factory):
    """Caso real: el dueño corrige el nombre mientras se le preguntaba qué vende."""
    container, _ = env
    script.onboarding = [interp(), interp(name="Mi tienda"), interp("¡Corregido!", name="Scalea")]
    await send(container, "hola")
    await send(container, "Mi tienda")
    out = await send(container, "no no, mi negocio se llama scalea")
    reply = out.replies[0]["text"]
    assert reply.startswith("¡Corregido!") and "¿Qué vendes?" in reply
    assert (await business_row(session_factory)).onboarding_draft["name"] == "Scalea"
    instructions = script.onboarding_instructions[-1]
    assert "name: Mi tienda" in instructions and "se le preguntó al dueño fue qué vende" in instructions
    assert "Dueño: Mi tienda" in instructions  # la conversación previa llega al intérprete


async def test_onboarding_greeting_is_never_taken_as_name(env, script, session_factory):
    container, _ = env
    script.onboarding = [interp(), interp(name="hola")]  # aunque el intérprete se equivoque
    await send(container, "hola")
    out = await send(container, "hola")
    assert "¿Cómo se llama tu negocio?" in out.replies[0]["text"]
    assert (await business_row(session_factory)).onboarding_draft.get("name") is None


async def test_onboarding_changes_at_summary_are_applied_and_shown_again(env, script, session_factory):
    container, _ = env
    await onboard_until_summary(container, script)
    script.onboarding = [interp(charge_rate_mode="market")]
    changed = await send(container, "no, mejor cobro a la de mercado")
    assert "*Tasa para cobrar:* tasa de mercado" in changed.replies[0]["text"]
    assert (await business_row(session_factory)).onboarding_state == OnboardingState.IN_PROGRESS.value

    script.onboarding = [interp("¡Perfecto!", confirmed=True)]
    done = await send(container, "así está bien pues")
    assert "Ya puedes usarme" in done.replies[0]["text"]
    b = await business_row(session_factory)
    assert b.charge_rate_mode == "market" and b.onboarding_state == OnboardingState.DONE.value


async def test_onboarding_question_at_summary_does_not_repeat_it(env, script):
    container, _ = env
    await onboard_until_summary(container, script)
    script.onboarding = [interp("Puedes cambiarlo cuando quieras desde el panel.")]
    out = await send(container, "¿y eso lo puedo cambiar después?")
    text = out.replies[0]["text"]
    assert "desde el panel" in text and "respóndeme *sí* para guardar" in text and "Revisa que esté todo bien" not in text


async def test_onboarding_interpreter_failure_changes_nothing(env, script, session_factory):
    container, _ = env
    script.onboarding = [interp()]
    await send(container, "hola")
    out = await send(container, "Repuestos El Rayo")  # sin pasos: el modelo "falla"
    assert out.replies[0]["text"].startswith("Perdona, no te entendí") and "¿Cómo se llama tu negocio?" in out.replies[0]["text"]
    assert (await business_row(session_factory)).onboarding_draft.get("name") is None


async def test_onboarding_intro_survives_identity_created_before_first_message(env, script, session_factory):
    """El panel crea negocio+miembro al hacer login, antes de cualquier mensaje: la presentación igual sale una vez."""
    container, _ = env
    identity = await container.identity.resolve(PHONE)
    assert identity is not None and not identity.business.welcomed
    script.onboarding = [interp(name="Ferretería La Esquina"), interp(what_sells="ferretería")]
    first = await send(container, "hola, mi negocio es Ferretería La Esquina")
    assert "Soy *Bolos*" in first.replies[0]["text"] and "¿Qué vendes?" in first.replies[0]["text"]
    second = await send(container, "tornillos y herramientas")
    assert "Soy *Bolos*" not in second.replies[0]["text"]
    b = await business_row(session_factory)
    assert b.welcomed is True and b.onboarding_draft["name"] == "Ferretería La Esquina"


async def test_onboarding_asks_country_and_keeps_an_unsupported_one(env, script, session_factory):
    container, _ = env
    script.onboarding = [interp(name="Scalea", what_sells="software")]
    first = await send(container, "hola, soy de Scalea y vendemos software")
    assert "¿Tu negocio está en Venezuela?" in first.replies[0]["text"]  # el prefijo +58 solo sugiere
    asked = await send(container, "no")  # sin LLM
    assert "¿En qué país está tu negocio?" in asked.replies[0]["text"]
    script.onboarding = [interp(country="Colombia")]
    out = await send(container, "Colombia")
    text = out.replies[0]["text"]
    assert "la moneda de Colombia todavía no está" in text and "*dólares*" in text
    assert "¿Cómo te pagan normalmente?" in text  # la tasa para cobrar ya no se pregunta: registra en dólares
    script.onboarding = [interp(payment_methods=["zelle"])]
    summary = await send(container, "zelle")
    assert "Colombia (registras en dólares)" in summary.replies[0]["text"]
    await send(container, "sí")
    b = await business_row(session_factory)
    assert b.onboarding_state == OnboardingState.DONE.value and b.declared_country == "Colombia"
    assert b.country_code == "VE" and b.display_currency == "usd_applied"  # sigue en el perfil por defecto, en USD


async def onboard_until_summary(container, script):
    script.onboarding = [interp(name="Scalea", what_sells="software"), interp(charge_rate_mode="official"),
                         interp(payment_methods=["transfer"])]
    for text in ("hola, soy de Scalea y vendemos software", "sí", "1", "transferencia"):
        last = await send(container, text)
    assert "¿Lo guardo así?" in last.replies[0]["text"]


async def test_unknown_phone_gets_gate_without_llm(env):
    container, _ = env
    out = await send(container, "hola", phone="584149999999")
    assert "acceso privado" in out.replies[0]["text"]


async def test_fast_lane_sale_records_receipt_links_and_observability(env, script, session_factory):
    container, _ = env
    await onboard(container, script)
    script.lane = "fast"
    script.steps = [
        {"tool": "record_sale", "args": {"items": [{"product": "bujía NGK", "quantity": 2, "unit_price": 3, "currency": "USD"}],
                                         "payment_method": "cash"}},
        {"text": "Listo 👍"},
    ]
    out = await send(container, "vendí 2 bujías NGK a 3$ en efectivo")
    reply = out.replies[0]["text"]
    assert "Venta registrada" in reply and "$6,00" in reply
    assert out.lane == "fast"
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Sale))).scalar_one() == 1
        tc = (await s.execute(select(ToolCall))).scalar_one()
        assert tc.tool_name == "record_sale" and tc.outcome == "ok" and tc.injected_args["business_id"]
        links = (await s.execute(select(MessageEntityLink))).scalars().all()
        assert {link.message_kind for link in links} == {"inbound", "outbound"}
        turn = (await s.execute(select(AgentTurn).where(AgentTurn.lane == "fast"))).scalar_one()
        assert turn.status == "ok" and turn.grounding_ok is True
    window = await container.store.get_window(f"conv:{(await container.identity.member_by_phone(PHONE)).business.id}:{PHONE}")
    assert window[-1].user.startswith("vendí")


async def test_confirmation_is_resolved_without_llm(env, script, session_factory):
    from tests.agent.confirmations import ask_sale_customer_correction, sale_customer_name

    container, _ = env
    await onboard(container, script)
    ask = await ask_sale_customer_correction(container, script)
    assert "¿Lo hago?" in ask.replies[0]["text"]
    assert await sale_customer_name(session_factory) == "Moisés"  # preview did not persist the edit
    calls_before = script.calls
    yes = await send(container, "sí")
    assert script.calls == calls_before  # el "sí" no pasó por el modelo
    assert "Corregí" in yes.replies[0]["text"]
    assert await sale_customer_name(session_factory) == "Pedro"
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Receivable))).scalar_one() == 1


async def test_non_yes_no_discards_pending(env, script, session_factory):
    container, _ = env
    await onboard(container, script)
    script.steps = [
        {"tool": "delete_record", "args": {"handle": "R99"}},
        {"text": "No encontré ese registro."},
    ]
    await send(container, "borra la venta de ayer")
    assert script.seen_tool_returns and "unknown_handle" in str(script.seen_tool_returns[-1])


async def test_correction_by_quoting_receipt(env, script, session_factory):
    container, _ = env
    await onboard(container, script)
    script.steps = [
        {"tool": "record_sale", "args": {"items": [{"product": "cadena 428", "unit_price": 12, "currency": "USD"}],
                                         "payment_status": "credit", "customer_name": "Moisés", "force_new_customer": True}},
        {"text": "👍"},
    ]
    confirmed = await send(container, "fiado a Moisés una cadena de 12$")
    receipt_id = confirmed.replies[0]["provider_message_id"]

    # Un mensaje sin relación en medio, para que el registro NO sea "el último"
    script.steps = [{"text": "¡Hola!"}]
    await send(container, "buenas")

    def edit(messages, info):
        from pydantic_ai.messages import ModelResponse, ToolCallPart
        return ModelResponse(parts=[ToolCallPart("edit_record", {"handle": handle_in_quoted_section(info),
                                                                 "changes": {"customer_name": "Pedro"}})])

    script.steps = [edit, {"text": "👍"}]
    out = await send(container, "no era Moisés, era Pedro", reply_to=receipt_id)
    reply = out.replies[0]["text"]
    assert "Corregí" in reply and "Pedro" in reply  # aplicado directo: el objetivo fue citado
    async with session_factory() as s:
        sale = (await s.execute(select(Sale))).scalar_one()
        rec = (await s.execute(select(Receivable))).scalar_one()
        assert sale.customer.name == "Pedro" and rec.customer.name == "Pedro"


async def test_grounding_retries_invented_numbers(env, script, session_factory):
    container, _ = env
    await onboard(container, script)
    script.steps = [
        {"tool": "query_metrics", "args": {"metrics": ["sales_total"], "period": "today"}},
        {"text": "Hoy vendiste $1.234,00 🎉"},  # inventado
        {"text": "Hoy no has registrado ventas todavía."},
    ]
    out = await send(container, "¿cuánto vendí hoy?")
    assert out.replies[0]["text"] == "Hoy no has registrado ventas todavía."
    async with session_factory() as s:
        turn = (await s.execute(select(AgentTurn).where(AgentTurn.lane == "agent"))).scalar_one()
        assert turn.grounding_ok is True and turn.model_text == "Hoy no has registrado ventas todavía."


async def test_menu_is_static(env, script):
    container, _ = env
    await onboard(container, script)
    out = await send(container, "menú")
    assert "Esto es lo que sé hacer" in out.replies[0]["text"]


async def test_tool_domain_error_reaches_model_and_nothing_is_saved(env, script, session_factory):
    container, _ = env
    await onboard(container, script)
    script.steps = [
        {"tool": "record_sale", "args": {"items": [{"product": "kit de arrastre"}]}},
        {"text": "¿A cuánto lo vendiste?"},
    ]
    out = await send(container, "vendí un kit de arrastre")
    assert "¿A cuánto lo vendiste?" in out.replies[0]["text"]
    assert "missing" in str(script.seen_tool_returns[-1])
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Sale))).scalar_one() == 0


async def test_duplicate_tool_call_in_same_turn_is_blocked(env, script, session_factory):
    container, _ = env
    await onboard(container, script)
    args = {"description": "luz", "amount": 20, "currency": "USD", "category": "utilities"}
    script.steps = [{"tool": "record_expense", "args": args}, {"tool": "record_expense", "args": args}, {"text": "Listo"}]
    await send(container, "pagué 20$ de luz")
    from app.db.models import Expense
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Expense))).scalar_one() == 1
