"""End-to-end tests of the web flow, including the consent logic."""

import importlib
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

    # Expired/unknown token -> back to the front page
    gone = c.get("/result/finnesikke", follow_redirects=False)
    assert gone.status_code == 303 and gone.headers["location"] == "/"


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
    from datetime import datetime, timezone

    c, main = client
    c.post("/analyze", files={"report": ("r.txt", FIXTURE.read_bytes(), "text/plain")},
           headers={"CF-Connecting-IP": "203.0.113.77"})
    import sqlite3

    stored = sqlite3.connect(main.database.path).execute(
        "SELECT ip_hash FROM usage_events"
    ).fetchone()[0]
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
