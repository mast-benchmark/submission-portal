"""Team roster: ``teams.csv`` in storage, cached ~60 s, matched by normalized name or alias.

Normalization ignores case, punctuation, repeated spaces and a leading "team ";
word order is significant ("Sahel Test" and "Test Sahel" are different teams).
The roster is never rendered. A miss yields at most one "did you mean" suggestion.
"""
from __future__ import annotations

import csv
import difflib
import io
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

ROSTER_PATH = "teams.csv"
COLUMNS = ["team_name", "contact_email", "member_emails", "tracks", "aliases", "registered_at"]


def normalize_name(name: str) -> str:
    s = unicodedata.normalize("NFKC", name or "").lower()
    s = re.sub(r"[^\w\s]|_", " ", s)   # underscore is a word character in regex; treat it as punctuation too
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("team "):
        s = s[5:].strip()
    return s


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", normalize_name(name)).strip("-")
    return s or "team"


@dataclass
class Team:
    name: str
    contact_email: str
    member_emails: list[str] = field(default_factory=list)
    tracks: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def notify_emails(self) -> list[str]:
        return [self.contact_email] if self.contact_email else []

    @property
    def all_emails(self) -> set[str]:
        """Every address registered for the team, lower-cased."""
        return {e.strip().lower() for e in [self.contact_email, *self.member_emails] if e and e.strip()}

    def knows_email(self, email: str) -> bool:
        return (email or "").strip().lower() in self.all_emails


@dataclass
class Match:
    team: Optional[Team]
    suggestion: Optional[str] = None


def parse_roster(text: str) -> list[Team]:
    reader = csv.DictReader(io.StringIO(text))
    teams: list[Team] = []
    for row in reader:
        name = (row.get("team_name") or "").strip()
        if not name:
            continue
        split = lambda s: [x.strip() for x in re.split(r"[;,]", s or "") if x.strip()]  # noqa: E731
        teams.append(Team(
            name=name,
            contact_email=(row.get("contact_email") or "").strip(),
            member_emails=split(row.get("member_emails")),
            tracks=[t.lower() for t in split(row.get("tracks"))],
            aliases=[a for a in re.split(r";", row.get("aliases") or "") if a.strip()],
        ))
    return teams


class Roster:
    def __init__(self, storage, ttl_seconds: int = 60) -> None:
        self.storage = storage
        self.ttl = ttl_seconds
        self._teams: list[Team] = []
        self._index: dict[str, Team] = {}
        self._loaded_at = 0.0

    def _refresh(self, force: bool = False) -> None:
        if not force and time.time() - self._loaded_at < self.ttl:
            return
        raw = self.storage.read(ROSTER_PATH)
        teams = parse_roster(raw.decode("utf-8-sig")) if raw else []
        index: dict[str, Team] = {}
        for t in teams:
            for key in [t.name, *t.aliases]:
                n = normalize_name(key)
                if n:
                    index.setdefault(n, t)
        self._teams, self._index, self._loaded_at = teams, index, time.time()

    def match(self, name: str) -> Match:
        self._refresh()
        n = normalize_name(name)
        if not n:
            return Match(None)
        team = self._index.get(n)
        if team:
            return Match(team)
        close = difflib.get_close_matches(n, list(self._index), n=1, cutoff=0.6)
        if close:
            return Match(None, self._index[close[0]].name)
        return Match(None)

    def size(self) -> int:
        self._refresh()
        return len(self._teams)
