"""Parser for «ECU Software Version Report» fra OceanLink Pro (OLP).

Formatet (verifisert mot ekte rapport 2026-08-28):

    OceanLink Pro
    ECU Software Version Report
    Date: 2026-08-28 18:15:16.564271
    VIN: VCF1ZBE21PG0xxxxx
    BODY                                  <- seksjon (BODY/INFOTAINMENT/POWERTRAIN/CHASSIS/ADAS)
    GW - Gateway                          <- ECU-blokk: KODE - Fullt navn
    Software Version: FM298034S100K
    Hardware Version: FM298034H100C
    Supplier SW Version: GW500002         <- feltet som brukes i Marlin-sammenligningen
    Bootloader Version: HIRAIN1.0.8
    ...

Parseren tar både PDF (tekst trekkes ut med pdfplumber) og ren tekst med
samme linjestruktur.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

VIN_RE = re.compile(r"^VIN:\s*([A-HJ-NPR-Z0-9]{17})\s*$", re.MULTILINE)
DATE_RE = re.compile(r"^Date:\s*(.+)$", re.MULTILINE)
# Seksjonsoverskrifter er korte linjer i bare store bokstaver (BODY, ADAS, ...)
SECTION_RE = re.compile(r"^[A-Z][A-Z ]{2,24}$")
# ECU-blokk: "KODE - Fullt navn", f.eks. "MCU_F - Motor Control Unit Front"
ECU_HEADER_RE = re.compile(r"^([A-Z][A-Z0-9_]{0,11}) - (.+)$")
FIELD_RE = re.compile(
    r"^(Software|Hardware|Supplier SW|Bootloader) Version:\s*(.*)$"
)

MAX_REPORT_BYTES = 15 * 1024 * 1024


class ReportParseError(Exception):
    """Filen kunne ikke tolkes som en gyldig OLP-rapport.

    `key` er en i18n-nøkkel (locales: "parse_<key>") slik at årsaken kan vises
    på brukerens språk; `detail` er valgfri teknisk tilleggsinfo (uoversatt).
    """

    def __init__(self, key: str, detail: str = ""):
        self.key = key
        self.detail = detail
        super().__init__(key if not detail else f"{key}: {detail}")


@dataclass
class ModuleReading:
    code: str            # f.eks. "VCU", "MCU_F"
    name: str            # f.eks. "Vehicle Control Unit"
    section: str         # f.eks. "POWERTRAIN"
    supplier_sw: str     # "Supplier SW Version" — brukes i sammenligningen
    software: str = ""
    hardware: str = ""
    bootloader: str = ""

    @property
    def raw_name(self) -> str:
        return f"{self.code} - {self.name}"

    @property
    def version(self) -> str:
        return self.supplier_sw


@dataclass
class ParsedReport:
    vin: str
    modules: list[ModuleReading] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


def _extract_text(data: bytes, filename: str) -> str:
    if data[:5] == b"%PDF-":
        try:
            import io

            import pdfplumber

            with pdfplumber.open(io.BytesIO(data)) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
            return "\n".join(pages)
        except Exception as exc:  # korrupt/kryptert PDF o.l.
            raise ReportParseError("bad_pdf", str(exc)) from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReportParseError("unreadable") from exc


_FIELD_ATTR = {
    "Software": "software",
    "Hardware": "hardware",
    "Supplier SW": "supplier_sw",
    "Bootloader": "bootloader",
}


def parse_report(data: bytes, filename: str = "") -> ParsedReport:
    if len(data) > MAX_REPORT_BYTES:
        raise ReportParseError("too_large")
    if not data:
        raise ReportParseError("empty")

    text = _extract_text(data, filename)

    if "ECU Software Version Report" not in text:
        raise ReportParseError("no_header")

    vin_match = VIN_RE.search(text)
    if not vin_match:
        raise ReportParseError("no_vin")
    vin = vin_match.group(1)

    date_match = DATE_RE.search(text)

    modules: list[ModuleReading] = []
    section = ""
    current: dict | None = None

    def _flush() -> None:
        nonlocal current
        if current is not None:
            modules.append(ModuleReading(**current))
            current = None

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        header = ECU_HEADER_RE.match(line)
        if header:
            _flush()
            current = {
                "code": header.group(1),
                "name": header.group(2).strip(),
                "section": section,
                "supplier_sw": "",
            }
            continue
        fld = FIELD_RE.match(line)
        if fld and current is not None:
            current[_FIELD_ATTR[fld.group(1)]] = fld.group(2).strip()
            continue
        if SECTION_RE.match(line) and " - " not in line:
            _flush()
            section = line
            continue

    _flush()

    if not modules:
        raise ReportParseError("no_modules")

    return ParsedReport(
        vin=vin,
        modules=modules,
        meta={
            "filename": filename,
            "report_date": date_match.group(1).strip() if date_match else "",
        },
    )
