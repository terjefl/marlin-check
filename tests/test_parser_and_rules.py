from pathlib import Path

import pytest

from app.parser import ReportParseError, parse_report
from app.rules import MISSING, OK, OUTDATED, VERDICT_READY, VERDICT_ZEBRA, evaluate, load_requirements

FIXTURES = Path(__file__).parent / "fixtures"
REQUIREMENTS = Path(__file__).parent.parent / "requirements.example.yaml"


def _report():
    return parse_report((FIXTURES / "synthetic_report.txt").read_bytes(), "synthetic_report.txt")


def test_parse_synthetic_report():
    report = _report()
    assert report.vin == "VCF1ZBU25PG012345"
    names = [m.raw_name for m in report.modules]
    assert names == [
        "Vehicle Control Unit",
        "Battery Management",
        "Infotainment",
        "Door Module FL",
        "Door Module FR",
    ]
    assert report.modules[0].version == "2.1.4"


def test_parse_rejects_garbage():
    with pytest.raises(ReportParseError):
        parse_report(b"not a report at all")
    with pytest.raises(ReportParseError):
        parse_report(b"")


def test_evaluate_ready_and_zebra():
    requirements = load_requirements(REQUIREMENTS)
    report = _report()
    evaluation = evaluate(report, requirements)

    by_id = {r.requirement.id: r for r in evaluation.results}
    assert by_id["VCU"].status == OK
    assert by_id["BMS"].status == OK
    assert by_id["ICM"].status == OUTDATED  # 1.9.2 < 2.0.0, men ikke critical
    # ICM er ikke kritisk -> bilen er likevel Marlin-klar
    assert evaluation.verdict == VERDICT_READY
    # Dørmodulene har ingen krav og havner som ekstra
    assert len(evaluation.extra_modules) == 2

    # Senk VCU under kravet -> zebra
    report.modules[0].version = "1.8.0"
    evaluation = evaluate(report, requirements)
    assert evaluation.verdict == VERDICT_ZEBRA
    assert [r.requirement.id for r in evaluation.failing_critical] == ["VCU"]


def test_missing_critical_module_gives_zebra():
    requirements = load_requirements(REQUIREMENTS)
    report = _report()
    report.modules = [m for m in report.modules if m.raw_name != "Battery Management"]
    evaluation = evaluate(report, requirements)
    assert evaluation.verdict == VERDICT_ZEBRA
    by_id = {r.requirement.id: r for r in evaluation.results}
    assert by_id["BMS"].status == MISSING
