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
    requirements = load_requirements(REQUIREMENTS)
    evaluation = evaluate(_report(), requirements)

    by_id = {r.requirement.id: r for r in evaluation.results}
    # 2.1 level satisfied
    assert by_id["ECC"].status == OK and by_id["ECC"].extracted == 24
    assert by_id["BMS"].status == OK and by_id["BMS"].extracted == 21
    # 2.0 level — below the 2.1 requirement
    assert by_id["VCU"].status == OUTDATED
    assert (by_id["VCU"].extracted, by_id["VCU"].required, by_id["VCU"].level) == (21, 23, "2.0")
    assert by_id["MCU_F"].status == OUTDATED and by_id["MCU_F"].extracted == 19
    assert by_id["MCU_R"].status == OUTDATED and by_id["MCU_R"].extracted == 19
    assert by_id["ESP"].status == OUTDATED
    assert (by_id["ESP"].extracted, by_id["ESP"].required) == (402, 500)
    # BCM: assumed extraction gives 21 — below both 2.0 (30) and 2.1 (42)
    assert by_id["BCM"].status == OUTDATED
    assert (by_id["BCM"].extracted, by_id["BCM"].level) == (21, None)

    assert evaluation.verdict == VERDICT_ZEBRA
    assert {r.requirement.id for r in evaluation.failing_critical} == {
        "BCM", "ESP", "VCU", "MCU_F", "MCU_R",
    }
    # 37 modules in the report, 7 with requirements -> 30 without
    assert len(evaluation.extra_modules) == 30


def test_evaluate_full_21_car():
    """Lift all modules to 2.1 level -> directly Marlin-ready."""
    requirements = load_requirements(REQUIREMENTS)
    report = _report()
    upgrades = {
        "BCM": "BCM395042",
        "ESP": "89324V050000990131",
        "VCU": "VCU039023",
        "MCU_F": "MCU5000021",
        "MCU_R": "MCU5000021",
    }
    for module in report.modules:
        if module.code in upgrades:
            module.supplier_sw = upgrades[module.code]
    evaluation = evaluate(report, requirements)
    assert evaluation.verdict == VERDICT_READY
    assert all(r.status == OK for r in evaluation.results)


def test_missing_critical_module_gives_zebra():
    requirements = load_requirements(REQUIREMENTS)
    report = _report()
    report.modules = [m for m in report.modules if m.code != "BMS"]
    evaluation = evaluate(report, requirements)
    assert evaluation.verdict == VERDICT_ZEBRA
    by_id = {r.requirement.id: r for r in evaluation.results}
    assert by_id["BMS"].status == MISSING
