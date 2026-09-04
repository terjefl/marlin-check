"""Tests against the real OLP format.

Two fixtures of the same real OceanLink Pro report (2026-08-28, a Fisker Ocean
One, a real "zebra": everything at 2.1 level except BCM):

- `olp_report.pdf`: the PDF exactly as exported by the OLP app, unmodified
  (the owner chose to keep the real VIN). This is what members upload, so it
  exercises the pdfplumber text-extraction step with the app's real layout.
- `olp_report.txt`: its text extraction with the VIN replaced, used by the
  many tests that mutate module values.
"""

from pathlib import Path

import pytest

from app.parser import ReportParseError, parse_report
from app.rules import (
    MISSING,
    OK,
    OUTDATED,
    VERDICT_READY,
    VERDICT_ZEBRA,
    RequirementsValidationError,
    evaluate,
    load_requirements,
    parse_requirements_text,
)

FIXTURES = Path(__file__).parent / "fixtures"
REQUIREMENTS = Path(__file__).parent.parent / "requirements.example.yaml"


def _report():
    return parse_report((FIXTURES / "olp_report.txt").read_bytes(), "olp_report.txt")


def test_parse_real_format():
    report = _report()
    assert report.vin == "VCF1ZBE20PG099999"
    assert report.meta["report_date"].startswith("2026-08-28")

    by_code = {m.code: m for m in report.modules}
    assert len(report.modules) == 37
    assert by_code["GW"].supplier_sw == "GW500002"
    assert by_code["GW"].section == "BODY"
    assert by_code["GW"].name == "Gateway"
    assert by_code["VCU"].supplier_sw == "VCU039021"
    assert by_code["VCU"].section == "POWERTRAIN"
    assert by_code["ECC"].supplier_sw == "ECC395 24"
    assert by_code["MCU_F"].supplier_sw == "MCU5000019"
    assert by_code["IBS"].supplier_sw == "88211V040000420131"
    assert by_code["EPS1"].supplier_sw == "EPS395001"  # leading whitespace trimmed
    assert by_code["HYDRA"].section == "ADAS"
    assert by_code["ESP"].software == "FM292045S020J"
    assert by_code["ESP"].bootloader == "FM292045B020B"


def test_parse_real_olp_pdf_end_to_end():
    """The real PDF from the OLP app must extract to exactly the module list of
    the text fixture and get the same verdict. A layout change in the app or a
    pdfplumber upgrade that reads the report differently fails here."""
    pdf_report = parse_report((FIXTURES / "olp_report.pdf").read_bytes(), "olp_report.pdf")
    text_report = _report()
    assert pdf_report.vin == "VCF1ZBE21PG002387"
    assert pdf_report.meta["report_date"].startswith("2026-08-28 18:15:16")
    assert len(pdf_report.modules) == 37
    assert [(m.code, m.name, m.section, m.supplier_sw, m.software, m.hardware, m.bootloader)
            for m in pdf_report.modules] == [
        (m.code, m.name, m.section, m.supplier_sw, m.software, m.hardware, m.bootloader)
        for m in text_report.modules
    ]

    evaluation = evaluate(pdf_report, load_requirements(REQUIREMENTS))
    assert evaluation.verdict == VERDICT_ZEBRA
    assert (evaluation.trim, evaluation.trim_name) == ("Z", "One")
    assert {r.requirement.id for r in evaluation.failing_critical} == {"BCM"}


def test_parse_pdf_roundtrip(tmp_path):
    """The PDF path: render the fixture to PDF and parse it back."""
    weasyprint = pytest.importorskip("weasyprint")
    text = (FIXTURES / "olp_report.txt").read_text()
    html = "<pre style='font-family: monospace'>" + text + "</pre>"
    pdf_bytes = weasyprint.HTML(string=html).write_pdf()

    report = parse_report(pdf_bytes, "report.pdf")
    assert report.vin == "VCF1ZBE20PG099999"
    assert len(report.modules) == 37


def test_parse_rejects_garbage():
    with pytest.raises(ReportParseError):
        parse_report(b"not a report at all")
    with pytest.raises(ReportParseError):
        parse_report(b"")
    # Correct heading but no VIN
    with pytest.raises(ReportParseError):
        parse_report(b"ECU Software Version Report\nGW - Gateway\n")


