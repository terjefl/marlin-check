"""Tests for the admin page: auth, validation, saving and audit log."""

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
        assert "target_profile" in response.text  # the YAML content is shown


def test_save_rejects_invalid_yaml_without_writing(client):
    c, main = client
    original = main.REQUIREMENTS_PATH.read_text()
    response = c.post(
        "/admin/save",
        headers=_basic("terje", "hemmelig123"),
        data={"yaml_text": "dette er: [ikke gyldig"},
    )
    assert response.status_code == 422
    assert "validation error" in response.text
    assert main.REQUIREMENTS_PATH.read_text() == original
    assert main.database.audit_entries() == []

    # Valid YAML but invalid structure (missing target_profile level)
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
        'version: "2026-08-web-meeting-draft"', 'version: "2026-09-official"'
    )
    response = c.post(
        "/admin/save",
        headers={**_basic("styremedlem", "ogsåhemmelig"), "CF-Connecting-IP": "203.0.113.7"},
        data={"yaml_text": new_text},
    )
    assert response.status_code == 200
    assert "Saved" in response.text
    assert 'version: "2026-09-official"' in main.REQUIREMENTS_PATH.read_text()

    entries = main.database.audit_entries()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["username"] == "styremedlem"
    assert entry["ip"] == "203.0.113.7"
    assert entry["action"] == "requirements_update"
    assert '-version: "2026-08-web-meeting-draft"' in entry["detail"]
    assert '+version: "2026-09-official"' in entry["detail"]

    # The analysis uses the new requirements version immediately
    fixture = Path(__file__).parent / "fixtures" / "olp_report.txt"
    result = c.post("/analyze", files={"report": ("r.txt", fixture.read_bytes(), "text/plain")})
    assert "2026-09-official" in result.text


def test_form_save_builds_valid_yaml_and_audits(client):
    c, main = client
    form = {
        "version": "2026-09-form",
        "profiles": "2.0, 2.1",
        "target_profile": "2.1",
        "mod-0-id": "VCU",
        "mod-0-label": "Vehicle Control Unit",
        "mod-0-match": "VCU",
        "mod-0-extract": r"VCU\d{3}0*(\d+)$",
        "mod-0-level-2.0": "21",
        "mod-0-level-2.1": "23",
        "mod-0-critical": "yes",
        "mod-7-id": "BMS",  # non-contiguous index (the JS uses random ones)
        "mod-7-label": "Battery Management System",
        "mod-7-match": "BMS",
        "mod-7-extract": r"BMSN\d{3}0*(\d+)$",
        "mod-7-level-2.1": "21",
    }
    response = c.post("/admin/save-form", headers=_basic("terje", "hemmelig123"), data=form)
    assert response.status_code == 200, response.text
    assert "Saved" in response.text

    saved = main.REQUIREMENTS_PATH.read_text()
    assert "2026-09-form" in saved
    from app.rules import load_requirements

    parsed = load_requirements(main.REQUIREMENTS_PATH)
    assert [m.id for m in parsed.modules] == ["VCU", "BMS"]
    assert parsed.modules[0].critical is True
    assert parsed.modules[1].critical is False  # checkbox not submitted
    assert parsed.modules[1].levels == {"2.1": 21}
    assert main.database.audit_entries()[0]["action"] == "requirements_update"


def test_form_save_rejects_bad_level(client):
    c, main = client
    original = main.REQUIREMENTS_PATH.read_text()
    form = {
        "version": "x", "profiles": "2.1", "target_profile": "2.1",
        "mod-0-id": "VCU", "mod-0-level-2.1": "ikke-tall",
    }
    response = c.post("/admin/save-form", headers=_basic("terje", "hemmelig123"), data=form)
    assert response.status_code == 422
    assert main.REQUIREMENTS_PATH.read_text() == original


def test_form_roundtrip_from_rendered_html(client):
    """Regression: render the admin page and post the form back UNCHANGED. The form
    regenerates the YAML (comments are dropped), so we require that the save
    validates OK and that the content is semantically identical."""
    import html as html_module
    import re as re_module

    c, main = client
    page = c.get("/admin", headers=_basic("terje", "hemmelig123")).text
    form_html = re_module.search(
        r'<form method="post" action="/admin/save-form">(.*?)</form>', page, re_module.S
    ).group(1)
    fields: list[tuple[str, str]] = []
    for m in re_module.finditer(r"<input([^>]*)>", form_html):
        attrs = dict(re_module.findall(r'(\w+)="([^"]*)"', m.group(1)))
        if attrs.get("type") == "checkbox":
            if "checked" in m.group(1):
                fields.append((attrs["name"], attrs.get("value", "on")))
        elif "name" in attrs:
            fields.append((attrs["name"], html_module.unescape(attrs.get("value", ""))))

    # Field names must be unique (a loop.index0 bug caused collisions between rows)
    names = [n for n, _ in fields]
    assert len(names) == len(set(names)), f"colliding field names: {sorted(names)}"

    from app.rules import load_requirements

    before = load_requirements(main.REQUIREMENTS_PATH)
    response = c.post(
        "/admin/save-form", headers=_basic("terje", "hemmelig123"), data=dict(fields)
    )
    assert response.status_code == 200, response.text
    after = load_requirements(main.REQUIREMENTS_PATH)
    assert [m.id for m in after.modules] == [m.id for m in before.modules]
    assert {m.id: m.levels for m in after.modules} == {m.id: m.levels for m in before.modules}
    assert {m.id: m.extract for m in after.modules} == {m.id: m.extract for m in before.modules}
    assert after.target_profile == before.target_profile


def test_rate_limit_on_failed_logins(client):
    c, _ = client
    for _ in range(10):
        c.get("/admin", headers={**_basic("terje", "feil"), "CF-Connecting-IP": "198.51.100.9"})
    response = c.get(
        "/admin", headers={**_basic("terje", "hemmelig123"), "CF-Connecting-IP": "198.51.100.9"}
    )
    assert response.status_code == 429
