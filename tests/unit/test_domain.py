"""Dominio puro: sin base, sin red. Rápidos."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.agent import grounding
from app.application.onboarding import OnboardingDraft, OnboardingField, OnboardingUpdates, _acknowledgement
from app.domain.countries import VENEZUELA
from app.domain.dates import (
    CompareTo,
    DateRange,
    PeriodName,
    comparison_range,
    occurred_at_for,
    resolve_period,
)
from app.domain.enums import ChargeRateMode, Currency, PaymentMethod, RateAppliedSource
from app.domain.errors import RatesUnavailable, ValidationFailed
from app.domain.money import EffectiveRates, compute_legs, convert_between, fmt_amount, sum_legs
from app.domain.receivables import OpenReceivable, allocate_fifo
from app.domain.text import (
    extract_numbers,
    is_affirmative,
    is_menu_request,
    is_negative,
    normalize_name,
    parse_number_token,
)

RATES = EffectiveRates(local_currency="VES", official=Decimal("800"), market=Decimal("900"), applied=Decimal("900"),
                       applied_source=RateAppliedSource.MARKET)


class TestMoney:
    def test_usd_origin(self):
        legs = compute_legs(Decimal("10"), Currency.USD, RATES)
        assert (legs.usd_official, legs.usd_market, legs.local) == (Decimal("10.0000"), Decimal("10.0000"), Decimal("9000.0000"))

    def test_ves_origin(self):
        legs = compute_legs(Decimal("9000"), Currency.VES, RATES)
        assert legs.usd_official == Decimal("11.2500") and legs.usd_market == Decimal("10.0000")

    def test_zero_rates_never_write(self):
        with pytest.raises(RatesUnavailable):
            compute_legs(Decimal(1), Currency.USD, EffectiveRates("VES", Decimal(0), Decimal(0), Decimal(0), RateAppliedSource.OFFICIAL))

    def test_real_cost_needs_funds_rate(self):
        from app.domain.money import real_cost_usd

        assert real_cost_usd(Decimal(4000), "VES", "VES", Decimal(800)) == Decimal("5.0000")
        assert real_cost_usd(Decimal(4000), "VES", "VES", None) is None  # sin tasa real no se inventa
        assert real_cost_usd(Decimal(40), "USD", "VES", None) == Decimal("40.0000")

    def test_sum_mixed_currencies_goes_to_usd_market(self):
        total = sum_legs([compute_legs(Decimal(10), Currency.USD, RATES), compute_legs(Decimal(900), Currency.VES, RATES)])
        assert total.currency == Currency.USD and total.amount == Decimal("11.0000") and total.local == Decimal("9900.0000")

    def test_sum_same_currency_keeps_it(self):
        total = sum_legs([compute_legs(Decimal(900), Currency.VES, RATES), compute_legs(Decimal(100), Currency.VES, RATES)])
        assert total.currency == Currency.VES and total.amount == Decimal("1000.0000")

    def test_format(self):
        assert fmt_amount(Decimal("1234.5"), Currency.VES) == "1.234,50 Bs"
        assert fmt_amount(Decimal("1234.5"), Currency.USD) == "$1.234,50"

    def test_convert_between(self):
        assert convert_between(Decimal(1800), Currency.VES, Currency.USD, Decimal(900)) == Decimal("2.0000")


class TestFifo:
    def test_allocates_oldest_first_and_reports_surplus(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        items = [OpenReceivable(b, date(2026, 9, 10), Currency.USD, Decimal(20)), OpenReceivable(a, date(2026, 9, 1), Currency.USD, Decimal(5))]
        plan = allocate_fifo(Decimal(30), Currency.USD, Decimal(900), items)
        assert [(x.receivable_id, x.applied_amount) for x in plan.allocations] == [(a, Decimal("5.0000")), (b, Decimal("20.0000"))]
        assert plan.unapplied_in_payment_currency == Decimal("5.0000")

    def test_cross_currency(self):
        rid = uuid.uuid4()
        plan = allocate_fifo(Decimal(4500), Currency.VES, Decimal(900), [OpenReceivable(rid, date(2026, 9, 1), Currency.USD, Decimal(10))])
        assert plan.allocations[0].applied_amount == Decimal("5.0000") and plan.unapplied_in_payment_currency == 0


class TestDates:
    today = date(2026, 9, 16)  # miércoles

    def test_periods(self):
        assert resolve_period(PeriodName.THIS_WEEK, self.today) == DateRange(date(2026, 9, 14), self.today)
        assert resolve_period(PeriodName.LAST_WEEK, self.today) == DateRange(date(2026, 9, 7), date(2026, 9, 13))
        assert resolve_period(PeriodName.LAST_MONTH, self.today) == DateRange(date(2026, 8, 1), date(2026, 8, 31))

    def test_custom_requires_start(self):
        with pytest.raises(ValidationFailed):
            resolve_period(PeriodName.CUSTOM, self.today)

    def test_comparison(self):
        rng = DateRange(date(2026, 9, 1), date(2026, 9, 10))
        assert comparison_range(rng, CompareTo.PREVIOUS_PERIOD) == DateRange(date(2026, 8, 22), date(2026, 8, 31))

    def test_occurred_at_past_day_is_local_noon(self):
        now = datetime(2026, 9, 16, 15, 0, tzinfo=UTC)
        assert occurred_at_for(date(2026, 9, 15), "America/Caracas", now) == datetime(2026, 9, 15, 16, 0, tzinfo=UTC)


class TestText:
    @pytest.mark.parametrize("word", ["sí", "Si", "dale", "ok", "👍", "sí, hazlo", "de una"])
    def test_affirmative(self, word):
        assert is_affirmative(word.replace(",", ""))

    @pytest.mark.parametrize("word", ["no", "cancela", "mejor no", "❌"])
    def test_negative(self, word):
        assert is_negative(word)

    def test_long_sentence_is_not_yes(self):
        assert not is_affirmative("sí pero la venta era de 5 dólares no de 3")

    def test_normalize(self):
        assert normalize_name("  Moisés  Pérez! ") == "moises perez"

    @pytest.mark.parametrize("token,value", [("1.234,56", 1234.56), ("1,234.56", 1234.56), ("14,50", 14.5), ("1.500", 1500.0),
                                             ("0.5", 0.5), ("3", 3.0)])
    def test_number_tokens(self, token, value):
        assert parse_number_token(token) == value

    def test_menu(self):
        assert is_menu_request("Menú") and not is_menu_request("menudo")

    def test_currency_hint(self):
        assert VENEZUELA.currency_hint("compré 10 cadenas a 8$ cada una") == "USD"
        assert VENEZUELA.currency_hint("pagué 45 lucas de luz") == "VES"
        assert VENEZUELA.currency_hint("5$ y 300 Bs") is None
        assert VENEZUELA.currency_hint("vendí una bujía") is None

    def test_extract_numbers(self):
        assert extract_numbers("vendí 2 a $3,50 y 1.200 Bs") == [2.0, 3.5, 1200.0]


class TestGrounding:
    def test_numbers_from_tools_and_derivations_pass(self):
        results = [{"totals": {"sales_total": {"value": "120.0000", "display": "$120,00"}},
                    "comparison": {"totals": {"sales_total": {"value": "100.0000"}}}}]
        assert grounding.check("Vendiste $120,00, un 20% más que antes ($100). Diferencia: $20", results).ok

    def test_invented_number_fails(self):
        report = grounding.check("Vendiste $950,00", [{"totals": {"x": "120.0000"}}])
        assert not report.ok and 950.0 in report.ungrounded

    def test_small_integers_and_user_numbers_are_ignored(self):
        assert grounding.check("Tus 3 mejores días del mes 9; lo que dijiste de 45 lucas", [], user_text="45 lucas").ok


class TestOnboardingDraft:
    def test_draft_applies_any_field_and_reports_what_is_missing(self):
        draft = OnboardingDraft().apply(OnboardingUpdates(name=" *Scalea*. ", payment_methods=[PaymentMethod.ZELLE, PaymentMethod.ZELLE]),
                                        VENEZUELA)
        assert draft.name == "Scalea" and draft.payment_methods == (PaymentMethod.ZELLE,)
        assert draft.missing() == [OnboardingField.WHAT_SELLS, OnboardingField.COUNTRY, OnboardingField.CHARGE_RATE]
        assert OnboardingDraft.from_json(draft.to_json()) == draft

    def test_acknowledgement_drops_questions_the_flow_already_asks(self):
        assert _acknowledgement("¡Perfecto! ¿Qué vendes? ") == "¡Perfecto!"
        assert _acknowledgement("¿Qué dato quieres cambiar?") == ""
        assert _acknowledgement("Hola, para empezar cuéntame, ¿cómo se llama tu negocio?") == ""
        assert _acknowledgement("Listo, anoté *Scalea*. Sigamos") == "Listo, anoté *Scalea*. Sigamos"

    def test_draft_ignores_invalid_values(self):
        draft = OnboardingDraft(name="Scalea").apply(OnboardingUpdates(name="Buenas tardes", what_sells="x", payment_methods=[]),
                                                     VENEZUELA)
        assert draft == OnboardingDraft(name="Scalea")
        complete = OnboardingDraft("Scalea", "software", ChargeRateMode.OFFICIAL, payment_methods=(PaymentMethod.TRANSFER,),
                                   country_code="VE")
        assert complete.complete and "*Tasa para cobrar:* tasa BCV" in complete.summary(VENEZUELA)
        assert "*País:* Venezuela" in complete.summary(VENEZUELA)

    def test_country_is_always_confirmed(self):
        base = OnboardingDraft("Scalea", "software", ChargeRateMode.MARKET, payment_methods=(PaymentMethod.TRANSFER,))
        assert not base.complete and base.missing() == [OnboardingField.COUNTRY]  # el prefijo solo sugiere
        assert base.apply(OnboardingUpdates(in_suggested_country=True), VENEZUELA).complete
        said_no = base.apply(OnboardingUpdates(in_suggested_country=False), VENEZUELA)
        assert said_no.missing() == [OnboardingField.OTHER_COUNTRY]
        assert OnboardingDraft.from_json(said_no.to_json()) == said_no
        assert said_no.apply(OnboardingUpdates(country="venezuela"), VENEZUELA).country_code == "VE"

    def test_unsupported_country_is_kept_and_forces_dollars(self):
        draft = OnboardingDraft("Scalea", "software", ChargeRateMode.CUSTOM, Decimal(300), (PaymentMethod.TRANSFER,)).apply(
            OnboardingUpdates(in_suggested_country=False, country="Colombia"), VENEZUELA)
        assert draft.complete and draft.other_country == "Colombia" and draft.country_code is None
        assert draft.charge_rate_mode == ChargeRateMode.OFFICIAL and draft.custom_charge_rate is None  # todo en dólares
        assert "Colombia (registras en dólares)" in draft.summary(VENEZUELA) and "no aplica" in draft.summary(VENEZUELA)

    def test_own_rate_needs_the_rate_and_giving_it_implies_own_rate(self):
        base = OnboardingDraft("Scalea", "software", payment_methods=(PaymentMethod.TRANSFER,), country_code="VE")
        chose_own = base.apply(OnboardingUpdates(charge_rate_mode=ChargeRateMode.CUSTOM), VENEZUELA)
        assert chose_own.missing() == [OnboardingField.CUSTOM_CHARGE_RATE]
        with_rate = chose_own.apply(OnboardingUpdates(custom_charge_rate=Decimal("812.5")), VENEZUELA)
        assert with_rate.complete and "tu propia tasa: 812,50 Bs/$" in with_rate.summary(VENEZUELA)
        assert OnboardingDraft.from_json(with_rate.to_json()) == with_rate
        direct = base.apply(OnboardingUpdates(custom_charge_rate=Decimal(800)), VENEZUELA)  # "cobro a 800"
        assert direct.charge_rate_mode == ChargeRateMode.CUSTOM and direct.complete
        back = with_rate.apply(OnboardingUpdates(charge_rate_mode=ChargeRateMode.OFFICIAL), VENEZUELA)
        assert back.custom_charge_rate is None and back.complete

    def test_old_drafts_keep_their_choice(self):
        """Borradores guardados con la pregunta vieja ("¿en qué moneda ves tus resúmenes?")."""
        assert OnboardingDraft.from_json({"display_currency": "usd_market"}).charge_rate_mode == ChargeRateMode.MARKET
        assert OnboardingDraft.from_json({"display_currency": "local"}).charge_rate_mode is None


class TestMultiCountry:
    """El núcleo no sabe de bolívares: un país ficticio con otra moneda funciona sin tocar código."""

    def test_legs_for_another_local_currency(self):
        from app.domain.currencies import CURRENCIES, CurrencyInfo

        CURRENCIES.setdefault("COP", CurrencyInfo("COP", "peso colombiano", "pesos colombianos", "$", symbol_after=False, decimals=0))
        rates = EffectiveRates(local_currency="COP", official=Decimal("4000"), market=Decimal("4000"), applied=Decimal("4000"),
                               applied_source=RateAppliedSource.OFFICIAL)
        legs = compute_legs(Decimal("80000"), "COP", rates)
        assert legs.local == Decimal("80000.0000") and legs.usd_official == Decimal("20.0000")
        assert fmt_amount(Decimal("80000"), "COP") == "$80.000"
        with pytest.raises(ValidationFailed):
            compute_legs(Decimal(1), "VES", rates)  # un negocio colombiano no registra en bolívares

    def test_profile_selected_by_phone_prefix(self):
        from app.domain.countries import profile_for_phone

        assert profile_for_phone("+58 414 1234567").code == "VE"
        assert profile_for_phone("15551234567").code == "VE"  # prefijo desconocido → país por defecto

    def test_longest_phone_prefix_wins(self, monkeypatch):
        from dataclasses import replace

        from app.domain import countries

        dom = replace(VENEZUELA, code="DO", name="República Dominicana", phone_prefixes=("1809",))
        us = replace(VENEZUELA, code="US", name="Estados Unidos", phone_prefixes=("1",))
        monkeypatch.setattr(countries, "PROFILES", {"US": us, "DO": dom, "VE": VENEZUELA})
        assert countries.profile_for_phone("18095551234").code == "DO"
        assert countries.profile_for_phone("13055551234").code == "US"

    def test_payment_labels_are_local(self):
        assert VENEZUELA.payment_label("crypto") == "Binance" and VENEZUELA.payment_label("transfer") == "Transferencia"


class TestAppliedRate:
    """Tasa para cobrar: la del modo del negocio; BCV y mercado quedan como referencias en cada movimiento."""

    @staticmethod
    def snap(mode, custom=None, market=Decimal("900")):
        from app.application.rates import RatesSnapshot
        from app.domain.enums import ChargeRateMode

        return RatesSnapshot(local_currency="VES", official=Decimal("800"), official_as_of=None, market_auto=market,
                             market_auto_as_of=None, custom=custom, custom_label=None, custom_set_at=None,
                             charge_rate_mode=ChargeRateMode(mode))

    def test_each_mode_picks_its_rate(self):
        assert self.snap("official").effective().applied == 800
        assert self.snap("market").effective().applied == 900
        custom = self.snap("custom", custom=Decimal("300")).effective()
        assert (custom.applied, custom.applied_source, custom.market) == (300, RateAppliedSource.CUSTOM, 900)

    def test_missing_rate_of_the_mode_falls_back_to_official(self):
        assert self.snap("custom").effective().applied_source == RateAppliedSource.OFFICIAL
        assert self.snap("market", market=None).effective().applied == 800

    def test_usd_amount_is_charged_in_local_at_applied_rate(self):
        legs = compute_legs(Decimal("10"), Currency.USD, self.snap("custom", custom=Decimal("300")).effective())
        assert legs.local == Decimal("3000.0000") and legs.usd_applied == Decimal("10.0000")

    def test_local_amount_in_usd_at_applied_rate(self):
        legs = compute_legs(Decimal("3000"), Currency.VES, self.snap("custom", custom=Decimal("300")).effective())
        assert (legs.usd_applied, legs.usd_official, legs.usd_market) == (Decimal("10.0000"), Decimal("3.7500"), Decimal("3.3333"))
