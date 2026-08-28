"""Tester for admin-siden: auth, validering, lagring og revisjonslogg."""

import base64
import importlib
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password

EXAMPLE = Path(__file__).parent.parent / "requirements.example.yaml"


def _basic(username: str, password: str) -> dict:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(EXAMPLE, config / "requirements.yaml")
    (config / "admin_users.yaml").write_text(
        f"users:\n  terje: {hash_password('hemmelig123')}\n"
        f"  styremedlem: {hash_password('ogsåhemmelig')}\n"
    )
    monkeypatch.setenv("MARLIN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MARLIN_UPLOADS_DIR", str(tmp_path / "data" / "uploads"))
    monkeypatch.setenv("MARLIN_REQUIREMENTS_PATH", str(config / "requirements.yaml"))
    monkeypatch.setenv("MARLIN_ADMIN_USERS_PATH", str(config / "admin_users.yaml"))

    import app.auth as auth_module
    from app import main

    importlib.reload(auth_module)
    importlib.reload(main)
    return TestClient(main.app), main


def test_admin_requires_auth(client):
    c, _ = client
    assert c.get("/admin").status_code == 401
    assert c.get("/admin", headers=_basic("terje", "feilpassord")).status_code == 401
    assert c.get("/admin", headers=_basic("finnesikke", "x")).status_code == 401


def test_admin_page_renders_for_both_users(client):
    c, _ = client
    for user, pw in [("terje", "hemmelig123"), ("styremedlem", "ogsåhemmelig")]:
        response = c.get("/admin", headers=_basic(user, pw))
        assert response.status_code == 200
        assert user in response.text
        assert "target_profile" in response.text  # YAML-innholdet vises


def test_save_rejects_invalid_yaml_without_writing(client):
    c, main = client
    original = main.REQUIREMENTS_PATH.read_text()
    response = c.post(
        "/admin/save",
        headers=_basic("terje", "hemmelig123"),
        data={"yaml_text": "dette er: [ikke gyldig"},
    )
    assert response.status_code == 422
    assert "valideringsfeil" in response.text
    assert main.REQUIREMENTS_PATH.read_text() == original
    assert main.database.audit_entries() == []

    # Gyldig YAML men ugyldig struktur (mangler target_profile-nivå)
    response = c.post(
        "/admin/save",
        headers=_basic("terje", "hemmelig123"),
        data={"yaml_text": "version: x\nprofiles: ['2.1']\ntarget_profile: '2.1'\nmodules: []\n"},
    )
    assert response.status_code == 422
    assert main.REQUIREMENTS_PATH.read_text() == original


def test_save_writes_and_audits_with_user_ip_and_diff(client):
    c, main = client
    new_text = main.REQUIREMENTS_PATH.read_text().replace(
        'version: "2026-08-videomote-utkast"', 'version: "2026-09-offisiell"'
    )
    response = c.post(
        "/admin/save",
        headers={**_basic("styremedlem", "ogsåhemmelig"), "CF-Connecting-IP": "203.0.113.7"},
        data={"yaml_text": new_text},
    )
    assert response.status_code == 200
    assert "Lagret" in response.text
    assert 'version: "2026-09-offisiell"' in main.REQUIREMENTS_PATH.read_text()

    entries = main.database.audit_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["username"] == "styremedlem"
    assert entry["ip"] == "203.0.113.7"
    assert entry["action"] == "requirements_update"
    assert '-version: "2026-08-videomote-utkast"' in entry["detail"]
    assert '+version: "2026-09-offisiell"' in entry["detail"]

    # Analysen bruker den nye kravversjonen umiddelbart
    fixture = Path(__file__).parent / "fixtures" / "olp_report.txt"
    result = c.post("/analyze", files={"report": ("r.txt", fixture.read_bytes(), "text/plain")})
    assert "2026-09-offisiell" in result.text


def test_rate_limit_on_failed_logins(client):
    c, _ = client
    for _ in range(10):
        c.get("/admin", headers={**_basic("terje", "feil"), "CF-Connecting-IP": "198.51.100.9"})
    response = c.get(
        "/admin", headers={**_basic("terje", "hemmelig123"), "CF-Connecting-IP": "198.51.100.9"}
    )
    assert response.status_code == 429
