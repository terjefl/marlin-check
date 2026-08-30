"""Rule engine: compares a parsed OLP report against requirements.yaml.

The requirements model follows the association's table from the 2026-08 web
meeting ("MINIMUM ECU requirements"): each relevant ECU has a minimum level per
software profile (2.0, 2.1, ...). A car is "100% <profile>" when ALL required
modules reach at least the profile's level. A 100% 2.1 car can be updated
directly to Marlin; a car with mixed levels (a "zebra") must first go via
SW 2.2 / targeted module updates.

The number being compared is extracted from the "Supplier SW Version" field
with a module-specific regex (`extract` in the YAML, capture group 1), because
the field's shape differs per supplier (BCM395021, MCU5000019, "ECC395 24",
89324V04...).

requirements.yaml is bind-mounted into the container and re-read on every
evaluation, so updated requirements take effect immediately without a rebuild.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .parser import ParsedReport

# Module status
OK = "ok"                    # level >= target profile requirement
OUTDATED = "outdated"        # level < target profile requirement
MISSING = "missing"          # required module not found in the report
UNPARSEABLE = "unparseable"  # could not extract a number from Supplier SW Version

VERDICT_READY = "ready"
VERDICT_ZEBRA = "zebra"

# Fallback when the module has no extract regex of its own: last digit group
_DEFAULT_EXTRACT = re.compile(r"(\d+)\s*$")


@dataclass
class Requirement:
    id: str
    match: list[str]              # ECU codes in the report (e.g. ["MCU_R", "MCU_RR"])
    levels: dict[str, int]        # profile -> minimum level, e.g. {"2.0": 19, "2.1": 21}
    extract: str | None = None    # regex with a capture group, applied to Supplier SW Version
    critical: bool = True
    label: str = ""


@dataclass
class RequirementSet:
    version: str
    target_profile: str           # profile required for direct Marlin (currently "2.1")
    profiles: list[str]           # ascending order, e.g. ["2.0", "2.1"]
    modules: list[Requirement]


@dataclass
class ModuleResult:
    requirement: Requirement
    status: str
    raw_name: str = ""
    version: str = ""             # Supplier SW Version as found in the report
    extracted: int | None = None  # the extracted number
    required: int | None = None   # the target profile minimum level
    level: str | None = None      # highest profile the module satisfies, None = below all


@dataclass
class Evaluation:
    verdict: str
    requirements_version: str
    target_profile: str
    results: list[ModuleResult]
    extra_modules: list = field(default_factory=list)  # report modules without a requirement

    @property
    def failing(self) -> list[ModuleResult]:
        return [r for r in self.results if r.status != OK]

    @property
    def failing_critical(self) -> list[ModuleResult]:
        return [r for r in self.results if r.status != OK and r.requirement.critical]


class RequirementsValidationError(Exception):
    """The requirements text could not be parsed as a valid rule set."""


def parse_requirements_text(text: str) -> RequirementSet:
    """Parses and validates requirements text (YAML). Raises RequirementsValidationError."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RequirementsValidationError(f"Invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise RequirementsValidationError("The top level must be a YAML mapping.")
    try:
        result = _build_requirement_set(raw)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise RequirementsValidationError(f"Invalid structure: {exc!r}") from exc
    if not result.modules:
        raise RequirementsValidationError("No modules defined under `modules:`.")
    if not result.target_profile or result.target_profile == "None":
        raise RequirementsValidationError("`target_profile` is missing.")
    if result.target_profile not in result.profiles:
        raise RequirementsValidationError(
            f"target_profile {result.target_profile!r} is not in profiles {result.profiles}."
        )
    for module in result.modules:
        if module.extract:
            try:
                pattern = re.compile(module.extract)
            except re.error as exc:
                raise RequirementsValidationError(
                    f"Module {module.id}: invalid extract regex: {exc}"
                ) from exc
            if pattern.groups < 1:
                raise RequirementsValidationError(
                    f"Module {module.id}: the extract regex has no capture group."
                )
        if module.levels.get(result.target_profile) is None:
            raise RequirementsValidationError(
                f"Module {module.id}: missing level for target_profile {result.target_profile!r}."
            )
    return result


def load_requirements(path: str | Path) -> RequirementSet:
    return parse_requirements_text(Path(path).read_text(encoding="utf-8"))


def _build_requirement_set(raw: dict) -> RequirementSet:
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
    """Highest profile (in ascending order) whose requirement is satisfied."""
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
