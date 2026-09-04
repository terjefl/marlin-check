# Marlin Readiness Check

Web portal for the Fisker Owners Association: members upload an ECU diagnostics
report exported from **OceanLink Pro (OLP)**, and the portal checks whether the
car's control modules meet the minimum software levels required for the big
**Marlin** software update.

- Instant per-module result (OK / outdated / missing) and an overall verdict:
  Marlin-ready, or a "zebra" (mixed versions — needs targeted updates via
  SW 2.2 first).
- Downloadable PDF report (WeasyPrint), generated in the active language.
- Optional consent for storage → anonymous fleet statistics (unique VINs,
  version distribution per module). Without consent nothing is stored.
- 7 languages (en, nb, sv, da, de, fr, es) with browser auto-detection.
- Deterministic parsing and comparison — no LLMs involved.

## Repository layout

```
app/
  main.py       FastAPI app: routes, upload flow, admin, usage logging
  parser.py     Parses the OLP "ECU Software Version Report" (PDF or text)
  rules.py      Rule engine: profile-based requirements model + validation
  db.py         SQLite: consented submissions, usage stats, audit log
  auth.py       HTTP Basic multi-user auth for /admin (PBKDF2 hashes)
  i18n.py       Language negotiation + JSON dictionaries in app/locales/
  templates/    Jinja2 templates (base/index/result/stats/privacy/admin/pdf)
  static/       style.css (served with a content-hash cache buster)
tests/          pytest suite incl. an anonymized real report as fixture
scripts/        hash_password.py — create admin password hashes
```

## Status / phases

| Phase | Content | Status |
|---|---|---|
| 1 | App scaffold | ✅ |
| 2 | Real OLP parser — verified against a real report 2026-08-28 | ✅ |
| 3 | Requirements in `requirements.yaml` — draft from the members' web meeting; official numbers and two extraction assumptions (BCM, ESP) must be confirmed | ⏳ |
| 4 | Deployment (currently self-hosted behind Cloudflare at marlin.flagan.net, ALPHA banner) | ✅ |

The parser reads the OLP "ECU Software Version Report" PDF (or its extracted
text): sections (BODY/INFOTAINMENT/POWERTRAIN/CHASSIS/ADAS), ECU blocks
(`CODE - Name`) and the **Supplier SW Version** field, which is what gets
compared against the requirements. The requirements model is profile-based
(2.0/2.1): a car that reaches the 2.1 level on all required modules is
"100% 2.1" and can be updated directly to Marlin; otherwise it is a "zebra"
and must go via SW 2.2 / targeted updates first. See the comments in
`requirements.example.yaml` for the assumptions.

## The requirements file

`requirements.example.yaml` defines the module requirements. In production it
lives as `/config/requirements.yaml` (bind-mounted directory). It is re-read
on every analysis, so requirements can be updated **without** a rebuild —
either directly on the host or via the admin page.

## Admin page (`/admin`)

Protected page for web editing of the requirements spec:

- **Login:** HTTP Basic with multiple users. Users live in
  `/config/admin_users.yaml` (see `admin_users.example.yaml`) as
  `username: pbkdf2-hash` — create hashes with
  `python3 scripts/hash_password.py`. Only hashes are stored. Failed attempts
  are rate limited (10 per 15 min per IP).
- **Editing:** a form editor (one row per module) plus a raw YAML editor as an
  advanced option. Everything is validated (syntax, structure, regexes,
  profiles) before saving; invalid content is rejected without touching the
  file. Saves are atomic and take effect on the next analysis.
- **Audit log:** every save is logged in SQLite (`audit_log`) with timestamp,
  username, client IP (from `CF-Connecting-IP`) and a unified diff. The log is
  shown at the bottom of the admin page.
- **Usage statistics:** anonymous per-upload counters (country from
  Cloudflare's `CF-IPCountry`, language, outcome, keyed daily IP hash for
  unique users — see "Privacy model"). Never VIN, report content or raw IP.

## Running locally

```bash
docker compose up --build
```

Open <http://localhost:8000>. Test with `tests/fixtures/olp_report.txt`
(an anonymized real report). For the admin page, copy
`requirements.example.yaml` to `dev-config/requirements.yaml` and create
`dev-config/admin_users.yaml` (see `admin_users.example.yaml`).

Without Docker (PDF download requires pango/cairo installed):

```bash
pip install -r requirements.txt pytest httpx
pytest
uvicorn app.main:app --reload
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MARLIN_DATA_DIR` | `/data` | SQLite database (`marlin.sqlite3`) |
| `MARLIN_UPLOADS_DIR` | `/data/uploads` | Stored report files (only with consent) |
| `MARLIN_REQUIREMENTS_PATH` | `/config/requirements.yaml` | The requirements file |
| `MARLIN_ADMIN_USERS_PATH` | `/config/admin_users.yaml` | Admin users (PBKDF2 hashes) |
| `MARLIN_CLIENT_IP_HEADER` | `cf-connecting-ip` | The one request header trusted for the client IP (rate limits, login lockout, audit log, usage hash). `X-Forwarded-For` is never used because Cloudflare appends to a client-supplied value. Set to empty to use the socket address when no proxy is in front. |

## Build and deploy

GitHub Actions runs the test suite and builds/publishes
`ghcr.io/terjefl/marlin-check` (`latest` + git SHA) on every push to `main`.
The container is self-contained: mount a data directory on `/data` and a
config directory on `/config` (with `requirements.yaml` and
`admin_users.yaml`), publish port 8000, and put it behind any HTTPS reverse
proxy. Country statistics use Cloudflare's `CF-IPCountry` header and degrade
gracefully without it.

Run exactly **one** uvicorn worker/replica: result tokens, upload rate limits
and the admin login lockout live in process memory. A restart invalidates all
open result/PDF links (visitors are sent back to the front page). The origin
must only be reachable through the proxy that sets the trusted client-IP
header (see `MARLIN_CLIENT_IP_HEADER`).

## Privacy model

- Without the consent checkbox nothing from the report is stored — the
  analysis happens in memory and the result lives 30 minutes behind an
  unguessable token (`/result/<token>`).
- With consent: report file + VIN + module versions are stored for aggregated
  fleet statistics (`/stats` never shows individual VINs).
- Anonymous usage counting per upload (admin-only view): country, language,
  outcome, and a keyed daily hash of the IP for unique-user counts. The HMAC
  key is random, lives only in process memory and is replaced at the UTC day
  rollover and on restart, so a stored hash cannot be brute-forced back to an
  IP. No VIN, no report data, no raw IP.
- Email delivery was deliberately left out (abuse surface).
