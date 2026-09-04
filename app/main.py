"""Marlin Readiness Check — web portal for the Fisker Owners Association."""

from __future__ import annotations

import difflib
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth
from . import db as db_module
from .auth import LoginRequired, client_ip
from .i18n import LANGUAGE_NAMES, SUPPORTED, negotiate_language, translator
from .parser import MAX_REPORT_BYTES, ReportParseError, parse_report
from .rules import (
    RequirementsValidationError,
    evaluate,
    load_requirements,
    parse_requirements_text,
)

BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("MARLIN_DATA_DIR", "./data"))
UPLOADS_DIR = Path(os.environ.get("MARLIN_UPLOADS_DIR", "./data/uploads"))
REQUIREMENTS_PATH = Path(
    os.environ.get("MARLIN_REQUIREMENTS_PATH", "./requirements.example.yaml")
)

RESULT_TTL_SECONDS = 30 * 60  # result/PDF link lives in memory for half an hour
# The admin session cookie is marked Secure unless explicitly disabled (local
# dev over plain http). Behind the Cloudflare tunnel the origin only sees http,
# so this cannot be derived from the request.
COOKIE_SECURE = os.environ.get("MARLIN_COOKIE_SECURE", "1").strip().lower() not in ("0", "false", "no", "")
RATE_LIMIT_UPLOADS = 10       # per IP per window
RATE_LIMIT_WINDOW = 60        # seconds

