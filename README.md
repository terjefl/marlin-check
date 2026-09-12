# Marlin Readiness Check

Web portal for the Fisker Owners Association: members upload an ECU diagnostics
report exported from **OceanLink Pro (OLP)**, and the portal checks whether the
car's control modules meet the minimum software levels required for the
**Marlin** software update. Live at <https://marlin.flagan.net> (BETA since 2026-09-05).

- Per-module result (OK / outdated / missing / version not recognised / empty
  field), one column per software release (2.0 / 2.1 / 2.2) with the minimum
  and a tick/cross, and one of five outcomes: **full 2.2** (Marlin-ready),
  **clean 2.1** (Marlin possible, 2.2 recommended first), **2.2 zebra**
  (started but incomplete 2.2), **2.1 zebra** (not Marlin-ready) or
  **already on Marlin** (VCU 2.4 detected). Each comes with the list of
  modules holding the car back and a recommendation.
- Trim read from the VIN (5th character): a Sport has no rear motor controller,
  so it is not counted as missing there. Battery management is checked per
  software line (NMC vs LFP pack).
- Downloadable PDF report (WeasyPrint), generated in the active language.
- A public "How it works" page (`/how-it-works`) explaining the interpretation
  at processing level, in all languages.
- Storage is mandatory (association decision, Sep 2026): every analyzed
  report goes into the **vehicle register** — file, VIN, all ECU version
  fields, outcome. Public dashboard (`/stats`, aggregated, no VINs) and an
  admin register with per-VIN history, filters, CSV exports, re-evaluation
  against changed requirements and deletion per VIN.
- 7 languages (en, nb, sv, da, de, fr, es) with browser auto-detection.
- Deterministic parsing and comparison — no LLMs involved.

## Repository layout

```
app/
  main.py       FastAPI app: routes, upload flow, admin (form login, CSRF), register, CSV
  parser.py     Parses the OLP "ECU Software Version Report" (PDF via pdfplumber, or text)
  rules.py      Rule engine: profile-based requirements, variants, trims, Marlin marker,
                outcome classification, validation of the YAML
  db.py         SQLite: vehicle register (submissions + all module readings), fleet
                statistics, re-evaluation, usage stats, audit log, admin sessions
  auth.py       Credentials (PBKDF2), login lockout, trusted client-IP header
  i18n.py       Language negotiation + JSON dictionaries in app/locales/
  templates/    Jinja2: base/index/result/how/stats/privacy/admin/admin_fleet/
                admin_vehicle/admin_login/pdf
  static/       style.css and app.js (all page JS; no inline scripts, CSP-enforced),
                the two front-page videos (olp-howto-nb/-en.mp4 + posters; nb gets the
                Norwegian one, every other language the English one) and the dongle photo
tests/          pytest suite. Fixtures: a real OLP export (olp_report.pdf, unmodified)
                and its text extraction with an anonymized VIN, plus synthetic
                reference cars (100% 2.1, full 2.2, two Marlin cars) built from it
scripts/        hash_password.py — create admin password hashes
requirements.example.yaml   the requirements spec with field documentation
requirements.txt / .lock    loose spec / pinned+hashed set used by Docker and CI
pyproject.toml              ruff configuration
```

## Status

| Area | State |
|---|---|
| Parser | Verified against a real OLP PDF export (fixture) and the consented uploads of the BETA period |
| Requirements | The association's minimum table (2.0/2.1/2.2), still marked draft by the association. Verified against real cars sitting exactly at the 2.1 minimums, on full 2.2 and on Marlin. Open points are listed below and tracked in the `notes:` field of the requirements file |
| Trim logic | Verified for One (Z) and Extreme (E, one upload 2026-09-12, NMC battery line as expected); the Sport case (VIN letter S, no MCU_R, BMSL battery line) awaits a real Sport report |
| Deployment | Automatic: push to `main` → tests → image → Portainer webhook → new container (see below) |

## Open points (2026-09-12)

Waiting on the association (Jens):

