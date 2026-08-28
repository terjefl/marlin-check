"""Regelmotor: sammenligner en parset OLP-rapport mot requirements.yaml.

Kravmodellen følger foreningens tabell fra videomøtet 2026-08 («MINIMUM ECU
requirements»): hver relevant ECU har et minimumsnivå per programvareprofil
(2.0, 2.1, ...). En bil er «100 % <profil>» når ALLE kravmodulene minst når
profilens nivå. En 100 % 2.1-bil kan oppdateres direkte til Marlin; en bil
med blandede nivåer («zebra») må først via SW 2.2 / målrettede oppdateringer.

Tallet som sammenlignes trekkes ut av feltet «Supplier SW Version» med en
modulspesifikk regex (`extract` i YAML, capture-gruppe 1), fordi feltet har
ulik form per leverandør (BCM395021, MCU5000019, «ECC395 24», 89324V04...).

requirements.yaml bind-mountes inn i containeren og leses på nytt ved hver
evaluering, slik at oppdaterte krav virker umiddelbart uten rebuild.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .parser import ParsedReport

# Modulstatus
OK = "ok"                    # nivå >= målprofilens krav
OUTDATED = "outdated"        # nivå < målprofilens krav
MISSING = "missing"          # kravmodul ikke funnet i rapporten
UNPARSEABLE = "unparseable"  # klarte ikke trekke tall ut av Supplier SW Version

VERDICT_READY = "ready"
VERDICT_ZEBRA = "zebra"

# Fallback når modulen mangler egen extract-regex: siste siffergruppe
_DEFAULT_EXTRACT = re.compile(r"(\d+)\s*$")


@dataclass
class Requirement:
    id: str
    match: list[str]              # ECU-koder i rapporten (f.eks. ["MCU_R", "MCU_RR"])
    levels: dict[str, int]        # profil -> minimumsnivå, f.eks. {"2.0": 19, "2.1": 21}
    extract: str | None = None    # regex med capture-gruppe mot Supplier SW Version
    critical: bool = True
    label: str = ""


@dataclass
class RequirementSet:
    version: str
    target_profile: str           # profilen som kreves for direkte Marlin (nå "2.1")
    profiles: list[str]           # stigende rekkefølge, f.eks. ["2.0", "2.1"]
    modules: list[Requirement]


@dataclass
class ModuleResult:
    requirement: Requirement
    status: str
    raw_name: str = ""
    version: str = ""             # Supplier SW Version som funnet i rapporten
    extracted: int | None = None  # tallet som ble trukket ut
    required: int | None = None   # målprofilens minimumsnivå
    level: str | None = None      # høyeste profil modulen oppfyller, None = under alle


@dataclass
class Evaluation:
    verdict: str
    requirements_version: str
    target_profile: str
    results: list[ModuleResult]
    extra_modules: list = field(default_factory=list)  # moduler i rapporten uten krav

    @property
    def failing(self) -> list[ModuleResult]:
        return [r for r in self.results if r.status != OK]

    @property
    def failing_critical(self) -> list[ModuleResult]:
        return [r for r in self.results if r.status != OK and r.requirement.critical]


def load_requirements(path: str | Path) -> RequirementSet:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    modules = [
        Requirement(
            id=m["id"],
            match=[s.upper() for s in m.get("match", [m["id"]])],
            levels={str(k): int(v) for k, v in m["levels"].items()},
            extract=m.get("extract"),
            critical=bool(m.get("critical", True)),
            label=m.get("label", m["id"]),
        )
        for m in raw.get("modules", [])
    ]
    return RequirementSet(
        version=str(raw.get("version", "unknown")),
        target_profile=str(raw.get("target_profile")),
        profiles=[str(p) for p in raw.get("profiles", [])],
        modules=modules,
    )


def _extract_number(supplier_sw: str, req: Requirement) -> int | None:
    pattern = re.compile(req.extract) if req.extract else _DEFAULT_EXTRACT
    m = pattern.search(supplier_sw)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (IndexError, ValueError):
        return None


def _profile_level(extracted: int, req: Requirement, profiles: list[str]) -> str | None:
    """Høyeste profil (i stigende rekkefølge) hvor kravet er oppfylt."""
    level = None
    for profile in profiles:
        minimum = req.levels.get(profile)
        if minimum is not None and extracted >= minimum:
            level = profile
    return level


def evaluate(report: ParsedReport, requirements: RequirementSet) -> Evaluation:
    results: list[ModuleResult] = []
    matched_codes: set[str] = set()
    target = requirements.target_profile

    for req in requirements.modules:
        reading = next(
            (m for m in report.modules if m.code.upper() in req.match), None
        )
        required = req.levels.get(target)
        if reading is None:
            results.append(ModuleResult(requirement=req, status=MISSING, required=required))
            continue
        matched_codes.add(reading.code.upper())
        extracted = _extract_number(reading.supplier_sw, req)
        if extracted is None or required is None:
            results.append(
                ModuleResult(
                    requirement=req, status=UNPARSEABLE,
                    raw_name=reading.raw_name, version=reading.supplier_sw,
                    required=required,
                )
            )
            continue
        results.append(
            ModuleResult(
                requirement=req,
                status=OK if extracted >= required else OUTDATED,
                raw_name=reading.raw_name,
                version=reading.supplier_sw,
                extracted=extracted,
                required=required,
                level=_profile_level(extracted, req, requirements.profiles),
            )
        )

    extra = [m for m in report.modules if m.code.upper() not in matched_codes]
    failing_critical = [r for r in results if r.status != OK and r.requirement.critical]
    return Evaluation(
        verdict=VERDICT_READY if not failing_critical else VERDICT_ZEBRA,
        requirements_version=requirements.version,
        target_profile=target,
        results=results,
        extra_modules=extra,
    )
