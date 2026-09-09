"""The vehicle register: schema migration of a pre-v2 database, re-evaluation
of stored reports against the current requirements, and deletion per VIN."""

import sqlite3
from pathlib import Path

from app.db import Database
from app.parser import parse_report
from app.rules import evaluate, load_requirements

FIXTURES = Path(__file__).parent / "fixtures"
REQUIREMENTS = Path(__file__).parent.parent / "requirements.example.yaml"

# The schema as shipped in the consent period (before Sep 2026), verbatim.
OLD_SCHEMA = """
CREATE TABLE submissions (
    id TEXT PRIMARY KEY, vin TEXT NOT NULL, vin_hash TEXT NOT NULL,
    uploaded_at TEXT NOT NULL, verdict TEXT NOT NULL,
    requirements_version TEXT NOT NULL, lang TEXT NOT NULL DEFAULT 'en',
    stored_filename TEXT
);
CREATE TABLE module_readings (
    submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    module_id TEXT, raw_name TEXT NOT NULL, version TEXT NOT NULL, status TEXT NOT NULL
);
"""


def _old_database(path: Path, report_name: str, vin: str) -> None:
    """Writes a consent-era database with one submission, the way v1 stored it."""
    report = parse_report((FIXTURES / report_name).read_bytes(), report_name)
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO submissions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("old1", vin, "hash-" + vin, "2026-09-01T10:00:00+00:00", "ready", "old", "en", None),
    )
    conn.executemany(
        "INSERT INTO module_readings VALUES (?, ?, ?, ?, ?)",
        [("old1", None, m.raw_name, m.supplier_sw, "extra") for m in report.modules],
    )
    conn.commit()
    conn.close()


def test_old_database_is_migrated_and_backfilled_by_reevaluation(tmp_path):
    path = tmp_path / "marlin.sqlite3"
    _old_database(path, "olp_report_22_full.txt", "VCF1ZBE20PG099997")

    db = Database(path)  # migration runs here
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(submissions)")}
    assert {"trim", "outcome", "complete_profile", "top_evidence", "country"} <= columns
    old = conn.execute("SELECT * FROM submissions").fetchone()
    assert old["outcome"] == "" and old["trim"] == ""  # not yet evaluated

    requirements = load_requirements(REQUIREMENTS)
    assert db.reevaluate_all(requirements) == 1

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    sub = conn.execute("SELECT * FROM submissions").fetchone()
    assert (sub["outcome"], sub["complete_profile"], sub["top_evidence"], sub["trim"], sub["verdict"]) == (
        "full_22", "2.2", "2.2", "Z", "ready"
    )
    assert sub["requirements_version"] == requirements.version
    readings = conn.execute("SELECT * FROM module_readings ORDER BY rowid").fetchall()
    assert len(readings) == 37
    bcm = next(r for r in readings if r["code"] == "BCM")
    assert (bcm["module_id"], bcm["extracted"], bcm["level"], bcm["status"]) == ("BCM", 42, "2.2", "ok")
    assert all(r["code"] for r in readings)  # code recovered from raw_name

    # Opening again is a no-op (idempotent migration), and the data survives
    Database(path)
    assert db.stats()["unique_vins"] == 1


def test_reevaluation_applies_changed_requirements(tmp_path):
    db = Database(tmp_path / "m.sqlite3")
    requirements = load_requirements(REQUIREMENTS)
    report = parse_report((FIXTURES / "olp_report_22_full.txt").read_bytes(), "r.txt")
    db.store_submission(report, evaluate(report, requirements), "en", None)
    assert db.stats()["outcomes"] == {"full_22": 1}

    # Raise the ECC 2.2 requirement to 25 (an open point): the 2.2 car shows 25, still full 2.2.
    stricter = REQUIREMENTS.read_text().replace('levels: {"2.0": 19, "2.1": 24, "2.2": 24}',
                                                'levels: {"2.0": 19, "2.1": 24, "2.2": 25}')
    from app.rules import parse_requirements_text
    db.reevaluate_all(parse_requirements_text(stricter))
    assert db.stats()["outcomes"] == {"full_22": 1}
    # ...and to 26: now the car is a 2.2 zebra (ECC below 2.2, everything else on 2.2)
    db.reevaluate_all(parse_requirements_text(stricter.replace('"2.2": 25}', '"2.2": 26}')))
    assert db.stats()["outcomes"] == {"zebra_22": 1}


