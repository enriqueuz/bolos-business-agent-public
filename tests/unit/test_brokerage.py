from decimal import Decimal

import pytest

from app.agent.prompts.system import static_instructions
from app.agent.receipts import (
    RECEIPT_MARKERS,
    confirmation_block,
    render,
    render_broker_balance_summary,
    render_preview,
)
from app.agent.tools.registry import TOOLS
from app.application.bus import CommandResult
from app.application.business_profile import panel_capabilities
from app.domain.brokerage import (
    account_purchase_effect,
    default_broker_abbreviation,
    normalize_broker_abbreviation,
    operation_credit,
)
from app.domain.enums import BusinessVertical, Lane, MemberRole
from app.domain.errors import ValidationFailed


def test_operation_credit_uses_nominal_purchase_for_commission() -> None:
    credit = operation_credit(Decimal("987"), None, Decimal("0.01"), operation_type="buy", received_amount=Decimal("1000"))
    assert credit.excess == Decimal("13.00")
    assert credit.commission == Decimal("9.87")
    assert credit.credit == Decimal("22.87")


def test_operation_rejects_delivery_above_operation() -> None:
    with pytest.raises(ValidationFailed):
        operation_credit(Decimal("100"), Decimal("101"), Decimal("0.01"))


def test_operation_rejects_values_that_round_to_zero() -> None:
    with pytest.raises(ValidationFailed):
        operation_credit(Decimal("0.004"), Decimal("0.004"), Decimal("0.01"))


def test_operation_with_zero_credit_still_exists() -> None:
    credit = operation_credit(Decimal("100"), None, Decimal("0"))

    assert credit.excess == Decimal("0.00")
    assert credit.commission == Decimal("0.00")
    assert credit.credit == Decimal("0.00")


def test_settled_buy_has_no_excess() -> None:
    credit = operation_credit(Decimal("450"), None, Decimal("0.01"), operation_type="buy")

    assert credit.excess == Decimal("0.00")
    assert credit.commission == Decimal("4.50")


def test_commission_keeps_half_up_rounding_for_fractional_cents() -> None:
    credit = operation_credit(Decimal("197.77"), None, Decimal("0.01"), operation_type="buy")

    assert credit.commission == Decimal("1.98")


def test_account_purchase_credits_commission_and_leaves_principal_in_balance() -> None:
    effect = account_purchase_effect(Decimal("40"), Decimal("0.01"))

    assert effect.commission == Decimal("0.40")
    assert effect.balance_delta == Decimal("-39.60")


def test_broker_abbreviation_uses_initials_and_normalizes_user_input() -> None:
    assert default_broker_abbreviation("Alex Example") == "AE"
    assert normalize_broker_abbreviation(" s.b ") == "SB"


def test_broker_profile_exposes_only_implemented_capability() -> None:
    assert panel_capabilities(BusinessVertical.BROKERAGE, MemberRole.BROKER) == ["brokerage.ledger"]
    assert panel_capabilities(BusinessVertical.BROKERAGE, MemberRole.OWNER) == ["brokerage.ledger"]


def test_broker_prompt_does_not_inherit_retail_registration_rules() -> None:
    prompt = static_instructions(BusinessVertical.BROKERAGE)

    assert "Cuenta del corredor" in prompt
    assert "Registrar una venta" not in prompt
    assert "Registrar un abono a cuenta" in prompt
    assert "Registrar una salida" not in prompt
    assert "No registras ventas de artículos físicos" in prompt
    assert "reject_physical_sale" in prompt
    assert "usa `need_more_info`" in prompt


def test_physical_sale_rejection_is_a_brokerage_ui_action_in_both_lanes() -> None:
    static_instructions(BusinessVertical.BROKERAGE)  # carga el registro de tools

    spec = TOOLS["reject_physical_sale"]
    assert spec.kind == "ui"
    assert spec.verticals == {BusinessVertical.BROKERAGE}
    assert spec.lanes == {Lane.FAST, Lane.AGENT}


def test_confirmation_only_wraps_an_already_conditional_preview() -> None:
    preview = confirmation_block(
        "Voy a borrar este movimiento.",
        "⚠️ *Movimiento por eliminar · Sin guardar*\n\ncompra | Samu | -$5,00\n\nBalance resultante: $0,00",
        with_buttons=True,
    )

    assert "Balance resultante: $0,00" in preview
    assert "Balance actual" not in preview


def test_broker_receipts_keep_signed_balances_and_separate_sections() -> None:
    for balance in ("-$99,00", "$99,00", "$0,00"):
        result = CommandResult(
            kind="record_pending_broker_purchase",
            data={
                "account_kind": "broker", "customer": "Orlando", "amount": "$100,00",
                "broker": "Enrique", "commission": "$1,00", "balance": balance,
                "description": None,
            },
        )
        receipt = render(result)

        assert receipt == (
            "📒 *Compra pendiente registrada*\n\n*Orlando · $100,00*\n\n"
            "Corredor: Enrique\nComisión: +$1,00\n\n📌 Orlando debe: *$100,00*\n\n"
            f"Balance de Enrique: *{"+" if balance == "$99,00" else ""}{balance}*"
        )