def test_evaluate_zebra_car():
    """With the corrected V2-workbook numbers, the fixture car (a One trim)
    meets 2.1 on everything except BCM (21 < 30)."""
    requirements = load_requirements(REQUIREMENTS)
    evaluation = evaluate(_report(), requirements)

    by_id = {r.requirement.id: r for r in evaluation.results}
    assert by_id["ECC"].status == OK and by_id["ECC"].extracted == 24
    assert by_id["BMS"].status == OK and by_id["BMS"].extracted == 21
    assert by_id["BMS"].variant == "NMC (One/Extreme/Ultra)"
    assert by_id["VCU"].status == OK and by_id["VCU"].level == "2.1"
    assert by_id["MCU_F"].status == OK and by_id["MCU_F"].extracted == 19
    assert by_id["MCU_R"].status == OK
    assert by_id["ESP"].status == OK
    assert (by_id["ESP"].extracted, by_id["ESP"].required) == (402, 401)
    # BCM 21 is below even the 2.0 level (30)
    assert by_id["BCM"].status == OUTDATED
    assert (by_id["BCM"].extracted, by_id["BCM"].required, by_id["BCM"].level) == (21, 30, None)

    assert evaluation.verdict == VERDICT_ZEBRA
    assert {r.requirement.id for r in evaluation.failing_critical} == {"BCM"}
    # 37 modules in the report, 7 with requirements -> 30 without
    assert len(evaluation.extra_modules) == 30


def test_evaluate_full_21_car():
    """Lift BCM (the only failing module) to 2.1 level -> directly Marlin-ready."""
    requirements = load_requirements(REQUIREMENTS)
    report = _report()
    for module in report.modules:
        if module.code == "BCM":
            module.supplier_sw = "BCM395030"
    evaluation = evaluate(report, requirements)
    assert evaluation.verdict == VERDICT_READY
    assert all(r.status == OK for r in evaluation.results)
    # Jens' note 2: a direct 2.1->Marlin jump leaves the 2.2-only ECUs behind.
    # This car meets 2.1 but not 2.2 on BCM (30<42), ESP (402<501),
    # MCU_F/R (19<21) and VCU (21<23); ECC (24) and BMS (21) already meet 2.2.
    assert {r.requirement.id for r in evaluation.ok_below_top} == {
        "BCM", "ESP", "MCU_F", "MCU_R", "VCU",
    }


def test_sport_trim_variants_and_missing_rear_mcu():
    """A Sport (VIN trim letter S): LFP BMS (BMSL39015) is OK via its variant,
    and the absent MCU_R is not treated as missing."""
    requirements = load_requirements(REQUIREMENTS)
    report = _report()
    report.vin = report.vin[:4] + "S" + report.vin[5:]
    report.modules = [m for m in report.modules if m.code != "MCU_R"]
    for module in report.modules:
        if module.code == "BMS":
            module.supplier_sw = "BMSL39015"
        if module.code == "BCM":
            module.supplier_sw = "BCM395030"
    evaluation = evaluate(report, requirements)

    by_id = {r.requirement.id: r for r in evaluation.results}
    assert "MCU_R" not in by_id  # not required for Sport
    assert by_id["BMS"].status == OK
    assert (by_id["BMS"].extracted, by_id["BMS"].required) == (15, 15)
    assert by_id["BMS"].variant == "LFP (Sport)"
    assert evaluation.verdict == VERDICT_READY

    # ...but on an Extreme (E), a missing MCU_R is still a failure
    report.vin = report.vin[:4] + "E" + report.vin[5:]
    evaluation = evaluate(report, requirements)
    by_id = {r.requirement.id: r for r in evaluation.results}
    assert by_id["MCU_R"].status == MISSING
    assert evaluation.verdict == VERDICT_ZEBRA


def test_ecc_alternate_format_and_unknown_bms_line():
    """ECC appears both as "ECC395 24" and "ECC39519"; an unknown BMS software
    line (neither BMSN nor BMSL) must surface as unparseable, not as a pass."""
    requirements = load_requirements(REQUIREMENTS)
    report = _report()
    for module in report.modules:
        if module.code == "ECC":
            module.supplier_sw = "ECC39519"  # 2.0-level, no-space form
        if module.code == "BMS":
            module.supplier_sw = "BMSX99999"
    evaluation = evaluate(report, requirements)
    by_id = {r.requirement.id: r for r in evaluation.results}
    assert by_id["ECC"].status == OUTDATED
    assert (by_id["ECC"].extracted, by_id["ECC"].required) == (19, 24)
    assert by_id["BMS"].status == "unparseable"


def test_missing_critical_module_gives_zebra():
    requirements = load_requirements(REQUIREMENTS)
    report = _report()
    report.modules = [m for m in report.modules if m.code != "BMS"]
    evaluation = evaluate(report, requirements)
    assert evaluation.verdict == VERDICT_ZEBRA
    by_id = {r.requirement.id: r for r in evaluation.results}
    assert by_id["BMS"].status == MISSING


