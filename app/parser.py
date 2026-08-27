"""Parser for diagnoserapporter fra Ocean Link Pro (OLP).

VIKTIG: Det ekte rapportformatet er ikke kjent ennå (Fase 2 i planen).
Denne modulen definerer det stabile grensesnittet resten av appen bruker
(`parse_report`), med en midlertidig implementasjon som forstår:

1. Ren tekst / enkel PDF med linjer på formen
       VIN: <17 tegn>
       <Modulnavn>: <versjon>
   Dette er et SYNTETISK format brukt til utvikling og tester.

Når en ekte OLP-rapport foreligger, byttes uttrekkslogikken her ut uten at
resten av appen røres. Behold funksjonssignaturen og datamodellene.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

VIN_RE = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
# Linje: "Modulnavn : versjon" der versjonen inneholder minst ett siffer med punktum
MODULE_LINE_RE = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z0-9 _/&().-]{1,60}?)\s*[:\t]\s*(?P<version>\d[\w.-]*)\s*$"
)

MAX_REPORT_BYTES = 15 * 1024 * 1024


class ReportParseError(Exception):
    """Filen kunne ikke tolkes som en gyldig OLP-rapport."""


@dataclass
class ModuleReading:
    raw_name: str
    version: str


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
            raise ReportParseError(f"Kunne ikke lese PDF-innhold: {exc}") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReportParseError("Filen er verken PDF eller lesbar tekst.") from exc


def parse_report(data: bytes, filename: str = "") -> ParsedReport:
    if len(data) > MAX_REPORT_BYTES:
        raise ReportParseError("Filen er større enn maksgrensen på 15 MB.")
    if not data:
        raise ReportParseError("Filen er tom.")

    text = _extract_text(data, filename)

    vin_match = VIN_RE.search(text)
    if not vin_match:
        raise ReportParseError("Fant ingen VIN (17 tegn) i rapporten.")
    vin = vin_match.group(1)

    modules: list[ModuleReading] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = MODULE_LINE_RE.match(line)
        if not m:
            continue
        name = m.group("name").strip()
        # Metadatalinjer i rapporten skal ikke tolkes som moduler
        if name.lower() in {"vin", "generated", "date", "report date"} or name.lower() in seen:
            continue
        seen.add(name.lower())
        modules.append(ModuleReading(raw_name=name, version=m.group("version")))

    if not modules:
        raise ReportParseError("Fant ingen moduler med versjonsnummer i rapporten.")

    return ParsedReport(vin=vin, modules=modules, meta={"filename": filename})
