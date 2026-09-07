import gzip
import json

import pytest

from portal.slots import Meta, SlotError, SlotKey, SlotService, prepare
from portal.storage import Conflict, LocalStorage

META = Meta(llm="llm-a", retriever="bm25", system_type="Agentic", submitter_email="a@x.org")


def report(warnings=0, records=50):
    fr = {"records": records, "warnings": warnings, "errors": 0,
          "findings": [{"kind": "docid.unknown", "level": "warning"}] * warnings}
    return {"validator": {"name": "mast-validate", "version": "0.1.0"}, "status": "ok", "files": [fr]}


def prepared(tmp_path, content: bytes, gz=False, name="hi.jsonl", warnings=0):
    p = tmp_path / name
    p.write_bytes(gzip.compress(content) if gz else content)
    return prepare(p, name, report(warnings))


def test_prepare_normalizes_to_gzip_and_hashes_content(tmp_path):
    a = prepared(tmp_path, b'{"a":1}\n')
    b = prepared(tmp_path, b'{"a":1}\n', gz=True, name="hi.jsonl.gz")
    assert a.content_sha256 == b.content_sha256 and a.stored_sha256 == b.stored_sha256
    assert gzip.decompress(a.gz_bytes) == b'{"a":1}\n' and a.size_bytes == 8


def test_fill_replace_dedupe_and_cap(tmp_path):
    st = LocalStorage(tmp_path / "store")
    svc = SlotService(st, max_slots=3, max_uploads=5)
    key = SlotKey("indic", "hi", "waterloo-nlp")
    r1 = svc.submit(key, "Waterloo NLP", prepared(tmp_path, b"run1\n"), META)
    r2 = svc.submit(key, "Waterloo NLP", prepared(tmp_path, b"run2\n"), META)
    r3 = svc.submit(key, "Waterloo NLP", prepared(tmp_path, b"run3\n"), META)
    assert [r1["slot"], r2["slot"], r3["slot"]] == [1, 2, 3]
    m, _ = svc.manifest(key)
    assert sorted(svc.filled_slots(m)) == [1, 2, 3] and m["uploads"] == 3
    assert svc.next_free_slot(m) is None
    with pytest.raises(SlotError, match="All 3 slots"):
        svc.submit(key, "Waterloo NLP", prepared(tmp_path, b"run4\n"), META)
    with pytest.raises(SlotError, match="identical"):
        svc.submit(key, "Waterloo NLP", prepared(tmp_path, b"run2\n"), META, replace_slot=1)
    with pytest.raises(SlotError, match="empty"):
        SlotService(st, max_slots=4).submit(key, "Waterloo NLP", prepared(tmp_path, b"run4\n"), META, replace_slot=4)
    r4 = svc.submit(key, "Waterloo NLP", prepared(tmp_path, b"run4\n", warnings=2), META, replace_slot=2)
    assert r4["slot"] == 2 and r4["replaced"]["receipt_id"] == r2["receipt_id"] and r4["warnings"] == 2
    assert gzip.decompress(st.read(f"{key.slot_dir(2)}/run.jsonl.gz")) == b"run4\n"
    receipts = st.list_files(f"receipts/indic/hi/waterloo-nlp")
    assert len(receipts) == 4  # the replaced upload's receipt survives
    assert json.loads(st.read(f"{key.slot_dir(2)}/receipt.json"))["receipt_id"] == r4["receipt_id"]
    svc.submit(key, "Waterloo NLP", prepared(tmp_path, b"run5\n"), META, replace_slot=1)
    with pytest.raises(SlotError, match="cap"):
        svc.submit(key, "Waterloo NLP", prepared(tmp_path, b"run6\n"), META, replace_slot=1)


def test_languages_submitted(tmp_path):
    st = LocalStorage(tmp_path / "store")
    svc = SlotService(st)
    svc.submit(SlotKey("indic", "hi", "t"), "T", prepared(tmp_path, b"a\n"), META)
    svc.submit(SlotKey("indic", "gu", "t"), "T", prepared(tmp_path, b"b\n"), META)
    svc.submit(SlotKey("indic", "hi", "other"), "O", prepared(tmp_path, b"c\n"), META)
    assert svc.languages_submitted("indic", "t") == {"hi", "gu"}
    assert svc.languages_submitted("multilingual", "t") == set()


def test_conflict_retries_from_fresh_state(tmp_path, monkeypatch):
    st = LocalStorage(tmp_path / "store")
    svc = SlotService(st, attempts=3)
    key = SlotKey("indic", "hi", "t")
    real = st.commit
    calls = {"n": 0}

    def flaky(ops, message, parent=None):
        calls["n"] += 1
        if calls["n"] == 1:
            # someone else fills slot 1 in between
            real([type(ops[3])(ops[3].path, json.dumps({"slots": {"1": {"content_sha256": "zzz"}}, "uploads": 1}))], "race")
            raise Conflict("stale")
        return real(ops, message, parent)

    monkeypatch.setattr(st, "commit", flaky)
    r = svc.submit(key, "T", prepared(tmp_path, b"a\n"), META)
    assert r["slot"] == 2 and calls["n"] == 2  # re-read saw slot 1 taken


def test_local_storage_cas(tmp_path):
    st = LocalStorage(tmp_path)
    from portal.storage import Add
    _, rev0 = st.read_versioned("manifests/x.json")
    st.commit([Add("manifests/x.json", "{}")], "a", parent=rev0)
    with pytest.raises(Conflict):
        st.commit([Add("manifests/x.json", "{}")], "b", parent=rev0)
    with pytest.raises(ValueError):
        st.read("../escape")


def test_rejected_name_logged(tmp_path):
    st = LocalStorage(tmp_path)
    SlotService(st).log_rejected_name("Nobody", "indic", "hi")
    files = st.list_files("rejected")
    assert len(files) == 1 and json.loads(st.read(files[0]))["name"] == "Nobody"
