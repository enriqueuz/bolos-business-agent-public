"""Recibos DETERMINISTAS: el detalle de lo escrito sale del resultado real del comando, nunca del modelo.

Así el modelo no puede "confirmar" algo que no se ejecutó ni equivocarse en un monto al contarlo.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.application.bus import CommandResult
from app.application.summaries import method_label
from app.domain.currencies import USD
from app.domain.enums import EXPENSE_CATEGORY_LABELS, ExpenseCategory
from app.domain.money import fmt_amount, fmt_rate
from app.domain.text import optional_text


def _method(value: str | None) -> str:
    return f" · {method_label(value)}" if value else ""


def _balance(bal: dict[str, Any] | None, customer: str | None) -> str:
    if not bal or not customer:
        return ""
    return f"\n{customer} debe ahora: *{bal['display']}*" + _credit(bal)


def _credit(bal: dict[str, Any]) -> str:
    if not bal.get("credit_in_favor"):
        return ""
    from decimal import Decimal

    from app.domain.money import fmt_amount

    return " (saldo a favor: " + ", ".join(fmt_amount(Decimal(v), k) for k, v in bal["credit_in_favor"].items()) + ")"


def _credit_used(amount: str | None) -> str:
    return f"\nSe descontó {amount} de su saldo a favor." if amount else ""


def _assumed(flag: bool) -> str:
    return "\n_(Asumí la moneda por el monto; si no es así, dime.)_" if flag else ""


def _attachment(data: dict[str, Any]) -> str:
    text = "\nCartera global · Cliente compartido." if data.get("portfolio_scope") == "global" else ""
    text += "\n📎 Comprobante guardado." if data.get("attachment") else ""
    if data.get("multicurrency_status") == "complete":
        text += f"\nBolívares: {fmt_amount(Decimal(data['operation_amount_bs']), 'VES')} · Tasa: {fmt_rate(Decimal(data['exchange_rate']), 'VES')} Bs/USD"
    elif data.get("multicurrency_status") == "incomplete":
        text += "\n⚠️ No se registró el monto en bolívares ni la tasa. Puedes proporcionarlos después o adjuntar el comprobante."
    if data.get("extraction_warning"):
        text += "\n⚠️ " + data["extraction_warning"]
    return text


def _purchase_receipt_lines(data: dict[str, Any]) -> list[str]:
    if data.get("received") is None:
        return []
    lines = [f"USD recibidos: {data['received']}", f"Excedente: {_positive(data['excess'])}"]
    if data.get("benefit") is not None:
        lines.append(f"Beneficio (excedente + comisión): {_positive(data['benefit'])}")
    if data.get("balance_effect") is not None and data.get("operation_type") != "buy":
        lines.append(f"Variación del balance: {_signed_amount(data['balance_effect'])}")
    return lines


def _broker_sections(*sections: str) -> str:
    """Une bloques cortos de un recibo de corredores para WhatsApp."""
    return "\n\n".join(section for section in sections if section)


def _reconciliation_money(value: str) -> str:
    amount = Decimal(value)
    return ("−" if amount < 0 else "+" if amount > 0 else "") + fmt_amount(abs(amount), "USD")


def _reconciliation_date(value: str) -> str:
    year, month, day = value.split("-")
    return f"{day}/{month}/{year}"


def _signed_amount(amount: str) -> str:
    """Añade el signo al importe ya formateado, conservando moneda y separadores."""
    unsigned = amount.lstrip("+-")
    if not any(digit in "123456789" for digit in unsigned):
        return unsigned
    return ("-" if amount.startswith("-") else "+") + unsigned


def _broker_balance(data: dict[str, Any], *, preview: bool = False) -> str:
    if data.get("balance") is None:
        return ""
    if data.get("balance_is_own"):
        label = "Tu balance quedaría en" if preview else "Tu balance actual"
    else:
        label = f"Balance de {data['broker']}"
    return f"{label}: *{_signed_amount(data['balance'])}*"


def _broker_money(data: dict[str, Any]) -> str:
    from app.domain.money import fmt_rate

    rate, bs = data.get("exchange_rate"), data.get("operation_amount_bs")
    return "\n".join(filter(None, [f"Tasa: {fmt_rate(Decimal(rate), 'VES')} Bs/$" if rate else "",
                                  f"Total: {fmt_amount(Decimal(bs), 'VES')}" if bs else ""]))


def _broker_customer_balance(data: dict[str, Any], *, preview: bool = False) -> str:
    """Deuda y crédito acumulados del ámbito autorizado, nunca el sobrante del movimiento."""
    balance = data["customer_balance"]
    customer = data["customer"]
    if "balance_usd" in balance:
        amount = Decimal(balance["balance_usd"])
        if amount == 0:
            return f"Saldo de {customer}: *{balance['display']}*. " + ("Quedaría todo saldado." if preview else "Todo saldado.")
        label = f"Saldo a favor de {customer}" if amount > 0 else f"{customer} debe a Casa"
        verb = " quedaría en" if preview else ""
        return f"{label}{verb}: *{balance['display']}*"
    credit = " + ".join(
        _signed_amount(fmt_amount(Decimal(value), currency))
        for currency, value in sorted(balance.get("credit_in_favor", {}).items()) if Decimal(value) > 0
    )
    if preview:
        debt = f"{customer} quedaría debiendo: {balance['display']}" if balance["owed"] else ""
        if not debt and not credit:
            debt = f"{customer} quedaría sin deuda"
        return _broker_sections(debt, f"Balance {customer}: *{credit}*" if credit else "")
    debt = f"📌 {customer} debe: *{balance['display']}*" if balance["owed"] else f"📌 {customer} no tiene deuda"
    return _broker_sections(debt, f"Saldo {customer}: *{credit}*" if credit else "")


def _positive(amount: str) -> str:
    return amount if amount.startswith(("+", "-")) else f"+{amount}"


def _note(description: str | None) -> str:
    description = optional_text(description)
    return f"Nota: {description}" if description else ""


def render_broker_balance_summary(
    data: dict[str, Any], *, account_scope: str, viewer_is_broker: bool,
) -> str:
    """Resumen de cuenta de corredores con cifras de la consulta, no del modelo."""
    from decimal import Decimal

    from app.domain.money import fmt_amount

    def money(value: str) -> str:
        return fmt_amount(Decimal(value), "USD")

    balances = data["balances"]
    totals = data["totals"]
    # El ámbito autorizado es explícito; el número de cuentas nunca lo determina.
    scope = data.get("account_scope", account_scope)
    period = data.get("period_label", "Hoy")
    activity = [f"• Compras: {money(totals['bought'])}", f"• Ventas: {money(totals['sold'])}"]
    balance = ""
    portfolio = ""
    if scope == "all":
        title = f"📊 *Resumen del negocio · {period}*"
        activity.insert(0, "*Operaciones*")
        activity.append(f"• Comisiones de corredores: {money(totals['commission'])}")
        if "receivables_open" in totals:
            portfolio = "\n".join((
                "*Por cobrar a clientes · Actual*",
                f"• Total: {money(totals['receivables_open'])}",
                f"• Global: {money(totals.get('receivables_global', totals['receivables_house']))}",
                f"• Personal: {money(totals.get('receivables_personal', totals['receivables_brokers']))}",
            ))
    elif scope == "house":
        title = f"🏠 *Casa · {period}*"
        if "customer_payments" in totals:
            activity.append(f"• Abonos de clientes: {money(totals['customer_payments'])}")
        if "receivables_open" in totals:
            portfolio = f"*Pendiente por cobrar actual: {money(totals['receivables_open'])}*"
    else:
        name = balances[0]["name"] if balances else "Tu cuenta" if viewer_is_broker else "Corredor"
        title = f"👤 *{name} · Corredor*"
        if balances:
            balance = f"*Balance actual: {balances[0]['balance_display']}*"
        activity.insert(0, f"*Operaciones · {period}*")
        activity.append(f"• Comisiones acreditadas: {money(totals['commission'])}")
        if "receivables_open" in totals:
            portfolio = f"*Clientes por cobrar actual: {money(totals['receivables_open'])}*"
    if scope != "house":
        if "payments_to_accounts" in totals:
            activity.append(f"• Abonos a cuentas: {money(totals['payments_to_accounts'])}")
        if "withdrawals_from_accounts" in totals:
            activity.append(f"• Salidas de cuentas: {money(totals['withdrawals_from_accounts'])}")
    if "customer_credit" in totals:
        portfolio += f"\nA favor de clientes actual: {money(totals['customer_credit'])}"
    fx = []
    for kind, label in (("buy", "Compras"), ("sell", "Ventas")):
        complete = data.get("multicurrency", {}).get(kind)
        if complete:
            rate = complete["weighted_rate"]
            fx.append(f"{label}: {fmt_amount(Decimal(complete['amount_bs']), 'VES')} · "
                      f"tasa {fmt_rate(Decimal(rate), 'VES') if rate is not None else 'sin datos'} · "
                      f"{complete['complete_count']}/{complete['total_count']} completas")
    return _broker_sections(title, balance, "\n".join(activity), portfolio,
                            "*Datos completos en Bs*\n" + "\n".join(fx) if fx else "")


def _legs_line(total: dict[str, Any]) -> str:
    """Monto nativo + su equivalente en la otra moneda del negocio (local ↔ USD a la tasa para cobrar aplicada)."""
    from decimal import Decimal

    from app.domain.money import fmt_amount

    if total["currency"] == USD:
        return f"{total['display']} (≈ {fmt_amount(Decimal(total['local']), total['local_currency'])})"
    return f"{total['display']} (≈ {fmt_amount(Decimal(total['usd_applied']), USD)})"


# Títulos de un recibo del SISTEMA. Si el MODELO imita un encabezado y el turno no escribió nada, está
# afirmando algo que no pasó: el pipeline lo bloquea (`TurnPipeline._impersonates_receipt`). Viven aquí, junto
# a los renderers que las producen, para que guarda y recibo no se desincronicen al tocar un texto.
_OPERATION_TITLES: tuple[str, ...] = (
    "Cierre de caja", "Movimiento de corredor", "Operación de corredor", "Compra de divisas",
    "Venta de divisas", "Compra pendiente", "Abono de cliente", "Pago entregado",
)
RECEIPT_MARKERS: tuple[str, ...] = (
    "Venta registrada", "Gasto registrado", "Compra registrada", "Abono registrado", "Fiado registrado",
    "Cambio registrado", "Precio de *", "Costo de *", "Anotado:",
    "Compra propia registrada", "Abono a cuenta registrado", "Salida de cuenta registrada", "Salida de cuenta corregida", "Compra pendiente registrada",
    "Compra pendiente corregida", "Movimiento corregido", "Movimiento eliminado",
    "Venta pendiente registrada", "Registrado", "Registrada", "Anotado", "Borrado",
    "Corregido", "Actualizado", "Ajuste de saldo registrado", "Ajuste de saldo eliminado", *_OPERATION_TITLES,
)


def has_receipt_header(text: str) -> bool:
    """Detect receipt syntax, not mentions of operations inside an explanation.

    Match complete title lines (including WhatsApp emphasis/emoji and inline receipt
    separators). A sentence such as «Compra pendiente significa…» is not a title.
    """
    for line in text.splitlines():
        # Generic operation labels are also used in analytical lists. They are receipt
        # titles only with the explicit receipt glyphs emitted by our renderers.
        decorated = line.lstrip().startswith(("✅", "📒", "💱", "💵", "✏", "🗑", "💰"))
        raw_title = line.strip()
        while raw_title and not raw_title[0].isalnum():
            raw_title = raw_title[1:].lstrip()
        title = line.strip().replace("*", "").replace("_", "")
        while title and not title[0].isalnum():
            title = title[1:].lstrip()
        for marker in RECEIPT_MARKERS:
            if marker in _OPERATION_TITLES and not decorated:
                continue
            # These legacy templates have a variable product/record after the prefix.
            if marker.endswith(("*", " ")) and raw_title.casefold().startswith(marker.casefold()):
                return True
            marker = marker.strip().rstrip(":").casefold()
            head = title.casefold()
            if head == marker or any(head.startswith(marker + sep) for sep in (" ·", " •", " |", ":")):
                return True
    return False


def render(result: CommandResult) -> str:
    text = _render_receipt(result)
    if result.kind == "edit_broker_movement":
        text += _attachment({**result.data, "attachment": False})
    creations = []
    for customer in result.data.get("created_customers", []):
        lines = [f"✅ Cliente creado: {customer['display_name']}"]
        if customer.get("nickname"):
            lines += [f"Apodo: {customer['nickname']}",
                      f'Para cambiarlo, escribe: "Cambia el apodo de {customer["nickname"]} por otro apodo".']
        elif customer.get("nickname_conflict"):
            lines += [f"⚠️ El apodo {customer['nickname_conflict']} ya está ocupado.",
                      f'Puedes asignar otro: "Ponle el apodo que prefieras a {customer["display_name"]}".']
        else:
            lines += [f'Puedes asignarle un apodo: "Ponle el apodo que prefieras a {customer["display_name"]}".']
        creations.append("\n".join(lines))
    return "\n\n".join([*creations, text])


def _render_receipt(result: CommandResult) -> str:
    d = result.data
    kind = result.kind
    if kind in {"reconcile_account_balance", "delete_balance_adjustment"}:
        if d.get("no_change"):
            today = _reconciliation_date(d['today'])[:5]
            return _broker_sections(
                f"El saldo de {d['holder']} ya coincide al {_reconciliation_date(d['business_date'])}: {_reconciliation_money(d['declared_balance'])}.",
                f"Saldo registrado al corte: {_reconciliation_money(d['cutoff_balance'])}",
                f"Variación posterior al corte: {_reconciliation_money(str(Decimal(d['current_balance']) - Decimal(d['cutoff_balance'])))}",
                f"Saldo actual ({today}): {_reconciliation_money(d['current_balance'])}",
                "No se registró ningún ajuste.")
        title = "Ajuste de saldo eliminado" if kind == "delete_balance_adjustment" else "Ajuste de saldo registrado"
        label = "Cliente" if d["account_type"] == "customer" else "Corredor"
        return _broker_sections(f"✅ *{title}*", f"{label}: {d['holder']}",
            f"Fecha histórica: {_reconciliation_date(d['business_date'])}",
            f"Ajuste aplicado: {_reconciliation_money(d['adjustment_delta'])}",
            f"*Saldo actual: {_reconciliation_money(d['new_balance'])}*", "Reconciliación registrada correctamente.")
    if kind == "edit_customer_profile":
        label = "Apodo actualizado" if d.get("nickname_changed") else "Nombre actualizado"
        lines = [f"✅ {label}", f"Cliente: {d['customer']}"]
        if d.get("nickname_changed"):
            lines += [f"Anterior: {d.get('previous_nickname') or 'Sin apodo'}", f"Nuevo: {d.get('nickname') or 'Sin apodo'}"]
        elif d.get("nickname"):
            lines.append(f"Apodo: {d['nickname']}")
        return "\n".join(lines)
    if kind == "upsert_customer":
        return f"✅ Cliente guardado: {d['customer']}"
    if kind == "record_sale":
        return _sale_text(d)
    if kind == "record_broker_account_payment":
        movement = d["movement"]
        return _broker_sections(
            "💵 *Abono a cuenta registrado*",
            f"*{d['broker']} abonó {movement['balance_delta']}*",
            _note(movement.get("description")),
            _broker_balance(d),
        ) + _attachment(d)
    if kind == "record_broker_account_withdrawal":
        return _broker_sections(
            "✅ *Salida de cuenta registrada*",
            f"Monto: *{d['movement']['balance_delta']}*",
            f"Corredor: {d['broker']}",
            _note(d["movement"].get("description")), _broker_balance(d),
        ) + _attachment(d)
    if kind == "record_broker_customer_payout":
        return _broker_sections("✅ *Pago entregado registrado*", f"Cliente: {d['customer']}",
                                f"Casa entregó: *{d['amount']}*", _broker_customer_balance(d),
                                _broker_balance(d)) + _attachment(d)
    if kind == "record_broker_operation" and d.get("settlement") == "pending":
        return _broker_sections("✅ *Venta pendiente registrada*", f"Cliente: {d['customer']}",
            f"Venta: *{d['operation']}*",
            f"Corredor: {d['broker']}\nComisión: {_positive(d['commission'])}\nVariación del balance: {_signed_amount(d['balance_effect'])}" if d.get("account_kind") != "house" else "Cuenta: Casa",
            _broker_customer_balance(d), _broker_balance(d)) + _attachment(d)
    if kind == "record_broker_operation":
        label = "Compra" if d["operation_type"] == "buy" else "Venta"
        headline = f"*{d['customer']} · {d['operation']}*" if d.get("customer") else f"*{label}: {d['operation']}*"
        if d.get("account_kind") == "house":
            return _broker_sections(
                f"✅ *{label} de divisas registrada*", headline,
                "Cuenta: Casa\nSin comisión de corredor.\nEstado: liquidada", _note(d.get("description")),
            ) + _attachment(d)
        details = [f"Corredor: {d['broker']}"]
        if d["operation_type"] == "buy":
            details.extend(_purchase_receipt_lines(d))
        rate = f" ({Decimal(d['commission_rate']) * 100:g} %)" if d.get("commission_rate") is not None else ""
        details.append(f"Comisión{rate}: {_positive(d['commission'])}")
        if d.get("balance_effect") is not None:
            details.append(f"Variación del balance: {_signed_amount(d['balance_effect'])}")
        return _broker_sections(
            f"✅ *{label} de divisas registrada*", headline, "Estado: liquidada", "\n".join(details),
            _note(d.get("description")), _broker_balance(d),
        ) + _attachment(d)
    if kind == "record_broker_account_purchase":
        return _broker_sections(
            "✅ *Compra propia registrada*",
            f"*Compra: {d['operation']}*",
            f"Corredor: {d['broker']}\nComisión: {_positive(d['commission'])}",
            "\n".join(_purchase_receipt_lines(d)),
            _note(d.get("description")),
            _broker_balance(d),
        ) + _attachment(d)
    if kind == "record_pending_broker_purchase":
        if d.get("account_kind") == "house":
            return _broker_sections(
                "📒 *Compra pendiente registrada*", f"*{d['customer']} · {d['amount']}*", "Cuenta: Casa",
                _broker_customer_balance(d) if d.get("customer_balance") else f"📌 {d['customer']} debe: *{d['amount']}*", _note(d.get("description")),
            ) + _attachment(d)
        return _broker_sections(
            "📒 *Compra pendiente registrada*", f"*{d['customer']} · {d['amount']}*",
            f"Corredor: {d['broker']}\nComisión: {_positive(d['commission'])}",
            "\n".join(_purchase_receipt_lines(d)),
            _broker_customer_balance(d) if d.get("customer_balance") else f"📌 {d['customer']} debe: *{d['amount']}*", _note(d.get("description")),
            _broker_balance(d),
        ) + _attachment(d)
    if kind == "record_broker_receivable_payment":
        if d.get("account_kind") == "house":
            return _broker_sections(
                "💵 *Abono registrado*", f"*{d['customer']} pagó {d['amount']}*", "Cuenta: Casa",
                _broker_customer_balance(d),
            ) + _attachment(d)
        return _broker_sections(
            "💵 *Abono registrado*", f"*{d['customer']} pagó {d['amount']}*", f"Corredor: {d['broker']}",
            _broker_customer_balance(d),
            _broker_balance(d),
        ) + _attachment(d)
    if kind == "attach_broker_attachment":
        return f"📎 *Comprobante guardado* · {d['movement']}" + _attachment({**d, "attachment": False})
    if kind == "edit_broker_movement":
        if d["logical_type"] == "pending":
            if d.get("account_kind") == "house":
                return _broker_sections(
                    "✏️ *Compra pendiente corregida*", f"*{d['customer']} · {d['amount']}*", "Cuenta: Casa",
                    _broker_customer_balance(d) if d.get("customer_balance") else f"📌 {d['customer']} debe: *{d['receivable_amount']}*",
                )
            return _broker_sections(
                "✏️ *Compra pendiente corregida*", f"*{d['customer']} · {d['amount']}*",
                f"Corredor: {d['broker']}\nComisión: {_positive(d['commission'])}",
                "\n".join(_purchase_receipt_lines(d)),
                _broker_customer_balance(d) if d.get("customer_balance") else f"📌 {d['customer']} debe: *{d['receivable_amount']}*", _broker_balance(d),
            )
        logical = d["logical_type"]
        labels = {
            "customer_payout": "Pago entregado", "receivable_payment": "Abono de cliente", "account_payment": "Abono a cuenta",
            "account_withdrawal": "Salida de cuenta",
            "account": "Compra propia", "buy": "Compra de divisas", "sell": "Venta de divisas",
        }
        title = f"✏️ *{labels.get(logical, 'Movimiento')} corregido*"
        if logical == "account_withdrawal":
            title = "✏️ *Salida de cuenta corregida*"
        if logical == "receivable_payment" and d.get("customer"):
            headline = f"*{d['customer']} pagó {d['amount']}*"
        elif logical == "account_payment":
            headline = f"*{d['broker']} abonó {d['amount']}*"
        elif logical == "account_withdrawal":
            headline = f"Monto: *{d['balance_effect']}*"
        elif logical == "account":
            headline = f"*Compra: {d['amount']}*"
        elif logical in {"buy", "sell"}:
            operation = "Compra" if logical == "buy" else "Venta"
            headline = f"*{d['customer']} · {d['amount']}*" if d.get("customer") else f"*{operation}: {d['amount']}*"
        else:
            headline = d["summary"]
        detail = "Cuenta: Casa" if d.get("account_kind") == "house" else f"Corredor: {d['broker']}"
        if d.get("account_kind") != "house" and logical in {"buy", "sell", "account"}:
            detail += f"\nComisión: {_positive(d['commission'])}"
            if d.get("operation_type") == "buy" and d.get("received") is not None:
                detail += "\n" + "\n".join(_purchase_receipt_lines(d))
            if d.get("balance_effect") is not None and (logical == "sell" or d.get("operation_type") == "buy"):
                detail += f"\nVariación del balance: {_signed_amount(d['balance_effect'])}"
        return _broker_sections(
            title, headline, detail,
            _broker_customer_balance(d) if d.get("customer_balance") else "",
            "" if d.get("account_kind") == "house" else _broker_balance(d),
        )
    if kind == "delete_broker_movement":
        linked = []
        if d.get("deleted_receivable"):
            linked.append(f"Deuda vinculada eliminada: {d['deleted_receivable']}")
        if d.get("deleted_payment"):
            linked.append(f"Abono vinculado eliminado: {d['deleted_payment']}")
        return _broker_sections(
            "🗑️ *Movimiento eliminado*", d["deleted"], "\n".join(linked),
            _broker_customer_balance(d) if d.get("customer_balance") else "",
            "" if d.get("account_kind") == "house" else _broker_balance(d),
        )
    if kind == "record_expense":
        e = d["expense"]
        cat = EXPENSE_CATEGORY_LABELS[ExpenseCategory(e["category"])]
        return (f"✅ *Gasto registrado*: {e['description']} — *{_legs_line(e['amount'])}* ({cat}){_method(e['payment_method'])}"
                + _real_cost(e.get("real_cost")) + _assumed(e["currency_assumed"]))
    if kind == "record_purchase":
        p = d["purchase"]
        text = f"✅ *Compra registrada*: {p['description']} — *{_legs_line(p['amount'])}*{_method(p['payment_method'])}"
        if p.get("supplier"):
            text += f"\nProveedor: {p['supplier']}"
        for i in p.get("items", []):
            prev = f" (antes {i['previous_unit_cost']})" if i.get("previous_unit_cost") else ""
            text += f"\n• {i['quantity']} × {i['product']} a {i['unit_cost']} c/u{prev}"
        if p.get("total_mismatch"):
            m = p["total_mismatch"]
            text += f"\n⚠️ Dijiste {m['stated_total']} pero los ítems suman {m['items_total']}; guardé la suma de los ítems."
        return text + _real_cost(p.get("real_cost")) + _assumed(p["currency_assumed"])
    if kind == "create_receivable":
        r = d["receivable"]
        return f"📒 *Fiado registrado* a {d['customer']}: {r['amount']}" + (f" ({r['description']})" if r.get("description") else "") + \
            _credit_used(d.get("credit_applied")) + _balance(d["customer_balance"], d["customer"]) + _assumed(r["currency_assumed"])
    if kind == "record_payment":
        p = d["payment"]
        text = f"💵 *Abono registrado* de {d['customer']}: {p['amount']}{_method(p['payment_method'])}"
        settled = [a for a in d["applied_to"] if a["settled"]]
        if settled:
            text += f"\nSaldó {len(settled)} fiado(s) completo(s)."
        if d.get("unapplied"):
            text += f"\nSobraron {d['unapplied']} como saldo a favor."
        return text + _balance(d["customer_balance"], d["customer"]) + _assumed(p["currency_assumed"])
    if kind == "edit_record":
        text = f"✏️ *Corregí {d['entity']}*\nAntes: {d['before']}\nAhora: {d['after']}"
        if bal := d.get("customer_balance"):
            text += f"\nSaldo del cliente: *{bal['display']}*" + _credit(bal)
        if prev := d.get("previous_customer_balance"):
            text += f"\n{prev['customer']} queda debiendo: *{prev['display']}*" + _credit(prev)
        return text
    if kind == "delete_record":
        text = f"🗑️ *Borré {d['entity']}*: {d['deleted']}"
        if bal := d.get("customer_balance"):
            text += f"\nSaldo del cliente: *{bal['display']}*" + _credit(bal)
        if d.get("note"):
            text += f"\n{d['note']}"
        return text
    if kind == "set_product_price":
        prev = f" (antes {d['previous_price']})" if d.get("previous_price") else ""
        return f"🏷️ Precio de *{d['product']}*: {d['price']}{prev}"
    if kind == "set_product_cost":
        prev = f" (antes {d['previous_cost']})" if d.get("previous_cost") else ""
        return f"🏷️ Costo de *{d['product']}*: {d['unit_cost']} c/u{prev}"
    if kind == "delete_product":
        text = f"🗑️ *Borré el producto* {d['product']} del catálogo."
        if d.get("movements"):
            text += "\nSus ventas y compras ya registradas siguen contando en las cifras."
        return text
    if kind == "set_rate":
        return _set_rate_text(d)
    if kind == "set_charge_rate_mode":
        rate = f" ({d['rate']} {d['unit']} hoy)" if d.get("rate") else ""
        return (f"💱 Listo: cobras con la tasa *{d['label']}*{rate}. Se aplicará a los próximos movimientos; "
                "los anteriores no cambian.")
    if kind == "record_exchange":
        return _exchange_text(d)
    if kind == "close_cash":
        return render_cash_summary(d["summary"], closed=True, reclosed=d.get("already_closed_before", False))
    if kind == "remember_note":
        return f"📝 Anotado: _{d['note']}_"
    return "✅ Listo."


def _real_cost(rc: dict[str, Any] | None) -> str:
    """Pagos en moneda local: cuánto costaron realmente (tasa real de tus Bs) vs la referencia oficial, con su nombre."""
    if not rc or not rc.get("known"):
        return ""
    return (f"\nCosto real: *{rc['usd']}* (tu tasa real {rc['rate']} {rc['unit']}) · "
            f"Referencia {rc['official_label']}: {rc['official_ref']}")


def _sale_text(d: dict[str, Any]) -> str:
    """Precio acordado, lo que entró de verdad (moneda, método y tasa), descuento y deuda: cada cosa con su nombre."""
    s = d["sale"]
    lines = [f"• {i['quantity']} × {i['product']} a {i['unit_price']} = {i['line_total']}" for i in s["items"]]
    text = "✅ *Venta registrada*\n" + "\n".join(lines)
    if s.get("discount"):
        text += f"\nPrecio habitual: {s['list_total']['display']} · Descuento: {s['discount']}"
    text += f"\nTotal: *{_legs_line(s['total'])}*"
    if s["payment_status"] == "credit":
        text += f"{_method(s['payment_method'])} · *fiada*"
    elif s.get("paid_in_other_currency") or s["payment_status"] == "partial":
        rate = f" (tasa {s['rate']['label']} {s['rate']['rate']})" if s.get("rate") and s.get("paid_in_other_currency") else ""
        text += f"\nCobrado: *{s['paid']}*{_method(s['payment_method'])}{rate}"
    else:
        text += _method(s["payment_method"])
    if d.get("customer") and s["payment_status"] == "paid":
        text += f"\nCliente: {d['customer']}"
    if rec := d.get("receivable"):
        today = f" (≈ {rec['pending_today']} hoy)" if rec.get("pending_today") else ""
        text += f"\nQuedó debiendo: *{rec['pending']}*{today}" + _credit_used(rec.get("credit_applied")) + \
            _balance(rec["customer_balance"], d.get("customer"))
    for i in s["items"]:
        if fp := i.get("price_from_payment"):
            how = f"{fp['paid']} ÷ tasa {fp['rate']}" if fp.get("rate") else fp["paid"]
            text += f"\n_Tomé el precio del pago: {how}._"
    if kept := s.get("price_kept"):
        text += f"\nEl precio habitual de {kept['product']} sigue siendo {kept['usual']} (esta venta: {kept['this_sale']})."
    for change in s.get("price_updated") or []:
        text += f"\n🏷️ Precio habitual de *{change['product']}*: {change['from']} → {change['to']}"
    if s.get("overpaid"):
        text += f"\n⚠️ Pagó {s['overpaid']} de más." + (f" {s['rate_warning']}" if s.get("rate_warning") else "")
    return text + _assumed(any(i["currency_assumed"] for i in s["items"]))


def _set_rate_text(d: dict[str, Any]) -> str:
    rate = f"{d['rate']} {d['unit']}"
    label = f" ({d['label']})" if d.get("label") else ""
    parts = []
    if d["purpose"] in ("charge", "both"):
        parts.append(f"💱 Actualicé tu *tasa para cobrar* a *{rate}*{label}. Se aplicará a los próximos movimientos; "
                     "los anteriores no cambian.")
    if d["purpose"] == "funds":
        parts.append(f"💱 Usaré *{rate}*{label} para calcular cuánto te cuestan realmente tus gastos en bolívares. "
                     "Esto no cambia la tasa con la que cobras a tus clientes.")
    if d["purpose"] == "both":
        parts.append("También la usaré como la *tasa real* de tus bolívares, para calcular cuánto te cuestan realmente tus gastos.")
    return "\n".join(parts)


def _exchange_text(d: dict[str, Any]) -> str:
    x = d["exchange"]
    same = x["out_method"] and x["out_method"] == x["in_method"]
    out_m = "" if same or not x["out_method"] else f" ({x['out_method']})"
    in_m = "" if same or not x["in_method"] else f" ({x['in_method']})"
    via = f" por {x['out_method']}" if same else ""
    text = f"💱 *Cambio registrado*: salieron {x['out']}{out_m} y entraron {x['in']}{in_m}{via}."
    if d.get("obtained_local"):
        text += f" Tu tasa real fue *{x['rate']} {x['unit']}*."
        if not d.get("funds_rate_set"):
            text += " (Tu tasa real de los bolívares sigue siendo la de un cambio más reciente.)"
    else:
        text += f" Los compraste a *{x['rate']} {x['unit']}*."
    return text + "\n_No cuenta como venta ni como gasto._"


def render_cash_summary(s: dict[str, Any], *, closed: bool, reclosed: bool = False) -> str:
    """Primero el dinero real (por método y moneda, sin mezclar), luego los cambios, luego el equivalente y los totales."""
    title = "🧾 *Cierre de caja*" if closed else "🧾 *Caja de hoy (sin cerrar)*"
    if reclosed:
        title += " _(actualizado)_"
    lines = [f"{title} — {s['date_label']}"]
    if s["by_payment_method_net"]:
        lines.append("*Dinero real (neto por método):*")
        for method, by_currency in s["by_payment_method_net"].items():
            lines.append(f"• {method}: {' · '.join(by_currency.values())}")
    for x in s.get("exchanges") or []:
        lines.append(f"💱 Cambio: {x['text']} (a {x['rate']})")
    if report := s.get("report"):
        lines.append(f"*Equivalente para reporte: {report['equivalent']}* _(usando {report['using']})_")
    lines += [
        f"_Resumen {s.get('view_title', '')}:_",
        f"Ventas: {s['sales']['count']} por {s['sales']['total']} (cobrado {s['sales']['collected']})",
        f"Abonos recibidos: {s['payments_in']['total']}",
        f"Gastos: {s['expenses']['total']} · Compras: {s['purchases']['total']}",
        f"Fiado nuevo: {s['new_credit']}",
    ]
    return "\n".join(lines)


_LEGACY_PREVIEW_REPLACEMENTS = [
    ("✅ *Venta registrada*", "*Venta*"), ("✅ *Gasto registrado*", "*Gasto*"), ("✅ *Compra registrada*", "*Compra*"),
    ("📒 *Fiado registrado*", "📒 *Fiado*"), ("Quedó debiendo:", "Quedaría debiendo:"), ("sigue siendo", "seguiría siendo"), ("💵 *Abono registrado*", "💵 *Abono*"), ("✏️ *Corregí ", "✏️ *Corregir "),
    ("🗑️ *Borré ", "🗑️ *Borrar "), ("ya registradas siguen contando", "ya registradas seguirían contando"), ("debe ahora:", "quedaría debiendo:"), ("queda debiendo:", "quedaría debiendo:"),
    ("Saldo del cliente:", "Saldo del cliente quedaría:"), ("Queda fiado:", "Quedaría fiado:"), ("Se descontó ", "Se descontaría "), ("\nAhora: ", "\nQuedaría: "),
    ("Balance actual:", "El balance quedaría en:"), ("balance actual:", "el balance quedaría en:"),
    ("💱 *Cambio registrado*", "💱 *Cambio*"), ("Tu tasa real fue", "Tu tasa real sería"), ("Los compraste a", "Los comprarías a"),
    ("Actualicé tu *tasa para cobrar*", "Actualizaría tu *tasa para cobrar*"), ("Se aplicará a los próximos", "Se aplicaría a los próximos"),
    ("Usaré *", "Usaría *"), ("También la usaré", "También la usaría"),
]


def render_preview(result: CommandResult) -> str:
    """Vista previa determinista de una escritura aún no persistida.

    Los recibos de corredores no se convierten con sustituciones: sus verbos y
    resultados futuros se construyen directamente desde el resultado calculado
    por el comando que quedará congelado al confirmar.
    """
    d = result.data
    if result.kind in {"reconcile_account_balance", "delete_balance_adjustment"}:
        label = "Cliente" if d["account_type"] == "customer" else "Corredor"
        today = _reconciliation_date(d['today'])[:5]
        title = "Confirmar eliminación de ajuste" if d.get("deleting") else "Confirmar ajuste"
        balance = Decimal(d["new_balance"])
        explanation = (f"{d['holder']} quedará debiendo {fmt_amount(abs(balance), 'USD')}." if balance < 0 else
                       f"{d['holder']} quedará con {fmt_amount(balance, 'USD')} a favor." if balance > 0 else
                       f"{d['holder']} quedará con la cuenta saldada.")
        return _broker_sections(f"*{title} — {d['holder']} ({label})*",
            f"Fecha de ajuste: {_reconciliation_date(d['business_date'])}",
            f"Saldo actual ({today}): {_reconciliation_money(d['current_balance'])}",
            f"Saldo registrado al corte: {_reconciliation_money(d['cutoff_balance'])}",
            f"Variación posterior al corte: {_reconciliation_money(str(Decimal(d['current_balance']) - Decimal(d['cutoff_balance'])))}",
            f"Saldo histórico declarado: {_reconciliation_money(d['declared_balance'])}",
            f"Ajuste a registrar: {_reconciliation_money(d['adjustment_delta'])}",
            f"*Nuevo saldo actual ({today}): {_reconciliation_money(d['new_balance'])}*", explanation)
    if result.kind in {"attach_broker_attachment", "complete_broker_receipt"}:
        return _broker_sections("📎 *Comprobante por asociar · Sin guardar*", d["movement"]) + _attachment({**d, "attachment": False})
    if result.kind == "record_broker_customer_payout":
        return _broker_sections("💵 *Pago entregado · Sin guardar*", f"Casa entregaría {d['amount']} a {d['customer']}",
                                _broker_customer_balance(d, preview=True))
    if result.kind == "record_broker_operation" and d.get("settlement") == "pending":
        return _broker_sections("✅ *Venta pendiente · Sin guardar*", f"{d['customer']} · {d['operation']}",
                                _broker_money(d), _broker_customer_balance(d, preview=True),
                                _broker_balance(d, preview=True))
    if result.kind == "record_pending_broker_purchase":
        details = (
            f"Comisión: {_positive(d['commission'])}\n" + (_broker_customer_balance(d, preview=True) if d.get("customer_balance") else f"{d['customer']} quedaría debiendo: {d['amount']}") + "\n"
            + _broker_balance(d, preview=True)
            if d.get("account_kind") != "house"
            else _broker_customer_balance(d, preview=True) if d.get("customer_balance") else f"{d['customer']} quedaría debiendo: {d['amount']}"
        )
        return _broker_sections(
            "📒 *Compra pendiente · Sin guardar*", f"*{d['customer']} · {d['amount']}*",
            "" if d.get("account_kind") == "house" else f"Corredor: {d['broker']}",
            "" if d.get("account_kind") == "house" else "\n".join(_purchase_receipt_lines(d)), details,
        )
    if result.kind == "record_broker_receivable_payment":
        return _broker_sections(
            "💵 *Abono de cliente · Sin guardar*", f"*{d['customer']} pagaría {d['amount']}*",
            "" if d.get("account_kind") == "house" else f"Corredor: {d['broker']}",
            _broker_customer_balance(d, preview=True),
            "" if d.get("account_kind") == "house" else _broker_balance(d, preview=True),
        )
    if result.kind == "record_broker_account_payment":
        return _broker_sections(
            "💵 *Abono a cuenta · Sin guardar*", f"*{d['broker']} abonaría {d['movement']['balance_delta']}*",
            _note(d['movement'].get('description')), _broker_balance(d, preview=True),
        )
    if result.kind == "record_broker_account_withdrawal":
        return _broker_sections(
            "✅ *Salida de cuenta · Sin guardar*", f"Monto: *{d['movement']['balance_delta']}*",
            f"Corredor: {d['broker']}", _note(d['movement'].get('description')), _broker_balance(d, preview=True),
        )
    if result.kind == "record_broker_account_purchase":
        return _broker_sections(
            "✅ *Compra propia · Sin guardar*", f"*Compra: {d['operation']}*",
            f"Corredor: {d['broker']}\nComisión: {_positive(d['commission'])}",
            "\n".join(_purchase_receipt_lines(d)),
            _broker_balance(d, preview=True),
        )
    if result.kind == "record_broker_operation":
        label = "Compra" if d["operation_type"] == "buy" else "Venta"
        headline = f"*{d['customer']} · {d['operation']}*" if d.get("customer") else f"*{label}: {d['operation']}*"
        details = []
        if d.get("account_kind") != "house":
            details.append(f"Corredor: {d['broker']}")
            details.append(f"Comisión: {_positive(d['commission'])}")
        if d.get("account_kind") != "house":
            if d["operation_type"] == "buy":
                details.extend(_purchase_receipt_lines(d))
            if d.get("balance_effect") is not None:
                details.append(f"Variación del balance: {_signed_amount(d['balance_effect'])}")
        if d.get("account_kind") != "house":
            details.append(_broker_balance(d, preview=True))
        return _broker_sections(f"✅ *{label} de divisas · Sin guardar*", headline, "\n".join(details))
    if result.kind in {"edit_broker_movement", "delete_broker_movement"}:
        action = "corregir" if result.kind == "edit_broker_movement" else "eliminar"
        summary = d.get("summary") or d.get("deleted")
        return _broker_sections(
            f"⚠️ *Movimiento por {action} · Sin guardar*", summary,
            "" if d.get("account_kind") == "house" else _broker_balance(d, preview=True),
        )

    # Los receipts fuera de corredores siguen teniendo sus previews históricos;
    # este fallback conserva su contrato mientras migren a renderers explícitos.
    body = _render_receipt(result)
    for past, future in _LEGACY_PREVIEW_REPLACEMENTS:
        body = body.replace(past, future)
    return body


def confirmation_block(reason: str, preview: str, *, with_buttons: bool = False) -> str:
    """Presenta una vista previa ya redactada en condicional (aún no se guardó).

    Con botones [Sí]/[No] no hace falta decir cómo responder; en texto, sí.
    """
    if preview.startswith(("*Confirmar ajuste —", "*Confirmar eliminación de ajuste —")):
        return f"{preview}\n\n¿Confirmas?"
    if "· Sin guardar*" in preview and "¿Quieres registrarlo de todos modos?" in reason:
        return f"⚠️ {reason}\n\n{preview}"
    ask = "¿Lo hago?" if with_buttons else "¿Lo hago? Responde *sí* o *no*."
    return f"⚠️ {reason}\n\n{preview}\n\n{ask}"


def panel_cta(url: str, *, welcome: bool = False) -> str:
    """El CTA al panel, al pie del recibo. Lo pone el sistema: el modelo nunca escribe enlaces (ver `system.py`).

    `welcome` es el primer enlace de la vida del negocio, el que sigue al primer movimiento después de
    configurarse. Es el único que dice en voz alta la relación entre los dos lados, porque es la única vez
    que hace falta decirla.
    """
    if welcome:
        return f"📊 Mira cómo quedó en tu panel: {url}\n_Todo lo que me digas por aquí aparece organizado allá._"
    return f"📊 Abrir en el panel: {url}"