def test_broker_sale_shows_commission_and_net_effect() -> None:
    receipt = render(CommandResult(
        kind="record_broker_operation",
        data={
            "account_kind": "broker", "operation_type": "sell", "operation": "$100,00",
            "delivered": "$100,00", "excess": "$0,00", "commission": "$1,00",
            "balance_effect": "$1,00", "credit": "$1,00", "broker": "Enrique", "customer": None,
            "balance": "$1,00", "description": None,
        },
    ))

    assert receipt == (
        "✅ *Venta de divisas registrada*\n\n*Venta: $100,00*\n\nEstado: liquidada\n\nCorredor: Enrique\n"
        "Comisión: +$1,00\n"
        "Variación del balance: +$1,00\n\nBalance de Enrique: *+$1,00*"
    )


def test_house_receipts_omit_broker_balance_and_commission() -> None:
    receipt = render(CommandResult(
        kind="record_broker_operation",
        data={
            "account_kind": "house", "operation_type": "sell", "operation": "$100,00",
            "delivered": "$90,00", "excess": "$10,00", "commission": "$0,00", "credit": "$0,00",
            "broker": "Casa", "customer": "Orlando", "balance": None, "description": None,
        },
    ))

    assert "Cuenta: Casa" in receipt
    assert "Excedente:" not in receipt
    assert "Comisión:" not in receipt
    assert "Balance actual" not in receipt


def test_deleted_broker_movement_keeps_linked_records_and_signed_balance() -> None:
    receipt = render(CommandResult(
        kind="delete_broker_movement",
        data={
            "deleted": "compra pendiente | Enrique | $100,00", "account_kind": "broker",
            "broker": "Enrique", "balance": "-$25,00", "deleted_receivable": "Fiado de Orlando: $100,00",
            "deleted_payment": None,
        },
    ))

    assert "🗑️ *Movimiento eliminado*" in receipt
    assert "Deuda vinculada eliminada: Fiado de Orlando: $100,00" in receipt
    assert "Balance de Enrique: *-$25,00*" in receipt


def test_broker_confirmation_is_explicitly_unpersisted_and_conditional() -> None:
    preview = render_preview(CommandResult(
        kind="record_pending_broker_purchase",
        data={
            "account_kind": "broker", "customer": "Orlando", "amount": "$100,00",
            "broker": "Enrique", "commission": "$1,00", "balance": "-$99,00",
        },
    ))

    assert "Sin guardar" in preview
    assert "quedaría debiendo" in preview
    assert "Balance de Enrique: *-$99,00*" in preview
    assert "registrada" not in preview


def test_new_broker_receipt_headers_remain_fake_receipt_markers() -> None:
    assert {
        "Compra propia registrada", "Abono a cuenta registrado", "Compra pendiente registrada",
        "Compra pendiente corregida", "Movimiento corregido", "Movimiento eliminado",
    }.issubset(RECEIPT_MARKERS)


@pytest.mark.parametrize("balance", ["-$49,00", "$49,00", "$0,00"])
def test_broker_balance_summary_preserves_the_signed_balance(balance: str) -> None:
    summary = render_broker_balance_summary(
        {
            "balances": [{"name": "Enrique", "balance_display": balance}],
            "totals": {
                "bought": "100.00", "sold": "0.00", "payments_to_accounts": "50.00", "commission": "1.00",
            },
        },
        account_scope="broker",
        viewer_is_broker=True,
    )

    assert f"*Balance actual: {balance}*" in summary
    assert "👤 *Enrique · Corredor*" in summary
    assert "Comisiones acreditadas: $1,00" in summary


@pytest.mark.parametrize("count", [0, 1, 3])
def test_consolidated_summary_never_infers_scope_from_number_of_brokers(count):
    summary = render_broker_balance_summary({
        "balances": [{"name": f"Persona {i}", "balance_display": "-$94,00"} for i in range(count)],
        "totals": {"bought": "2300", "sold": "0", "commission": "5", "receivables_open": "900",
                   "receivables_house": "800", "receivables_brokers": "100"},
    }, account_scope="all", viewer_is_broker=False)
    assert "Resumen del negocio" in summary
    assert "$2.300,00" in summary
    assert "Global: $800,00" in summary and "Personal: $100,00" in summary
    assert "👤" not in summary and "Balance actual" not in summary


def test_house_summary_omits_broker_concepts():
    summary = render_broker_balance_summary({
        "balances": [], "account_scope": "house", "period_label": "Ayer",
        "totals": {"bought": "1800", "sold": "0", "commission": "0", "customer_payments": "20",
                   "receivables_open": "800"},
    }, account_scope="house", viewer_is_broker=False)
    assert "🏠 *Casa · Ayer*" in summary
    assert "Abonos de clientes: $20,00" in summary
    assert "actual: $800,00" in summary
    assert "balance" not in summary.lower() and "comisi" not in summary.lower()


