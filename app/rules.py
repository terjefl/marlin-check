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
VERDICT_MARLIN = "marlin"    # the car already runs Marlin; the readiness check does not apply

# Fallback when the module has no extract regex of its own: last digit group
_DEFAULT_EXTRACT = re.compile(r"(\d+)\s*$")


@dataclass
class Variant:
    """A trim/region-specific flavor of a module (e.g. BMS for LFP vs NMC packs,
    or RHD vs LHD steering). The first variant whose `pattern` matches the
    Supplier SW Version is used; its extract/levels override the module's."""

    name: str
    pattern: str                  # regex matched against Supplier SW Version
    levels: dict[str, int]
    extract: str | None = None


@dataclass
class Requirement:
    id: str
    match: list[str]              # ECU codes in the report (e.g. ["MCU_R", "MCU_RR"])
    levels: dict[str, int]        # profile -> minimum level, e.g. {"2.0": 19, "2.1": 21}
    extract: str | None = None    # regex with a capture group, applied to Supplier SW Version
    critical: bool = True
    label: str = ""
    variants: list[Variant] = field(default_factory=list)
    # Level at which this module shows the car is ALREADY on Marlin (e.g. VCU
    # 24 = "VCU 2.4", which only Marlin installs). When every module that has
    # a marlin_level reaches it, the verdict is "marlin" instead of
    # ready/zebra. None = this module is not a Marlin marker.
    marlin_level: int | None = None
    # Trim letters (5th VIN character: Z/E/U/S = One/Extreme/Ultra/Sport) this
    # module is required for. None = required for all trims. Example: MCU_R is
    # absent on the single-motor Sport, so only_trims: [Z, E, U]. The exemption
    # only applies to a KNOWN trim letter: a VIN whose trim cannot be decoded
    # still requires every module (fail-safe, see `evaluate`).
    only_trims: list[str] | None = None


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
    variant: str = ""             # name of the matched variant, if any
    top_required: int | None = None  # minimum for the highest profile (variant-aware), if defined


@dataclass
class Evaluation:
    verdict: str
    requirements_version: str
    target_profile: str
    results: list[ModuleResult]
    profiles: list[str] = field(default_factory=list)
    extra_modules: list = field(default_factory=list)  # report modules without a requirement
    trim: str = ""                # trim letter read from the VIN (5th character)
    trim_name: str = ""           # "One"/"Extreme"/"Ultra"/"Sport", or "" if the letter is unknown

    @property
    def ok_below_top(self) -> list[ModuleResult]:
        """Modules that meet the target profile but not the highest profile —
        i.e. what a direct 2.1→Marlin update will leave behind (2.2-only ECUs).
        Only modules that actually DEFINE a level for the top profile can be
        below it; a module with no top-profile requirement is not listed."""
        return [
            r for r in self.results
            if r.status == OK
            and r.top_required is not None
            and r.extracted is not None
            and r.extracted < r.top_required
        ]

    @property
    def below_top(self) -> list[ModuleResult]:
        """Every module with a number that is below the highest profile,
        whatever its status. Used for cars already on Marlin, which does not
        update every ECU: this is what is still on older software."""
        return [
            r for r in self.results
            if r.top_required is not None
            and r.extracted is not None
            and r.extracted < r.top_required
        ]

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
        for owner, extract in [(module.id, module.extract)] + [
            (f"{module.id}/{v.name}", v.extract) for v in module.variants
        ]:
            if not extract:
                continue
            try:
                pattern = re.compile(extract)
            except re.error as exc:
                raise RequirementsValidationError(
                    f"Module {owner}: invalid extract regex: {exc}"
                ) from exc
            if pattern.groups < 1:
                raise RequirementsValidationError(
                    f"Module {owner}: the extract regex has no capture group."
                )
        for variant in module.variants:
            try:
                re.compile(variant.pattern)
            except re.error as exc:
                raise RequirementsValidationError(
                    f"Module {module.id}/{variant.name}: invalid pattern: {exc}"
                ) from exc
        has_base_target = module.levels.get(result.target_profile) is not None
        variants_cover_target = bool(module.variants) and all(
            v.levels.get(result.target_profile) is not None for v in module.variants
        )
        if not has_base_target and not variants_cover_target:
            raise RequirementsValidationError(
                f"Module {module.id}: missing level for target_profile {result.target_profile!r}"
                " (set it on the module or on every variant)."
            )
    return result


