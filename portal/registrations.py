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

    Rows are in sheet order (chronological). For every (team, track) pair the
    **latest** row is the registration: its members are the eligible submitters
    for that track, earlier rows for the same pair are superseded entirely. A
    row for both tracks defines both. Rows with no recognizable track are skipped.
    """
    from .roster import normalize_name

    rows = [list(r) for r in rows]
    notes: list[str] = []
    if not rows:
        return [], ["empty sheet"]
    headers = [str(h) for h in rows[0]]
    c_name, c_track, c_ts = find_col(headers, "Team Name"), find_col(headers, "Which MAST Track"), find_col(headers, "Timestamp")
    c_emails = [find_col(headers, f"Member #{i} Email") for i in range(1, 5)]
    if c_name is None or c_track is None or c_emails[0] is None:
        return [], [f"unexpected headers: {headers[:4]}..."]

    def cell(r: list, i: Optional[int]) -> str:
        return str(r[i]).strip() if i is not None and i < len(r) and r[i] is not None else ""

    teams: dict[str, Team] = {}
    defined_by: dict[tuple[str, str], int] = {}
    for n, r in enumerate(rows[1:], 2):
        name = cell(r, c_name)
        if not name:
            continue
        key = normalize_name(name)
        emails = list(dict.fromkeys(e for e in (cell(r, c) for c in c_emails) if e))
        tracks = tracks_from_cell(cell(r, c_track))
        if not tracks:
            notes.append(f"row {n} ({name!r}): no track recognized; skipped")
            continue
        team = teams.setdefault(key, Team(name=name))
        team.name = name                                   # latest spelling
        for track in tracks:
            if (key, track) in defined_by:
                notes.append(f"row {n} ({name!r}, {track}): supersedes row {defined_by[(key, track)]}")
            defined_by[(key, track)] = n
            team.members[track] = emails
            team.registered_at[track] = cell(r, c_ts)
    alias_map = {normalize_name(k): v for k, v in (aliases or {}).items()}
    for key, team in teams.items():
        team.aliases = [a.strip() for a in alias_map.get(key, "").split(";") if a.strip()]
    return list(teams.values()), notes
