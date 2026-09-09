"""SQLite storage: the association's vehicle register (every analyzed report,
with all module readings), usage statistics, audit log and admin sessions.

Storage is mandatory since v2 (Sep 2026): a member must accept it to run the
analysis. Earlier rows come from the consent period and are kept.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .parser import ModuleReading, ParsedReport
from .rules import Evaluation, RequirementSet, evaluate

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id TEXT PRIMARY KEY,
    vin TEXT NOT NULL,
    vin_hash TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    verdict TEXT NOT NULL,
    requirements_version TEXT NOT NULL,
    lang TEXT NOT NULL DEFAULT 'en',
    stored_filename TEXT,
    trim TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    complete_profile TEXT,
    top_evidence TEXT,
    report_date TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_submissions_vin_hash ON submissions(vin_hash);

-- One row per ECU block in the report. module_id/status/extracted/level/
-- evidence_level are the rule engine's view and are rewritten on
-- re-evaluation; code/section and the four version fields are the report's.
CREATE TABLE IF NOT EXISTS module_readings (
    submission_id TEXT NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    module_id TEXT,
    raw_name TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    section TEXT NOT NULL DEFAULT '',
    extracted INTEGER,
    level TEXT,
    evidence_level TEXT,
    software TEXT NOT NULL DEFAULT '',
    hardware TEXT NOT NULL DEFAULT '',
    bootloader TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_readings_module ON module_readings(module_id);
CREATE INDEX IF NOT EXISTS idx_readings_submission ON module_readings(submission_id);

-- Anonymous usage statistics: never VIN, report content, or raw IP.
-- ip_hash is a daily-rotating hash, only used to count unique users per day.
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

-- Admin login sessions (form login). Only a hash of the cookie token is
-- stored, so a copy of the database does not yield usable sessions.
CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    csrf_token TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
"""

# Columns added after the first release, applied to existing databases with
# ALTER TABLE (SQLite cannot add columns through CREATE TABLE IF NOT EXISTS).
_MIGRATIONS = {
    "submissions": [
        ("trim", "TEXT NOT NULL DEFAULT ''"),
        ("outcome", "TEXT NOT NULL DEFAULT ''"),
        ("complete_profile", "TEXT"),
        ("top_evidence", "TEXT"),
        ("report_date", "TEXT NOT NULL DEFAULT ''"),
        ("country", "TEXT NOT NULL DEFAULT ''"),
    ],
    "module_readings": [
        ("code", "TEXT NOT NULL DEFAULT ''"),
        ("section", "TEXT NOT NULL DEFAULT ''"),
        ("extracted", "INTEGER"),
        ("level", "TEXT"),
        ("evidence_level", "TEXT"),
        ("software", "TEXT NOT NULL DEFAULT ''"),
        ("hardware", "TEXT NOT NULL DEFAULT ''"),
        ("bootloader", "TEXT NOT NULL DEFAULT ''"),
    ],
}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def vin_hash(vin: str) -> str:
    return hashlib.sha256(vin.upper().encode()).hexdigest()


