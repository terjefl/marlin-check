"""Simple multi-user authentication (HTTP Basic) for the admin pages.

Users live in a YAML file on the host (bind-mounted, read on every call):

    users:
      terje: pbkdf2_sha256$600000$<salt_hex>$<hash_hex>
      styremedlem: pbkdf2_sha256$600000$...

Hashes are created with scripts/hash_password.py. Only hashes are stored —
never plaintext. Failed attempts are rate limited per IP (10 per 15 min).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from pathlib import Path

import yaml
from fastapi import HTTPException, Request

USERS_PATH = Path(os.environ.get("MARLIN_ADMIN_USERS_PATH", "/config/admin_users.yaml"))

PBKDF2_ITERATIONS = 600_000
MAX_FAILED = 10
FAILED_WINDOW = 15 * 60

_failed_attempts: dict[str, list[float]] = {}


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, hash_hex = stored.strip().split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def load_users() -> dict[str, str]:
    if not USERS_PATH.exists():
        return {}
    raw = yaml.safe_load(USERS_PATH.read_text(encoding="utf-8")) or {}
    return {str(k): str(v) for k, v in (raw.get("users") or {}).items()}


def client_ip(request: Request) -> str:
    # Behind Cloudflare: CF-Connecting-IP is the real client IP
    for header in ("cf-connecting-ip", "x-forwarded-for"):
        value = request.headers.get(header, "")
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _too_many_failures(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _failed_attempts.get(ip, []) if t > now - FAILED_WINDOW]
    _failed_attempts[ip] = attempts
    return len(attempts) >= MAX_FAILED


def _register_failure(ip: str) -> None:
    _failed_attempts.setdefault(ip, []).append(time.time())


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401, detail=detail,
        headers={"WWW-Authenticate": 'Basic realm="marlin-admin", charset="UTF-8"'},
    )


def require_admin(request: Request) -> str:
    """FastAPI dependency: returns the logged-in username or raises 401/429."""
    ip = client_ip(request)
    if _too_many_failures(ip):
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again later.")

    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise _unauthorized("Authentication required")
    try:
        username, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        raise _unauthorized("Invalid authorization header")

    stored = load_users().get(username)
    # Always run a hash verification so timing does not reveal whether the user exists
    dummy = hash_password("dummy") if stored is None else stored
    if stored is None or not verify_password(password, dummy):
        _register_failure(ip)
        raise _unauthorized("Invalid username or password")
    return username
