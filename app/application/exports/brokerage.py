"""Self-contained, editable Casa de Cambio workbook, using persisted financial effects."""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from uuid import UUID

import xlsxwriter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.exports.brokerage_data import ZERO, WorkbookData, load_workbook_data

LOGO = Path(__file__).parents[2] / "assets" / "bolos-negocios-logo.png"


def workbook_filename(start: date, end: date) -> str:
    period = start.strftime("%Y-%m") if start.day == 1 and start.year == end.year and start.month == end.month and end.day == calendar.monthrange(end.year, end.month)[1] else f"{start.isoformat()}_{end.isoformat()}"
    return f"Bolos_Casa_de_Cambio_{period}.xlsx"


@dataclass(frozen=True)
class Formula:
    expression: str
    value: Decimal | int | str


class BrokerageWorkbook:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def build(
        self, business_id: UUID, *, date_from: date, date_to: date,
        broker_id: UUID | None = None, account_scope: str = "all",
    ) -> bytes:
        data = await load_workbook_data(self._factory, business_id, start=date_from, end=date_to,
                                        broker_id=broker_id, account_scope=account_scope)
        return render_workbook(data)


def render_workbook(data: WorkbookData) -> bytes:
    """Pure rendering entrypoint, also used by the synthetic example and workbook tests."""
    return _Renderer(data).render()


