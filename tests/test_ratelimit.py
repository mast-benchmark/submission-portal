import pytest

from portal.config import Settings
from portal.ratelimit import Limit, Limiter, client_address, parse_limit


def test_parse():
    assert parse_limit("20/600", "1/1") == Limit(20, 600)
    assert parse_limit(None, "10/3600") == Limit(10, 3600)
    assert parse_limit("off", "1/1") == Limit(0, 0) and not parse_limit("0", "1/1").enabled
    with pytest.raises(ValueError):
        parse_limit("twenty", "1/1")
    assert Limit(20, 600).describe() == "20 per 10 minutes" and Limit(10, 3600).describe() == "10 per 1 hour"


def test_sliding_window():
    t = [1000.0]
    lim = Limiter(Limit(3, 60), clock=lambda: t[0])
    assert [lim.hit("a")[0] for _ in range(3)] == [True, True, True]
    allowed, retry = lim.hit("a")
    assert not allowed and 1 <= retry <= 61
    assert lim.hit("b")[0]                       # other key unaffected
    t[0] += 30
    assert not lim.hit("a")[0]                   # still within the window
    t[0] += 31
    assert lim.hit("a")[0]                       # oldest event expired
    t[0] += 1000
    lim.hit("c")                                 # triggers prune of idle keys
    assert lim.keys() <= 2


def test_disabled_and_unknown_key():
    lim = Limiter(Limit(0, 0))
    assert all(lim.hit("a")[0] for _ in range(100))
    assert Limiter(Limit(1, 60)).hit("")[0] and Limiter(Limit(1, 60)).hit("")[0]


class _Req:
    def __init__(self, headers=None, host=None):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})() if host else None


def test_client_address():
    assert client_address(None) == ""
    assert client_address(_Req({"x-forwarded-for": "1.2.3.4, 10.0.0.1"})) == "1.2.3.4"
    assert client_address(_Req(host="9.9.9.9")) == "9.9.9.9"
    assert client_address(_Req()) == ""


def test_settings_env(monkeypatch):
    monkeypatch.setenv("RATE_VALIDATE_PER_IP", "5/60")
    monkeypatch.setenv("RATE_RECORD_PER_TEAM", "off")
    monkeypatch.delenv("RATE_RECORD_PER_IP", raising=False)
    s = Settings.from_env()
    assert s.rate_validate_per_ip == Limit(5, 60) and not s.rate_record_per_team.enabled and s.rate_record_per_ip == Limit(10, 3600)


def test_bad_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("RATE_VALIDATE_PER_IP", "lots")
    assert Settings.from_env().rate_validate_per_ip == Limit(20, 600)
