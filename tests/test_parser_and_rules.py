"""Tester mot det ekte OLP-formatet.

Fixturen `olp_report.txt` er tekstuttrekket fra en ekte OceanLink Pro-rapport
(2026-08-28) med VIN anonymisert. Bilen i fixturen er en ekte «zebra»:
BMS/ECC på 2.1-nivå, VCU/MCU/ESP på 2.0-nivå.
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
    assert by_code["EPS1"].supplier_sw == "EPS395001"  # ledende mellomrom trimmet
    assert by_code["HYDRA"].section == "ADAS"
    assert by_code["ESP"].software == "FM292045S020J"
    assert by_code["ESP"].bootloader == "FM292045B020B"


def test_parse_pdf_roundtrip(tmp_path):
    """PDF-stien: render fixturen til PDF og parse den tilbake."""
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
    # Riktig overskrift men ingen VIN
    with pytest.raises(ReportParseError):
        parse_report(b"ECU Software Version Report\nGW - Gateway\n")


def test_evaluate_zebra_car():
    requirements = load_requirements(REQUIREMENTS)
    evaluation = evaluate(_report(), requirements)

    by_id = {r.requirement.id: r for r in evaluation.results}
    # 2.1-nivå oppfylt
    assert by_id["ECC"].status == OK and by_id["ECC"].extracted == 24
    assert by_id["BMS"].status == OK and by_id["BMS"].extracted == 21
    # 2.0-nivå — under 2.1-kravet
    assert by_id["VCU"].status == OUTDATED
    assert (by_id["VCU"].extracted, by_id["VCU"].required, by_id["VCU"].level) == (21, 23, "2.0")
    assert by_id["MCU_F"].status == OUTDATED and by_id["MCU_F"].extracted == 19
    assert by_id["MCU_R"].status == OUTDATED and by_id["MCU_R"].extracted == 19
    assert by_id["ESP"].status == OUTDATED
    assert (by_id["ESP"].extracted, by_id["ESP"].required) == (402, 500)
    # BCM: antatt uttrekk gir 21 — under både 2.0 (30) og 2.1 (42)
    assert by_id["BCM"].status == OUTDATED
    assert (by_id["BCM"].extracted, by_id["BCM"].level) == (21, None)

    assert evaluation.verdict == VERDICT_ZEBRA
    assert {r.requirement.id for r in evaluation.failing_critical} == {
        "BCM", "ESP", "VCU", "MCU_F", "MCU_R",
    }
    # 37 moduler i rapporten, 7 med krav -> 30 uten
    assert len(evaluation.extra_modules) == 30


def test_evaluate_full_21_car():
    """Løft alle moduler til 2.1-nivå -> direkte Marlin-klar."""
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
