"""Small OOXML reader for export assertions; never evaluates or rewrites formulas."""

import posixpath
import re
from decimal import Decimal
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

NS = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def column_index(name):
    result = 0
    for letter in name:
        result = result * 26 + ord(letter) - ord("A") + 1
    return result - 1


def cell_name(col, row):
    letters = ""
    col += 1
    while col:
        col, remainder = divmod(col - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row}"


class WorkbookView:
    def __init__(self, data):
        self.zip = ZipFile(BytesIO(data))
        assert self.zip.testzip() is None
        self.names = self.zip.namelist()
        self.strings = ["".join(s.itertext()) for s in ET.fromstring(self.zip.read("xl/sharedStrings.xml"))] if "xl/sharedStrings.xml" in self.names else []
        self.root = ET.fromstring(self.zip.read("xl/workbook.xml"))
        relationships = {r.attrib["Id"]: r.attrib["Target"] for r in ET.fromstring(self.zip.read("xl/_rels/workbook.xml.rels"))}
        self.sheets, self.tables = {}, {}
        for sheet in self.root.find("s:sheets", NS):
            target = relationships[sheet.attrib[f"{{{REL}}}id"]]
            path = posixpath.normpath(posixpath.join("xl", target)) if not target.startswith("/") else target[1:]
            root = ET.fromstring(self.zip.read(path))
            self.sheets[sheet.attrib["name"]] = root
            relpath = posixpath.join(posixpath.dirname(path), "_rels", posixpath.basename(path) + ".rels")
            if relpath not in self.names:
                continue
            for rel in ET.fromstring(self.zip.read(relpath)):
                if rel.attrib["Type"].endswith("/table"):
                    target = rel.attrib["Target"]
                    tablepath = target[1:] if target.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                    table = ET.fromstring(self.zip.read(tablepath))
                    self.tables[table.attrib["name"]] = (sheet.attrib["name"], table)

    def cell(self, sheet, address):
        return self.sheets[sheet].find(f'.//s:c[@r="{address}"]', NS)

    def value(self, sheet, address):
        cell = self.cell(sheet, address)
        if cell is None:
            return None
        value = cell.find("s:v", NS)
        if value is None or value.text is None:
            return None
        if cell.attrib.get("t") == "s":
            return self.strings[int(value.text)]
        if cell.attrib.get("t") in {"str", "e"}:
            return value.text
        return Decimal(value.text)

    def formula(self, sheet, address):
        cell = self.cell(sheet, address)
        f = cell.find("s:f", NS) if cell is not None else None
        return f.text if f is not None else None

    def records(self, table_name):
        sheet, table = self.tables[table_name]
        start, finish = table.attrib["ref"].split(":")
        col, row = re.fullmatch(r"([A-Z]+)(\d+)", start).groups()
        lastrow = int(re.search(r"\d+", finish)[0])
        columns = [c.attrib["name"] for c in table.find("s:tableColumns", NS)]
        return [{name: self.value(sheet, cell_name(column_index(col)+i, r)) for i, name in enumerate(columns)}
                for r in range(int(row)+1, lastrow+1)]
