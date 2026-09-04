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

# The ONE request header trusted to carry the real client IP (used for rate
# limits, login lockout, the audit log and the daily usage hash). Behind
# Cloudflare this is CF-Connecting-IP, which Cloudflare always overwrites.
# X-Forwarded-For is deliberately NOT consulted: Cloudflare appends the visitor
# to a client-supplied X-Forwarded-For, so its first element is attacker
# controlled. Set the variable to an empty string to use the socket address
# (no proxy in front).
CLIENT_IP_HEADER = os.environ.get("MARLIN_CLIENT_IP_HEADER", "cf-connecting-ip").strip().lower()

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
    """Client IP from the single trusted proxy header, else the socket address."""
    if CLIENT_IP_HEADER:
        value = request.headers.get(CLIENT_IP_HEADER, "")
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _too_many_failures(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _failed_attempts.get(ip, []) if t > now - FAILED_WINDOW]
    if attempts:
        _failed_attempts[ip] = attempts
    else:
        _failed_attempts.pop(ip, None)  # do not keep a key per IP forever
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