1. **ECC at 2.2**: the table says 24, all 2.2/Marlin reference cars show 25.
   Changing it to 25 makes ECC evidence of 2.2 (today 2.1 and 2.2 share the
   minimum, so ECC never proves 2.2) and shifts some cars from "clean 2.1"
   to "2.2 zebra". The how-it-works page names ECC 24 as the example of a
   shared minimum and must be rewritten in 7 languages at the same time.
2. **BCM 41 vs 42**: one Marlin car shows BCM 41 where the table says 42
   for 2.2.
3. **Sport**: the LFP BMS level (15) rests on one reference report, and the
   trim letter S has never been seen in a real upload. S is the letter that
   exempts MCU_R, so this is the one trim rule that is still unverified.
4. **Workbook questions**: the B-vs-C interpretation of the comparison
   sheet, and the 2.0 column that shows 2.1 values.
5. **Privacy**: controller/contact and retention period for the register
   (see Privacy model below).

Operational, on Terje's signal only: reset all data at go-live (delete
`marlin.sqlite3` + `-wal`/`-shm` and `uploads/*` on the host with the
container stopped) and drop the BETA banner. Pre-v2 rows show country
"(unknown)" on `/stats` because country was not stored before v2; left as is.

## How the check works (short)

Outcomes (rule engine, `app/rules.py`): each module gets `meets[profile]`
and an `evidence_level` — the LOWEST profile that shares the minimum of the
highest profile the module satisfies, so a module whose 2.1 and 2.2 minimums
are equal (ECC 24, BMS 21) never counts as proof of 2.2. Per car:
`complete_profile` (highest profile every critical module meets) and
`top_evidence` (highest evidence over the modules). Then: VCU at the Marlin
level → `marlin`; complete = 2.2 → `full_22`; complete = 2.1 and evidence
2.1 → `full_21`; complete = 2.1 and evidence 2.2 → `zebra_22`; anything
below 2.1 → `zebra_21`. The old `verdict` (ready/zebra/marlin) is derived
from the same data and kept for compatibility.


The parser reads the OLP report: sections (BODY/INFOTAINMENT/POWERTRAIN/CHASSIS/ADAS),
ECU blocks (`CODE - Name`) and the **Supplier SW Version** field, which is
the only field compared against the requirements. A module-specific `extract`
regex turns that text into a number (`BCM395030` → 30, `ECC395 24` → 24,
`89324V0402…` → 402). Anything the regex does not recognise is "version not
recognised" and counts as failing; doubt never yields "ready".

The requirements are profile-based (2.0/2.1/2.2). A car that reaches the 2.1
level on all seven required modules is "100% 2.1" and can be updated directly
to Marlin; otherwise it is a zebra and must go via SW 2.2 first. A ready car is
also shown which modules sit below the 2.2 level (a direct 2.1→Marlin update
leaves those behind). A car whose VCU is at the Marlin level (`marlin_level:
24` on VCU) gets the informational "already on Marlin" verdict. The full,
member-facing explanation is the `/how-it-works` page; the field documentation
is in `requirements.example.yaml`.

## The requirements file

`requirements.example.yaml` defines the module requirements. In production it
lives as `/config/requirements.yaml` (bind-mounted directory). It is re-read
on every analysis, so requirements can be updated **without** a rebuild —
either directly on the host or via the admin page. Per module: `match` (ECU
codes), `extract` (regex with one capture group), `levels` per profile,
`critical`, optional `variants` (parallel software lines, e.g. BMS NMC/LFP),
`only_trims` (e.g. MCU_R only on Z/E/U) and `marlin_level`. The top-level
`notes:` field holds sources and open points and survives form-editor saves,
unlike YAML comments.

The profile names are effectively fixed: the result-page texts in all seven
languages are written for target profile `2.1` and highest profile `2.2`. The
rule engine does not care, but if the profiles ever change, the texts in
`app/locales/*.json` (`verdict_ready_text`, `verdict_zebra_text`,
`ready_22_note`, `verdict_marlin_text`, `marlin_below_top_note`) must be
rewritten; the admin page warns when the file deviates from these names.

