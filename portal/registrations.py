"""Turn registration-form rows (the Sheets export layout) into teams.

Shared by the live Google Sheets roster source and the offline importer. Header
matching is a case-insensitive prefix match on the question title, so it works
whether or not the export carries the question's description line.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from .roster import Team

TRACK_WORDS = ("multilingual", "indic")


def find_col(headers: list[str], prefix: str) -> Optional[int]:
    p = prefix.lower()
    for i, h in enumerate(headers):
        if (h or "").strip().lower().startswith(p):
            return i
    return None


def tracks_from_cell(cell: str) -> list[str]:
    """Multi-select labels contain commas; never split on them, substring-match the track words."""
    low = (cell or "").lower()
    return [t for t in TRACK_WORDS if t in low]


def rows_to_teams(rows: Iterable[list], aliases: Optional[dict[str, str]] = None) -> tuple[list[Team], list[str]]:
    """Rows including the header row -> (teams, notes).

    A team registered more than once keeps the latest row's name, contact and
    timestamp; member emails and tracks are unioned so nobody who registered
    under the name is locked out. Rows without a recognizable track are skipped
    and reported in ``notes``.
    """
    rows = [list(r) for r in rows]
    notes: list[str] = []
    if not rows:
        return [], ["empty sheet"]
    headers = [str(h) for h in rows[0]]
    c_name, c_track = find_col(headers, "Team Name"), find_col(headers, "Which MAST Track")
    c_emails = [find_col(headers, f"Member #{i} Email") for i in range(1, 5)]
    if c_name is None or c_track is None or c_emails[0] is None:
        return [], [f"unexpected headers: {headers[:4]}..."]

    def cell(r: list, i: Optional[int]) -> str:
        return str(r[i]).strip() if i is not None and i < len(r) and r[i] is not None else ""

    by_key: dict[str, dict] = {}
    for n, r in enumerate(rows[1:], 2):
        name = cell(r, c_name)
        if not name:
            continue
        key = re.sub(r"\s+", " ", name).lower()
        emails = [e for e in (cell(r, c) for c in c_emails) if e]
        tracks = tracks_from_cell(cell(r, c_track))
        entry = by_key.get(key)
        if not tracks and entry is None:
            notes.append(f"row {n} ({name!r}): no track recognized; skipped")
            continue
        if entry is None:
            by_key[key] = {"name": name, "contact": emails[0] if emails else "", "emails": list(dict.fromkeys(emails)),
                           "tracks": list(tracks)}
        else:
            notes.append(f"row {n} ({name!r}): duplicate registration merged")
            entry["name"] = name
            if emails:
                entry["contact"] = emails[0]
            entry["emails"] = list(dict.fromkeys(entry["emails"] + emails))
            entry["tracks"] = list(dict.fromkeys(entry["tracks"] + tracks))
    alias_map = {re.sub(r"\s+", " ", k).lower(): v for k, v in (aliases or {}).items()}
    teams = [Team(name=e["name"], contact_email=e["contact"], member_emails=e["emails"], tracks=e["tracks"],
                  aliases=[a.strip() for a in alias_map.get(k, "").split(";") if a.strip()])
             for k, e in by_key.items()]
    return teams, notes
