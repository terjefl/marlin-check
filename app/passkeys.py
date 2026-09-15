"""Passkeys (WebAuthn) for the admin login: a thin wrapper around py_webauthn
so the routes stay readable and the tests can replace the verification.

A passkey is registered on the profile page (discoverable credential with
user verification, so it also works for passwordless sign-in). It is accepted
as the second factor instead of a TOTP code, and on its own for sign-in.
The challenge lives on the session row; credential ids are base64url."""

from __future__ import annotations

import base64
import hashlib
import json

import webauthn
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

RP_NAME = "Ocean Software Check"


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def user_handle(username: str) -> bytes:
    """A stable, non-identifying user id for the authenticator."""
    return hashlib.sha256(f"osc-admin:{username}".encode()).digest()


def registration_options(*, rp_id: str, username: str, existing_ids: list[str]) -> tuple[str, str]:
    """(options JSON for navigator.credentials.create, challenge base64url)."""
    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=RP_NAME,
        user_id=user_handle(username),
        user_name=username,
        user_display_name=username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=b64url_decode(c)) for c in existing_ids],
    )
    return webauthn.options_to_json(options), bytes_to_base64url(options.challenge)


def verify_registration(*, credential: dict, challenge: str, rp_id: str, origin: str) -> tuple[str, bytes, int]:
    """(credential id base64url, public key bytes, sign count). Raises on failure."""
    verified = webauthn.verify_registration_response(
        credential=credential,
        expected_challenge=b64url_decode(challenge),
        expected_rp_id=rp_id,
        expected_origin=origin,
        require_user_verification=True,
    )
    return bytes_to_base64url(verified.credential_id), verified.credential_public_key, verified.sign_count


def authentication_options(*, rp_id: str, allowed_ids: list[str]) -> tuple[str, str]:
    """(options JSON for navigator.credentials.get, challenge base64url).
    An empty allow list lets the authenticator pick a discoverable credential."""
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[PublicKeyCredentialDescriptor(id=b64url_decode(c)) for c in allowed_ids] or None,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return webauthn.options_to_json(options), bytes_to_base64url(options.challenge)


def verify_authentication(*, credential: dict, challenge: str, rp_id: str, origin: str,
                          public_key: bytes, sign_count: int) -> int:
    """The new sign count. Raises on failure."""
    verified = webauthn.verify_authentication_response(
        credential=credential,
        expected_challenge=b64url_decode(challenge),
        expected_rp_id=rp_id,
        expected_origin=origin,
        credential_public_key=public_key,
        credential_current_sign_count=sign_count,
        require_user_verification=True,
    )
    return verified.new_sign_count


def credential_id_of(credential: dict) -> str:
    """The id the browser sends (already base64url)."""
    return str(credential.get("id", ""))


def parse_body(raw: bytes) -> dict:
    data = json.loads(raw or b"{}")
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data
