"""Marlin Readiness Check — webportal for Fisker Owners Association."""

from __future__ import annotations

import difflib
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db as db_module
from .auth import client_ip as auth_client_ip
from .auth import require_admin
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

RESULT_TTL_SECONDS = 30 * 60  # resultat/PDF-lenke lever en halvtime i minnet
RATE_LIMIT_UPLOADS = 10       # per IP per vindu
RATE_LIMIT_WINDOW = 60        # sekunder

app = FastAPI(title="Marlin Readiness Check", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Cache-busting: innholdshash av style.css i URL-en, så Cloudflare/nettlesere
# aldri serverer utdatert CSS etter en deploy.
import hashlib as _hashlib

STATIC_VERSION = _hashlib.md5(
    (BASE_DIR / "static" / "style.css").read_bytes()
).hexdigest()[:8]

database = db_module.Database(DATA_DIR / "marlin.sqlite3")

# Nylige analyser i minnet, så resultatsiden kan tilby PDF uten re-opplasting.
_recent_results: dict[str, dict] = {}
_upload_hits: dict[str, list[float]] = {}


def _prune_results() -> None:
    cutoff = time.time() - RESULT_TTL_SECONDS
    for token in [t for t, r in _recent_results.items() if r["at"] < cutoff]:
        _recent_results.pop(token, None)


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _upload_hits.get(ip, []) if t > now - RATE_LIMIT_WINDOW]
    _upload_hits[ip] = hits
    if len(hits) >= RATE_LIMIT_UPLOADS:
        return True
    hits.append(now)
    return False


def _client_ip(request: Request) -> str:
    # Bak NPM/Cloudflare: bruk første X-Forwarded-For-adresse hvis satt
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


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


@app.post("/analyze")
async def analyze(request: Request, report: UploadFile):
    lang = negotiate_language(request)
    t = translator(lang)

    if _rate_limited(_client_ip(request)):
        return _render(request, "index.html", {"error": t("error_rate_limited"), "requirements": _current_requirements()}, status_code=429)

    data = await report.read()
    if len(data) > MAX_REPORT_BYTES:
        return _render(request, "index.html", {"error": t("error_too_large"), "requirements": _current_requirements()}, status_code=413)

    form = await request.form()
    consent = form.get("consent") == "yes"

    try:
        parsed = parse_report(data, report.filename or "")
        requirements = load_requirements(REQUIREMENTS_PATH)
        evaluation = evaluate(parsed, requirements)
    except ReportParseError as exc:
        return _render(
            request, "index.html", {"error": t("error_parse", reason=str(exc)), "requirements": _current_requirements()}, status_code=422
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

    _prune_results()
    token = secrets.token_urlsafe(16)
    _recent_results[token] = {"report": parsed, "evaluation": evaluation, "at": time.time()}

    # POST-redirect-GET: resultatsiden er en GET-side, så språkbytte og
    # sideoppdatering fungerer uten re-innsending av rapporten.
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
    from weasyprint import HTML  # tung import — utsettes til første PDF

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


# --- Admin: web-redigering av kravspec med revisjonslogg -------------------

def _render_admin(request: Request, username: str, *, message: str = "",
                  error: str = "", yaml_text: str | None = None,
                  status_code: int = 200) -> Response:
    current_text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    try:
        requirements = parse_requirements_text(current_text)
    except RequirementsValidationError:
        requirements = None  # vis kun YAML-editoren hvis fila er ugyldig
    return _render(
        request,
        "admin.html",
        {
            "username": username,
            "message": message,
            "error": error,
            "requirements": requirements,
            "yaml_text": yaml_text if yaml_text is not None else current_text,
            "audit": database.audit_entries(50),
        },
        status_code=status_code,
    )


def _save_requirements(request: Request, username: str, new_text: str) -> Response:
    """Felles lagringslogikk for skjema- og YAML-redigering."""
    old_text = REQUIREMENTS_PATH.read_text(encoding="utf-8")
    if new_text.strip() == old_text.strip():
        return _render_admin(request, username, message="Ingen endringer å lagre.")

    try:
        parsed = parse_requirements_text(new_text)
    except RequirementsValidationError as exc:
        return _render_admin(
            request, username, error=f"Ikke lagret — valideringsfeil: {exc}",
            yaml_text=new_text, status_code=422,
        )

    diff = "\n".join(
        difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(),
            fromfile="requirements.yaml (før)", tofile="requirements.yaml (etter)",
            lineterm="",
        )
    )[:20000]

    # Atomisk erstatning i samme katalog (derfor er /config mountet som katalog)
    tmp_path = REQUIREMENTS_PATH.with_suffix(".yaml.tmp")
    tmp_path.write_text(new_text, encoding="utf-8")
    os.replace(tmp_path, REQUIREMENTS_PATH)

    database.add_audit(username, auth_client_ip(request), "requirements_update", diff)
    return _render_admin(
        request, username,
        message=f"Lagret. Ny kravversjon: {parsed.version} "
                f"({len(parsed.modules)} moduler, target {parsed.target_profile}).",
    )


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, username: str = Depends(require_admin)):
    return _render_admin(request, username)


@app.post("/admin/save")
async def admin_save(request: Request, username: str = Depends(require_admin)):
    form = await request.form()
    new_text = str(form.get("yaml_text", "")).replace("\r\n", "\n")
    return _save_requirements(request, username, new_text)


@app.post("/admin/save-form")
async def admin_save_form(request: Request, username: str = Depends(require_admin)):
    form = await request.form()
    try:
        new_text = _form_to_yaml(form, username)
    except ValueError as exc:
        return _render_admin(
            request, username, error=f"Ikke lagret — {exc}", status_code=422
        )
    return _save_requirements(request, username, new_text)


def _form_to_yaml(form, username: str) -> str:
    """Bygger kravfil-YAML fra admin-skjemaet. Kaster ValueError ved åpenbare feil."""
    import yaml as yaml_module

    profiles = [p.strip() for p in str(form.get("profiles", "")).split(",") if p.strip()]
    if not profiles:
        raise ValueError("minst én profil må angis.")

    modules = []
    indices = sorted(
        {m.group(1) for k in form.keys() if (m := re.match(r"mod-(\d+)-id$", k))},
        key=int,
    )
    for i in indices:
        module_id = str(form.get(f"mod-{i}-id", "")).strip()
        if not module_id:
            continue  # tom rad
        levels = {}
        for profile in profiles:
            value = str(form.get(f"mod-{i}-level-{profile}", "")).strip()
            if value:
                try:
                    levels[profile] = int(value)
                except ValueError:
                    raise ValueError(
                        f"modul {module_id}: nivå for {profile} må være et heltall (fikk {value!r})."
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

    data = {
        "version": str(form.get("version", "")).strip(),
        "profiles": profiles,
        "target_profile": str(form.get("target_profile", "")).strip(),
        "modules": modules,
    }
    header = (
        "# Marlin-krav: minimumsnivåer per ECU og programvareprofil.\n"
        f"# Generert av admin-skjemaet på marlin-portalen (bruker: {username}).\n"
        "# Feltdokumentasjon: requirements.example.yaml i kilderepoet\n"
        "# https://github.com/terjefl/marlin-check\n\n"
    )
    return header + yaml_module.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100
    )
