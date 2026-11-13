"""Onboarding como formulario: el LLM interpreta, el código valida, guarda y decide qué preguntar.

- Las respuestas viven en `businesses.onboarding_draft`; las columnas reales se escriben SOLO cuando el dueño
  aprueba el resumen final.
- Cada mensaje pasa por el intérprete (puerto) con los datos ya capturados y la conversación reciente. Puede llenar
  o corregir CUALQUIER campo, no solo el que se preguntó, y devuelve un acuse corto.
- La siguiente pregunta y el resumen son plantillas: el flujo no depende de que el modelo se acuerde de preguntar.
- El país sugerido por el prefijo telefónico SIEMPRE se confirma ("¿Tu negocio está en Venezuela?"); si es otro país
  no soportado, se guarda lo que dijo y registra en dólares.
- Un "sí" claro al resumen (o un sí/no a la pregunta del país) se resuelve sin LLM. Si el intérprete falla, no se cambia nada y se repite la pregunta.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from app.application.bus import CommandBus
from app.application.choices import NO, ONBOARDING, YES, Choice, onboarding_id, parse_button
from app.application.commands.business import UpdateBusiness
from app.application.commands.rates import SetRate
from app.application.context import Actor, ExecContext
from app.application.conversation import ConversationStore, WindowEntry, conversation_scope
from app.application.identity import Identity
from app.domain.countries import CountryProfile, country_profile, profile_for_country_name
from app.domain.enums import (
    ActorType,
    ChargeRateMode,
    CurrencyView,
    OnboardingState,
    PaymentMethod,
    RateScope,
)
from app.domain.money import fmt_rate, to_decimal
from app.domain.text import extract_numbers, is_affirmative, is_negative, normalize_name

INTRO = (
    "¡Hola{name}! 👋 Soy *Bolos*, tu asistente para llevar las cuentas del negocio por WhatsApp.\n\n"
    "Me dices lo que vendes, gastas, compras y lo que te quedan debiendo, y yo lo anoto, calculo en {local_plural} "
    "y dólares, y puedes consultar tus registros y resultados del negocio.\n\n"
    "Antes, unos datos rápidos para configurarte."
)
# País no soportado: se guarda lo que dijo y puede usar Bolos en dólares (su moneda local todavía no existe).
UNSUPPORTED_COUNTRY = (
    "Por ahora solo manejo {local_plural} y dólares, así que la moneda de {country} todavía no está 🙏. "
    "Igual puedes usarme registrando todo en *dólares* 💵, y pronto sumaremos más países."
)
NOT_UNDERSTOOD = "Perdona, no te entendí bien 🙈."
ASK_CHANGE = "Dime qué dato quieres cambiar, o respóndeme *sí* para guardar."
SUMMARY = (
    "Revisa que esté todo bien:\n\n"
    "• *Negocio:* {name}\n"
    "• *Vendes:* {what_sells}\n"
    "• *País:* {country}\n"
    "• *Tasa para cobrar:* {rate}\n"
    "• *Te pagan con:* {methods}\n\n"
    "¿Lo guardo así? Respóndeme *sí*, o dime qué cambio."
)
SUMMARY_ASK_TEXT = "¿Lo guardo así? Respóndeme *sí*, o dime qué cambio."
SUMMARY_ASK_BUTTONS = "¿Lo guardo así?"
SUMMARY_CHOICES = (Choice(onboarding_id("summary", "save"), "Guardar"), Choice(onboarding_id("summary", "change"), "Cambiar algo"))
DONE = (
    "¡Listo! ✅ Ya puedes usarme. Háblame normal, por ejemplo:\n\n"
    "• _Vendí 2 {example} a 3$ en efectivo_\n"
    "• _Moisés se llevó un caucho de 40$ fiado_\n"
    "• _Pagué 25$ de luz_\n"
    "• _Compré 10 cadenas a 8$ cada una a mi proveedor_\n"
    "• _¿Cuánto vendí hoy?_ · _¿Cuánto me debe Moisés?_ · _Cierra caja_\n\n"
    "Si me equivoco, respóndeme citando el mensaje y dime qué cambiar. Escribe *menú* cuando quieras ver todo lo que sé hacer."
)

_SENTENCE = re.compile(r"[^.!?…]*(?:[.!?…]+|$)")

# Frases que nunca son el nombre de un negocio, aunque el intérprete se equivoque.
_NOT_A_NAME = {normalize_name(w) for w in (
    "hola", "holaa", "buenas", "buenos dias", "buenas tardes", "buenas noches", "hey", "epa", "saludos", "que tal",
    "hola que tal", "si", "no", "ok", "listo", "dale", "gracias",
)}


class OnboardingField(StrEnum):
    NAME = "name"
    WHAT_SELLS = "what_sells"
    COUNTRY = "country"  # "¿Tu negocio está en <país sugerido por el prefijo>?"
    OTHER_COUNTRY = "other_country"  # solo si dijo que no: "¿En qué país está?"
    CHARGE_RATE = "charge_rate_mode"  # con qué tasa cobra en moneda local lo que tiene precio en $
    CUSTOM_CHARGE_RATE = "custom_charge_rate"  # solo si usa su propia tasa: "¿Qué tasa estás usando hoy?"
    PAYMENT_METHODS = "payment_methods"


def charge_rate_options(profile: CountryProfile) -> list[tuple[str, ChargeRateMode]]:
    """Opciones numeradas tal como se muestran al dueño: la pregunta y el intérprete usan la MISMA tabla."""
    modes = [ChargeRateMode.OFFICIAL] + ([ChargeRateMode.MARKET] if profile.has_market_rate else []) + [ChargeRateMode.CUSTOM]
    return [(str(i), mode) for i, mode in enumerate(modes, start=1)]


def charge_rate_title(mode: ChargeRateMode, profile: CountryProfile) -> str:
    """Título corto (cabe en un botón de WhatsApp: 20 caracteres)."""
    return {ChargeRateMode.OFFICIAL: f"{profile.official_rate_label} (la más común)",
            ChargeRateMode.MARKET: "Tasa de mercado",
            ChargeRateMode.CUSTOM: "Uso mi propia tasa"}[mode]


def charge_rate_ack(draft: OnboardingDraft, profile: CountryProfile) -> str:
    """Lo que Bolos promete hacer con la tasa elegida (se dice al elegirla)."""
    local = profile.local.plural
    if draft.charge_rate_mode == ChargeRateMode.CUSTOM:
        return (f"Usaré esta tasa para calcular cuánto cobrar en {local} y para preparar tus reportes y cierres. "
                "Te preguntaré si quieres actualizarla cuando haya quedado desactualizada.")
    which = f"la tasa {profile.official_rate_label}" if draft.charge_rate_mode == ChargeRateMode.OFFICIAL else "la tasa de mercado"
    return (f"Usaré {which} vigente para calcular cuánto cobrar en {local} y para preparar tus reportes y cierres. "
            "Guardaré la tasa exacta utilizada en cada movimiento.")


def payment_examples(profile: CountryProfile) -> str:
    return ", ".join((profile.payment_label(m.value) or m.value).lower() for m in
                     (PaymentMethod.CASH, PaymentMethod.MOBILE_PAYMENT, PaymentMethod.CARD, PaymentMethod.ZELLE))


def _charge_rate_question(profile: CountryProfile) -> str:
    return (f"*Cuando tienes un precio en dólares y te pagan en {profile.local.plural}, ¿qué tasa usas para calcular "
            "cuánto cobrar?*")


def question(f: OnboardingField, profile: CountryProfile) -> str:
    if f == OnboardingField.NAME:
        return "*¿Cómo se llama tu negocio?*"
    if f == OnboardingField.WHAT_SELLS:
        return "*¿Qué vendes?* (por ejemplo: repuestos de moto, comida, ropa…)"
    if f == OnboardingField.COUNTRY:
        return f"*¿Tu negocio está en {profile.name}?* {_flag(profile.code)} Responde *sí* o *no*."
    if f == OnboardingField.OTHER_COUNTRY:
        return "*¿En qué país está tu negocio?*"
    if f == OnboardingField.CHARGE_RATE:
        options = charge_rate_options(profile)
        lines = [f"{n}️⃣ {charge_rate_title(m, profile)}" for n, m in options]
        numbers = ", ".join(n for n, _ in options[:-1]) + f" o {options[-1][0]}"
        return _charge_rate_question(profile) + "\n" + "\n".join(lines) + f"\n\nResponde {numbers}."
    if f == OnboardingField.CUSTOM_CHARGE_RATE:
        return f"*¿Qué tasa estás usando hoy?* (cuántos {profile.local.symbol} por dólar, por ejemplo: 800)"
    return f"*¿Cómo te pagan normalmente?* Dime todos, por ejemplo: _{payment_examples(profile)}_."


def question_with_buttons(f: OnboardingField, profile: CountryProfile) -> tuple[str, tuple[Choice, ...]] | None:
    """Las preguntas cerradas del onboarding, en versión botones (sin "responde 1, 2 o 3"). None = va en texto."""
    if f == OnboardingField.COUNTRY:
        return (f"*¿Tu negocio está en {profile.name}?* {_flag(profile.code)}",
                (Choice(onboarding_id("country", YES), "Sí"), Choice(onboarding_id("country", NO), "No")))
    if f == OnboardingField.CHARGE_RATE:
        return (_charge_rate_question(profile),
                tuple(Choice(onboarding_id("charge", mode.value), charge_rate_title(mode, profile))
                      for _, mode in charge_rate_options(profile)))
    return None


@dataclass(frozen=True, slots=True)
class OnboardingUpdates:
    name: str | None = None
    what_sells: str | None = None
    charge_rate_mode: ChargeRateMode | None = None
    custom_charge_rate: Decimal | None = None
    payment_methods: list[PaymentMethod] | None = None
    in_suggested_country: bool | None = None  # respuesta a "¿Tu negocio está en <país sugerido>?"
    country: str | None = None  # país que nombró el dueño


# Borradores anteriores a la pregunta de la tasa para cobrar: su "moneda de resúmenes" ya decía con qué tasa veía el $.
_LEGACY_VIEW_TO_MODE = {"usd_official": ChargeRateMode.OFFICIAL, "usd_market": ChargeRateMode.MARKET}


@dataclass(frozen=True, slots=True)
class OnboardingDraft:
    name: str | None = None
    what_sells: str | None = None
    charge_rate_mode: ChargeRateMode | None = None
    custom_charge_rate: Decimal | None = None
    payment_methods: tuple[PaymentMethod, ...] = ()
    country_code: str | None = None  # país SOPORTADO que confirmó el dueño
    other_country: str | None = None  # país NO soportado que declaró (registra en USD)
    not_in_suggested: bool = False  # dijo que no al país sugerido y aún no dijo cuál

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> OnboardingDraft:
        mode = data.get("charge_rate_mode")
        return cls(name=data.get("name"), what_sells=data.get("what_sells"),
                   charge_rate_mode=ChargeRateMode(mode) if mode else _LEGACY_VIEW_TO_MODE.get(data.get("display_currency") or ""),
                   custom_charge_rate=Decimal(data["custom_charge_rate"]) if data.get("custom_charge_rate") else None,
                   payment_methods=tuple(PaymentMethod(m) for m in data.get("payment_methods") or ()),
                   country_code=data.get("country_code"), other_country=data.get("other_country"),
                   not_in_suggested=bool(data.get("not_in_suggested")))

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "what_sells": self.what_sells,
                "charge_rate_mode": self.charge_rate_mode.value if self.charge_rate_mode else None,
                "custom_charge_rate": str(self.custom_charge_rate) if self.custom_charge_rate else None,
                "payment_methods": [m.value for m in self.payment_methods],
                "country_code": self.country_code, "other_country": self.other_country,
                "not_in_suggested": self.not_in_suggested}

    def effective_profile(self, suggested: CountryProfile) -> CountryProfile:
        """Perfil con el que se pregunta y se guarda: el confirmado, o el sugerido si su país no está soportado."""
        return country_profile(self.country_code) if self.country_code else suggested

    def missing(self) -> list[OnboardingField]:
        out = [f for f in (OnboardingField.NAME, OnboardingField.WHAT_SELLS) if not getattr(self, f.value)]
        if not (self.country_code or self.other_country):
            out.append(OnboardingField.OTHER_COUNTRY if self.not_in_suggested else OnboardingField.COUNTRY)
        if not self.charge_rate_mode:
            out.append(OnboardingField.CHARGE_RATE)
        elif self.charge_rate_mode == ChargeRateMode.CUSTOM and not self.custom_charge_rate:
            out.append(OnboardingField.CUSTOM_CHARGE_RATE)
        if not self.payment_methods:
            out.append(OnboardingField.PAYMENT_METHODS)
        return out

    @property
    def complete(self) -> bool:
        return not self.missing()

    def apply(self, updates: OnboardingUpdates, suggested: CountryProfile) -> OnboardingDraft:
        """Aplica solo los valores válidos; lo inválido se ignora y el campo sigue pendiente (se vuelve a preguntar)."""
        draft = self
        country = (updates.country or "").strip(" .!¡¿?\"'*_")
        if 0 < len(country) <= 80:
            match = profile_for_country_name(country)
            draft = replace(draft, country_code=match.code if match else None, other_country=None if match else country,
                            not_in_suggested=match is None)
        elif updates.in_suggested_country is True:
            draft = replace(draft, country_code=suggested.code, other_country=None, not_in_suggested=False)
        elif updates.in_suggested_country is False:
            draft = replace(draft, country_code=None, other_country=None, not_in_suggested=True)
        profile = draft.effective_profile(suggested)
        if updates.name is not None:
            name = updates.name.strip(" .!¡¿?\"'*_")
            if 0 < len(name) <= 80 and len(name.split()) <= 10 and normalize_name(name) not in _NOT_A_NAME:
                draft = replace(draft, name=name)
        if updates.what_sells is not None:
            what = updates.what_sells.strip(" .")
            if 3 <= len(what) <= 300:
                draft = replace(draft, what_sells=what)
        allowed = {m for _, m in charge_rate_options(profile)}
        if updates.charge_rate_mode is not None and updates.charge_rate_mode in allowed:
            draft = replace(draft, charge_rate_mode=updates.charge_rate_mode)
        if updates.custom_charge_rate is not None and updates.custom_charge_rate > 0:
            # dar su tasa ya dice que cobra con su tasa propia
            draft = replace(draft, custom_charge_rate=updates.custom_charge_rate, charge_rate_mode=ChargeRateMode.CUSTOM)
        if updates.payment_methods:
            draft = replace(draft, payment_methods=tuple(dict.fromkeys(updates.payment_methods)))
        if draft.other_country:  # sin su moneda local, todo en dólares: no hay tasa que elegir
            draft = replace(draft, charge_rate_mode=ChargeRateMode.OFFICIAL, custom_charge_rate=None)
        elif draft.charge_rate_mode is not None and draft.charge_rate_mode not in allowed:
            draft = replace(draft, charge_rate_mode=None)
        if draft.charge_rate_mode != ChargeRateMode.CUSTOM:
            draft = replace(draft, custom_charge_rate=None)
        return draft

    def charge_rate_text(self, profile: CountryProfile) -> str:
        if self.charge_rate_mode == ChargeRateMode.CUSTOM and self.custom_charge_rate:
            return f"tu propia tasa: {fmt_rate(self.custom_charge_rate, profile.local_currency)} {profile.local.symbol}/$"
        if self.charge_rate_mode == ChargeRateMode.MARKET:
            return "tasa de mercado"
        return f"tasa {profile.official_rate_label}"

    def summary(self, suggested: CountryProfile) -> str:
        assert self.charge_rate_mode is not None
        profile = self.effective_profile(suggested)
        if self.other_country:
            country, rate = f"{self.other_country} (registras en dólares)", "no aplica (registras en dólares)"
        else:
            country, rate = profile.name, self.charge_rate_text(profile)
        return SUMMARY.format(name=self.name, what_sells=self.what_sells, country=country, rate=rate,
                              methods=", ".join(profile.payment_label(m.value) or m.value for m in self.payment_methods))


@dataclass(frozen=True, slots=True)
class InterpretRequest:
    turn_id: UUID | None
    profile: CountryProfile
    draft: OnboardingDraft
    history: list[WindowEntry]
    message: str
    first_message: bool

    @property
    def awaiting_confirmation(self) -> bool:
        return self.draft.complete

    @property
    def pending_field(self) -> OnboardingField | None:
        missing = self.draft.missing()
        return missing[0] if missing else None


@dataclass(frozen=True, slots=True)
class Interpretation:
    updates: OnboardingUpdates = field(default_factory=OnboardingUpdates)
    confirmed: bool = False
    reply: str = ""


class OnboardingInterpreter(Protocol):
    async def interpret(self, request: InterpretRequest) -> Interpretation | None:
        """None = no se pudo interpretar (error del proveedor, timeout, salida inválida)."""
        ...


@dataclass(frozen=True, slots=True)
class OnboardingReply:
    text: str  # versión en texto (si no van botones)
    done: bool
    used_llm: bool
    choices: tuple[Choice, ...] = ()  # opciones tocables de la última pregunta
    button_text: str = ""  # el mismo mensaje en versión botones (sin "responde 1, 2 o 3")


def onboarding_scope(identity: Identity) -> str:
    return conversation_scope(identity.business.id, identity.phone) + ":onboarding"


class OnboardingFlow:
    def __init__(self, bus: CommandBus, interpreter: OnboardingInterpreter, store: ConversationStore) -> None:
        self._bus = bus
        self._interpreter = interpreter
        self._store = store

    async def handle(self, identity: Identity, text: str | None, *, turn_id: UUID | None = None,
                     button_id: str | None = None) -> OnboardingReply:
        business = identity.business
        profile = business.profile
        draft = OnboardingDraft.from_json(business.onboarding_draft)
        text = (text or "").strip()
        ctx = ExecContext(business_id=business.id, actor=Actor(ActorType.SYSTEM, identity.member_id), timezone=business.timezone)
        scope = onboarding_scope(identity)

        press = parse_button(button_id)
        press = press if press is not None and press.kind == ONBOARDING else None
        if press is not None and press.token == "summary":
            if press.value == "save" and draft.complete:
                return await self._complete(ctx, draft, profile, used_llm=False, identity=identity)
            if press.value == "change":
                await self._store.append_window(scope, WindowEntry(user="(tocó Cambiar algo)", assistant=ASK_CHANGE))
                return OnboardingReply(ASK_CHANGE, done=False, used_llm=False)
        if draft.complete and is_affirmative(text) and press is None:
            return await self._complete(ctx, draft, profile, used_llm=False, identity=identity)

        # Botón tocado o sí/no a "¿Tu negocio está en Venezuela?": se resuelve sin LLM, como el sí del resumen.
        result = (_button_answer(press) or (_country_answer(draft, text) if business.welcomed else None)
                  or (_custom_rate_answer(draft, text) if business.welcomed else None))
        used_llm = result is None
        if result is None:
            request = InterpretRequest(turn_id=turn_id, profile=profile, draft=draft, history=await self._store.get_window(scope),
                                       message=text, first_message=not business.welcomed)
            result = await self._interpreter.interpret(request)
        new_draft = draft.apply(result.updates, profile) if result else draft
        changed = new_draft != draft
        shown = new_draft.effective_profile(profile)

        if result and result.confirmed and draft.complete and not changed:
            return await self._complete(ctx, draft, profile, used_llm=True, identity=identity)

        parts: list[str] = []
        if not business.welcomed:
            first = (identity.member_name or "").split(" ")[0]
            parts.append(INTRO.format(name=f" {first}" if first else "", local_plural=profile.local.plural))
        elif result is None:
            parts.append(NOT_UNDERSTOOD)
        # Con la presentación, un acuse sin datos nuevos sobra (suele ser otro saludo).
        if result and (changed or business.welcomed) and (ack := _acknowledgement(result.reply)):
            parts.append(ack)
        if new_draft.other_country and new_draft.other_country != draft.other_country:
            parts.append(UNSUPPORTED_COUNTRY.format(local_plural=profile.local.plural, country=new_draft.other_country))
        elif not new_draft.other_country and _charge_rate_settled(new_draft) and not _charge_rate_settled(draft):
            parts.append(charge_rate_ack(new_draft, shown))
        choices: tuple[Choice, ...] = ()
        last_with_buttons: str | None = None
        if not new_draft.complete:
            pending_field = new_draft.missing()[0]
            parts.append(question(pending_field, shown))
            if (with_buttons := question_with_buttons(pending_field, shown)) is not None:
                last_with_buttons, choices = with_buttons
        elif changed or not draft.complete:
            summary = new_draft.summary(profile)
            parts.append(summary)
            last_with_buttons, choices = summary.replace(SUMMARY_ASK_TEXT, SUMMARY_ASK_BUTTONS), SUMMARY_CHOICES
        else:
            parts.append(ASK_CHANGE)
        reply = "\n\n".join(parts)
        button_text = "\n\n".join([*parts[:-1], last_with_buttons]) if last_with_buttons else ""

        if changed or not business.welcomed:
            await self._bus.dispatch(UpdateBusiness(onboarding_draft=new_draft.to_json(),
                                                    welcomed=True if not business.welcomed else None), ctx)
        await self._store.append_window(scope, WindowEntry(user=text or "(sin texto)", assistant=reply))
        return OnboardingReply(reply, done=False, used_llm=used_llm, choices=choices, button_text=button_text)

    async def _complete(self, ctx: ExecContext, draft: OnboardingDraft, suggested: CountryProfile, *, used_llm: bool,
                        identity: Identity) -> OnboardingReply:
        profile = draft.effective_profile(suggested)
        custom = draft.charge_rate_mode == ChargeRateMode.CUSTOM and draft.custom_charge_rate
        await self._bus.dispatch(UpdateBusiness(
            name=draft.name, what_sells=draft.what_sells, display_currency=CurrencyView.USD_APPLIED,
            charge_rate_mode=None if custom else draft.charge_rate_mode,
            payment_methods=list(draft.payment_methods), country_code=draft.country_code,
            declared_country=draft.other_country or profile.name,
            onboarding_state=OnboardingState.DONE, welcomed=True), ctx)
        if custom:  # aprobar el resumen (que muestra la tasa) ya es la confirmación: no se pregunta de nuevo
            await self._bus.dispatch(SetRate(rate=draft.custom_charge_rate, purpose=RateScope.CHARGE), ctx, confirmed=True)
        # El primer movimiento que registre a partir de ahora lleva el enlace de bienvenida al panel, el único
        # que entra sin pedir código. Se arma en el ámbito de la conversación normal, que es donde caerá ese turno.
        await self._store.arm_welcome_link(conversation_scope(identity.business.id, identity.phone))
        return OnboardingReply(DONE.format(example=_example_product(draft.what_sells, profile)), done=True, used_llm=used_llm)


def _button_answer(press) -> Interpretation | None:
    """Botón del onboarding → el dato exacto, sin interpretar texto."""
    if press is None:
        return None
    if press.token == "country" and press.value in (YES, NO):
        return Interpretation(OnboardingUpdates(in_suggested_country=press.value == YES))
    if press.token == "charge":
        try:
            return Interpretation(OnboardingUpdates(charge_rate_mode=ChargeRateMode(press.value)))
        except ValueError:
            return None
    return None


def _charge_rate_settled(draft: OnboardingDraft) -> bool:
    """Ya sabemos con qué tasa cobra (y, si es la propia, cuál es)."""
    return draft.charge_rate_mode is not None and (draft.charge_rate_mode != ChargeRateMode.CUSTOM or bool(draft.custom_charge_rate))


def _custom_rate_answer(draft: OnboardingDraft, text: str) -> Interpretation | None:
    """Respuesta a "¿Qué tasa estás usando hoy?" que es solo un número ("800", "812,5 bs"): sin LLM."""
    if OnboardingField.CUSTOM_CHARGE_RATE not in draft.missing()[:1]:
        return None
    numbers = extract_numbers(text)
    if len(numbers) != 1 or numbers[0] <= 0:
        return None
    return Interpretation(OnboardingUpdates(custom_charge_rate=to_decimal(numbers[0])))


def _country_answer(draft: OnboardingDraft, text: str) -> Interpretation | None:
    if OnboardingField.COUNTRY not in draft.missing()[:1]:
        return None
    if is_affirmative(text):
        return Interpretation(OnboardingUpdates(in_suggested_country=True))
    if is_negative(text):
        return Interpretation(OnboardingUpdates(in_suggested_country=False))
    return None


def _flag(code: str) -> str:
    """'VE' → 🇻🇪 (indicadores regionales Unicode)."""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper())


def _acknowledgement(reply: str) -> str:
    """Preguntar le toca a las plantillas: se descarta cada oración del acuse que contenga una pregunta."""
    kept = [s.strip() for s in _SENTENCE.findall(reply) if s.strip() and "?" not in s and "¿" not in s]
    return " ".join(kept)


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


def _example_product(what_sells: str | None, profile: CountryProfile) -> str:
    t = normalize_name(what_sells or "")
    if "moto" in t or "repuesto" in t:
        return "bujías NGK"
    if "comida" in t or "empanada" in t or "arepa" in t:
        return "empanadas"
    return profile.onboarding_example_product
