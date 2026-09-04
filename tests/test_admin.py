"""Tests for the admin page: form login (SQLite sessions), CSRF, validation,
saving and the activity log."""

import importlib
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password

EXAMPLE = Path(__file__).parent.parent / "requirements.example.yaml"
FIXTURE = Path(__file__).parent / "fixtures" / "olp_report.txt"


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
    # https base URL: the session cookie is Secure and would otherwise be
    # dropped by the cookie jar on plain http.
    return TestClient(main.app, base_url="https://testserver"), main


def _login(c, username: str, password: str, **kwargs):
    return c.post(
        "/admin/login", data={"username": username, "password": password},
        follow_redirects=False, **kwargs,
    )


def _csrf(page_html: str) -> str:
    import re

    return re.search(r'name="csrf" value="([^"]+)"', page_html).group(1)


def _updates(main):
    return [e for e in main.database.audit_entries() if e["action"] == "requirements_update"]


def test_admin_requires_login(client):
    c, _ = client
    response = c.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login?next=/admin"
    assert c.get("/admin/login").status_code == 200

    for user, pw in [("terje", "feilpassord"), ("finnesikke", "x")]:
        response = _login(c, user, pw)
        assert response.status_code == 401
        assert "Invalid username or password" in response.text
        assert "marlin_admin" not in response.cookies
    assert c.get("/admin", follow_redirects=False).status_code == 303


def test_login_sets_cookie_and_page_renders_for_both_users(client):
    c, main = client
    for user, pw in [("terje", "hemmelig123"), ("styremedlem", "ogsåhemmelig")]:
        response = _login(c, user, pw, headers={"CF-Connecting-IP": "203.0.113.9"})
        assert response.status_code == 303 and response.headers["location"] == "/admin"
        cookie = response.headers["set-cookie"]
        assert "marlin_admin=" in cookie
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie.replace("Lax", "lax")
        assert "Path=/admin" in cookie

        page = c.get("/admin")
        assert page.status_code == 200
        assert user in page.text
        assert "target_profile" in page.text  # the YAML content is shown
        assert 'name="csrf"' in page.text

        # A logged-in user is sent straight from the login form to /admin
        assert c.get("/admin/login", follow_redirects=False).headers["location"] == "/admin"

        logins = [e for e in main.database.audit_entries() if e["action"] == "login"]
        assert logins[0]["username"] == user and logins[0]["ip"] == "203.0.113.9"
        c.cookies.clear()


