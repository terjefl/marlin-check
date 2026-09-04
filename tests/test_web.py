"""End-to-end tests of the web flow, including the consent logic."""

import importlib
from datetime import UTC
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

FIXTURE = Path(__file__).parent / "fixtures" / "olp_report.txt"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MARLIN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MARLIN_UPLOADS_DIR", str(tmp_path / "data" / "uploads"))
    from app import main

    importlib.reload(main)
    return TestClient(main.app), main


def _upload(client, consent: bool):
    data = {"consent": "yes"} if consent else {}
    return client.post(
        "/analyze",
        files={"report": ("report.txt", FIXTURE.read_bytes(), "text/plain")},
        data=data,
    )


def test_upload_without_consent_stores_nothing(client):
    c, main = client
    response = _upload(c, consent=False)
    assert response.status_code == 200
    assert "VCF1ZBE20PG099999" in response.text
    assert main.database.stats()["unique_vins"] == 0
    assert not Path(main.UPLOADS_DIR).exists()


def test_upload_with_consent_stores_submission_and_file(client):
    c, main = client
    response = _upload(c, consent=True)
    assert response.status_code == 200
    stats = main.database.stats()
    assert stats["unique_vins"] == 1
    assert stats["total_submissions"] == 1
    stored = list(Path(main.UPLOADS_DIR).iterdir())
    assert len(stored) == 1
    assert "VCF1ZBE20PG099999" in stored[0].name

    # Same VIN again -> still 1 unique car, 2 submissions
    _upload(c, consent=True)
    stats = main.database.stats()
    assert stats["unique_vins"] == 1
    assert stats["total_submissions"] == 2


def test_invalid_file_shows_error(client):
    c, _ = client
    response = c.post(
        "/analyze",
        files={"report": ("junk.txt", b"nothing useful here", "text/plain")},
    )
    assert response.status_code == 422


def test_parse_error_is_fully_translated(client):
    """Regression: the error reason must follow the page language, not be hardcoded Norwegian."""
    c, _ = client
    english = c.post(
        "/analyze?lang=en",
        files={"report": ("junk.txt", b"nothing useful here", "text/plain")},
    )
    assert "Could not find the heading" in english.text
    assert "Fant ikke overskriften" not in english.text

    german = c.post(
        "/analyze?lang=de",
        files={"report": ("junk.txt", b"nothing useful here", "text/plain")},
    )
    assert "wurde nicht gefunden" in german.text


