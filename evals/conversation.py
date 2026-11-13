"""Checks de UX para modelos reales. No participan en routing ni en la ejecución del negocio.

Son checks conservadores de texto, no un juez semántico: revisar también turn_replies en el reporte.
"""

import re
from typing import Any

from app.application.choices import Choice
from app.channels.types import OutboundInteractive
from app.domain.enums import BusinessVertical


def visible_reply_text(reply: dict[str, Any]) -> str:
    """Include the choice labels that the owner sees beside an interactive body."""
    choices = reply.get("choices") or []
    if not choices:
        return reply["text"]
    interactive = OutboundInteractive(reply["text"], tuple(
        Choice(choice["id"], choice["title"], choice.get("description")) for choice in choices
    ))
    return interactive.as_text()


def uncertain_receipt_reply(text: str) -> bool:
    text = text.casefold()
    has_bolivars = "bol" in text or re.search(r"\bbs\b", text) is not None
    has_amount_context = any(word in text for word in (
        "capture", "comprobante", "imagen", "transferencia", "monto",
    ))
    asks_for_clarification = any(word in text for word in ("?", "dime", "envíame", "identificar"))
    return has_bolivars and has_amount_context and asks_for_clarification


def business_reply(text: str, vertical: BusinessVertical) -> bool:
    text = text.casefold()
    examples = ("venta", "vend", "gasto", "compra", "fiado", "cuentas por cobrar", "caja", "resultado", "pagu", "abon")
    if vertical == BusinessVertical.BROKERAGE:
        examples = ("divisas", "operaci", "balance", "saldo", "deuda", "cartera", "comision", "comisión",
                    "compra", "venta", "abono", "resultado")
        if any(word in text for word in ("inventario", "bujías", "gastos", "mercancía")):
            return False
    return (
        any(word in text for word in examples)
        and len(text) < 700
        and not any(word in text for word in ("con lo que necesites", "cualquier tema", "asistente general"))
    )


def operational_reply(text: str) -> bool:
    return not any(word in text.casefold() for word in ("soy bolos", "soy *bolos*", "puedo ayudarte", "¿qué necesitas"))


def repeated_greeting_reply(text: str) -> bool:
    # El primer turno ya presenta las capacidades. El siguiente puede referirse a registrar/anotar sin reenumerarlas.
    return (
        operational_reply(text) and len(text) < 300
        and any(word in text.casefold() for word in ("negocio", "registr", "anot", "operaci", "saldo", "venta", "gasto"))
    )