def test_unknown_trim_letter_still_requires_rear_mcu():
    """A VIN whose 5th character is not a known trim letter must NOT exempt
    MCU_R: treating an unknown trim as 'Sport-like' would let a two-motor car
    with a missing rear MCU pass as Marlin-ready."""
    requirements = load_requirements(REQUIREMENTS)
    report = _report()
    report.vin = report.vin[:4] + "X" + report.vin[5:]
    report.modules = [m for m in report.modules if m.code != "MCU_R"]
    for module in report.modules:
        if module.code == "BCM":
            module.supplier_sw = "BCM395030"
    evaluation = evaluate(report, requirements)

    assert evaluation.trim == "X" and evaluation.trim_name == ""
    by_id = {r.requirement.id: r for r in evaluation.results}
    assert by_id["MCU_R"].status == MISSING
    assert evaluation.verdict == VERDICT_ZEBRA

    # ...whereas a known One (Z) exposes trim name and passes with MCU_R present
    evaluation = evaluate(_report(), requirements)
    assert (evaluation.trim, evaluation.trim_name) == ("Z", "One")


def test_pdf_with_too_many_pages_is_rejected():
    """Text extraction is CPU-bound and linear in page count; a PDF far larger
    than any real OLP report is rejected before extraction starts."""
    weasyprint = pytest.importorskip("weasyprint")
    from app.parser import MAX_REPORT_PAGES

    html = "".join(
        f"<p style='page-break-after: always'>page {i}</p>" for i in range(MAX_REPORT_PAGES + 5)
    )
    pdf_bytes = weasyprint.HTML(string=html).write_pdf()
    with pytest.raises(ReportParseError) as excinfo:
        parse_report(pdf_bytes, "big.pdf")
    assert excinfo.value.key == "too_many_pages"


_MINIMAL = """
version: t
profiles: ["2.0", "2.1", "2.2"]
target_profile: "2.1"
modules:
  - id: VCU
    match: [VCU]
    extract: 'VCU\\d{3}0*(\\d+)$'
    levels: {"2.0": 20, "2.1": 21}
"""


def test_requirements_validation_rejects_wrong_types():
    """A bare string for `match` used to be iterated character by character
    (VCU -> V, C, U) and silently marked the module missing on every car."""
    cases = {
        "match: [VCU]": ("match: VCU", "`match` must be a non-empty list"),
        'levels: {"2.0": 20, "2.1": 21}': ('levels: {"2.0": 20, "2.1": "21a"}', "must be an integer"),
        "    extract: 'VCU": ("    variants: nope\n    extract: 'VCU", "`variants` must be a list"),
        'profiles: ["2.0", "2.1", "2.2"]': ('profiles: "2.0, 2.1"', "`profiles` must be a non-empty list"),
        "  - id: VCU\n    match: [VCU]": ("  - VCU\n  - match: [VCU]", "must be a mapping"),
    }
    for original, (replacement, message) in cases.items():
        text = _MINIMAL.replace(original, replacement)
        assert text != _MINIMAL
        with pytest.raises(RequirementsValidationError) as excinfo:
            parse_requirements_text(text)
        assert message in str(excinfo.value), (replacement, str(excinfo.value))
    # And the unmodified minimal file is fine
    assert parse_requirements_text(_MINIMAL).modules[0].match == ["VCU"]


def test_ok_below_top_only_lists_modules_with_a_top_level():
    """A module that defines no level for the highest profile cannot be
    'left behind by 2.2' and must not appear in that list."""
    requirements = parse_requirements_text(_MINIMAL)  # VCU has no 2.2 level
    evaluation = evaluate(_report(), requirements)
    assert evaluation.verdict == VERDICT_READY
    assert evaluation.ok_below_top == []

    with_top = parse_requirements_text(_MINIMAL.replace('"2.1": 21}', '"2.1": 21, "2.2": 23}'))
    evaluation = evaluate(_report(), with_top)  # VCU039021 -> 21 < 23
    assert [r.requirement.id for r in evaluation.ok_below_top] == ["VCU"]
    assert evaluation.results[0].top_required == 23


def _fixture_report(name: str):
    return parse_report((FIXTURES / name).read_bytes(), name)


def test_reference_cars_from_the_fleet():
    """Module values observed on real consented uploads (Sep 2026), applied to
    the fixture report: a 100% 2.1 car, a full 2.2 car and two Marlin cars.
    These pin the verdicts the association saw and agreed with."""
    requirements = load_requirements(REQUIREMENTS)

    full_21 = evaluate(_fixture_report("olp_report_21_full.txt"), requirements)
    assert full_21.verdict == VERDICT_READY
    assert all(r.status == OK for r in full_21.results)
    # Every module exactly at the 2.1 minimum -> all five 2.2-only ECUs are left behind
    assert {r.requirement.id for r in full_21.ok_below_top} == {"BCM", "ESP", "MCU_F", "MCU_R", "VCU"}

    full_22 = evaluate(_fixture_report("olp_report_22_full.txt"), requirements)
    assert full_22.verdict == VERDICT_READY
    assert full_22.ok_below_top == []
    assert {r.requirement.id: r.level for r in full_22.results} == {
        m: "2.2" for m in ["BCM", "ESP", "ECC", "BMS", "MCU_R", "MCU_F", "VCU"]
    }