app = FastAPI(title="Marlin Readiness Check", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Cache busting: content hash of style.css in the URL, so Cloudflare/browsers
# never serve stale CSS after a deploy.
STATIC_VERSION = hashlib.md5(
    (BASE_DIR / "static" / "style.css").read_bytes()
).hexdigest()[:8]

database = db_module.Database(DATA_DIR / "marlin.sqlite3")

# Recent analyses kept in memory, so the result page can offer the PDF without re-upload.
# NOTE: all of this state (results, rate limits, login lockout) is per process —
# the app must run as exactly one uvicorn worker/replica.
_recent_results: dict[str, dict] = {}
_upload_hits: dict[str, list[float]] = {}


def _prune_results() -> None:
    cutoff = time.time() - RESULT_TTL_SECONDS
    for token in [t for t, r in _recent_results.items() if r["at"] < cutoff]:
        _recent_results.pop(token, None)


def _rate_limited(ip: str) -> bool:
    now = time.time()
    # Drop expired hits for every IP so the dict cannot grow without bound
    for known_ip in list(_upload_hits):
        recent = [t for t in _upload_hits[known_ip] if t > now - RATE_LIMIT_WINDOW]
        if recent:
            _upload_hits[known_ip] = recent
        else:
            del _upload_hits[known_ip]
    hits = _upload_hits.setdefault(ip, [])
    if len(hits) >= RATE_LIMIT_UPLOADS:
        return True
    hits.append(now)
    return False


def _render(request: Request, template: str, context: dict, status_code: int = 200) -> Response:
    lang = negotiate_language(request)
    response = templates.TemplateResponse(
        request,
        template,
        {"lang": lang, "t": translator(lang), "languages": LANGUAGE_NAMES,
         "static_v": STATIC_VERSION, **context},
        status_code=status_code,
    )
    if request.query_params.get("lang") in SUPPORTED:
        response.set_cookie("lang", lang, max_age=365 * 24 * 3600, samesite="lax")
    return response


# Key for the daily usage hash: random, held only in memory, replaced at the
# UTC day rollover (and on every restart). Because the key is never stored, a
# hash in the database cannot be brute-forced back to an IP address — a plain
# sha256(day|ip) with a public salt could be reversed over the IPv4 space in
# under an hour. The cost is that a restart splits that day's unique-user count.
_usage_key: dict = {"day": "", "key": b""}


def _usage_ip_hash(request: Request) -> str:
    """Daily-rotating keyed hash of the client IP — counts unique users per day
    without storing anything that can be linked back to the IP."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _usage_key["day"] != day:
        _usage_key["day"], _usage_key["key"] = day, secrets.token_bytes(32)
    return hmac.new(_usage_key["key"], client_ip(request).encode(), hashlib.sha256).hexdigest()[:16]


def _log_usage(request: Request, lang: str, outcome: str, consent: bool) -> None:
    browser_lang = request.headers.get("accept-language", "").split(",")[0].split(";")[0].strip()
    try:
        database.add_usage(
            country=request.headers.get("cf-ipcountry", "").upper(),
            ui_lang=lang,
            browser_lang=browser_lang[:16],
            outcome=outcome,
            consent=consent,
            ip_hash=_usage_ip_hash(request),
        )
    except Exception:  # usage stats must never break the analysis itself
        pass


def _current_requirements():
    try:
        return load_requirements(REQUIREMENTS_PATH)
    except (RequirementsValidationError, OSError):
        return None


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return _render(request, "index.html",
                   {"error": None, "requirements": _current_requirements()})


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/analyze")
def analyze_get(request: Request):
    """The language picker (and bookmarks) can hit /analyze with GET — e.g. from
    the error page, which is rendered directly on the POST URL. Redirect to the
    front page with the language choice preserved instead of returning 405."""
    lang = request.query_params.get("lang")
    return RedirectResponse(f"/?lang={lang}" if lang in SUPPORTED else "/", status_code=303)


def _parse_and_evaluate(data: bytes, filename: str):
    """CPU-bound part of an upload (pdfplumber + rule engine). Runs in the
    threadpool so a slow PDF never blocks the event loop for other visitors."""
    parsed = parse_report(data, filename)
    requirements = load_requirements(REQUIREMENTS_PATH)
    return parsed, evaluate(parsed, requirements)


@app.post("/analyze")
async def analyze(request: Request, report: UploadFile):
    lang = negotiate_language(request)
    t = translator(lang)

    if _rate_limited(client_ip(request)):
        return _render(request, "index.html", {"error": t("error_rate_limited"), "requirements": _current_requirements()}, status_code=429)

    data = await report.read()
    if len(data) > MAX_REPORT_BYTES:
        return _render(request, "index.html", {"error": t("error_too_large"), "requirements": _current_requirements()}, status_code=413)

    form = await request.form()
    consent = form.get("consent") == "yes"

    try:
        parsed, evaluation = await run_in_threadpool(
            _parse_and_evaluate, data, report.filename or ""
        )
    except ReportParseError as exc:
        _log_usage(request, lang, "parse_error", consent=False)
        reason = t(f"parse_{exc.key}")
        if exc.detail:
            reason += f" ({exc.detail})"
        return _render(
            request, "index.html", {"error": t("error_parse", reason=reason), "requirements": _current_requirements()}, status_code=422
        )

    if consent:
        UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
        safe_ext = ".pdf" if data[:5] == b"%PDF-" else ".txt"
        stored_filename = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            f"_{re.sub(r'[^A-Z0-9]', '', parsed.vin.upper())}{safe_ext}"
        )
        (UPLOADS_DIR / stored_filename).write_bytes(data)
        database.store_submission(parsed, evaluation, lang, stored_filename)

    _log_usage(request, lang, evaluation.verdict, consent=consent)

    _prune_results()
    token = secrets.token_urlsafe(16)
    _recent_results[token] = {"report": parsed, "evaluation": evaluation, "at": time.time()}

    # POST-redirect-GET: the result page is a GET page, so switching language
    # and reloading work without re-submitting the report.
    return RedirectResponse(f"/result/{token}", status_code=303)


@app.get("/result/{token}", response_class=HTMLResponse)
def result(request: Request, token: str):
    _prune_results()
    cached = _recent_results.get(token)
    if cached is None:
        return RedirectResponse("/", status_code=303)
    return _render(
        request,
        "result.html",
        {"report": cached["report"], "evaluation": cached["evaluation"], "token": token},
    )


@app.get("/pdf/{token}")
def download_pdf(request: Request, token: str):
    _prune_results()
    cached = _recent_results.get(token)
    if cached is None:
        return RedirectResponse("/", status_code=303)

    lang = negotiate_language(request)
    html = templates.get_template("pdf.html").render(
        lang=lang,
        t=translator(lang),
        report=cached["report"],
        evaluation=cached["evaluation"],
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    from weasyprint import HTML  # heavy import — deferred until the first PDF

    pdf_bytes = HTML(string=html).write_pdf()
    filename = f"marlin-check_{cached['report'].vin}.pdf"
    return Response(
        pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/stats", response_class=HTMLResponse)
def stats(request: Request):
    return _render(request, "stats.html", {"stats": database.stats()})


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    return _render(request, "privacy.html", {})


# --- Admin: form login (SQLite sessions), requirements editing, audit log ---

def _safe_next(value: str | None) -> str:
    """Only redirect back to admin pages on the same site after login."""
    if value and value.startswith("/admin") and not value.startswith("//"):
        return value
    return "/admin"


def require_admin(request: Request) -> str:
    """FastAPI dependency: username of the logged-in admin, or LoginRequired
    (turned into a redirect to the login form by the handler below)."""
    session = database.get_session(
        request.cookies.get(auth.SESSION_COOKIE, ""),
        idle_seconds=auth.SESSION_IDLE_SECONDS,
        max_age_seconds=auth.SESSION_MAX_SECONDS,
    )
    if session is None:
        raise LoginRequired()
    request.state.csrf = session["csrf_token"]
    return session["username"]


async def require_csrf(request: Request, username: str = Depends(require_admin)) -> str:
    """For state-changing admin POSTs: the browser must say the request is
    same-site (Sec-Fetch-Site, unforgeable by other sites) AND the form must
    carry the session's CSRF token. Cookies are SameSite=Lax as a third layer."""
    fetch_site = request.headers.get("sec-fetch-site", "")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="Cross-site request rejected.")
    form = await request.form()
    submitted = str(form.get("csrf", ""))
    if not submitted or not secrets.compare_digest(submitted, request.state.csrf):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token — reload the page and try again.")
    return username


