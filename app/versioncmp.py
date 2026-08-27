"""Sammenligning av modulversjoner fra OLP-rapporter.

Semantikken her er et førsteutkast basert på antatte versjonsstrenger som
"2.1.0", "1.14" og "2.1.0-b123". Den MÅ verifiseres mot ekte versjonsstrenger
fra en OLP-rapport (Fase 2) før portalen tas i bruk.

Regler:
- Strengen deles i numeriske segmenter (alle ikke-sifre er skilletegn).
- Segmentene sammenlignes tallmessig, venstre mot høyre.
- Manglende segmenter regnes som 0 ("2.1" == "2.1.0").
- En streng helt uten sifre kan ikke sammenlignes -> ValueError.
"""

from __future__ import annotations

import re

_SEGMENT_RE = re.compile(r"\d+")


def parse_version(version: str) -> tuple[int, ...]:
    segments = tuple(int(s) for s in _SEGMENT_RE.findall(version))
    if not segments:
        raise ValueError(f"Fant ingen numeriske segmenter i versjonsstrengen: {version!r}")
    return segments


def compare_versions(a: str, b: str) -> int:
    """Returnerer -1 hvis a < b, 0 hvis a == b, 1 hvis a > b."""
    va, vb = parse_version(a), parse_version(b)
    length = max(len(va), len(vb))
    va += (0,) * (length - len(va))
    vb += (0,) * (length - len(vb))
    if va < vb:
        return -1
    if va > vb:
        return 1
    return 0


def meets_minimum(version: str, minimum: str) -> bool:
    return compare_versions(version, minimum) >= 0
