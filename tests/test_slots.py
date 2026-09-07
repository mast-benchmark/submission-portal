import gzip
import json

import pytest

from portal.slots import Meta, SlotError, SlotKey, SlotService, prepare
from portal.storage import Conflict, LocalStorage

META = Meta(system_type="Agentic", submitter_email="a@x.org")


def report(warnings=0, records=50):
    fr = {"records": records, "warnings": warnings, "errors": 0, "llm": "llm-a", "retriever": "bm25",
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
    assert gzip.decompress(a.read_gz()) == b'{"a":1}\n' and a.size_bytes == 8
    assert a.gz_path.is_file() and a.read_gz() == b.read_gz()  # deterministic bytes on disk
    assert a.llm == "llm-a" and a.retriever == "bm25"


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
    assert r4["llm"] == "llm-a" and r4["retriever"] == "bm25"
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
            # someone else fills slot 1 in between: bumps the revision, so the planned commit conflicts
            real([type(ops[3])(ops[3].path, json.dumps({"slots": {"1": {"content_sha256": "zzz"}}, "uploads": 1}))], "race")
        return real(ops, message, parent)

    monkeypatch.setattr(st, "commit", flaky)
    r = svc.submit(key, "T", prepared(tmp_path, b"a\n"), META)
    assert r["slot"] == 2 and calls["n"] == 2  # re-read saw slot 1 taken


def bulk_items(tmp_path, langs, tag=b"v1", warnings=0):
    return {l: prepared(tmp_path, tag + b" " + l.encode() + b"\n", name=f"{l}.jsonl", warnings=warnings) for l in langs}


def test_submit_many_fills_skips_and_reports(tmp_path):
    st = LocalStorage(tmp_path / "store")
    svc = SlotService(st, max_slots=3, max_uploads=10)
    items = bulk_items(tmp_path, ["bn", "gu", "hi"])
    s1 = svc.submit_many("indic", "t", "T", items, META)
    assert [(i["language"], i["action"], i["slot"]) for i in s1["items"]] == [("bn", "slot", 1), ("gu", "slot", 1), ("hi", "slot", 1)]
    assert len(st.list_files("receipts/indic/bulk/t")) == 1 and len(st.list_files("slots/indic")) == 6
    # identical re-upload: nothing written, no new bulk receipt
    s2 = svc.submit_many("indic", "t", "T", items, META)
    assert {i["action"] for i in s2["items"]} == {"unchanged"} and len(st.list_files("receipts/indic/bulk/t")) == 1
    # fill hi to 3, then a zip with a new hi + a new gu: hi skipped (full), gu to slot 2
    svc.submit(SlotKey("indic", "hi", "t"), "T", prepared(tmp_path, b"hi2\n"), META)
    svc.submit(SlotKey("indic", "hi", "t"), "T", prepared(tmp_path, b"hi3\n"), META)
    s3 = svc.submit_many("indic", "t", "T", bulk_items(tmp_path, ["gu", "hi"], tag=b"v2"), META)
    acts = {i["language"]: (i["action"], i["slot"]) for i in s3["items"]}
    assert acts == {"gu": ("slot", 2), "hi": ("skipped_full", None)}
    # same zip with replace_oldest: gu unchanged, hi replaces its oldest (slot 1)
    s4 = svc.submit_many("indic", "t", "T", bulk_items(tmp_path, ["gu", "hi"], tag=b"v2"), META, replace_oldest=True)
    acts = {i["language"]: (i["action"], i["slot"]) for i in s4["items"]}
    assert acts == {"gu": ("unchanged", 2), "hi": ("replace", 1)} and s4["items"][1]["replaced"]["receipt_id"]
    m, _ = svc.manifest(SlotKey("indic", "hi", "t"))
    assert m["uploads"] == 4 and gzip.decompress(st.read("slots/indic/hi/t/1/run.jsonl.gz")) == b"v2 hi\n"
    assert len(st.list_files("receipts/indic/hi/t")) == 4  # every hi upload kept


def test_submit_many_is_atomic_on_conflict(tmp_path, monkeypatch):
    st = LocalStorage(tmp_path / "store")
    svc = SlotService(st, attempts=2)
    calls = {"n": 0}
    real = st.commit

    def always_conflict(ops, message, parent=None):
        calls["n"] += 1
        raise Conflict("busy")

    monkeypatch.setattr(st, "commit", always_conflict)
    with pytest.raises(SlotError, match="Nothing was recorded"):
        svc.submit_many("indic", "t", "T", bulk_items(tmp_path, ["bn", "gu"]), META)
    monkeypatch.setattr(st, "commit", real)
    assert calls["n"] == 2 and st.list_files("slots") == []


def test_submit_many_nothing_to_write_is_not_stored(tmp_path):
    st = LocalStorage(tmp_path / "store")
    svc = SlotService(st)
    items = bulk_items(tmp_path, ["bn"])
    assert svc.submit_many("indic", "t", "T", items, META)["stored"] is True
    again = svc.submit_many("indic", "t", "T", items, META)
    assert again["stored"] is False and len(st.list_files("receipts/indic/bulk")) == 1


def test_manifests_read_at_one_revision(tmp_path):
    st = LocalStorage(tmp_path / "store")
    svc = SlotService(st)
    svc.submit(SlotKey("indic", "hi", "t"), "T", prepared(tmp_path, b"a\n"), META)
    ms, rev = svc.manifests("indic", "t", "T", ["hi", "gu"])
    assert set(ms) == {"hi", "gu"} and ms["hi"]["uploads"] == 1 and ms["gu"]["uploads"] == 0 and rev == "1"