@app.exception_handler(LoginRequired)
def _login_redirect(request: Request, exc: LoginRequired):
    return RedirectResponse(
        f"/admin/login?next={quote(request.url.path, safe='/')}", status_code=303
    )


def _render_login(request: Request, *, error: str = "", next_path: str = "/admin",
                  status_code: int = 200) -> Response:
    return _render(request, "admin_login.html",
                   {"error": error, "next": next_path}, status_code=status_code)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(request: Request):
    if database.get_session(
        request.cookies.get(auth.SESSION_COOKIE, ""),
        idle_seconds=auth.SESSION_IDLE_SECONDS, max_age_seconds=auth.SESSION_MAX_SECONDS,
    ):
        return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=303)
    return _render_login(request, next_path=_safe_next(request.query_params.get("next")))


@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    next_path = _safe_next(str(form.get("next", "")))
    ip = client_ip(request)

    if auth.is_locked_out(ip, username):
        return _render_login(
            request, error="Too many failed login attempts. Try again in 15 minutes.",
            next_path=next_path, status_code=429,
        )
    # PBKDF2 is CPU-bound: keep it off the event loop
    if not username or not await run_in_threadpool(auth.authenticate, username, password, ip):
        return _render_login(
            request, error="Invalid username or password.", next_path=next_path, status_code=401
        )

    token, _csrf = database.create_session(username)
    database.add_audit(username, ip, "login", "")
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=auth.SESSION_MAX_SECONDS, path="/admin",
        httponly=True, secure=COOKIE_SECURE, samesite="lax",
    )
    return response


@app.post("/admin/logout")
async def admin_logout(request: Request, username: str = Depends(require_csrf)):
    database.delete_session(request.cookies.get(auth.SESSION_COOKIE, ""))
    database.add_audit(username, client_ip(request), "logout", "")
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(auth.SESSION_COOKIE, path="/admin")
    return response


def _render_admin(request: Request, username: str, *, message: str = "",
                  error: str = "", yaml_text: str | None = None,
                  status_code: int = 200) -> Response:
    current_text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    try:
        requirements = parse_requirements_text(current_text)
    except RequirementsValidationError:
        requirements = None  # show only the YAML editor if the file is invalid
    return _render(
        request,
        "admin.html",
        {
            "username": username,
            "csrf": request.state.csrf,
            "message": message,
            "error": error,
            "requirements": requirements,
            "yaml_text": yaml_text if yaml_text is not None else current_text,
            "audit": database.audit_entries(50),
            "usage": database.usage_stats(14),
        },
        status_code=status_code,
    )