def test_car_already_on_marlin_gets_marlin_verdict_not_ready():
    """VCU 24 (= VCU 2.4) only exists on Marlin cars. Such a car must not be
    told it 'can be updated to Marlin' (or worse, that it is a zebra); it gets
    the informational 'already on Marlin' verdict and a list of what Marlin
    left below the 2.2 level."""
    from app.rules import VERDICT_MARLIN

    requirements = load_requirements(REQUIREMENTS)

    marlin = evaluate(_fixture_report("olp_report_marlin.txt"), requirements)
    assert marlin.verdict == VERDICT_MARLIN
    assert marlin.below_top == []
    by_id = {r.requirement.id: r for r in marlin.results}
    assert by_id["VCU"].extracted == 24 and by_id["VCU"].requirement.marlin_level == 24

    bcm41 = evaluate(_fixture_report("olp_report_marlin_bcm41.txt"), requirements)
    assert bcm41.verdict == VERDICT_MARLIN
    assert [(r.requirement.id, r.extracted, r.top_required) for r in bcm41.below_top] == [("BCM", 41, 42)]

    # A Marlin car with a failing critical module is still "on Marlin", not a zebra
    report = _fixture_report("olp_report_marlin.txt")
    report.modules = [m for m in report.modules if m.code != "BMS"]
    assert evaluate(report, requirements).verdict == VERDICT_MARLIN

    # VCU below the marker -> ordinary readiness logic applies
    report = _fixture_report("olp_report_marlin.txt")
    for m in report.modules:
        if m.code == "VCU":
            m.supplier_sw = "VCU039023"
    assert evaluate(report, requirements).verdict == VERDICT_READY

    # Without any marlin_level in the file, no car can be "on Marlin"
    plain = parse_requirements_text(REQUIREMENTS.read_text().replace("marlin_level: 24", ""))
    assert all(m.marlin_level is None for m in plain.modules)
    assert evaluate(_fixture_report("olp_report_marlin.txt"), plain).verdict == VERDICT_READY


def test_marlin_level_must_be_an_integer():
    with pytest.raises(RequirementsValidationError) as excinfo:
        parse_requirements_text(REQUIREMENTS.read_text().replace("marlin_level: 24", "marlin_level: soon"))
    assert "marlin_level" in str(excinfo.value)


def test_duplicate_ecu_block_stays_visible_as_extra_module():
    """Two blocks with the same code: the first is evaluated, the second must
    show up under 'other modules' instead of vanishing."""
    import copy

    requirements = load_requirements(REQUIREMENTS)
    report = _report()
    bms = next(m for m in report.modules if m.code == "BMS")
    duplicate = copy.copy(bms)
    duplicate.supplier_sw = "BMSN39001"
    report.modules.append(duplicate)
    evaluation = evaluate(report, requirements)
    assert next(r for r in evaluation.results if r.requirement.id == "BMS").version == "BMSN39021"
    assert any(m.supplier_sw == "BMSN39001" for m in evaluation.extra_modules)
    assert len(evaluation.extra_modules) == 31


def test_variant_levels_override_per_profile_not_wholesale():
    """A variant that only sets the 2.2 level keeps the module's 2.1 level;
    it used to replace the whole mapping and produce 'unparseable'."""
    text = _MINIMAL.replace(
        'levels: {"2.0": 20, "2.1": 21}',
        'levels: {"2.0": 20, "2.1": 21}\n    variants:\n      - name: X\n        pattern: "^VCU"\n        levels: {"2.2": 23}',
    )
    requirements = parse_requirements_text(text)
    result = evaluate(_report(), requirements).results[0]
    assert (result.variant, result.status, result.required, result.top_required) == ("X", OK, 21, 23)


def test_empty_supplier_version_gets_its_own_status():
    from app.rules import EMPTY

    requirements = load_requirements(REQUIREMENTS)
    report = _report()
    for m in report.modules:
        if m.code == "VCU":
            m.supplier_sw = "   "
    evaluation = evaluate(report, requirements)
    vcu = next(r for r in evaluation.results if r.requirement.id == "VCU")
    assert vcu.status == EMPTY and vcu.required == 21
    assert evaluation.verdict == VERDICT_ZEBRA


def test_notes_field_is_parsed_and_must_be_a_string():
    requirements = load_requirements(REQUIREMENTS)
    assert "Open points" in requirements.notes
    with pytest.raises(RequirementsValidationError):
        parse_requirements_text(_MINIMAL + "notes: [not, a, string]\n")