_INSERT_READING = (
    "INSERT INTO module_readings (submission_id, module_id, raw_name, version, status,"
    " code, section, extracted, level, evidence_level, software, hardware, bootloader)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _reading_rows(submission_id: str, evaluation: Evaluation) -> list[tuple]:
    """One row per ECU block: the evaluated modules (with the rule engine's
    view) followed by the report's other modules (status "extra")."""
    rows = []
    for r in evaluation.results:
        m = r.reading
        if m is None:
            continue
        rows.append((
            submission_id, r.requirement.id, m.raw_name, m.supplier_sw, r.status,
            m.code, m.section, r.extracted, r.level, r.evidence_level,
            m.software, m.hardware, m.bootloader,
        ))
    for m in evaluation.extra_modules:
        rows.append((
            submission_id, None, m.raw_name, m.supplier_sw, "extra",
            m.code, m.section, None, None, None, m.software, m.hardware, m.bootloader,
        ))
    return rows


def _report_from_rows(vin: str, rows) -> ParsedReport:
    """Rebuilds a ParsedReport from stored readings. Rows from before v2 have
    no `code`; it is recovered from raw_name ("CODE - Name")."""
    modules = []
    for row in rows:
        code = row["code"] or row["raw_name"].split(" - ", 1)[0]
        name = row["raw_name"].split(" - ", 1)[1] if " - " in row["raw_name"] else row["raw_name"]
        modules.append(ModuleReading(
            code=code, name=name, section=row["section"] or "", supplier_sw=row["version"],
            software=row["software"] or "", hardware=row["hardware"] or "",
            bootloader=row["bootloader"] or "",
        ))
    return ParsedReport(vin=vin, modules=modules)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # persistent; set once per database file
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        for table, columns in _MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One connection per unit of work: commits on success, rolls back on
        error, and always closes (sqlite3's own context manager only commits)."""
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.row_factory = sqlite3.Row
            with conn:
                yield conn
        finally:
            conn.close()

    def store_submission(
        self,
        report: ParsedReport,
        evaluation: Evaluation,
        lang: str,
        stored_filename: str | None,
        country: str = "",
    ) -> str:
        submission_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO submissions (id, vin, vin_hash, uploaded_at, verdict,"
                " requirements_version, lang, stored_filename, trim, outcome,"
                " complete_profile, top_evidence, report_date, country)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    submission_id,
                    report.vin.upper(),
                    vin_hash(report.vin),
                    datetime.now(UTC).isoformat(),
                    evaluation.verdict,
                    evaluation.requirements_version,
                    lang,
                    stored_filename,
                    evaluation.trim,
                    evaluation.outcome,
                    evaluation.complete_profile,
                    evaluation.top_evidence,
                    str(report.meta.get("report_date", ""))[:32],
                    country[:8],
                ),
            )
            conn.executemany(_INSERT_READING, _reading_rows(submission_id, evaluation))
        return submission_id

    # --- re-evaluation and deletion (vehicle register maintenance) ---------

    def reevaluate_all(self, requirements: RequirementSet) -> int:
        """Re-runs the rule engine on every stored report (rebuilt from its
        module readings) and rewrites the derived columns. Backfills rows from
        before v2 and applies changed requirements. All-or-nothing."""
        count = 0
        with self._connect() as conn:
            submissions = conn.execute("SELECT id, vin FROM submissions").fetchall()
            for sub in submissions:
                rows = conn.execute(
                    "SELECT raw_name, version, code, section, software, hardware, bootloader"
                    " FROM module_readings WHERE submission_id = ? ORDER BY rowid",
                    (sub["id"],),
                ).fetchall()
                report = _report_from_rows(sub["vin"], rows)
                evaluation = evaluate(report, requirements)
                conn.execute(
                    "UPDATE submissions SET verdict = ?, requirements_version = ?, trim = ?,"
                    " outcome = ?, complete_profile = ?, top_evidence = ? WHERE id = ?",
                    (evaluation.verdict, evaluation.requirements_version, evaluation.trim,
                     evaluation.outcome, evaluation.complete_profile, evaluation.top_evidence,
                     sub["id"]),
                )
                conn.execute("DELETE FROM module_readings WHERE submission_id = ?", (sub["id"],))
                conn.executemany(_INSERT_READING, _reading_rows(sub["id"], evaluation))
                count += 1
        return count

    def delete_vehicle(self, vin: str) -> list[str]:
        """Deletes every submission for the VIN; returns the stored filenames
        so the caller can remove the files too."""
        with self._connect() as conn:
            files = [
                row["stored_filename"]
                for row in conn.execute(
                    "SELECT stored_filename FROM submissions WHERE vin_hash = ?", (vin_hash(vin),)
                )
                if row["stored_filename"]
            ]
            conn.execute("DELETE FROM submissions WHERE vin_hash = ?", (vin_hash(vin),))
        return files

    def add_usage(self, *, country: str, ui_lang: str, browser_lang: str,
                  outcome: str, consent: bool, ip_hash: str) -> None:
        now = datetime.now(UTC)
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
                (datetime.now(UTC).isoformat(), username, ip, action, detail),
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

    # --- admin sessions -------------------------------------------------

    def create_session(self, username: str) -> tuple[str, str]:
        """Creates a login session; returns (cookie token, CSRF token)."""
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO admin_sessions (token_hash, username, csrf_token,"
                " created_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                (_token_hash(token), username, csrf, now, now),
            )
        return token, csrf

    def get_session(self, token: str, *, idle_seconds: float, max_age_seconds: float) -> dict | None:
        """Returns {"username", "csrf_token"} for a live session, else None.
        Expired sessions (idle or absolute) are deleted on sight."""
        if not token:
            return None
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM admin_sessions WHERE last_seen_at < ? OR created_at < ?",
                (now - idle_seconds, now - max_age_seconds),
            )
            row = conn.execute(
                "SELECT username, csrf_token, last_seen_at FROM admin_sessions WHERE token_hash = ?",
                (_token_hash(token),),
            ).fetchone()
            if row is None:
                return None
            if now - row["last_seen_at"] > 60:  # throttle writes to once a minute
                conn.execute(
                    "UPDATE admin_sessions SET last_seen_at = ? WHERE token_hash = ?",
                    (now, _token_hash(token)),
                )
        return {"username": row["username"], "csrf_token": row["csrf_token"]}

    def delete_session(self, token: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (_token_hash(token),))

    def stats(self, profiles: list[str] | None = None, target: str | None = None) -> dict:
        """Aggregated fleet statistics for the dashboard. Only the latest
        submission per VIN counts. Never returns VINs. `profiles` (ascending)
        and `target` come from the current requirements; without them the
        level names are sorted as strings."""
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
            outcomes = {
                row["outcome"]: row["n"]
                for row in conn.execute(
                    f"SELECT outcome, COUNT(*) AS n FROM ({latest}) GROUP BY outcome"
                )
            }
            trims = [
                dict(row)
                for row in conn.execute(
                    f"SELECT trim, COUNT(*) AS n FROM ({latest}) GROUP BY trim ORDER BY n DESC"
                )
            ]
            countries = [
                dict(row)
                for row in conn.execute(
                    f"SELECT country, COUNT(*) AS n FROM ({latest})"
                    " GROUP BY country ORDER BY n DESC LIMIT 20"
                )
            ]
            per_week = [
                dict(row)
                for row in conn.execute(
                    "SELECT strftime('%Y-W%W', uploaded_at) AS week, COUNT(*) AS uploads,"
                    " COUNT(DISTINCT vin_hash) AS vehicles"
                    " FROM submissions GROUP BY week ORDER BY week DESC LIMIT 26"
                )
            ]
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
            # Level per module over the latest reports: which profile each
            # module sits at ("below" = has a number but under every profile,
            # "unknown" = no number could be read)
            module_levels: dict[str, dict[str, int]] = {}
            seen_levels: set[str] = set()
            for row in conn.execute(
                f"SELECT mr.module_id, mr.level, mr.extracted, COUNT(*) AS n"
                f" FROM module_readings mr"
                f" JOIN ({latest}) s ON s.id = mr.submission_id"
                f" WHERE mr.module_id IS NOT NULL"
                f" GROUP BY mr.module_id, mr.level, mr.extracted IS NULL"
            ):
                if row["level"] is not None:
                    key = row["level"]
                    seen_levels.add(key)
                elif row["extracted"] is not None:
                    key = "below"
                else:
                    key = "unknown"
                bucket = module_levels.setdefault(row["module_id"], {})
                bucket[key] = bucket.get(key, 0) + row["n"]
            if not profiles:
                profiles = sorted(seen_levels)
            top = profiles[-1] if profiles else None
            target = target or (profiles[-2] if len(profiles) > 1 else top)

            # "Split" cars: which modules hold back the incomplete ones.
            # zebra_22: every module >= target, some below top -> count modules below top.
            # zebra_21: modules below the target level.
            def _below(outcome: str, floor: str | None) -> dict:
                if floor is None:
                    return {"cars": 0, "modules": []}
                at_or_above = [p for p in profiles if profiles.index(p) >= profiles.index(floor)]
                placeholders = ",".join("?" * len(at_or_above))
                cars = conn.execute(
                    f"SELECT COUNT(*) AS n FROM ({latest}) WHERE outcome = ?", (outcome,)
                ).fetchone()["n"]
                modules = [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT mr.module_id, COUNT(*) AS n FROM module_readings mr"
                        f" JOIN ({latest}) s ON s.id = mr.submission_id"
                        f" WHERE s.outcome = ? AND mr.module_id IS NOT NULL"
                        f" AND (mr.level IS NULL OR mr.level NOT IN ({placeholders}))"
                        f" GROUP BY mr.module_id ORDER BY n DESC",
                        (outcome, *at_or_above),
                    )
                ]
                return {"cars": cars, "modules": modules}

            split = {
                "zebra_22": _below("zebra_22", top),
                "zebra_21": _below("zebra_21", target),
            }
        return {
            "unique_vins": unique_vins,
            "total_submissions": total,
            "verdicts": verdicts,
            "outcomes": outcomes,
            "trims": trims,
            "countries": countries,
            "per_week": per_week,
            "module_versions": module_versions,
            "module_levels": module_levels,
            "profiles": profiles,
            "split": split,
        }