def test_language_switch_on_error_page_redirects_home(client):
    """The language picker on the error page does GET /analyze?lang=... — must not 405."""
    c, _ = client
    response = c.get("/analyze?lang=de", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/?lang=de"
    followed = c.get("/analyze?lang=de")
    assert followed.status_code == 200
    assert "Mein Auto prüfen" in followed.text


def test_stats_and_privacy_pages_render(client):
    c, _ = client
    assert c.get("/stats").status_code == 200
    assert c.get("/privacy").status_code == 200
    assert c.get("/healthz").json() == {"status": "ok"}


def test_result_page_survives_language_switch_and_reload(client):
    """POST /analyze redirects to GET /result/<token>; language switch and refresh work."""
    c, _ = client
    response = _upload(c, consent=False)
    assert response.status_code == 200
    assert "/result/" in str(response.url)

    # Refresh (GET of the same URL) works
    again = c.get(str(response.url))
    assert again.status_code == 200 and "VCF1ZBE20PG099999" in again.text

    # Language switch via ?lang= renders the same result in the new language
    german = c.get(str(response.url) + "?lang=de")
    assert german.status_code == 200
    assert "Ergebnis für VIN" in german.text

    # Expired/unknown token -> front page with an explanation, not a silent redirect
    gone = c.get("/result/finnesikke?lang=en")
    assert gone.status_code == 410
    assert "This result link has expired" in gone.text
    assert c.get("/pdf/finnesikke").status_code == 410
    # Result pages carry the VIN and must not be cached anywhere
    assert again.headers["cache-control"] == "private, no-store"


def test_usage_logged_without_consent_and_without_ip(client):
    """Usage is counted even without consent — but without VIN or raw IP."""
    c, main = client
    headers = {"CF-IPCountry": "NO", "Accept-Language": "nb-NO,nb;q=0.9"}
    response = c.post(
        "/analyze",
        files={"report": ("r.txt", FIXTURE.read_bytes(), "text/plain")},
        headers=headers,
    )
    assert response.status_code == 200
    # Parse errors are counted too
    c.post("/analyze", files={"report": ("junk.txt", b"garbage", "text/plain")}, headers=headers)

    usage = main.database.usage_stats()
    assert usage["total"] == 2
    assert usage["consented"] == 0
    assert usage["outcomes"] == {"zebra": 1, "parse_error": 1}
    assert usage["countries"][0]["country"] == "NO"
    assert usage["languages"][0]["ui_lang"] == "nb"
    assert usage["per_day"][0]["unique_users"] == 1  # same client both times

    # Raw IP or VIN must never appear in the usage table
    import sqlite3

    conn = sqlite3.connect(main.database.path)
    rows = conn.execute("SELECT * FROM usage_events").fetchall()
    blob = str(rows)
    assert "VCF1ZBE20PG099999" not in blob
    assert "testclient" not in blob and "127.0.0.1" not in blob

    # No submissions stored (consent not given)
    assert main.database.stats()["unique_vins"] == 0


def test_language_negotiation(client):
    c, _ = client
    norsk = c.get("/", headers={"accept-language": "nb-NO,nb;q=0.9"})
    assert "Sjekk bilen min" in norsk.text
    deutsch = c.get("/?lang=de")
    assert "Mein Auto prüfen" in deutsch.text
    assert deutsch.cookies.get("lang") == "de"


def test_upload_rate_limit_ignores_client_supplied_forwarded_for(client):
    """Only the configured proxy header (CF-Connecting-IP) identifies the
    client. Cloudflare appends to a client-supplied X-Forwarded-For, so its
    first element must never be used for rate limiting."""
    c, main = client
    fixture = FIXTURE.read_bytes()

    codes = []
    for i in range(main.RATE_LIMIT_UPLOADS + 2):
        response = c.post(
            "/analyze",
            files={"report": ("r.txt", fixture, "text/plain")},
            headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.5"},
            follow_redirects=False,
        )
        codes.append(response.status_code)
    assert codes[: main.RATE_LIMIT_UPLOADS] == [303] * main.RATE_LIMIT_UPLOADS
    assert codes[main.RATE_LIMIT_UPLOADS:] == [429, 429]

    # A different CF-Connecting-IP is a different client and is not blocked
    response = c.post(
        "/analyze",
        files={"report": ("r.txt", fixture, "text/plain")},
        headers={"CF-Connecting-IP": "198.51.100.42"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # ...and expired/blocked entries do not accumulate one key per spoofed value
    assert set(main._upload_hits) <= {"testclient", "198.51.100.42"}


def test_usage_ip_hash_is_keyed_and_not_reversible(client):
    """The daily unique-user hash must not be a plain sha256 over a public salt
    (that is brute-forceable over the IPv4 space)."""
    import hashlib
    from datetime import datetime

    c, main = client
    c.post("/analyze", files={"report": ("r.txt", FIXTURE.read_bytes(), "text/plain")},
           headers={"CF-Connecting-IP": "203.0.113.77"})
    import sqlite3

    stored = sqlite3.connect(main.database.path).execute(
        "SELECT ip_hash FROM usage_events"
    ).fetchone()[0]
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    public_scheme = hashlib.sha256(f"marlin-{day}|203.0.113.77".encode()).hexdigest()[:16]
    assert stored != public_scheme
    assert len(stored) == 16
    # Same client on the same day -> same hash (unique-user counting still works)
    c.post("/analyze", files={"report": ("r.txt", FIXTURE.read_bytes(), "text/plain")},
           headers={"CF-Connecting-IP": "203.0.113.77"})
    assert main.database.usage_stats()["per_day"][0]["unique_users"] == 1
    # A new key (restart / day rollover) yields a different hash for the same IP
    main._usage_key["day"] = ""
    c.post("/analyze", files={"report": ("r.txt", FIXTURE.read_bytes(), "text/plain")},
           headers={"CF-Connecting-IP": "203.0.113.77"})
    assert main.database.usage_stats()["per_day"][0]["unique_users"] == 2


def test_slow_pdf_does_not_block_other_requests(client, monkeypatch):
    """Parsing runs in the threadpool: while one upload is being parsed, the
    event loop must still serve /healthz. The TestClient is used as a context
    manager so both requests share ONE event loop (otherwise each request gets
    its own portal and blocking would go unnoticed)."""
    import threading
    import time

    _, main = client
    started = threading.Event()
    release = threading.Event()
    original_parse = main.parse_report

    def slow_parse(data, filename):
        started.set()
        release.wait(timeout=5)
        return original_parse(data, filename)

    monkeypatch.setattr(main, "parse_report", slow_parse)

    result = {}
    with TestClient(main.app) as shared:

        def upload():
            result["status"] = shared.post(
                "/analyze", files={"report": ("r.txt", FIXTURE.read_bytes(), "text/plain")},
                follow_redirects=False,
            ).status_code

        worker = threading.Thread(target=upload)
        worker.start()
        assert started.wait(timeout=5)
        t0 = time.perf_counter()
        health = shared.get("/healthz")
        elapsed = time.perf_counter() - t0
        release.set()
        worker.join(timeout=10)
    assert health.status_code == 200
    assert elapsed < 2, f"/healthz was blocked for {elapsed:.1f}s while a PDF was being parsed"
    assert result["status"] == 303


@pytest.fixture()
def client_with_config(tmp_path, monkeypatch):
    """Like `client`, but with a writable copy of the requirements file."""
    import shutil

    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(Path(__file__).parent.parent / "requirements.example.yaml", config / "requirements.yaml")
    monkeypatch.setenv("MARLIN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MARLIN_UPLOADS_DIR", str(tmp_path / "data" / "uploads"))
    monkeypatch.setenv("MARLIN_REQUIREMENTS_PATH", str(config / "requirements.yaml"))
    from app import main

    importlib.reload(main)
    return TestClient(main.app), main, config / "requirements.yaml"


def test_corrupt_requirements_keeps_last_good_and_degrades_healthz(client_with_config):
    c, _main, path = client_with_config
    good = path.read_text()
    assert c.get("/healthz").status_code == 200
    assert "2026-09-workbook-v2-draft" in _upload(c, consent=False).text

    path.write_text("modules: [\n")  # a bad edit on the host
    health = c.get("/healthz")
    assert health.status_code == 503
    assert health.json()["status"] == "degraded"
    # Analyses continue on the last valid set instead of failing with 500
    response = _upload(c, consent=False)
    assert response.status_code == 200
    assert "2026-09-workbook-v2-draft" in response.text
    assert c.get("/").status_code == 200

    path.unlink()  # mount gone entirely
    assert c.get("/healthz").status_code == 503
    assert _upload(c, consent=False).status_code == 200

    path.write_text(good)
    assert c.get("/healthz").status_code == 200


def test_corrupt_requirements_at_startup_gives_503_not_500(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    (config / "requirements.yaml").write_text("not: [valid")
    monkeypatch.setenv("MARLIN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MARLIN_UPLOADS_DIR", str(tmp_path / "data" / "uploads"))
    monkeypatch.setenv("MARLIN_REQUIREMENTS_PATH", str(config / "requirements.yaml"))
    from app import main

    importlib.reload(main)
    c = TestClient(main.app, raise_server_exceptions=False)
    assert c.get("/healthz").status_code == 503
    response = _upload(c, consent=False)
    assert response.status_code == 503
    assert "requirements file is currently unavailable" in response.text


def test_oversized_upload_rejected_by_content_length_and_by_chunked_read(client):
    c, main = client
    limit = main.MAX_REPORT_BYTES
    # Far over the limit: the middleware answers from Content-Length alone
    huge = c.post("/analyze", files={"report": ("big.txt", b"x" * (limit + 200 * 1024), "text/plain")})
    assert huge.status_code == 413
    assert "larger than the 15 MB limit" in huge.text
    # Just over the limit (inside the multipart slack): caught by the chunked read
    just_over = c.post("/analyze", files={"report": ("big.txt", b"x" * (limit + 1), "text/plain")})
    assert just_over.status_code == 413
    # Under the limit still goes through the parser (and is rejected as not a report)
    assert c.post("/analyze", files={"report": ("r.txt", b"x" * 1024, "text/plain")}).status_code == 422


def test_bad_pdf_error_hides_library_internals(client):
    c, _ = client
    response = c.post("/analyze?lang=en", files={"report": ("r.pdf", b"%PDF-1.7 garbage", "application/pdf")})
    assert response.status_code == 422
    assert "The PDF content could not be read." in response.text
    assert "The PDF content could not be read. (" not in response.text
    for leak in ("pdfminer", "pdfplumber", "Traceback", "PSEOF", "No /Root"):
        assert leak not in response.text


def test_pdf_download_is_not_cacheable(client):
    pytest.importorskip("weasyprint")
    c, _ = client
    response = _upload(c, consent=False)
    token = str(response.url).rsplit("/", 1)[1]
    pdf = c.get(f"/pdf/{token}")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.headers["cache-control"] == "private, no-store"


def test_marlin_car_result_page_and_statistics(client):
    """A car already on Marlin gets the informational verdict on the page and
    is counted separately in the public statistics."""
    c, main = client
    fixture = Path(__file__).parent / "fixtures" / "olp_report_marlin_bcm41.txt"
    response = c.post(
        "/analyze?lang=en",
        files={"report": ("r.txt", fixture.read_bytes(), "text/plain")},
        data={"consent": "yes"},
    )
    assert response.status_code == 200
    assert "Already on Marlin" in response.text
    assert "can be updated directly to Marlin" not in response.text
    assert "Marlin does not update every module" in response.text
    assert "Body Control Module — BCM395041 (41 &lt; 42)" in response.text

    stats = main.database.stats()
    assert stats["verdicts"] == {"marlin": 1}
    page = c.get("/stats?lang=en")
    assert "Already on Marlin" in page.text
    assert main.database.usage_stats()["outcomes"] == {"marlin": 1}


def test_security_headers_and_no_inline_scripts(client):
    c, _ = client
    for path in ("/", "/stats", "/privacy"):
        response = c.get(path)
        assert response.status_code == 200
        csp = response.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "onclick=" not in response.text and "onchange=" not in response.text
        assert "<script>" not in response.text  # only /static/app.js
    assert c.get("/static/app.js").status_code == 200


def test_front_page_shows_variant_levels_for_bms(client):
    c, _ = client
    page = c.get("/?lang=en").text
    assert "≥ —" not in page
    assert "≥ 21 <span class=\"crit\">[NMC (One/Extreme/Ultra)]</span> / ≥ 15 <span class=\"crit\">[LFP (Sport)]</span>" in page


def test_empty_version_field_is_explained_on_the_result_page(client):
    c, _ = client
    text = FIXTURE.read_text().replace("Supplier SW Version: VCU039021", "Supplier SW Version:")
    response = c.post("/analyze?lang=en", files={"report": ("r.txt", text.encode(), "text/plain")})
    assert response.status_code == 200
    assert "Version field empty in the report" in response.text
    assert "Version not recognized" not in response.text