def test_delete_vehicle_removes_every_submission_and_names_the_files(tmp_path):
    db = Database(tmp_path / "m.sqlite3")
    requirements = load_requirements(REQUIREMENTS)
    for name, filename in [("olp_report_21_full.txt", "a.txt"), ("olp_report_21_full.txt", "b.txt"),
                           ("olp_report_22_full.txt", "c.txt")]:
        report = parse_report((FIXTURES / name).read_bytes(), name)
        db.store_submission(report, evaluate(report, requirements), "en", filename)
    assert db.stats()["total_submissions"] == 3

    vin_21 = parse_report((FIXTURES / "olp_report_21_full.txt").read_bytes(), "x").vin
    assert sorted(db.delete_vehicle(vin_21.lower())) == ["a.txt", "b.txt"]
    stats = db.stats()
    assert (stats["unique_vins"], stats["total_submissions"]) == (1, 1)
    conn = sqlite3.connect(db.path)
    assert conn.execute("SELECT COUNT(*) FROM module_readings").fetchone()[0] == 37
    assert db.delete_vehicle("VCF1ZBE20PG000000") == []


def test_fleet_statistics_count_outcomes_levels_and_split_cars(tmp_path):
    db = Database(tmp_path / "m.sqlite3")
    requirements = load_requirements(REQUIREMENTS)

    def _store(name, vin_suffix, **overrides):
        report = parse_report((FIXTURES / name).read_bytes(), name)
        report.vin = report.vin[:-2] + vin_suffix
        for m in report.modules:
            if m.code in overrides:
                m.supplier_sw = overrides[m.code]
        db.store_submission(report, evaluate(report, requirements), "en", None, country="NO")

    _store("olp_report_21_full.txt", "01")
    _store("olp_report_22_full.txt", "02")
    _store("olp_report_21_full.txt", "03", BCM="BCM395042", VCU="VCU039023")   # 2.2 zebra
    _store("olp_report.txt", "04")                                           # BCM 21: 2.1 zebra
    _store("olp_report_marlin.txt", "05")
    _store("olp_report_21_full.txt", "01")                                   # re-upload, same car

    stats = db.stats(profiles=requirements.profiles, target=requirements.target_profile)
    assert (stats["unique_vins"], stats["total_submissions"]) == (5, 6)
    assert stats["outcomes"] == {"full_21": 1, "full_22": 1, "zebra_22": 1, "zebra_21": 1, "marlin": 1}
    assert stats["trims"] == [{"trim": "Z", "n": 5}]
    assert stats["countries"] == [{"country": "NO", "n": 5}]
    assert stats["per_week"][0]["uploads"] == 6 and stats["per_week"][0]["vehicles"] == 5
    # The 2.2 zebra is held back by ESP and both MCUs; the 2.1 zebra by BCM
    assert stats["split"]["zebra_22"]["cars"] == 1
    assert {m["module_id"] for m in stats["split"]["zebra_22"]["modules"]} == {"ESP", "MCU_F", "MCU_R"}
    assert stats["split"]["zebra_21"] == {"cars": 1, "modules": [{"module_id": "BCM", "n": 1}]}
    # BCM over the five cars: 21 (below every profile), 30, 42, 42, 42
    assert stats["module_levels"]["BCM"] == {"below": 1, "2.1": 1, "2.2": 3}
    assert stats["profiles"] == ["2.0", "2.1", "2.2"]
