"""Marlin Readiness Check — webportal for Fisker Owners Association."""

from __future__ import annotations

import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db as db_module
from .i18n import LANGUAGE_NAMES, SUPPORTED, negotiate_language, translator
from .parser import MAX_REPORT_BYTES, ReportParseError, parse_report
from .rules import evaluate, load_requirements

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
        {"lang": lang, "t": translator(lang), "languages": LANGUAGE_NAMES, **context},
        status_code=status_code,
    )
    if request.query_params.get("lang") in SUPPORTED:
        response.set_cookie("lang", lang, max_age=365 * 24 * 3600, samesite="lax")
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return _render(request, "index.html", {"error": None})


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/analyze")
async def analyze(request: Request, report: UploadFile):
    lang = negotiate_language(request)
    t = translator(lang)

    if _rate_limited(_client_ip(request)):
        return _render(request, "index.html", {"error": t("error_rate_limited")}, status_code=429)

    data = await report.read()
    if len(data) > MAX_REPORT_BYTES:
        return _render(request, "index.html", {"error": t("error_too_large")}, status_code=413)

    form = await request.form()
    consent = form.get("consent") == "yes"

    try:
        parsed = parse_report(data, report.filename or "")
        requirements = load_requirements(REQUIREMENTS_PATH)
        evaluation = evaluate(parsed, requirements)
    except ReportParseError as exc:
        return _render(
            request, "index.html", {"error": t("error_parse", reason=str(exc))}, status_code=422
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

    return _render(
        request,
        "result.html",
        {"report": parsed, "evaluation": evaluation, "token": token},
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
