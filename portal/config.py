"""Environment-driven settings. Everything the Space needs is a variable or a secret."""
from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from .ratelimit import Limit, parse_limit

DEFAULT_DEADLINE = "2026-09-16T11:59:59+00:00"   # Sep 15, 2026, end of day AoE (UTC-12)
ORGANIZER_EMAIL = "mast-organizers@googlegroups.com"
FIRE_URL = "http://fire.irsi.res.in/"
SITE_URL = "https://mast-benchmark.github.io/"


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _limit(name: str, default: str) -> Limit:
    try:
        return parse_limit(_env(name), default)
    except ValueError as exc:
        logging.getLogger("portal.config").warning("%s: %s; using default %s", name, exc, default)
        return parse_limit(default, default)


@dataclass
class Settings:
    storage_backend: str = "local"                 # "local" | "hf"
    storage_root: str = "local-store"              # local backend
    submissions_repo: str = "mast-benchmark/mast-2026-submissions"
    hf_token: Optional[str] = None
    deadline: dt.datetime = field(default_factory=lambda: dt.datetime.fromisoformat(DEFAULT_DEADLINE))
    max_slots: int = 3
    max_uploads_per_key: int = 10                  # (team, track, lang) lifetime cap, keeps git history finite
    roster_ttl_seconds: int = 60
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    commit_attempts: int = 5
    rate_validate_per_ip: Limit = Limit(20, 600)     # RATE_VALIDATE_PER_IP, e.g. '20/600' or 'off'
    rate_record_per_ip: Limit = Limit(10, 3600)      # RATE_RECORD_PER_IP
    rate_record_per_team: Limit = Limit(15, 3600)    # RATE_RECORD_PER_TEAM
    google_service_account_json: Optional[str] = None  # secret: the service-account key JSON, verbatim
    registration_sheet_id: Optional[str] = None        # variable: id from the responses sheet URL
    registration_sheet_range: str = "Form Responses 1"  # variable: tab name (or A1 range)

    @property
    def email_enabled(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    def is_closed(self, now: Optional[dt.datetime] = None) -> bool:
        now = now or dt.datetime.now(dt.timezone.utc)
        return now > self.deadline

    @classmethod
    def from_env(cls) -> "Settings":
        deadline_raw = _env("MAST_DEADLINE_UTC", DEFAULT_DEADLINE)
        deadline = dt.datetime.fromisoformat(deadline_raw.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=dt.timezone.utc)
        return cls(
            storage_backend=_env("STORAGE_BACKEND", "hf" if _env("HF_TOKEN") else "local"),
            storage_root=_env("STORAGE_ROOT", "local-store"),
            submissions_repo=_env("SUBMISSIONS_REPO", "mast-benchmark/mast-2026-submissions"),
            hf_token=_env("HF_TOKEN"),
            deadline=deadline,
            max_uploads_per_key=int(_env("MAX_UPLOADS_PER_SLOT_KEY", "10")),
            roster_ttl_seconds=int(_env("ROSTER_TTL_SECONDS", "60")),
            smtp_host=_env("SMTP_HOST"),
            smtp_port=int(_env("SMTP_PORT", "587")),
            smtp_user=_env("SMTP_USER"),
            smtp_password=_env("SMTP_PASSWORD"),
            smtp_from=_env("SMTP_FROM"),
            rate_validate_per_ip=_limit("RATE_VALIDATE_PER_IP", "20/600"),
            rate_record_per_ip=_limit("RATE_RECORD_PER_IP", "10/3600"),
            rate_record_per_team=_limit("RATE_RECORD_PER_TEAM", "15/3600"),
            google_service_account_json=_env("GOOGLE_SERVICE_ACCOUNT_JSON"),
            registration_sheet_id=_env("REGISTRATION_SHEET_ID"),
            registration_sheet_range=_env("REGISTRATION_SHEET_RANGE", "Form Responses 1"),
        )