The YAML is type-validated on load (lists, mappings, integer levels, regexes
with a capture group, a level for the target profile). If the file on disk
becomes invalid or unreadable, analyses keep using the last valid set loaded
since startup, `/healthz` returns 503 with the error (the container shows as
unhealthy), and the admin page shows the error above the YAML editor. If no
valid set has been loaded at all, uploads get a friendly 503 page.

## Admin page (`/admin`)

- **Login:** form login at `/admin/login`. Users live in
  `/config/admin_users.yaml` (see `admin_users.example.yaml`) as
  `username: pbkdf2-hash` — create hashes with
  `python3 scripts/hash_password.py`; the file is read on every login, so
  adding a user needs no restart. Failed attempts are locked out after 10 per
  15 min, per IP and per username. Sessions live in SQLite (only a hash of
  the cookie token is stored) behind an `HttpOnly; Secure; SameSite=Lax`
  cookie scoped to `/admin`, expire after 8 h idle or 24 h total, and "Log
  out" deletes them. Admin POSTs are CSRF-protected three ways: the SameSite
  cookie, a `Sec-Fetch-Site` check and a per-session token in every form.
- **Editing:** a form editor (one row per module) plus a raw YAML editor.
  Everything is validated before saving; invalid content is rejected without
  touching the file. Saves are atomic and take effect on the next analysis.
  The form editor preserves `variants`, `only_trims`, `marlin_level` and
  `notes`; edit those in the YAML editor.
- **Activity log:** saves (with unified diff), logins and logouts, with
  timestamp, username and client IP.
- **Vehicle register (`/admin/fleet`):** one row per VIN (latest upload)
  with outcome, complete/evidence profile and the extracted level of every
  required module; filter by outcome, trim and VIN. `/admin/fleet/<VIN>`
  shows the upload history and the full module table (all four version
  fields) of any upload, and can delete every record and file for that VIN
  (audited). Exports: `vehicles.csv` (one row per car) and `readings.csv`
  (one row per ECU per upload), semicolon-separated with a UTF-8 BOM.
- **Re-evaluate all:** re-runs the current requirements file on every
  stored report (rebuilt from its readings) and rewrites outcome and levels.
  Run it after changing levels, and once after the v2 upgrade so the rows
  from the consent period get an outcome.
- **Usage statistics:** anonymous per-upload counters (country from
  Cloudflare's `CF-IPCountry`, language, outcome, keyed daily IP hash for
  unique users — see "Privacy model"). Never VIN, report content or raw IP.

## Security notes

- Uploads: 15 MB limit enforced from `Content-Length` before the body is read,
  chunked read up to the limit, PDFs over 20 pages rejected before text
  extraction; parsing runs in the threadpool so a slow PDF never blocks other
  visitors. Rate limit of 10 uploads per minute per IP.
- The client IP comes from ONE trusted header (`MARLIN_CLIENT_IP_HEADER`,
  default `cf-connecting-ip`). `X-Forwarded-For` is never used: Cloudflare
  appends to a client-supplied value, so its first element is attacker
  controlled.
- Every response carries a Content-Security-Policy with no inline scripts
  (all JS is in `static/app.js`), `X-Content-Type-Options`, `X-Frame-Options`
  and `Referrer-Policy`. Result and PDF responses are `Cache-Control: no-store`.
- Result links are 128-bit random tokens that expire after 30 minutes (or on
  restart); an expired link shows an explanation instead of a silent redirect.

## Running locally

```bash
docker compose up --build
```

Open <http://localhost:8000>. Test with `tests/fixtures/olp_report.pdf` (a
real OLP export from a Fisker Ocean One) or `olp_report.txt` (its text
extraction with an anonymized VIN). For the admin page, copy
`requirements.example.yaml` to `dev-config/requirements.yaml` and create
`dev-config/admin_users.yaml` (see `admin_users.example.yaml`); `dev-config/`
is git-ignored.

