"""Enkel i18n: JSON-ordbøker per språk, engelsk som fallback.

Språkvalg (prioritert): ?lang=-parameter -> cookie -> Accept-Language -> en.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import Request

LOCALES_DIR = Path(__file__).parent / "locales"
SUPPORTED = ["en", "nb", "sv", "da", "de", "fr", "es"]
LANGUAGE_NAMES = {
    "en": "English",
    "nb": "Norsk",
    "sv": "Svenska",
    "da": "Dansk",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
}
# Accept-Language-koder som mappes til våre locales
_ALIASES = {"no": "nb", "nn": "nb"}

_translations: dict[str, dict[str, str]] = {}


def _load(lang: str) -> dict[str, str]:
    if lang not in _translations:
        _translations[lang] = json.loads(
            (LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8")
        )
    return _translations[lang]


def negotiate_language(request: Request) -> str:
    query_lang = request.query_params.get("lang")
    if query_lang in SUPPORTED:
        return query_lang
    cookie_lang = request.cookies.get("lang")
    if cookie_lang in SUPPORTED:
        return cookie_lang
    header = request.headers.get("accept-language", "")
    for part in header.split(","):
        code = part.split(";")[0].strip().lower()
        primary = _ALIASES.get(code.split("-")[0], code.split("-")[0])
        if primary in SUPPORTED:
            return primary
    return "en"


def translator(lang: str):
    table = _load(lang)
    fallback = _load("en")

    def t(key: str, **kwargs) -> str:
        text = table.get(key) or fallback.get(key) or key
        return text.format(**kwargs) if kwargs else text

    return t
