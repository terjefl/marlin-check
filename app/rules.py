"""Regelmotor: sammenligner en parset rapport mot requirements.yaml.

requirements.yaml bind-mountes inn i containeren og kan oppdateres uten
rebuild. Filen leses på nytt ved hver evaluering (den er liten), slik at en
oppdatert kravfil virker umiddelbart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .parser import ParsedReport
from .versioncmp import meets_minimum

# Modulstatus
OK = "ok"            # versjon >= krav
OUTDATED = "outdated"  # versjon < krav
MISSING = "missing"    # kravmodul ikke funnet i rapporten
UNPARSEABLE = "unparseable"  # versjonsstrengen kunne ikke tolkes

VERDICT_READY = "ready"
VERDICT_ZEBRA = "zebra"


@dataclass
class Requirement:
    id: str
    match: list[str]
    min_version: str
    critical: bool = True
    label: str = ""


@dataclass
class RequirementSet:
    version: str
    modules: list[Requirement]


@dataclass
class ModuleResult:
    requirement: Requirement
    status: str
    raw_name: str = ""
    version: str = ""


@dataclass
class Evaluation:
    verdict: str
    requirements_version: str
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
            match=[s.lower() for s in m.get("match", [m["id"]])],
            min_version=str(m["min_version"]),
            critical=bool(m.get("critical", True)),
            label=m.get("label", m["id"]),
        )
        for m in raw.get("modules", [])
    ]
    return RequirementSet(version=str(raw.get("version", "unknown")), modules=modules)


def evaluate(report: ParsedReport, requirements: RequirementSet) -> Evaluation:
    results: list[ModuleResult] = []
    matched_raw: set[str] = set()

    for req in requirements.modules:
        reading = None
        for mod in report.modules:
            name = mod.raw_name.lower()
            if any(pattern in name or name in pattern for pattern in req.match):
                reading = mod
                break
        if reading is None:
            results.append(ModuleResult(requirement=req, status=MISSING))
            continue
        matched_raw.add(reading.raw_name.lower())
        try:
            ok = meets_minimum(reading.version, req.min_version)
        except ValueError:
            results.append(
                ModuleResult(requirement=req, status=UNPARSEABLE,
                             raw_name=reading.raw_name, version=reading.version)
            )
            continue
        results.append(
            ModuleResult(requirement=req, status=OK if ok else OUTDATED,
                         raw_name=reading.raw_name, version=reading.version)
        )

    extra = [m for m in report.modules if m.raw_name.lower() not in matched_raw]
    verdict = VERDICT_READY if not [r for r in results if r.status != OK and r.requirement.critical] else VERDICT_ZEBRA
    return Evaluation(
        verdict=verdict,
        requirements_version=requirements.version,
        results=results,
        extra_modules=extra,
    )