Without Docker (PDF download requires pango/cairo installed):

```bash
pip install -r requirements.lock pytest httpx ruff
ruff check .
pytest
uvicorn app.main:app --reload
```

`requirements.txt` is the loose dependency spec; `requirements.lock` is the
fully pinned, hash-checked set that the Docker image and CI install. After
changing `requirements.txt`, regenerate the lock with
`uv pip compile requirements.txt --python-version 3.12 --python-platform linux --generate-hashes -o requirements.lock`.

## Environment variables

| Variable | Default in code | In the Docker image | Description |
|---|---|---|---|
| `MARLIN_DATA_DIR` | `./data` | `/data` | SQLite database (`marlin.sqlite3`) |
| `MARLIN_UPLOADS_DIR` | `./data/uploads` | `/data/uploads` | Stored report files |
| `MARLIN_REQUIREMENTS_PATH` | `./requirements.example.yaml` | `/config/requirements.yaml` | The requirements file |
| `MARLIN_ADMIN_USERS_PATH` | `/config/admin_users.yaml` | same | Admin users (PBKDF2 hashes) |
| `MARLIN_COOKIE_SECURE` | `1` | same | Mark the admin session cookie `Secure`. Set to `0` only for local development over plain http (compose.yml does). |
| `MARLIN_CLIENT_IP_HEADER` | `cf-connecting-ip` | same | The one request header trusted for the client IP (rate limits, login lockout, audit log, usage hash). Set to empty to use the socket address when no proxy is in front. |

## Build and deploy

GitHub Actions (`.github/workflows/build.yml`, actions pinned to commit SHAs)
runs ruff and the test suite, builds and publishes
`ghcr.io/terjefl/marlin-check` (`latest` + git SHA) on every push to `main`,
and then calls a Portainer stack webhook (repo secret `PORTAINER_WEBHOOK_URL`)
that re-pulls the image and recreates the container. A push is live about a
minute after a green build. The deploy step is a no-op when the secret is
absent, so forks build without deploying.

The container is self-contained: mount a data directory on `/data` and a
config directory on `/config` (with `requirements.yaml` and
`admin_users.yaml`), publish port 8000, and put it behind an HTTPS reverse
proxy that sets the trusted client-IP header. Country statistics use
Cloudflare's `CF-IPCountry` header and degrade gracefully without it.
Production runs as a Portainer git stack behind a Cloudflare Tunnel; the
compose file lives in the operator's infrastructure repo.

Run exactly **one** uvicorn worker/replica: result tokens, upload rate limits
and the admin login lockout live in process memory. A restart invalidates all
open result/PDF links. The origin must only be reachable through the proxy
that sets the trusted client-IP header.

## Privacy model

- Storage is mandatory: the member must tick the acceptance box, otherwise
  the upload is refused (422) and nothing is stored. Every analyzed report is
  stored: the file, the VIN, every ECU block with all four version fields,
  trim, outcome, upload country (`CF-IPCountry`) and time. The result page
  lives 30 minutes behind an unguessable token (`/result/<token>`).
- The database schema is migrated in place on startup (additive `ALTER
  TABLE`); rows from the consent period are kept and get an outcome after
  "Re-evaluate all" in the admin page.
- `/stats` is public and aggregated (outcomes, split cars per module, level
  per module, trims, countries, weeks) — never a VIN. VINs are visible only
  in the admin register. Deletion per VIN is an admin action.
- Anonymous usage counting per upload (admin-only view): country, language,
  outcome, and a keyed daily hash of the IP for unique-user counts. The HMAC
  key is random, lives only in process memory and is replaced at the UTC day
  rollover and on restart, so a stored hash cannot be brute-forced back to an
  IP. No VIN, no report data, no raw IP.
- Email delivery was deliberately left out (abuse surface).
- Open: the privacy page does not yet name a controller/contact or a
  retention period; these await the association's decision. The deletion
  routine exists (admin, per VIN).
