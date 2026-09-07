import json

from portal.rejected import RejectedLog
from portal.storage import LocalStorage


def test_buffered_flush_dedup_and_cap(tmp_path):
    st = LocalStorage(tmp_path)
    log = RejectedLog(st, flush_seconds=10_000, flush_at=1000, max_distinct=3, background=False)
    for name in ["Nobody", "nobody!", "Team Nobody", "Other", "Third", "Fourth", "Fifth"]:
        log.add(name, "indic")
    assert st.list_files("rejected") == []                    # nothing written yet: no commit per event
    assert log.flush() is True and log.flush() is False
    files = st.list_files("rejected")
    assert len(files) == 1
    payload = json.loads(st.read(files[0]))
    names = {e["name"]: e["count"] for e in payload["entries"]}
    assert names == {"Nobody": 3, "Other": 1, "Third": 1} and payload["dropped_distinct_names"] == 2


def test_flush_at_threshold(tmp_path):
    st = LocalStorage(tmp_path)
    log = RejectedLog(st, flush_seconds=10_000, flush_at=2, background=False)
    log.add("A", "indic")
    assert st.list_files("rejected") == []
    log.add("B", "indic")
    assert len(st.list_files("rejected")) == 1 and log.flushes == 1