@pytest.mark.parametrize("description", [None, "", "null", "None", "undefined"])
def test_broker_receipt_omits_absent_description(description):
    text = render(CommandResult(kind="record_broker_account_payment", data={
        "broker": "Samu", "movement": {"balance_delta": "$100,00", "description": description}, "balance": "$100,00",
    }))
    assert "Nota:" not in text
    assert "null" not in text and "None" not in text and "undefined" not in text


def test_direct_withdrawal_receipt_uses_backend_signed_values():
    result = CommandResult(kind="record_broker_account_withdrawal", data={
        "broker": "Samu", "movement": {"balance_delta": "-$500,00", "description": None},
        "balance": "-$573,62", "balance_is_own": True,
    })
    text = render(result)
    assert "Salida de cuenta registrada" in text
    assert "Monto: *-$500,00*" in text and "Tu balance actual: *-$573,62*" in text
    assert "Comisión" not in text and "Bs" not in text
    from app.agent.receipts import has_receipt_header
    assert has_receipt_header(text)
    preview = render_preview(result)
    assert "Sin guardar" in preview and "registrada" not in preview
    assert "quedaría" in preview


def test_attachment_preview_never_claims_persistence_and_formats_real_money():
    result = CommandResult(kind="attach_broker_attachment", data={
        "movement": "venta | Samu | $100,00", "attachment": True, "multicurrency_status": "complete",
        "operation_amount_bs": "80000.5000", "exchange_rate": "800.00500000",
    })
    preview = render_preview(result)
    assert "Sin guardar" in preview and "Comprobante guardado" not in preview
    assert "80.000,50 Bs" in preview and "800,01 Bs/USD" in preview
    assert "Comprobante guardado" in render(result)


def test_weighted_rate_summary_uses_the_existing_visual_rate_precision():
    text = render_broker_balance_summary({
        "balances": [], "account_scope": "all", "totals": {"bought": "100", "sold": "100", "commission": "1"},
        "multicurrency": {"sell": {"weighted_rate": "826.66666667", "amount_bs": "82666.6667", "complete_count": 1, "total_count": 1}},
    }, account_scope="all", viewer_is_broker=False)
    assert "826,67" in text and "826.66666667" not in text
    assert "1/1 completas" in text


@pytest.mark.parametrize("kind", [
    "record_broker_account_payment", "record_broker_operation", "record_broker_account_purchase",
    "record_pending_broker_purchase", "record_broker_receivable_payment", "edit_broker_movement",
    "delete_broker_movement",
])
@pytest.mark.parametrize("balance, expected", [
    ("$1.301,00", "+$1.301,00"), ("+$1.301,00", "+$1.301,00"),
    ("-$99,00", "-$99,00"), ("$0,00", "$0,00"), ("-$0,00", "$0,00"), ("+$0,00", "$0,00"),
])
@pytest.mark.parametrize("own", [True, False])
def test_every_broker_receipt_and_preview_labels_real_signed_balance(kind, balance, expected, own):
    result = CommandResult(kind=kind, data={
        "broker": "Enrique", "account_kind": "broker", "balance": balance, "balance_is_own": own,
        "customer": "Juan", "amount": "$600,00", "operation": "$600,00", "operation_type": "buy",
        "delivered": "$600,00", "commission": "$6,00", "logical_type": "receivable_payment",
        "summary": "Abono de Juan", "deleted": "Abono de Juan", "movement": {"balance_delta": "$600,00"},
        "customer_balance": {"owed": {}, "display": "$0,00", "credit_in_favor": {"USD": "100"}},
    })
    assert f"{'Tu balance actual' if own else 'Balance de Enrique'}: *{expected}*" in render(result)
    assert f"{'Tu balance quedaría en' if own else 'Balance de Enrique'}: *{expected}*" in render_preview(result)


@pytest.mark.parametrize("with_buttons", [False, True])
def test_overpayment_confirmation_has_one_warning_and_one_question(with_buttons):
    result = CommandResult(kind="record_broker_receivable_payment", data={
        "broker": "Enrique", "balance": "$1301,00", "balance_is_own": True,
        "customer": "Juan", "amount": "$600,00", "unapplied": "$100,00",
        "customer_balance": {"owed": {}, "display": "$0,00", "credit_in_favor": {"USD": "250"}},
    })
    text = confirmation_block(
        "El abono supera lo que debe Juan: sobran $100,00. ¿Quieres registrarlo de todos modos?",
        render_preview(result), with_buttons=with_buttons,
    )
    assert text.count("⚠️") == 1 and text.count("¿") == 1
    assert "¿Lo hago?" not in text
    assert "Balance Juan: *+$250,00*" in text  # accumulated, not this payment's excess
    assert "debiendo: $0,00" not in text
    normal = confirmation_block("Confirma la operación.", render_preview(result), with_buttons=with_buttons)
    assert normal.count("¿Lo hago?") == 1
