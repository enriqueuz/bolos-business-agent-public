"""Generate a reproducible, synthetic workbook without connecting to a database.

Run from the repository root: python -m scripts.example_brokerage_workbook --output /tmp/ejemplo.xlsx
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

from app.application.exports.brokerage import render_workbook
from app.application.exports.brokerage_data import (
    BrokerRow,
    CustomerAccountRow,
    CustomerEventRow,
    DebtRow,
    MovementRow,
    PaymentRow,
    WorkbookData,
)


def example_data() -> WorkbookData:
    def movement(key, day, account, operation="", **kwargs):
        if operation:
            kwargs.setdefault("original_settlement", "Pendiente" if kwargs.get("type") == "Compra pendiente" else "Cuenta propia" if kwargs.get("type") == "Compra propia" else "Liquidada")
        return MovementRow(key, date(2026, 11, day), account, "Samu (demo)" if account == "samu" else "Lía (demo)" if account else "Casa",
                           operation=operation, recorded_by="Dueño (demo)", **kwargs)

    rows = (
        movement("compra-casa", 2, "", "buy", type="Compra inmediata", usd=D("100"), bs=D("78000"), rate=D("780"), basis="usd_bs", status="Liquidado"),
        movement("compra-excedente", 3, "samu", "buy", type="Compra inmediata", customer="Gamal (demo)", classification="Global", portfolio="Global / Casa", usd=D("987"), bs=D("789600"), rate=D("800"), basis="usd_bs", received=D("1000"), excess=D("13"), commission=D("9.87"), delta=D("22.87"), status="Liquidado"),
        movement("compra-personal", 4, "samu", "buy", type="Compra pendiente", customer="Ana (demo)", classification="Personal", portfolio="Samu (demo)", usd=D("500"), bs=D("400000"), rate=D("800"), basis="usd_rate", commission=D("5"), delta=D("-495"), status="Parcial"),
        movement("compra-global", 5, "samu", "buy", type="Compra pendiente", customer="Luis (demo)", classification="Global", portfolio="Global / Casa", usd=D("1000"), bs=D("800000"), rate=D("800"), basis="usd_rate", commission=D("10"), delta=D("10"), status="Parcial"),
        movement("compra-propia", 6, "samu", "buy", type="Compra propia", usd=D("200"), bs=D("160000"), rate=D("800"), basis="usd_rate", commission=D("2"), delta=D("-198"), status="Cuenta propia"),
        movement("venta-corredor", 10, "samu", "sell", type="Venta", usd=D("200"), bs=D("164000"), rate=D("820"), basis="usd_rate", commission=D("2"), delta=D("2"), status="Liquidado"),
        movement("venta-casa", 12, "", "sell", type="Venta", usd=D("200"), bs=D("164000"), rate=D("820"), basis="usd_bs", status="Liquidado"),
        movement("abono-personal", 15, "samu", type="Abono de cliente", customer="Ana (demo)", classification="Personal", portfolio="Samu (demo)", usd=D("200"), delta=D("200")),
        movement("abono-global", 18, "lia", type="Abono de cliente", customer="Luis (demo)", classification="Global", portfolio="Global / Casa", usd=D("400")),
        movement("abono-cuenta", 20, "samu", type="Abono a cuenta", usd=D("50"), delta=D("50")),
        movement("venta-fin-mes", 30, "", "sell", type="Venta", usd=D("100"), bs=D("83000"), rate=D("830"), basis="usd_bs", status="Liquidado"),
    )
    return WorkbookData(
        business="Casa de Cambio · DEMOSTRACIÓN SINTÉTICA", start=date(2026, 11, 1), end=date(2026, 11, 30),
        generated_at=datetime(2026, 12, 1, 12, tzinfo=UTC), movements=rows,
        customer_accounts=(
            CustomerAccountRow("eva", "Eva (demo)", "Global", "Global / Casa", D("-200"), D("-200")),
            CustomerAccountRow("ana", "Ana (demo)", "Personal", "Samu (demo)", closing=D("-300")),
            CustomerAccountRow("luis", "Luis (demo)", "Global", "Global / Casa", closing=D("-600")),
        ),
        customer_events=(
            CustomerEventRow("compra-personal", "ana", "Ana (demo)", "Personal", "Samu (demo)", date(2026, 11, 4), "Compra pendiente", D("500"), D("-500"), "buy"),
            CustomerEventRow("compra-global", "luis", "Luis (demo)", "Global", "Global / Casa", date(2026, 11, 5), "Compra pendiente", D("1000"), D("-1000"), "buy"),
            CustomerEventRow("abono-personal", "ana", "Ana (demo)", "Personal", "Samu (demo)", date(2026, 11, 15), "Pago recibido", D("200"), D("200")),
            CustomerEventRow("abono-global", "luis", "Luis (demo)", "Global", "Global / Casa", date(2026, 11, 18), "Pago recibido", D("400"), D("400")),
        ),
        brokers=(BrokerRow("samu", "Samu (demo)", opening=D("-297")), BrokerRow("lia", "Lía (demo)", False, D("100"))),
        debts=(
            DebtRow("deuda-anterior", date(2026, 10, 20), "Eva (demo)", "Global", "Global / Casa", D("300"), D("100")),
            DebtRow("deuda-personal", date(2026, 11, 4), "Ana (demo)", "Personal", "Samu (demo)", D("500"), paid_period=D("200")),
            DebtRow("deuda-global", date(2026, 11, 5), "Luis (demo)", "Global", "Global / Casa", D("1000"), paid_period=D("400")),
        ),
        payments=(
            PaymentRow("pago-personal", "deuda-personal", date(2026, 11, 15), "Ana (demo)", "Personal", "Samu (demo)", applied=D("200"), total=D("200"), account="Samu (demo)"),
            PaymentRow("pago-global", "deuda-global", date(2026, 11, 18), "Luis (demo)", "Global", "Global / Casa", applied=D("400"), total=D("400"), account="Lía (demo)"),
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(render_workbook(example_data()))
    print(f"Excel sintético: {args.output}")


if __name__ == "__main__":
    main()
