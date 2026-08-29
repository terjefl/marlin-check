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

-- Anonym bruksstatistikk: aldri VIN, rapportinnhold eller rå IP.
-- ip_hash er en døgnroterende hash, kun for å telle unike brukere per dag.
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT '',
    ui_lang TEXT NOT NULL DEFAULT '',
    browser_lang TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL,
    consent INTEGER NOT NULL DEFAULT 0,
    ip_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_events(day);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    username TEXT NOT NULL,
    ip TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
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

    def add_usage(self, *, country: str, ui_lang: str, browser_lang: str,
                  outcome: str, consent: bool, ip_hash: str) -> None:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO usage_events (ts, day, country, ui_lang, browser_lang,"
                " outcome, consent, ip_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now.isoformat(), now.strftime("%Y-%m-%d"), country, ui_lang,
                 browser_lang, outcome, int(consent), ip_hash),
            )

    def usage_stats(self, days: int = 14) -> dict:
        with self._connect() as conn:
            totals = conn.execute(
                "SELECT COUNT(*) AS n, SUM(consent) AS consented FROM usage_events"
            ).fetchone()
            outcomes = {
                row["outcome"]: row["n"]
                for row in conn.execute(
                    "SELECT outcome, COUNT(*) AS n FROM usage_events GROUP BY outcome"
                )
            }
            countries = [
                dict(row)
                for row in conn.execute(
                    "SELECT country, COUNT(*) AS n FROM usage_events"
                    " GROUP BY country ORDER BY n DESC LIMIT 15"
                )
            ]
            languages = [
                dict(row)
                for row in conn.execute(
                    "SELECT ui_lang, COUNT(*) AS n FROM usage_events"
                    " GROUP BY ui_lang ORDER BY n DESC"
                )
            ]
            per_day = [
                dict(row)
                for row in conn.execute(
                    "SELECT day, COUNT(*) AS uploads,"
                    " COUNT(DISTINCT ip_hash) AS unique_users"
                    " FROM usage_events GROUP BY day ORDER BY day DESC LIMIT ?",
                    (days,),
                )
            ]
        return {
            "total": totals["n"],
            "consented": totals["consented"] or 0,
            "outcomes": outcomes,
            "countries": countries,
            "languages": languages,
            "per_day": per_day,
        }

    def add_audit(self, username: str, ip: str, action: str, detail: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_log (ts, username, ip, action, detail) VALUES (?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), username, ip, action, detail),
            )

    def audit_entries(self, limit: int = 50) -> list[dict]:
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT ts, username, ip, action, detail FROM audit_log"
                    " ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            ]

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
