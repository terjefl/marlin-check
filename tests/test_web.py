"""Ende-til-ende-tester av webflyten, inkl. samtykkelogikken."""

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

    # Samme VIN på nytt -> fortsatt 1 unik bil, 2 innsendinger
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


def test_stats_and_privacy_pages_render(client):
    c, _ = client
    assert c.get("/stats").status_code == 200
    assert c.get("/privacy").status_code == 200
    assert c.get("/healthz").json() == {"status": "ok"}


def test_language_negotiation(client):
    c, _ = client
    norsk = c.get("/", headers={"accept-language": "nb-NO,nb;q=0.9"})
    assert "Sjekk bilen min" in norsk.text
    deutsch = c.get("/?lang=de")
    assert "Mein Auto prüfen" in deutsch.text
    assert deutsch.cookies.get("lang") == "de"
