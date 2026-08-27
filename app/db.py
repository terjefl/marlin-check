"""SQLite-lagring for innsendinger med samtykke, og aggregert statistikk.

Uten samtykke skrives INGENTING hit — hele analysen skjer i minnet.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .parser import ParsedReport
from .rules import Evaluation

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    vin TEXT NOT NULL,
    vin_hash TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    requirements_version TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'en',
    stored_filename TEXT
);
CREATE INDEX IF NOT EXISTS idx_submissions_vin_hash ON submissions(vin_hash);

CREATE TABLE IF NOT EXISTS module_readings (
    submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    module_id TEXT,
    raw_name TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_module ON module_readings(module_id);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def store_submission(
        self,
        report: ParsedReport,
        evaluation: Evaluation,
        lang: str,
        stored_filename: str | None,
    ) -> str:
        submission_id = uuid.uuid4().hex
        vin_hash = hashlib.sha256(report.vin.upper().encode()).hexdigest()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO submissions (id, vin, vin_hash, uploaded_at, verdict,"
                " requirements_version, lang, stored_filename)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    submission_id,
                    report.vin.upper(),
                    vin_hash,
                    datetime.now(timezone.utc).isoformat(),
                    evaluation.verdict,
                    evaluation.requirements_version,
                    lang,
                    stored_filename,
                ),
            )
            rows = [
                (submission_id, r.requirement.id, r.raw_name, r.version, r.status)
                for r in evaluation.results
                if r.raw_name
            ] + [
                (submission_id, None, m.raw_name, m.version, "extra")
                for m in evaluation.extra_modules
            ]
            conn.executemany(
                "INSERT INTO module_readings (submission_id, module_id, raw_name, version, status)"
                " VALUES (?, ?, ?, ?, ?)",
                rows,
            )
        return submission_id

    def stats(self) -> dict:
        """Aggregert statistikk. Kun siste innsending per VIN teller."""
        with self._connect() as conn:
            latest = (
                "SELECT s.* FROM submissions s"
                " JOIN (SELECT vin_hash, MAX(uploaded_at) AS latest"
                "       FROM submissions GROUP BY vin_hash) m"
                " ON s.vin_hash = m.vin_hash AND s.uploaded_at = m.latest"
            )
            unique_vins = conn.execute(
                f"SELECT COUNT(*) AS n FROM ({latest})"
            ).fetchone()["n"]
            total = conn.execute("SELECT COUNT(*) AS n FROM submissions").fetchone()["n"]
            verdicts = {
                row["verdict"]: row["n"]
                for row in conn.execute(
                    f"SELECT verdict, COUNT(*) AS n FROM ({latest}) GROUP BY verdict"
                )
            }
            module_versions: dict[str, list[dict]] = {}
            for row in conn.execute(
                f"SELECT mr.module_id, mr.version, COUNT(*) AS n"
                f" FROM module_readings mr"
                f" JOIN ({latest}) s ON s.id = mr.submission_id"
                f" WHERE mr.module_id IS NOT NULL"
                f" GROUP BY mr.module_id, mr.version"
                f" ORDER BY mr.module_id, n DESC"
            ):
                module_versions.setdefault(row["module_id"], []).append(
                    {"version": row["version"], "count": row["n"]}
                )
        return {
            "unique_vins": unique_vins,
            "total_submissions": total,
            "verdicts": verdicts,
            "module_versions": module_versions,
        }