def test_next_parameter_only_allows_admin_paths(client):
    c, _ = client
    assert c.get("/admin/login?next=https://evil.example/", follow_redirects=False).status_code == 200
    response = c.post(
        "/admin/login",
        data={"username": "terje", "password": "hemmelig123", "next": "https://evil.example/"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/admin"
    c.cookies.clear()
    response = c.post(
        "/admin/login",
        data={"username": "terje", "password": "hemmelig123", "next": "/admin?x=1"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/admin?x=1"


def test_logout_invalidates_session(client):
    c, main = client
    _login(c, "terje", "hemmelig123")
    csrf = _csrf(c.get("/admin").text)
    response = c.post("/admin/logout", data={"csrf": csrf}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/login"
    assert c.get("/admin", follow_redirects=False).status_code == 303
    assert [e["action"] for e in main.database.audit_entries()][:2] == ["logout", "login"]


def test_session_expires_when_idle(client):
    c, main = client
    _login(c, "terje", "hemmelig123")
    assert c.get("/admin").status_code == 200
    import sqlite3

    conn = sqlite3.connect(main.database.path)
    conn.execute("UPDATE admin_sessions SET last_seen_at = last_seen_at - ?", (9 * 3600,))
    conn.commit()
    assert c.get("/admin", follow_redirects=False).status_code == 303
    assert conn.execute("SELECT COUNT(*) FROM admin_sessions").fetchone()[0] == 0


def test_save_requires_csrf_token_and_same_site(client):
    c, main = client
    original = main.REQUIREMENTS_PATH.read_text()
    _login(c, "terje", "hemmelig123")
    new_text = original.replace('version: "2026-09-workbook-v2-draft"', 'version: "csrf-test"')

    # No token: what a cross-site form post would look like if the cookie leaked
    response = c.post("/admin/save", data={"yaml_text": new_text})
    assert response.status_code == 403
    # Wrong token
    response = c.post("/admin/save", data={"yaml_text": new_text, "csrf": "nope"})
    assert response.status_code == 403
    # Right token but the browser says the request came from another site
    csrf = _csrf(c.get("/admin").text)
    response = c.post(
        "/admin/save", data={"yaml_text": new_text, "csrf": csrf},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403
    assert main.REQUIREMENTS_PATH.read_text() == original
    assert _updates(main) == []

    # Same-site with the right token works
    response = c.post(
        "/admin/save", data={"yaml_text": new_text, "csrf": csrf},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert response.status_code == 200
    assert 'version: "csrf-test"' in main.REQUIREMENTS_PATH.read_text()


def test_save_rejects_invalid_yaml_without_writing(client):
    c, main = client
    original = main.REQUIREMENTS_PATH.read_text()
    _login(c, "terje", "hemmelig123")
    csrf = _csrf(c.get("/admin").text)
    response = c.post("/admin/save", data={"yaml_text": "dette er: [ikke gyldig", "csrf": csrf})
    assert response.status_code == 422
    assert "validation error" in response.text
    assert main.REQUIREMENTS_PATH.read_text() == original
    assert _updates(main) == []

    # Valid YAML but invalid structure (missing target_profile level)
    response = c.post(
        "/admin/save",
        data={"yaml_text": "version: x\nprofiles: ['2.1']\ntarget_profile: '2.1'\nmodules: []\n", "csrf": csrf},
    )
    assert response.status_code == 422
    assert main.REQUIREMENTS_PATH.read_text() == original


def test_save_writes_and_audits_with_user_ip_and_diff(client):
    c, main = client
    new_text = main.REQUIREMENTS_PATH.read_text().replace(
        'version: "2026-09-workbook-v2-draft"', 'version: "2026-09-official"'
    )
    _login(c, "styremedlem", "ogsåhemmelig")
    csrf = _csrf(c.get("/admin").text)
    response = c.post(
        "/admin/save",
        headers={"CF-Connecting-IP": "203.0.113.7"},
        data={"yaml_text": new_text, "csrf": csrf},
    )
    assert response.status_code == 200
    assert "Saved" in response.text
    assert 'version: "2026-09-official"' in main.REQUIREMENTS_PATH.read_text()

    entries = _updates(main)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["username"] == "styremedlem"
    assert entry["ip"] == "203.0.113.7"
    assert '-version: "2026-09-workbook-v2-draft"' in entry["detail"]
    assert '+version: "2026-09-official"' in entry["detail"]

    # The analysis uses the new requirements version immediately
    result = c.post("/analyze", files={"report": ("r.txt", FIXTURE.read_bytes(), "text/plain")})
    assert "2026-09-official" in result.text


def test_form_save_builds_valid_yaml_and_audits(client):
    c, main = client
    _login(c, "terje", "hemmelig123")
    form = {
        "csrf": _csrf(c.get("/admin").text),
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
    response = c.post("/admin/save-form", data=form)
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
    assert _updates(main)[0]["action"] == "requirements_update"


def test_form_save_rejects_bad_level(client):
    c, main = client
    original = main.REQUIREMENTS_PATH.read_text()
    _login(c, "terje", "hemmelig123")
    form = {
        "csrf": _csrf(c.get("/admin").text),
        "version": "x", "profiles": "2.1", "target_profile": "2.1",
        "mod-0-id": "VCU", "mod-0-level-2.1": "ikke-tall",
    }
    response = c.post("/admin/save-form", data=form)
    assert response.status_code == 422
    assert main.REQUIREMENTS_PATH.read_text() == original


def test_form_roundtrip_from_rendered_html(client):
    """Regression: render the admin page and post the form back UNCHANGED. The form
    regenerates the YAML (comments are dropped), so we require that the save
    validates OK and that the content is semantically identical."""
    import html as html_module
    import re as re_module

    c, main = client
    _login(c, "terje", "hemmelig123")
    page = c.get("/admin").text
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
    assert "csrf" in names

    from app.rules import load_requirements

    before = load_requirements(main.REQUIREMENTS_PATH)
    response = c.post("/admin/save-form", data=dict(fields))
    assert response.status_code == 200, response.text
    after = load_requirements(main.REQUIREMENTS_PATH)
    assert [m.id for m in after.modules] == [m.id for m in before.modules]
    assert {m.id: m.levels for m in after.modules} == {m.id: m.levels for m in before.modules}
    assert {m.id: m.extract for m in after.modules} == {m.id: m.extract for m in before.modules}
    assert after.target_profile == before.target_profile
    # variants/only_trims survive a form save (the form cannot edit them)
    assert {m.id: m.only_trims for m in after.modules} == {m.id: m.only_trims for m in before.modules}
    assert {m.id: [(v.name, v.pattern, v.levels) for v in m.variants] for m in after.modules} == {
        m.id: [(v.name, v.pattern, v.levels) for v in m.variants] for m in before.modules
    }


def test_lockout_on_failed_logins_per_ip_and_per_user(client):
    c, _ = client
    ip = {"CF-Connecting-IP": "198.51.100.9"}
    for _ in range(10):
        assert _login(c, "terje", "feil", headers=ip).status_code == 401
    # Same IP, right password -> locked out
    assert _login(c, "terje", "hemmelig123", headers=ip).status_code == 429
    # Other IP, same username -> also locked out (per-user lock)
    assert _login(c, "terje", "hemmelig123", headers={"CF-Connecting-IP": "198.51.100.10"}).status_code == 429
    # Other IP, other user -> fine
    assert _login(c, "styremedlem", "ogsåhemmelig", headers={"CF-Connecting-IP": "198.51.100.10"}).status_code == 303
