import datetime as dt

from portal.config import Settings


def test_defaults_and_deadline(monkeypatch):
    for k in ("HF_TOKEN", "STORAGE_BACKEND", "MAST_DEADLINE_UTC", "SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    s = Settings.from_env()
    assert s.storage_backend == "local" and not s.email_enabled
    assert s.deadline == dt.datetime(2026, 9, 16, 11, 59, 59, tzinfo=dt.timezone.utc)
    assert not s.is_closed(dt.datetime(2026, 9, 16, 11, 0, tzinfo=dt.timezone.utc))
    assert s.is_closed(dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.timezone.utc))


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    monkeypatch.setenv("MAST_DEADLINE_UTC", "2026-09-20T00:00:00Z")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.org")
    monkeypatch.setenv("SMTP_FROM", "mast@example.org")
    s = Settings.from_env()
    assert s.storage_backend == "hf" and s.email_enabled
    assert s.deadline.isoformat() == "2026-09-20T00:00:00+00:00"
