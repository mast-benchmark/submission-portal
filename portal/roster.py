"""Team roster, cached ~60 s, matched by normalized name or alias.

Sources, in order of preference: the registration sheet read live through a
Google service account; ``responses.csv`` in the private dataset, a raw export
pushed by an Apps Script on every form submission (no Google Cloud needed);
and ``teams.csv``, a manual import. All keep the list private.

Normalization ignores case, punctuation, repeated spaces and a leading "team ";
word order is significant ("Sahel Test" and "Test Sahel" are different teams).
The roster is never rendered. A miss yields at most one "did you mean" suggestion.
"""
from __future__ import annotations

import csv
import difflib
import io
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

ROSTER_PATH = "teams.csv"
RESPONSES_PATH = "responses.csv"   # raw form export pushed by the Apps Script (see docs); parsed with registrations.rows_to_teams
ALIASES_PATH = "aliases.csv"   # optional: team_name,aliases (";"-separated), hand-maintained in the private dataset
log = logging.getLogger("portal.roster")
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
    """One registered team. Membership is per track: the latest registration row for a
    (team, track) pair defines that track's eligible submitters, replacing earlier rows."""
    name: str
    members: dict[str, list[str]] = field(default_factory=dict)   # track -> emails, first one is the contact
    registered_at: dict[str, str] = field(default_factory=dict)   # track -> timestamp of the defining row
    aliases: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def tracks(self) -> list[str]:
        return list(self.members)

    def emails_for(self, track: str) -> set[str]:
        return {e.strip().lower() for e in self.members.get(track, []) if e and e.strip()}

    def contact_for(self, track: str) -> Optional[str]:
        lst = self.members.get(track) or []
        return lst[0] if lst else None

    def knows_email(self, email: str, track: str) -> bool:
        return (email or "").strip().lower() in self.emails_for(track)

    def notify_emails(self, track: str) -> list[str]:
        c = self.contact_for(track)
        return [c] if c else []


@dataclass
class Match:
    team: Optional[Team]
    suggestion: Optional[str] = None


def parse_roster(text: str) -> list[Team]:
    """``teams.csv``: one row per (team, track): team_name, track, contact_email, member_emails, aliases, registered_at.

    The legacy one-row-per-team layout (a ``tracks`` column) is accepted too: its emails apply to every track.
    """
    reader = csv.DictReader(io.StringIO(text))
    by_key: dict[str, Team] = {}
    split = lambda v: [x.strip() for x in re.split(r"[;,]", v or "") if x.strip()]  # noqa: E731
    for row in reader:
        name = (row.get("team_name") or "").strip()
        if not name:
            continue
        key = normalize_name(name)
        team = by_key.setdefault(key, Team(name=name))
        team.name = name
        emails = split(row.get("member_emails"))
        contact = (row.get("contact_email") or "").strip()
        if contact and contact not in emails:
            emails.insert(0, contact)
        tracks = [t.lower() for t in split(row.get("track") or row.get("tracks"))]
        for track in tracks:
            team.members[track] = emails
            team.registered_at[track] = (row.get("registered_at") or "").strip()
        for a in re.split(r";", row.get("aliases") or ""):
            if a.strip() and a.strip() not in team.aliases:
                team.aliases.append(a.strip())
    return list(by_key.values())


class SheetSource:
    """Reads the registration responses sheet through a Google service account (read-only scope)."""

    SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

    def __init__(self, sheet_id: str, range_: str, service_account_info: dict, timeout: int = 20) -> None:
        self.sheet_id, self.range_, self.info, self.timeout = sheet_id, range_, service_account_info, timeout
        self._creds = None

    def _token(self) -> str:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account

        if self._creds is None:
            self._creds = service_account.Credentials.from_service_account_info(self.info, scopes=[self.SCOPE])
        if not self._creds.valid:
            self._creds.refresh(Request())
        return self._creds.token

    def fetch(self) -> list[list[str]]:
        import requests
        from urllib.parse import quote

        url = f"https://sheets.googleapis.com/v4/spreadsheets/{self.sheet_id}/values/{quote(self.range_, safe='')}"
        r = requests.get(url, headers={"Authorization": f"Bearer {self._token()}"}, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("values", [])

    @classmethod
    def from_settings(cls, settings) -> "Optional[SheetSource]":
        if not (settings.google_service_account_json and settings.registration_sheet_id):
            return None
        info = json.loads(settings.google_service_account_json)
        return cls(settings.registration_sheet_id, settings.registration_sheet_range, info)


class Roster:
    def __init__(self, storage, ttl_seconds: int = 60, sheet: "Optional[SheetSource]" = None) -> None:
        self.storage = storage
        self.ttl = ttl_seconds
        self.sheet = sheet
        self._teams: list[Team] = []
        self._index: dict[str, Team] = {}
        self._loaded_at = 0.0
        self.source = "none"
        self.last_error: Optional[str] = None

    def _aliases(self) -> dict[str, str]:
        raw = self.storage.read(ALIASES_PATH)
        if not raw:
            return {}
        return {(r.get("team_name") or "").strip(): (r.get("aliases") or "") for r in csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))}

    def _load(self) -> list[Team]:
        if self.sheet is not None:
            try:
                from .registrations import rows_to_teams

                teams, notes = rows_to_teams(self.sheet.fetch(), self._aliases())
                for n in notes:
                    log.info("registration sheet: %s", n)
                self.source, self.last_error = "sheet", None
                return teams
            except Exception as exc:  # keep serving the last good roster, or fall back to the CSV
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("registration sheet unavailable (%s); using %s", self.last_error,
                            "previous roster" if self._teams else ROSTER_PATH)
                if self._teams:
                    return self._teams
        raw = self.storage.read(RESPONSES_PATH)
        if raw:
            try:
                from .registrations import rows_to_teams

                rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
                teams, notes = rows_to_teams(rows, self._aliases())
                for n in notes:
                    log.info("responses.csv: %s", n)
                if teams:
                    self.source = RESPONSES_PATH
                    return teams
            except Exception as exc:
                log.warning("responses.csv unreadable (%s); falling back to %s", exc, ROSTER_PATH)
        raw = self.storage.read(ROSTER_PATH)
        self.source = ROSTER_PATH
        return parse_roster(raw.decode("utf-8-sig")) if raw else []

    def _refresh(self, force: bool = False) -> None:
        if not force and time.time() - self._loaded_at < self.ttl:
            return
        teams = self._load()
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