def load_requirements(path: str | Path) -> RequirementSet:
    return parse_requirements_text(Path(path).read_text(encoding="utf-8"))


def _str_list(value, where: str) -> list[str]:
    """A YAML list of scalars. A bare string is rejected: iterating over
    `match: VCU` would silently yield ["V", "C", "U"] and mark the module
    missing on every car."""
    if not isinstance(value, list) or not value:
        raise RequirementsValidationError(f"{where} must be a non-empty list (e.g. [VCU]).")
    if not all(isinstance(item, (str, int, float)) for item in value):
        raise RequirementsValidationError(f"{where} must contain only plain values.")
    return [str(item) for item in value]


def _levels(value, where: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RequirementsValidationError(f"{where} must be a mapping of profile -> integer level.")
    out: dict[str, int] = {}
    for k, v in value.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
            raise RequirementsValidationError(
                f"{where}: level for profile {k!r} must be an integer (got {v!r})."
            )
        out[str(k)] = int(v)
    return out


def _mapping(value, where: str) -> dict:
    if not isinstance(value, dict):
        raise RequirementsValidationError(f"{where} must be a mapping.")
    return value


def _optional_str(value, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RequirementsValidationError(f"{where} must be a string.")
    return value


def _build_requirement_set(raw: dict) -> RequirementSet:
    raw_modules = raw.get("modules", [])
    if not isinstance(raw_modules, list):
        raise RequirementsValidationError("`modules` must be a list.")
    modules = []
    for index, m in enumerate(raw_modules):
        m = _mapping(m, f"modules[{index}]")
        if "id" not in m or not isinstance(m["id"], str) or not m["id"].strip():
            raise RequirementsValidationError(f"modules[{index}]: `id` is missing or not a string.")
        module_id = m["id"].strip()
        where = f"Module {module_id}"
        raw_variants = m.get("variants", [])
        if raw_variants is None:
            raw_variants = []
        if not isinstance(raw_variants, list):
            raise RequirementsValidationError(f"{where}: `variants` must be a list.")
        variants = []
        for v_index, v in enumerate(raw_variants):
            v = _mapping(v, f"{where}: variants[{v_index}]")
            for key in ("name", "pattern"):
                if not isinstance(v.get(key), str) or not v[key]:
                    raise RequirementsValidationError(
                        f"{where}: variants[{v_index}] needs a string `{key}`."
                    )
            variants.append(
                Variant(
                    name=v["name"],
                    pattern=v["pattern"],
                    levels=_levels(v.get("levels"), f"{where}/{v['name']}: `levels`"),
                    extract=_optional_str(v.get("extract"), f"{where}/{v['name']}: `extract`"),
                )
            )
        marlin_level = m.get("marlin_level")
        if marlin_level is not None and (
            isinstance(marlin_level, bool) or not isinstance(marlin_level, int)
        ):
            raise RequirementsValidationError(f"{where}: `marlin_level` must be an integer.")
        modules.append(
            Requirement(
                id=module_id,
                marlin_level=marlin_level,
                match=[
                    code.upper()
                    for code in _str_list(m.get("match", [module_id]), f"{where}: `match`")
                ],
                levels=_levels(m.get("levels"), f"{where}: `levels`"),
                extract=_optional_str(m.get("extract"), f"{where}: `extract`"),
                critical=bool(m.get("critical", True)),
                label=str(m.get("label") or module_id),
                variants=variants,
                only_trims=(
                    [t.upper() for t in _str_list(m["only_trims"], f"{where}: `only_trims`")]
                    if m.get("only_trims")
                    else None
                ),
            )
        )
    return RequirementSet(
        version=str(raw.get("version", "unknown")),
        target_profile=str(raw.get("target_profile")),
        profiles=_str_list(raw.get("profiles"), "`profiles`"),
        modules=modules,
    )


def _extract_number(supplier_sw: str, extract: str | None) -> int | None:
    pattern = re.compile(extract) if extract else _DEFAULT_EXTRACT
    m = pattern.search(supplier_sw)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (IndexError, ValueError):
        return None


def _profile_level(extracted: int, levels: dict[str, int], profiles: list[str]) -> str | None:
    """Highest profile (in ascending order) whose requirement is satisfied."""
    level = None
    for profile in profiles:
        minimum = levels.get(profile)
        if minimum is not None and extracted >= minimum:
            level = profile
    return level


TRIM_NAMES = {"Z": "One", "E": "Extreme", "U": "Ultra", "S": "Sport"}


def vin_trim(vin: str) -> str:
    """Trim letter from the VIN (5th character): Z/E/U/S = One/Extreme/Ultra/Sport."""
    return vin[4].upper() if len(vin) > 4 else ""


def evaluate(report: ParsedReport, requirements: RequirementSet) -> Evaluation:
    results: list[ModuleResult] = []
    matched_codes: set[str] = set()
    target = requirements.target_profile
    trim = vin_trim(report.vin)
    trim_known = trim in TRIM_NAMES

    for req in requirements.modules:
        reading = next(
            (m for m in report.modules if m.code.upper() in req.match), None
        )
        if reading is None:
            # A module absent from the report is only a failure if this trim is
            # supposed to have it (e.g. the single-motor Sport has no MCU_R).
            # An unknown trim letter never exempts anything: treating it as
            # "not required" would let a two-motor car pass with MCU_R missing.
            if req.only_trims and trim_known and trim not in req.only_trims:
                continue
            results.append(
                ModuleResult(requirement=req, status=MISSING, required=req.levels.get(target))
            )
            continue
        matched_codes.add(reading.code.upper())

        # Variant selection: first variant whose pattern matches the value wins
        extract_regex = req.extract
        levels = req.levels
        variant_name = ""
        if req.variants:
            variant = next(
                (v for v in req.variants if re.search(v.pattern, reading.supplier_sw)),
                None,
            )
            if variant is not None:
                extract_regex = variant.extract or extract_regex
                levels = variant.levels or levels
                variant_name = variant.name

        required = levels.get(target)
        top_required = levels.get(requirements.profiles[-1]) if requirements.profiles else None
        extracted = _extract_number(reading.supplier_sw, extract_regex)
        if extracted is None or required is None:
            results.append(
                ModuleResult(
                    requirement=req, status=UNPARSEABLE,
                    raw_name=reading.raw_name, version=reading.supplier_sw,
                    required=required, variant=variant_name,
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
                level=_profile_level(extracted, levels, requirements.profiles),
                variant=variant_name,
                top_required=top_required,
            )
        )

    extra = [m for m in report.modules if m.code.upper() not in matched_codes]
    failing_critical = [r for r in results if r.status != OK and r.requirement.critical]

    # Already on Marlin: every marker module reached its marlin_level
    markers = [r for r in results if r.requirement.marlin_level is not None]
    on_marlin = bool(markers) and all(
        r.extracted is not None and r.extracted >= r.requirement.marlin_level for r in markers
    )
    if on_marlin:
        verdict = VERDICT_MARLIN
    elif failing_critical:
        verdict = VERDICT_ZEBRA
    else:
        verdict = VERDICT_READY
    return Evaluation(
        verdict=verdict,
        requirements_version=requirements.version,
        target_profile=target,
        results=results,
        profiles=list(requirements.profiles),
        extra_modules=extra,
        trim=trim,
        trim_name=TRIM_NAMES.get(trim, ""),
    )
