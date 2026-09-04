"""Tests against the real OLP format.

The `olp_report.txt` fixture is the text extraction of a real OceanLink Pro
report (2026-08-28) with the VIN anonymized. The car in the fixture is a real
"zebra": BMS/ECC at 2.1 level, VCU/MCU/ESP at 2.0 level.
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
    evaluate,
    load_requirements,
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