def _save_requirements(request: Request, username: str, new_text: str) -> Response:
    """Shared save logic for the form editor and the raw YAML editor."""
    old_text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    if new_text.strip() == old_text.strip():
        return _render_admin(request, username, message="No changes to save.")

    try:
        parsed = parse_requirements_text(new_text)
    except RequirementsValidationError as exc:
        return _render_admin(
            request, username, error=f"Not saved — validation error: {exc}",
            yaml_text=new_text, status_code=422,
        )

    diff = "\n".join(
        difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile="requirements.yaml (before)", tofile="requirements.yaml (after)",
            lineterm="",
        )
    )[:20000]

    # Atomic replace within the same directory (which is why /config is mounted as a directory)
    tmp_path = REQUIREMENTS_PATH.with_suffix(".yaml.tmp")
    tmp_path.write_text(new_text, encoding="utf-8")
    os.replace(tmp_path, REQUIREMENTS_PATH)

    database.add_audit(username, client_ip(request), "requirements_update", diff)
    return _render_admin(
        request, username,
        message=f"Saved. New requirements version: {parsed.version} "
                f"({len(parsed.modules)} modules, target {parsed.target_profile}).",
    )


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, username: str = Depends(require_admin)):
    return _render_admin(request, username)


@app.post("/admin/save")
async def admin_save(request: Request, username: str = Depends(require_csrf)):
    form = await request.form()
    new_text = str(form.get("yaml_text", "")).replace("\r\n", "\n")
    return _save_requirements(request, username, new_text)


@app.post("/admin/save-form")
async def admin_save_form(request: Request, username: str = Depends(require_csrf)):
    form = await request.form()
    try:
        new_text = _form_to_yaml(form, username)
    except ValueError as exc:
        return _render_admin(
            request, username, error=f"Not saved — {exc}", status_code=422
        )
    return _save_requirements(request, username, new_text)


def _form_to_yaml(form, username: str) -> str:
    """Builds requirements YAML from the admin form. Raises ValueError on obvious errors."""
    import yaml as yaml_module

    profiles = [p.strip() for p in str(form.get("profiles", "")).split(",") if p.strip()]
    if not profiles:
        raise ValueError("at least one profile must be specified.")

    modules = []
    indices = sorted(
        {m.group(1) for k in form.keys() if (m := re.match(r"mod-(\d+)-id$", k))},
        key=int,
    )
    for i in indices:
        module_id = str(form.get(f"mod-{i}-id", "")).strip()
        if not module_id:
            continue  # empty row
        levels = {}
        for profile in profiles:
            value = str(form.get(f"mod-{i}-level-{profile}", "")).strip()
            if value:
                try:
                    levels[profile] = int(value)
                except ValueError:
                    raise ValueError(
                        f"module {module_id}: level for {profile} must be an integer (got {value!r})."
                    )
        module: dict = {
            "id": module_id,
            "label": str(form.get(f"mod-{i}-label", "")).strip() or module_id,
            "match": [s.strip() for s in str(form.get(f"mod-{i}-match", "")).split(",") if s.strip()]
            or [module_id],
            "levels": levels,
            "critical": "yes" in form.getlist(f"mod-{i}-critical"),
        }
        extract = str(form.get(f"mod-{i}-extract", "")).strip()
        if extract:
            module["extract"] = extract
        modules.append(module)

    # The form cannot edit `variants`/`only_trims`; carry them over from the
    # current file so a form save does not silently drop them.
    try:
        current_raw = yaml_module.safe_load(REQUIREMENTS_PATH.read_text(encoding="utf-8")) or {}
        preserved = {
            m["id"]: {k: m[k] for k in ("variants", "only_trims") if k in m}
            for m in current_raw.get("modules", [])
            if isinstance(m, dict) and "id" in m
        }
    except Exception:
        preserved = {}
    for module in modules:
        extras = preserved.get(module["id"], {})
        module.update(extras)
        # A variant-only module keeps empty base levels out of the file
        if not module["levels"] and extras.get("variants"):
            module.pop("levels")

    data = {
        "version": str(form.get("version", "")).strip(),
        "profiles": profiles,
        "target_profile": str(form.get("target_profile", "")).strip(),
        "modules": modules,
    }
    header = (
        "# Marlin requirements: minimum levels per ECU and software profile.\n"
        "# NOTE: `variants` and `only_trims` are preserved from the previous file\n"
        "# (the form editor cannot change them — use the YAML editor for that).\n"
        f"# Generated by the admin form on the Marlin portal (user: {username}).\n"
        "# Field documentation: requirements.example.yaml in the source repo\n"
        "# https://github.com/terjefl/marlin-check\n\n"
    )
    return header + yaml_module.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
    )