class _Renderer:
    def __init__(self, data: WorkbookData):
        self.data = data
        self.output = BytesIO()
        self.book = xlsxwriter.Workbook(self.output, {
            "in_memory": True, "strings_to_formulas": False, "strings_to_urls": False,
        })
        self.book.set_properties({"title": "Bolos · Casa de Cambio", "company": "Bolos Negocios",
                                  "comments": "Copia editable independiente; no sincroniza cambios con Bolos."})
        self.book.set_calc_mode("auto")
        self.formats = self._formats()

    def _formats(self):
        base = {"font_name": "Calibri", "font_size": 11, "font_color": "#0D131C", "valign": "vcenter"}
        specs = {
            "text": {}, "title": {"bold": True, "font_size": 20},
            "note": {"font_color": "#5B6470", "text_wrap": True, "font_size": 10},
            "section": {"bold": True, "bg_color": "#DCECDF", "font_color": "#235A38"},
            "header": {"bold": True, "bg_color": "#0D131C", "font_color": "#FFFFFF", "text_wrap": True},
            "money": {"num_format": '#,##0.00;[Red](#,##0.00);"—"'},
            "usd": {"num_format": '"USD "#,##0.00;[Red]("USD "#,##0.00);"—"'},
            "bs": {"num_format": '"Bs "#,##0.00;[Red]("Bs "#,##0.00);"—"'},
            "percent": {"num_format": "0.00%;[Red](0.00%);—"},
            "date": {"num_format": "dd/mm/yyyy"},
            "input": {"bg_color": "#FFF7DD", "num_format": "#,##0.00"},
            "formula": {"bg_color": "#EDF5EF", "num_format": "#,##0.00;[Red](#,##0.00);—"},
            "formula_text": {"bg_color": "#EDF5EF"},
        }
        return {name: self.book.add_format(base | spec) for name, spec in specs.items()}

    def sheet(self, name: str, note: str):
        sheet = self.book.add_worksheet(name)
        sheet.hide_gridlines(2)
        sheet.set_default_row(22)
        sheet.set_column(0, 35, 20)
        sheet.set_row(0, 38)
        if LOGO.exists():
            sheet.insert_image("A1", str(LOGO), {"x_scale": 0.065, "y_scale": 0.065, "x_offset": 2, "y_offset": 4})
        sheet.merge_range("C1:H1", name, self.formats["title"])
        sheet.merge_range("A2:L2", f"{self.data.business} · {self.data.scope}", self.formats["note"])
        sheet.merge_range("A3:L3", f"Período: {self.data.start:%d/%m/%Y} — {self.data.end:%d/%m/%Y} · Corte: {self.data.end:%d/%m/%Y} · Generado: {self.data.generated_at:%d/%m/%Y %H:%M} UTC", self.formats["note"])
        sheet.merge_range("A4:L4", note, self.formats["note"])
        sheet.set_row(3, 34)
        sheet.freeze_panes(7, 2)
        sheet.set_landscape()
        sheet.set_paper(9)
        sheet.fit_to_pages(1, 0)
        sheet.set_margins(0.3, 0.3, 0.4, 0.4)
        sheet.repeat_rows(0, 6)
        sheet.set_footer("&LBolos · Copia editable&C&P / &N")
        return sheet

    def write(self, sheet, row, col, value, fmt="text"):
        style = self.formats[fmt]
        if isinstance(value, Formula):
            # Excel displays @ shorthand, but OOXML requires #This Row in cell
            # formulas too. add_table converts its formulas; write_formula doesn't.
            expression = re.sub(r"\[@(?:\[([^\]]+)\]|([^\]]+))\]",
                                lambda m: f'[[#This Row],[{m[1] or m[2]}]]', value.expression)
            sheet.write_formula(row, col, expression, style, value.value)
        elif isinstance(value, Decimal):
            sheet.write_number(row, col, value, style)
        elif isinstance(value, (date, datetime)):
            sheet.write_datetime(row, col, value, self.formats["date"])
        elif value is None:
            sheet.write_blank(row, col, None, style)
        else:
            sheet.write(row, col, value, style)

    def table(self, sheet, name, headers, rows, *, col=0, row=6, money=(), dates=(), hidden=(), calculated=None):
        calculated = calculated or {}
        columns = []
        for i, header in enumerate(headers):
            spec = {"header": header, "header_format": self.formats["header"]}
            if i in money:
                spec["format"] = self.formats["money"]
            if i in dates:
                spec["format"] = self.formats["date"]
            if header in calculated:
                spec["formula"] = calculated[header]
            columns.append(spec)
        sheet.add_table(row, col, row + max(1, len(rows)), col + len(headers) - 1, {
            "name": name, "columns": columns, "style": "Table Style Medium 4",
        })
        sheet.set_row(row, 38)
        for i, values in enumerate(rows or [[None] * len(headers)], start=row + 1):
            for j, value in enumerate(values):
                if not rows and headers[j] in calculated:
                    value = Formula(calculated[headers[j]], "")
                fmt = "date" if j in dates else ("formula" if j in money else "formula_text") if isinstance(value, Formula) else "money" if j in money else "text"
                self.write(sheet, i, col + j, value, fmt)
        for i in hidden:
            sheet.set_column(col + i, col + i, None, None, {"hidden": True})

    def render(self):
        # Workbook order is part of the public export contract.
        summary = self.sheet("Resumen", "Corte operativo con registros vigentes: correcciones o eliminaciones posteriores pueden cambiar otra descarga. Editar esta copia no modifica Bolos.")
        self.operations("Compra", "buy")
        self.operations("Venta", "sell")
        self.receivables()
        self.brokers()
        self.adjustments()
        self.summary(summary)
        self.book.close()
        return self.output.getvalue()

    def operations(self, name, kind):
        buy = kind == "buy"
        sheet = self.sheet(name, "Amarillo: entradas USD/Bs o USD/tasa. Verde: fórmulas. Comisión, excedente y efecto en balance son importes reconocidos; no se recalculan al editar el nominal.")
        nominal, bs, rate = ("Compra USD", "Compra Bs", "Tasa de compra") if buy else ("Venta USD", "Venta Bs", "Tasa de venta")
        headers = ["Fecha", "Cliente", nominal, bs, rate, "Cuenta / corredor", "Tipo de operación", "Tipo de cliente", "Cartera responsable"]
        if buy:
            headers += ["USD recibidos", "Excedente USD"]
        headers += ["Modalidad original", "Comisión USD", "Efecto en balance USD", "Estado de liquidación", "Descripción", "Editar", "Datos monetarios", "ID movimiento", "USD original", "Bs originales", "Tasa original"]
        positions = {h: i for i, h in enumerate(headers)}
        complete = f'=IF([@[{nominal}]]="","",IF(AND(ISNUMBER([@[{nominal}]]),[@[{nominal}]]>0,ISNUMBER([@[{bs}]]),[@[{bs}]]>0,ISNUMBER([@[{rate}]]),[@[{rate}]]>0),"Completa","Incompleta"))'
        rows = []
        for m in self.data.movements:
            if m.operation != kind:
                continue
            # USD/Bs preserves Bs and derives rate. USD/rate preserves rate and derives Bs.
            # Original values also preserve explicit triples accepted within backend tolerance.
            usd_bs = m.basis != "usd_rate"
            rate_formula = f'=IFERROR(IF(OR([@[{nominal}]]<=0,[@[{bs}]]<=0),"",IF(AND([@[{nominal}]]=[@[USD original]],[@[{bs}]]=[@[Bs originales]],ISNUMBER([@[Tasa original]])),[@[Tasa original]],ROUND([@[{bs}]]/[@[{nominal}]],2))),"")'
            bs_formula = f'=IFERROR(IF(OR([@[{nominal}]]<=0,[@[{rate}]]<=0),"",IF(AND([@[{nominal}]]=[@[USD original]],[@[{rate}]]=[@[Tasa original]],ISNUMBER([@[Bs originales]])),[@[Bs originales]],ROUND([@[{nominal}]]*[@[{rate}]],2))),"")'
            values = [m.day, m.customer, m.usd, m.bs if usd_bs else Formula(bs_formula, m.bs if m.bs is not None else ""), Formula(rate_formula, m.rate if m.rate is not None else "") if usd_bs else m.rate,
                      m.account, m.type, m.classification, m.portfolio]
            if buy:
                values += [m.received, m.excess]
            values += [m.original_settlement, m.commission, m.delta, m.status, m.description, "USD y Bs" if usd_bs else "USD y tasa",
                       Formula(complete, "Completa" if m.bs is not None and m.rate is not None else "Incompleta"),
                       m.key, m.usd, m.bs, m.rate]
            rows.append(values)
        money = [i for i, h in enumerate(headers) if h in {nominal, bs, rate, "USD recibidos", "Excedente USD", "Comisión USD", "Efecto en balance USD", "USD original", "Bs originales", "Tasa original"}]
        self.table(sheet, "Compras" if buy else "Ventas", headers, rows, money=money, dates=(0,),
                   hidden=range(positions["ID movimiento"], len(headers)), calculated={"Datos monetarios": complete})
        sheet.set_column(0, 0, 13)
        sheet.set_column(1, 1, 26)
        sheet.set_column(positions["Descripción"], positions["Descripción"], 38)
        for i, m in enumerate([m for m in self.data.movements if m.operation == kind], 7):
            sheet.write_number(i, 2, m.usd, self.formats["input"])
            input_col = 3 if m.basis != "usd_rate" else 4
            self.write(sheet, i, input_col, m.bs if input_col == 3 else m.rate, "input")
        sheet.write("A6", "Totales completos del período en Resumen; los filtros de esta tabla no modifican esos totales.", self.formats["note"])

    def receivables(self):
        sheet = self.sheet("Clientes y CxC", "Saldo firmado: positivo a favor del cliente; negativo por cobrar. Cada cliente tiene una sola cuenta. La modalidad original y los abonos históricos no son obligaciones adicionales.")
        headers = ["Cliente", "Tipo de cliente", "Cartera responsable", "Saldo inicial USD",
                   "Compras pendientes USD", "Ventas pendientes USD", "Pagos recibidos USD",
                   "Pagos entregados USD", "Ajustes de saldo USD", "Saldo neto USD", "Por cobrar USD", "A favor USD", "Estado", "ID cliente"]
        formulas = {}
        for col, type_ in [("Compras pendientes USD", "Compra pendiente"), ("Ventas pendientes USD", "Venta pendiente"),
                           ("Pagos recibidos USD", "Pago recibido"), ("Pagos entregados USD", "Pago entregado"),
                           ("Ajustes de saldo USD", "Ajuste de saldo")]:
            formulas[col] = f'=SUMIFS(MovimientosCliente[Importe USD],MovimientosCliente[ID cliente],[@[ID cliente]],MovimientosCliente[Tipo],"{type_}")'
        formulas.update({"Saldo neto USD": '=[@[Saldo inicial USD]]-[@[Compras pendientes USD]]+[@[Ventas pendientes USD]]+[@[Pagos recibidos USD]]-[@[Pagos entregados USD]]+[@[Ajustes de saldo USD]]',
                         "Por cobrar USD": '=MAX(0,-[@[Saldo neto USD]])', "A favor USD": '=MAX(0,[@[Saldo neto USD]])',
                         "Estado": '=IF([@[Saldo neto USD]]=0,"Saldado",IF([@[Saldo neto USD]]>0,"A favor del cliente","Cliente debe"))'})
        rows = []
        for c in self.data.customer_accounts:
            totals = {type_: sum((e.amount for e in self.data.customer_events if e.customer_key == c.key and e.type == type_), ZERO)
                      for type_ in ("Compra pendiente", "Venta pendiente", "Pago recibido", "Pago entregado", "Ajuste de saldo")}
            rows.append([c.customer, c.classification, c.portfolio, c.opening,
                         *[Formula(formulas[col], totals[type_]) for col, type_ in [
                             ("Compras pendientes USD", "Compra pendiente"), ("Ventas pendientes USD", "Venta pendiente"),
                             ("Pagos recibidos USD", "Pago recibido"), ("Pagos entregados USD", "Pago entregado"),
                           ("Ajustes de saldo USD", "Ajuste de saldo")]],
                         Formula(formulas["Saldo neto USD"], c.closing), Formula(formulas["Por cobrar USD"], max(-c.closing, ZERO)),
                         Formula(formulas["A favor USD"], max(c.closing, ZERO)),
                         Formula(formulas["Estado"], "Saldado" if c.closing == ZERO else "A favor del cliente" if c.closing > ZERO else "Cliente debe"), c.key])
        self.table(sheet, "Cartera", headers, rows, money=range(3, 12), hidden=(13,), calculated=formulas)
        eheaders = ["Fecha", "Cliente", "Tipo de cliente", "Cartera responsable", "Tipo", "Importe USD", "Efecto cliente USD", "ID cliente", "ID movimiento"]
        erows = []
        effect = '=IF(OR([@Tipo]="Compra pendiente",[@Tipo]="Pago entregado"),-[@[Importe USD]],[@[Importe USD]])'
        for e in self.data.customer_events:
            amount = e.amount
            if e.operation:
                table, nominal = ("Compras", "Compra USD") if e.operation == "buy" else ("Ventas", "Venta USD")
                amount = Formula(f'=SUMIF({table}[ID movimiento],[@[ID movimiento]],{table}[{nominal}])', e.amount)
            erows.append([e.day, e.customer, e.classification, e.portfolio, e.type, amount,
                          Formula(effect, e.delta), e.customer_key, e.key])
        self.table(sheet, "MovimientosCliente", eheaders, erows, col=15, dates=(0,), money=(5, 6), hidden=(7, 8),
                   calculated={"Efecto cliente USD": effect})
        sheet.write("P6", "Movimientos reales del período. Compras y ventas toman el nominal de sus hojas; pagos son importes editables. Apertura incluye toda la actividad anterior.", self.formats["note"])
        sheet.set_column(0, 0, 26)

    def brokers(self):
        sheet = self.sheet("Corredores", "Saldos por suma de efectos persistidos del ledger. Casa no es un corredor. Cambiar el nominal en Compra/Venta no recalcula estos efectos reconocidos.")
        headers = ["Corredor", "Acceso", "Balance inicial USD", "Entradas USD", "Salidas USD", "Compras propias USD", "Comisiones USD", "Excedentes USD", "Abonos a cuenta USD", "Variación USD", "Balance final USD", "ID corredor", "Salidas de cuenta USD"]
        rows = []
        for b in self.data.brokers:
            moves = [m for m in self.data.movements if m.account_key == b.key]
            def total(attr, selected=moves):
                return sum((getattr(m, attr) for m in selected), ZERO)
            delta = total("delta")
            ref = 'Diario[ID corredor],[@[ID corredor]]'
            rows.append([b.name, "Activo" if b.active else "Inactivo", b.opening,
                         Formula(f'=SUMIFS(Diario[Entrada USD],{ref})', sum((max(m.delta, ZERO) for m in moves), ZERO)),
                         Formula(f'=SUMIFS(Diario[Salida USD],{ref})', sum((max(-m.delta, ZERO) for m in moves), ZERO)),
                         Formula(f'=SUMIFS(Diario[Monto USD],{ref},Diario[Tipo],"Compra propia")', total("usd", [m for m in moves if m.type == "Compra propia"])),
                         Formula(f'=SUMIFS(Diario[Comisión USD],{ref})', total("commission")),
                         Formula(f'=SUMIFS(Diario[Excedente USD],{ref})', total("excess")),
                         Formula(f'=SUMIFS(Diario[Monto USD],{ref},Diario[Tipo],"Abono a cuenta")', total("usd", [m for m in moves if m.type == "Abono a cuenta"])),
                         Formula(f'=SUMIFS(Diario[Variación USD],{ref})', delta),
                         Formula('=[@[Balance inicial USD]]+[@[Variación USD]]', b.opening + delta), b.key,
                         Formula(f'=SUMIFS(Diario[Monto USD],{ref},Diario[Tipo],"Salida de cuenta")', total("usd", [m for m in moves if m.type == "Salida de cuenta"]))])
        self.table(sheet, "Cuentas", headers, rows, money=(*range(2, 11), 12), hidden=(11,),
                   calculated={h: rows[0][i].expression for i, h in enumerate(headers) if rows and isinstance(rows[0][i], Formula)})
        dheaders = ["Fecha", "Corredor", "Tipo", "Cliente", "Monto USD", "Comisión USD", "Excedente USD", "Variación USD", "Entrada USD", "Salida USD", "Balance USD", "Descripción", "Registrado por", "ID corredor", "Orden", "ID movimiento"]
        drows = []
        balances = {b.key: b.opening for b in self.data.brokers}
        entry, exit_ = '=MAX(0,[@[Variación USD]])', '=MAX(0,-[@[Variación USD]])'
        running = '=SUMIFS(Cuentas[Balance inicial USD],Cuentas[ID corredor],[@[ID corredor]])+SUMIFS(Diario[Variación USD],Diario[ID corredor],[@[ID corredor]],Diario[Orden],"<="&[@Orden])'
        for i, m in enumerate(self.data.movements, 1):
            if not m.account_key:
                continue
            balances[m.account_key] = balances.get(m.account_key, ZERO) + m.delta
            drows.append([m.day, m.account, m.type, m.customer, m.usd, m.commission, m.excess, m.delta,
                          Formula(entry, max(m.delta, ZERO)), Formula(exit_, max(-m.delta, ZERO)),
                          Formula(running, balances[m.account_key]), m.description, m.recorded_by, m.account_key, i, m.key])
        self.table(sheet, "Diario", dheaders, drows, col=13, dates=(0,), money=range(4, 11), hidden=(13, 14, 15),
                   calculated={"Entrada USD": entry, "Salida USD": exit_, "Balance USD": running})
        sheet.write("N6", "Diario del período · Entradas/salidas son efectos sobre el balance, no volúmenes cambiarios.", self.formats["note"])

    def adjustments(self):
        sheet = self.sheet("Ajustes manuales", "Entradas manuales de esta copia; no son registros de Bolos. Ingrese montos positivos y elija Ingreso/Egreso y USD/Bs. Sin conversión entre monedas ni cálculo de utilidad neta.")
        self.table(sheet, "Ajustes", ["Fecha", "Concepto", "Tipo", "Moneda", "Monto", "Observaciones"], [], dates=(0,), money=(4,))
        sheet.set_column(1, 1, 35)
        sheet.set_column(5, 5, 45)
        sheet.set_column(0, 0, 14, self.formats["date"])
        sheet.set_column(2, 4, 20, self.formats["input"])
        sheet.data_validation(7, 2, 1048575, 2, {"validate": "list", "source": ["Ingreso", "Egreso"]})
        sheet.data_validation(7, 3, 1048575, 3, {"validate": "list", "source": ["USD", "Bs"]})
        sheet.data_validation(7, 4, 1048575, 4, {"validate": "decimal", "criteria": ">", "value": 0})
        sheet.write("A6", "Agregue filas dentro de la tabla. Solo las fechas del período participan en Resumen.", self.formats["note"])

    def metrics(self, kind, start=None, end=None):
        rows = [m for m in self.data.movements if m.operation == kind and (start is None or m.day >= start) and (end is None or m.day <= end)]
        complete = [m for m in rows if m.bs is not None and m.rate is not None]
        usd = sum((m.usd for m in rows), ZERO)
        covered = sum((m.usd for m in complete), ZERO)
        bs = sum((m.bs for m in complete), ZERO)
        return dict(usd=usd, bs=bs, covered=covered, rate=bs / covered if covered else None,
                    incomplete=len(rows) - len(complete), missing=usd-covered, count=len(rows),
                    commission=sum((m.commission for m in rows), ZERO), excess=sum((m.excess for m in rows), ZERO))

    def summary(self, sheet):
        sheet.freeze_panes(6, 0)
        sheet.set_column(0, 0, 43)
        sheet.set_column(1, 1, 22)
        # This column also holds monthly purchase volume below the main blocks.
        sheet.set_column(2, 2, 16)
        sheet.set_column(3, 3, 43)
        sheet.set_column(4, 4, 22)
        for a, b, text_ in [("A6", "B6", "VENTAS"), ("D6", "E6", "COMPRAS"), ("A17", "B17", "BALANCES OPERATIVOS"), ("D17", "E17", "INDICADORES CAMBIARIOS"), ("A24", "B24", "Bolos · importes reconocidos"), ("D24", "E24", "AJUSTES MANUALES · separados")]:
            sheet.merge_range(f"{a}:{b}", text_, self.formats["section"])
        buy, sell = self.metrics("buy"), self.metrics("sell")
        for startcol, kind, metrics in [(0, "Venta", sell), (3, "Compra", buy)]:
            table = "Ventas" if kind == "Venta" else "Compras"
            usdcol, bscol = f"{kind} USD", f"{kind} Bs"
            values = [
                (f"Total USD {'vendidos' if kind == 'Venta' else 'comprados'}", f'=SUMIFS({table}[{usdcol}],{table}[Fecha],">="&InicioPeriodo,{table}[Fecha],"<="&FinPeriodo)', metrics["usd"], "usd"),
                (f"Total Bs {'recibidos' if kind == 'Venta' else 'pagados'} · conocidos", f'=SUMIFS({table}[{bscol}],{table}[Datos monetarios],"Completa",{table}[Fecha],">="&InicioPeriodo,{table}[Fecha],"<="&FinPeriodo)', metrics["bs"], "bs"),
                ("USD con datos completos", f'=SUMIFS({table}[{usdcol}],{table}[Datos monetarios],"Completa",{table}[Fecha],">="&InicioPeriodo,{table}[Fecha],"<="&FinPeriodo)', metrics["covered"], "usd"),
                ("Tasa promedio ponderada · Bs/USD", f'=IFERROR({"B" if startcol == 0 else "E"}8/{"B" if startcol == 0 else "E"}9,"")', metrics["rate"] if metrics["rate"] is not None else "", "money"),
                ("Operaciones", f'=COUNTIFS({table}[{usdcol}],">0",{table}[Fecha],">="&InicioPeriodo,{table}[Fecha],"<="&FinPeriodo)', metrics["count"], "text"),
                ("Operaciones incompletas", f'=COUNTIFS({table}[Datos monetarios],"Incompleta",{table}[Fecha],">="&InicioPeriodo,{table}[Fecha],"<="&FinPeriodo)', metrics["incomplete"], "text"),
                ("USD sin datos completos", f'=SUMIFS({table}[{usdcol}],{table}[Datos monetarios],"Incompleta",{table}[Fecha],">="&InicioPeriodo,{table}[Fecha],"<="&FinPeriodo)', metrics["missing"], "usd"),
                ("Cobertura Bs del período", f'=IF({"B" if startcol == 0 else "E"}12=0,"Completa","Parcial: faltan Bs/tasa")', "Completa" if not metrics["incomplete"] else "Parcial: faltan Bs/tasa", "text"),
            ]
            for r, (label, formula, cache, fmt) in enumerate(values, 6):
                self.write(sheet, r, startcol, label)
                self.write(sheet, r, startcol+1, Formula(formula, cache), fmt)
        operational = [
            ("USD comprados menos vendidos", "=E7-B7", buy["usd"]-sell["usd"], "usd"),
            ("Bs recibidos menos pagados · conocidos", "=B8-E8", sell["bs"]-buy["bs"], "bs"),
            ("Operaciones de compra y venta", "=B11+E11", buy["count"]+sell["count"], "text"),
        ]
        for row, (label, formula, cache, fmt) in enumerate(operational, 17):
            self.write(sheet, row, 0, label)
            self.write(sheet, row, 1, Formula(formula, cache), fmt)
        valid = not buy["incomplete"] and not sell["incomplete"] and buy["rate"] is not None and sell["rate"] is not None
        spread = sell["rate"]-buy["rate"] if valid else None
        guard = 'AND(B12=0,E12=0,ISNUMBER(B10),B10>0,ISNUMBER(E10),E10>0)'
        theoretical = spread * buy["usd"] / buy["rate"] if valid else ""
        for row, label, expr, cache, fmt in [
            (17, "Diferencia de tasas · Bs/USD", f'=IF({guard},B10-E10,"")', spread if valid else "", "money"),
            (18, "Diferencial porcentual sobre tasa de venta", f'=IF({guard},(B10-E10)/B10,"")', spread/sell["rate"] if valid else "", "percent"),
            (19, "Diferencial teórico estimado · USD", f'=IF({guard},(B10-E10)*E7/E10,"")', theoretical, "usd"),
            (20, "Estado del indicador", f'=IF({guard},"Estimación; no es utilidad neta","No calculable: faltan datos o volumen")', "Estimación; no es utilidad neta" if valid else "No calculable: faltan datos o volumen", "note"),
        ]:
            self.write(sheet, row, 3, label)
            self.write(sheet, row, 4, Formula(expr, cache), fmt)
        commission = buy["commission"]+sell["commission"]
        def period_sum(table, column):
            return f'SUMIFS({table}[{column}],{table}[Fecha],">="&InicioPeriodo,{table}[Fecha],"<="&FinPeriodo)'
        remaining = sum((max(ZERO, -c.closing) for c in self.data.customer_accounts), ZERO)
        for row, label, expr, cache in [
            (24, "Comisiones reconocidas a corredores", f'={period_sum("Compras", "Comisión USD")}+{period_sum("Ventas", "Comisión USD")}', commission),
            (25, "Excedentes reconocidos en compras", f'={period_sum("Compras", "Excedente USD")}', buy["excess"]),
            (26, "CxC pendientes al corte · total", '=SUM(Cartera[Por cobrar USD])', remaining),
            (27, "CxC global al corte", '=SUMIF(Cartera[Tipo de cliente],"Global",Cartera[Por cobrar USD])', sum((max(ZERO, -c.closing) for c in self.data.customer_accounts if c.classification == "Global"), ZERO)),
            (28, "CxC personal al corte", '=SUMIF(Cartera[Tipo de cliente],"Personal",Cartera[Por cobrar USD])', sum((max(ZERO, -c.closing) for c in self.data.customer_accounts if c.classification == "Personal"), ZERO)),
            (29, "Balance inicial de corredores", '=SUM(Cuentas[Balance inicial USD])', sum((b.opening for b in self.data.brokers), ZERO)),
            (30, "Balance final de corredores", '=SUM(Cuentas[Balance final USD])', sum((b.opening for b in self.data.brokers), ZERO)+sum((m.delta for m in self.data.movements if m.account_key), ZERO)),
            (31, "Saldo a favor de clientes al corte", '=SUM(Cartera[A favor USD])', sum((max(c.closing, ZERO) for c in self.data.customer_accounts), ZERO)),
        ]:
            self.write(sheet, row, 0, label)
            self.write(sheet, row, 1, Formula(expr, cache), "usd")
        # Named date cells make the manual period constraint explicit and editable.
        sheet.write("D33", "Desde fórmulas del período", self.formats["note"])
        sheet.write("D34", "Hasta fórmulas del período", self.formats["note"])
        self.write(sheet, 32, 4, self.data.start)
        self.write(sheet, 33, 4, self.data.end)
        self.book.define_name("InicioPeriodo", "=Resumen!$E$33")
        self.book.define_name("FinPeriodo", "=Resumen!$E$34")
        for base, currency in [(24, "USD"), (28, "Bs")]:
            for offset, type_ in enumerate(["Ingreso", "Egreso"]):
                self.write(sheet, base+offset, 3, f"{type_}s manuales · {currency}")
                formula = f'=SUMIFS(Ajustes[Monto],Ajustes[Tipo],"{type_}",Ajustes[Moneda],"{currency}",Ajustes[Fecha],">="&InicioPeriodo,Ajustes[Fecha],"<="&FinPeriodo)'
                self.write(sheet, base+offset, 4, Formula(formula, ZERO), "usd" if currency == "USD" else "bs")
            self.write(sheet, base+2, 3, f"Neto manual · {currency}")
            self.write(sheet, base+2, 4, Formula(f'=E{base+1}-E{base+2}', ZERO), "usd" if currency == "USD" else "bs")
        sheet.merge_range("A35:L35", "Lectura: los balances USD/Bs son diferencias operativas, no inventario ni caja conciliada. El diferencial teórico no supone que se vendió todo lo comprado. Comisiones y excedentes no se suman a esa estimación.", self.formats["note"])
        sheet.set_row(34, 34)
        sheet.merge_range("A36:L36", "Edición: agregue filas dentro de las tablas; copie una fila del mismo par USD/Bs o USD/tasa para conservar su fórmula. Los filtros cambian la vista, no el alcance del Resumen. Amarillo: entradas; verde: fórmulas.", self.formats["note"])
        sheet.set_row(35, 34)
        self.monthly(sheet)
        sheet.print_area(0, 0, 42 + len(self.months()), 11)

    def months(self):
        months, current = [], self.data.start.replace(day=1)
        while current <= self.data.end:
            end = current.replace(day=calendar.monthrange(current.year, current.month)[1])
            months.append((max(current, self.data.start), min(end, self.data.end)))
            current = end.fromordinal(end.toordinal()+1)
        return months

    def monthly(self, sheet):
        headers = ["Desde", "Hasta", "Compra USD", "Compra Bs conocidos", "Tasa compra", "Venta USD", "Venta Bs conocidos", "Tasa venta", "Operaciones", "Incompletas", "Diferencial teórico USD"]
        rows = []
        for start, end in self.months():
            buy, sell = self.metrics("buy", start, end), self.metrics("sell", start, end)
            def sums(table, column, value, *, complete=False):
                coverage = f',{table}[Datos monetarios],"Completa"' if complete else ""
                return Formula(f'=SUMIFS({table}[{column}],{table}[Fecha],">="&[@Desde],{table}[Fecha],"<="&[@Hasta]{coverage})', value)
            complete = buy["incomplete"]+sell["incomplete"] == 0 and buy["rate"] is not None and sell["rate"] is not None
            rows.append([start, end, sums("Compras", "Compra USD", buy["usd"]), sums("Compras", "Compra Bs", buy["bs"], complete=True),
                         Formula('=IFERROR(SUMIFS(Compras[Compra Bs],Compras[Fecha],">="&[@Desde],Compras[Fecha],"<="&[@Hasta],Compras[Datos monetarios],"Completa")/SUMIFS(Compras[Compra USD],Compras[Fecha],">="&[@Desde],Compras[Fecha],"<="&[@Hasta],Compras[Datos monetarios],"Completa"),"")', buy["rate"] if buy["rate"] is not None else ""),
                         sums("Ventas", "Venta USD", sell["usd"]), sums("Ventas", "Venta Bs", sell["bs"], complete=True),
                         Formula('=IFERROR(SUMIFS(Ventas[Venta Bs],Ventas[Fecha],">="&[@Desde],Ventas[Fecha],"<="&[@Hasta],Ventas[Datos monetarios],"Completa")/SUMIFS(Ventas[Venta USD],Ventas[Fecha],">="&[@Desde],Ventas[Fecha],"<="&[@Hasta],Ventas[Datos monetarios],"Completa"),"")', sell["rate"] if sell["rate"] is not None else ""),
                         Formula('=COUNTIFS(Compras[Fecha],">="&[@Desde],Compras[Fecha],"<="&[@Hasta],Compras[Compra USD],">0")+COUNTIFS(Ventas[Fecha],">="&[@Desde],Ventas[Fecha],"<="&[@Hasta],Ventas[Venta USD],">0")', buy["count"]+sell["count"]),
                         Formula('=COUNTIFS(Compras[Fecha],">="&[@Desde],Compras[Fecha],"<="&[@Hasta],Compras[Datos monetarios],"Incompleta")+COUNTIFS(Ventas[Fecha],">="&[@Desde],Ventas[Fecha],"<="&[@Hasta],Ventas[Datos monetarios],"Incompleta")', buy["incomplete"]+sell["incomplete"]),
                         Formula('=IF(AND([@Incompletas]=0,ISNUMBER([@[Tasa compra]]),[@[Tasa compra]]>0,ISNUMBER([@[Tasa venta]]),[@[Tasa venta]]>0),([@[Tasa venta]]-[@[Tasa compra]])*[@[Compra USD]]/[@[Tasa compra]],"")', (sell["rate"]-buy["rate"])*buy["usd"]/buy["rate"] if complete else "")])
        self.table(sheet, "Meses", headers, rows, row=38, dates=(0, 1), money=(*range(2, 8), 10))
