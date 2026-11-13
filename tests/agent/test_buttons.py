"""Botones y listas de WhatsApp de punta a punta: confirmaciones, Deshacer, cliente ambiguo, ask_choice y onboarding.

Regla de costo que se prueba en cada caso: los botones nunca agregan un mensaje. Lo que sale bien al tocar se acusa
con una reacción sobre el toque (gratis, no es un mensaje del hilo); solo el fallo se responde con un mensaje.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select

from app.application.commands.catalog import SetProductPrice
from app.application.commands.customers import UpsertCustomer
from app.application.commands.records import EditRecord, RecordChanges
from app.application.context import Actor, ExecContext
from app.db.models import Business, Customer, Product, Receivable, Sale, SaleItem
from app.domain.enums import ActorType, Currency, EntityType, OnboardingState
from tests.agent.confirmations import ask_sale_customer_correction, sale_customer_name
from tests.agent.test_pipeline import PHONE, interp, onboard, send


def titles(out) -> list[str]:
    return [c["title"] for c in out.replies[0]["choices"]]


def ids(out) -> list[str]:
    return [c["id"] for c in out.replies[0]["choices"]]


async def count(session_factory, model) -> int:
    async with session_factory() as s:
        return (await s.execute(select(func.count()).select_from(model).where(model.deleted_at.is_(None)))).scalar_one()


async def panel_ctx(container) -> ExecContext:
    member = await container.identity.member_by_phone(PHONE)
    return ExecContext(business_id=member.business.id, actor=Actor(ActorType.PANEL))


CREDIT_SALE = {"tool": "record_sale", "args": {"items": [{"product": "caucho", "unit_price": 40, "currency": "USD"}],
                                               "payment_status": "credit", "customer_name": "Moisés"}}


# ------------------------------------------------------------------------------------------------ confirmaciones


async def test_confirmation_goes_as_one_message_with_yes_no_buttons_and_a_tap_executes(env, script, session_factory):
    container, console = env
    await onboard(container, script)
    sent_before = len(console.sent)
    ask = await ask_sale_customer_correction(container, script)
    assert len(ask.replies) == 1 and titles(ask) == ["Sí", "No"]
    assert len(console.sent) == sent_before + 1 and console.sent[-1].interactive is not None  # UN solo mensaje
    assert "¿Lo hago?" in ask.replies[0]["text"] and "Responde *sí*" not in ask.replies[0]["text"]

    calls = script.calls
    yes = await send(container, "Sí", button_id=ids(ask)[0])
    assert script.calls == calls and "Corregí" in yes.replies[0]["text"]
    assert await count(session_factory, Sale) == 1
    assert await sale_customer_name(session_factory) == "Pedro"

    again = await send(container, "Sí", button_id=ids(ask)[0])  # segundo toque del mismo botón
    assert again.replies == [] and await count(session_factory, Sale) == 1  # en silencio y sin duplicar


async def test_no_button_is_acknowledged_with_a_reaction_and_an_unknown_confirmation_says_so(env, script, session_factory):
    container, console = env
    await onboard(container, script)
    ask = await ask_sale_customer_correction(container, script)
    no = await send(container, "No", button_id=ids(ask)[1])
    assert no.replies == [] and await sale_customer_name(session_factory) == "Moisés"
    assert [emoji for _, emoji in console.reactions] == ["👍"]  # pero sí se acusa recibo

    reactions_before = len(console.reactions)
    stale = await send(container, "Sí", button_id="c:tokenviejo:yes")
    assert "ya no está vigente" in stale.replies[0]["text"] and await sale_customer_name(session_factory) == "Moisés"
    assert len(console.reactions) == reactions_before  # un botón que no hizo nada no se acusa


async def test_old_yes_button_does_not_confirm_a_newer_pending_action(env, script, session_factory):
    container, _ = env
    await onboard(container, script)
    first = await ask_sale_customer_correction(container, script, customer_name="Pedro")
    await ask_sale_customer_correction(container, script, customer_name="Luis")  # la primera se descartó
    old = await send(container, "Sí", button_id=ids(first)[0])
    assert "ya no está vigente" in old.replies[0]["text"] and await sale_customer_name(session_factory) == "Moisés"


async def test_long_confirmation_goes_as_plain_text_exactly_as_before(env, script, session_factory):
    container, console = env
    await onboard(container, script)
    items = [{"product": f"repuesto modelo {i:02d} para moto", "quantity": 1, "unit_price": 10 + i, "currency": "USD"} for i in range(30)]
    sent_before = len(console.sent)
    ask = await ask_sale_customer_correction(container, script, items=items)
    assert all(r["choices"] == [] for r in ask.replies)  # no cabe en un mensaje con botones → texto
    assert all(m.interactive is None for m in console.sent[sent_before:])
    assert ask.replies[-1]["text"].endswith("Responde *sí* o *no*.")
    yes = await send(container, "sí")  # y el sí escrito sigue funcionando
    assert "Corregí" in yes.replies[0]["text"]
    assert await sale_customer_name(session_factory) == "Pedro"


# ------------------------------------------------------------------------------------------------ deshacer


async def test_undo_button_deletes_with_a_reaction_instead_of_a_message_and_only_once(env, script, session_factory):
    container, console = env
    await onboard(container, script)
    script.lane = "fast"
    script.steps = [{"tool": "record_sale", "args": {"items": [{"product": "bujía NGK", "quantity": 2, "unit_price": 3, "currency": "USD"}]}},
                    {"text": "Ok."}]
    out = await send(container, "vendí 2 bujías NGK a 3$")
    assert titles(out) == ["Deshacer"] and "Venta registrada" in out.replies[0]["text"]
    sent_before = len(console.sent)
    undo = await send(container, "Deshacer", button_id=ids(out)[0])
    assert undo.replies == [] and await count(session_factory, Sale) == 0
    assert len(console.sent) == sent_before  # borró sin mandar un mensaje más
    assert [emoji for _, emoji in console.reactions] == ["👍"]

    again = await send(container, "Deshacer", button_id=ids(out)[0])  # segundo toque del mismo botón
    assert again.replies == [] and len(console.reactions) == 1  # ya no hay nada que acusar


async def test_undo_is_refused_if_the_record_was_edited_afterwards(env, script, session_factory):
    container, _ = env
    await onboard(container, script)
    script.lane = "fast"
    script.steps = [{"tool": "record_sale", "args": {"items": [{"product": "bujía NGK", "quantity": 2, "unit_price": 3, "currency": "USD"}]}},
                    {"text": "Ok."}]
    out = await send(container, "vendí 2 bujías NGK a 3$")
    async with session_factory() as s:
        sale = (await s.execute(select(Sale))).scalar_one()
    await container.bus.dispatch(EditRecord(entity_type=EntityType.SALE, entity_id=sale.id, changes=RecordChanges(quantity=Decimal(3))),
                                 await panel_ctx(container))
    undo = await send(container, "Deshacer", button_id=ids(out)[0])
    assert "No pude deshacerlo" in undo.replies[0]["text"] and await count(session_factory, Sale) == 1
    assert env[1].reactions == []  # el fallo se avisa con un mensaje, no con una reacción


# ------------------------------------------------------------------------------------------------ cliente ambiguo


async def _two_anas(container):
    ctx = await panel_ctx(container)
    for name in ("Ana Pérez", "Ana Gómez"):
        # An automatic ANA nickname would make "Ana" an exact identity, not
        # an ambiguous partial name. These fixtures intentionally have no alias.
        await container.bus.dispatch(UpsertCustomer(name=name, nickname=None), ctx)


async def test_ambiguous_customer_offers_candidates_and_a_tap_records_without_llm(env, script, session_factory):
    container, _ = env
    await onboard(container, script)
    await _two_anas(container)
    script.steps = [{**CREDIT_SALE, "args": {**CREDIT_SALE["args"], "customer_name": "Ana"}}, {"text": "Ok."}]
    ask = await send(container, "Ana se llevó un caucho de 40$ fiado")
    assert set(titles(ask)) >= {"Ana Pérez", "Ana Gómez"} and "Es un cliente nuevo" in titles(ask)
    assert await count(session_factory, Sale) == 0

    calls = script.calls
    gomez = ids(ask)[titles(ask).index("Ana Gómez")]
    done = await send(container, "Ana Gómez", button_id=gomez)
    assert script.calls == calls and "Venta registrada" in done.replies[0]["text"]
    async with session_factory() as s:
        rec = (await s.execute(select(Receivable))).scalar_one()
        assert (await s.get(Customer, rec.customer_id)).name == "Ana Gómez"


async def test_ambiguous_customer_can_be_answered_by_number_too(env, script, session_factory):
    container, _ = env
    await onboard(container, script)
    await _two_anas(container)
    script.steps = [{**CREDIT_SALE, "args": {**CREDIT_SALE["args"], "customer_name": "Ana"}}, {"text": "Ok."}]
    ask = await send(container, "Ana se llevó un caucho de 40$ fiado")
    n = titles(ask).index("Ana Pérez") + 1
    done = await send(container, str(n))
    assert "Venta registrada" in done.replies[0]["text"]
    async with session_factory() as s:
        rec = (await s.execute(select(Receivable))).scalar_one()
        assert (await s.get(Customer, rec.customer_id)).name == "Ana Pérez"


# ------------------------------------------------------------------------------------------------ ask_choice


async def test_agent_can_ask_with_options_and_the_tap_reaches_the_model(env, script):
    container, _ = env
    await onboard(container, script)
    script.steps = [{"tool": "ask_choice", "args": {"question": "¿De qué período?", "options": ["Hoy", "Esta semana", "Este mes"]}},
                    {"text": "Te pregunto para darte la cifra correcta."}]
    ask = await send(container, "¿cuánto vendí?")
    assert titles(ask) == ["Hoy", "Esta semana", "Este mes"] and "¿De qué período?" in ask.replies[0]["text"]
    assert "Te pregunto" not in ask.replies[0]["text"]  # el texto del modelo no duplica la pregunta

    script.steps = [{"text": "Esta semana vendiste…"}]
    await send(container, "Esta semana", button_id=ids(ask)[1])
    assert "El dueño eligió «Esta semana» como respuesta a tu pregunta «¿De qué período?»" in script.seen_instructions[-1]


async def test_long_choice_preserves_full_amount_and_currency_when_displayed_and_selected(env, script):
    container, console = env
    await onboard(container, script)
    options = ["Transferencia 1 (80.000,00 Bs)", "Transferencia 2 (90.000,00 Bs)"]
    script.steps = [{"tool": "ask_choice", "args": {
        "question": "¿Cuál monto del comprobante corresponde?", "options": options,
    }}]
    asked = await send(container, "Hay dos transferencias en el comprobante")
    displayed = asked.replies[0]["choices"]
    assert [choice["description"] for choice in displayed] == options
    transport = console.sent[-1].interactive
    assert transport is not None and not transport.as_buttons
    assert all(len(choice.title) <= 24 for choice in transport.clipped())
    assert [choice.description for choice in transport.clipped()] == options
    assert len(asked.replies) == 1
    script.steps = [{"text": "Necesito la operación para continuar."}]
    await send(container, transport.clipped()[1].title, button_id=displayed[1]["id"])
    assert f"El dueño eligió «{options[1]}»" in script.seen_instructions[-1]


async def test_writing_something_else_discards_the_options_from_the_thread(env, script):
    """Escribir otra cosa saca las opciones del hilo: el modelo se entera de que quedaron sin responder."""
    container, _ = env
    await onboard(container, script)
    script.steps = [{"tool": "ask_choice", "args": {"question": "¿De qué período?", "options": ["Hoy", "Esta semana"]}}, {"text": "Ok."}]
    await send(container, "¿cuánto vendí?")
    script.steps = [{"text": "Ok."}]
    await send(container, "mejor dime cuánto me debe Moisés")
    assert "le habías ofrecido opciones" in script.seen_instructions[-1]


async def test_a_late_tap_still_answers_the_question_it_belongs_to(env, script):
    """Sacarlas del hilo NO mata el botón: el dueño puede tocarlo después y su elección cuenta.

    Antes se descartaban con un `getdel`, así que cualquier mensaje intermedio dejaba el botón muerto y el
    toque solo conseguía "esas opciones ya no están vigentes". Así se perdió una venta de $11 el 25-09-2026:
    el dueño escribió otra venta y 44 s después tocó la opción del producto.
    """
    container, _ = env
    await onboard(container, script)
    script.steps = [{"tool": "ask_choice", "args": {"question": "¿De qué período?", "options": ["Hoy", "Esta semana"]}}, {"text": "Ok."}]
    ask = await send(container, "¿cuánto vendí?")
    script.steps = [{"text": "Ok."}]
    await send(container, "mejor dime cuánto me debe Moisés")

    script.steps = [{"text": "Hoy vendiste $0,00."}]
    late = await send(container, "Hoy", button_id=ids(ask)[0])
    assert "Hoy vendiste" in late.replies[0]["text"]
    assert "eligió «Hoy»" in script.seen_instructions[-1], "el modelo recibe la opción como respuesta a SU pregunta"


async def test_a_tap_from_another_day_is_not_replayed(env, script, monkeypatch):
    """Un toque de otro día no revive el comando congelado: registraría con la fecha y la tasa de hoy."""
    from datetime import UTC, datetime, timedelta

    container, _ = env
    await onboard(container, script)
    script.steps = [{"tool": "ask_choice", "args": {"question": "¿De qué período?", "options": ["Hoy", "Esta semana"]}}, {"text": "Ok."}]
    ask = await send(container, "¿cuánto vendí?")
    script.steps = [{"text": "Ok."}]
    await send(container, "mejor dime cuánto me debe Moisés")

    import json
    key = next(k for k in await container.redis.keys("*:choice:*"))
    raw = json.loads(await container.redis.get(key))
    raw["created_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    await container.redis.set(key, json.dumps(raw))

    stale = await send(container, "Hoy", button_id=ids(ask)[0])
    assert "ya no están vigentes" in stale.replies[0]["text"]


async def test_late_new_product_choice_executes_the_original_sale_not_an_interleaved_one(env, script, business, session_factory):
    """«Es un producto nuevo» congela nombre, precio y cantidad de SU venta.

    Regresión del 26-09-2026: tras intercalar otra venta, el toque tardío del
    producto nuevo terminó registrando $11 sobre una bujía sugerida.
    """
    container, _ = env
    await container.bus.dispatch(SetProductPrice(product_name="Aceite Mobil", price=Decimal(13), currency=Currency.USD),
                                 await panel_ctx(container))
    script.lane = "fast"
    script.steps = [{"tool": "record_sale", "args": {
        "items": [{"product": "Aceite", "quantity": 1, "unit_price": 11, "currency": "USD"}],
    }}]
    ask = await send(container, "Vendí un aceite a 11$")
    assert titles(ask) == ["Aceite Mobil", "Es un producto nuevo"]

    # La elección queda fuera del hilo, pero su token sigue resolviendo SU comando.
    script.steps = [{"tool": "record_sale", "args": {
        "items": [{"product": "Bujía QA", "quantity": 1, "unit_price": 1.2, "currency": "USD"}],
    }}]
    interleaved = await send(container, "Vendí una Bujía QA a 1.2$")
    assert "Bujía QA" in interleaved.replies[0]["text"]

    calls = script.calls
    new_id = ids(ask)[titles(ask).index("Es un producto nuevo")]
    resolved = await send(container, "Es un producto nuevo", button_id=new_id)
    assert script.calls == calls  # resolución determinista, sin reinterpretar el toque con el modelo
    assert "1 × Aceite a $11,00" in resolved.replies[0]["text"]

    async with session_factory() as session:
        rows = (await session.execute(
            select(SaleItem, Product).join(Product, SaleItem.product_id == Product.id).order_by(SaleItem.unit_price_amount)
        )).all()
    assert [(product.name, item.unit_price_amount) for item, product in rows] == [
        ("Bujía QA", Decimal("1.2000")),
        ("Aceite", Decimal("11.0000")),
    ]


# ------------------------------------------------------------------------------------------------ onboarding


async def test_onboarding_closed_questions_are_answered_with_taps_without_llm(env, script, session_factory):
    container, _ = env
    script.onboarding = [interp(name="Scalea", what_sells="software"), interp(payment_methods=["zelle"])]
    country = await send(container, "hola, soy de Scalea y vendemos software")
    assert titles(country) == ["Sí", "No"]
    rate = await send(container, "Sí", button_id=ids(country)[0])
    assert titles(rate) == ["BCV (la más común)", "Tasa de mercado", "Uso mi propia tasa"]
    methods = await send(container, "Tasa de mercado", button_id=ids(rate)[1])
    assert "Usaré la tasa de mercado vigente" in methods.replies[0]["text"]
    assert "¿Cómo te pagan normalmente?" in methods.replies[0]["text"] and methods.replies[0]["choices"] == []
    summary = await send(container, "zelle")
    assert titles(summary) == ["Guardar", "Cambiar algo"]
    await send(container, "Guardar", button_id=ids(summary)[0])
    async with session_factory() as s:
        b = (await s.execute(select(Business))).scalar_one()
    assert b.onboarding_state == OnboardingState.DONE.value and b.display_currency == "usd_applied"
    assert b.charge_rate_mode == "market"
    assert script.onboarding == []  # solo 2 llamadas al LLM (nombre+qué vende y métodos); los toques no lo usan


async def test_onboarding_own_rate_asks_the_rate_and_saves_it(env, script, session_factory):
    from app.db.models import CustomRate

    container, _ = env
    script.onboarding = [interp(name="Scalea", what_sells="software"), interp(payment_methods=["zelle"])]
    country = await send(container, "hola, soy de Scalea y vendemos software")
    rate = await send(container, "Sí", button_id=ids(country)[0])
    own = await send(container, "Uso mi propia tasa", button_id=ids(rate)[2])
    assert "¿Qué tasa estás usando hoy?" in own.replies[0]["text"]
    methods = await send(container, "800")  # un número: sin LLM
    assert "Usaré esta tasa para calcular cuánto cobrar" in methods.replies[0]["text"]
    summary = await send(container, "zelle")
    assert "*Tasa para cobrar:* tu propia tasa: 800,00 Bs/$" in summary.replies[0]["text"]
    await send(container, "Guardar", button_id=ids(summary)[0])
    async with session_factory() as s:
        b = (await s.execute(select(Business))).scalar_one()
        own_rate = (await s.execute(select(CustomRate).where(CustomRate.cleared_at.is_(None)))).scalar_one()
    assert b.onboarding_state == OnboardingState.DONE.value and b.charge_rate_mode == "custom"
    assert (own_rate.purpose, own_rate.rate) == ("charge", 800) and b.charge_rate_confirmed_on is not None
    assert script.onboarding == []


# ------------------------------------------------------------------------------------------------ tasas y cambios


async def test_bare_rate_asks_what_it_is_for_and_a_tap_saves_it_without_llm(env, script, session_factory):
    from app.db.models import CustomRate

    container, _ = env
    await onboard(container, script)
    script.steps = [{"tool": "set_rate", "args": {"rate": 800}}, {"text": "Ok."}]
    ask = await send(container, "pon la tasa en 800")
    assert titles(ask) == ["Para cobrar", "Tasa real de mis Bs", "Para ambas cosas"]
    assert "¿Para qué quieres usar la tasa de *800,00 Bs/$*?" in ask.replies[0]["text"]
    calls = script.calls
    done = await send(container, "Tasa real de mis Bs", button_id=ids(ask)[1])
    assert script.calls == calls  # el toque no pasa por el LLM
    assert "Esto no cambia la tasa con la que cobras" in done.replies[0]["text"]
    async with session_factory() as s:
        saved = (await s.execute(select(CustomRate))).scalar_one()
        b = (await s.execute(select(Business))).scalar_one()
    assert (saved.purpose, saved.rate) == ("funds", 800) and b.charge_rate_mode == "official"


async def test_own_rate_from_yesterday_is_confirmed_with_a_tap_before_recording(env, script, session_factory):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.db.models import CustomRate, Expense

    container, _ = env
    await onboard(container, script)
    async with session_factory() as s, s.begin():
        b = (await s.execute(select(Business))).scalar_one()
        s.add(CustomRate(business_id=b.id, purpose="charge", source="manual", rate=Decimal(300),
                         set_at=datetime.now(UTC) - timedelta(days=1)))
        await s.execute(update(Business).values(charge_rate_mode="custom", charge_rate_confirmed_on=None))
    script.steps = [{"tool": "record_expense", "args": {"description": "almuerzo", "amount": 3000, "category": "food",
                                                        "currency": "VES"}}, {"text": "Ok."}]
    ask = await send(container, "almuerzo 3000 bs")
    assert titles(ask) == ["Mantener 300,00", "Actualizar tasa"]
    assert "Tu tasa para cobrar es *300,00 Bs/$* y la fijaste ayer. ¿La mantenemos para hoy?" in ask.replies[0]["text"]
    assert await count(session_factory, Expense) == 0
    calls = script.calls
    kept = await send(container, "Mantener 300,00", button_id=ids(ask)[0])
    assert script.calls == calls and "Gasto registrado" in kept.replies[0]["text"] and "$10,00" in kept.replies[0]["text"]
    assert await count(session_factory, Expense) == 1


async def test_currency_exchange_is_recorded_with_its_real_rate_and_can_be_undone(env, script, session_factory):
    from app.db.models import CurrencyExchange

    container, _ = env
    await onboard(container, script)
    script.steps = [{"tool": "record_exchange", "args": {"out_amount": 200, "out_currency": "USD", "in_amount": 160000,
                                                         "in_currency": "VES", "out_method": "binance", "in_method": "binance"}},
                    {"text": "Ok."}]
    out = await send(container, "Vendí $200 y recibí 160.000 Bs por Binance")
    text = out.replies[0]["text"]
    assert "Cambio registrado*: salieron $200,00 y entraron 160.000,00 Bs por Binance" in text
    assert "Tu tasa real fue *800,00 Bs/$*" in text and titles(out) == ["Deshacer"]
    assert await count(session_factory, CurrencyExchange) == 1 and await count(session_factory, Sale) == 0
    await send(container, "Deshacer", button_id=ids(out)[0])
    assert await count(session_factory, CurrencyExchange) == 0
