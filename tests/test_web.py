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
