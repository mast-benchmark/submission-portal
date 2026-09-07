"""Language codes, aliases, and track membership for MAST 2026.

Canonical form everywhere is the two-letter ISO 639-1 code. Participants may
write either the code or a name (``"chinese"``), in any case, in the
``language`` field; :func:`normalize` maps all of them to the code. Filenames
carry no meaning.
"""
from __future__ import annotations

import re
from typing import Optional

TRACKS: dict[str, tuple[str, ...]] = {
    "multilingual": (
        "ar", "bn", "cy", "de", "en", "es", "fi", "fr",
        "hi", "ru", "sw", "ta", "th", "ur", "zh",
    ),
    "indic": ("bn", "gu", "hi", "kn", "ml", "or", "pa", "ta", "te"),
}
TRACK_NAMES: tuple[str, ...] = tuple(TRACKS)

NAMES: dict[str, str] = {
    "ar": "Arabic", "bn": "Bengali", "cy": "Welsh", "de": "German", "en": "English",
    "es": "Spanish", "fi": "Finnish", "fr": "French", "hi": "Hindi", "ru": "Russian",
    "sw": "Swahili", "ta": "Tamil", "th": "Thai", "ur": "Urdu", "zh": "Chinese",
    "gu": "Gujarati", "kn": "Kannada", "ml": "Malayalam", "or": "Odia", "pa": "Punjabi",
    "te": "Telugu",
}

# Spec §3 aliases plus ISO 639-2/3 codes. Everything is matched after _clean().
ALIASES: dict[str, set[str]] = {
    "en": {"english", "eng"},
    "zh": {"chinese", "mandarin", "zh-cn", "zh_cn", "zh-hans", "zh-hant", "zh-tw", "zho", "cmn"},
    "fr": {"french", "fra", "fre"},
    "ru": {"russian", "rus"},
    "es": {"spanish", "spa"},
    "de": {"german", "deu", "ger"},
    "ar": {"arabic", "ara"},
    "bn": {"bengali", "bangla", "ben"},
    "fi": {"finnish", "fin"},
    "hi": {"hindi", "hin"},
    "th": {"thai", "tha"},
    "ur": {"urdu", "urd"},
    "ta": {"tamil", "tam"},
    "sw": {"swahili", "kiswahili", "swa"},
    "cy": {"welsh", "cymraeg", "cym", "wel"},
    "te": {"telugu", "tel"},
    "gu": {"gujarati", "guj"},
    "kn": {"kannada", "kan"},
    "pa": {"punjabi", "panjabi", "pan"},
    "ml": {"malayalam", "mal"},
    "or": {"odia", "oriya", "ori"},
}

ALL_CODES: frozenset[str] = frozenset(NAMES)


def _clean(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def _build_lookup() -> dict[str, str]:
    table: dict[str, str] = {}
    for code in NAMES:
        table[code] = code
    for code, aliases in ALIASES.items():
        for alias in aliases:
            key = _clean(alias)
            assert key not in table or table[key] == code, f"alias clash: {alias}"
            table[key] = code
    return table


_LOOKUP: dict[str, str] = _build_lookup()


def normalize(value: object) -> Optional[str]:
    """Map a code or a language name to the canonical ISO code, or ``None``.

    ``normalize("Chinese")``, ``normalize("zh_CN")``, ``normalize(" ZH ")`` all
    give ``"zh"``. Unknown or non-string input gives ``None``; the caller decides
    whether that is an error.
    """
    if not isinstance(value, str):
        return None
    key = _clean(value)
    if not key:
        return None
    return _LOOKUP.get(key)


def normalize_track(value: object) -> Optional[str]:
    """Map ``"multilingual"``, ``"MAST Indic (...)"`` etc. to a track name."""
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    if "multilingual" in key:
        return "multilingual"
    if "indic" in key:
        return "indic"
    return None


def langs_for(track: str) -> tuple[str, ...]:
    """Languages of a track, in canonical order. Raises ``KeyError`` for an unknown track."""
    return TRACKS[track]


def in_track(track: str, code: str) -> bool:
    return code in TRACKS.get(track, ())


def display_name(code: str) -> str:
    """``"hi"`` -> ``"Hindi (hi)"``."""
    return f"{NAMES.get(code, code)} ({code})"
