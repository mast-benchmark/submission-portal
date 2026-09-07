import os
import time

from portal import tmpfiles


def test_sweep_by_age(tmp_path, monkeypatch):
    monkeypatch.setattr(tmpfiles, "ROOT", tmp_path / "t")
    old = tmpfiles.new_path("a-", ".json"); new = tmpfiles.new_path("b-", ".json")
    past = time.time() - 7200
    os.utime(old, (past, past))
    assert tmpfiles.sweep(3600) == 1
    assert not old.exists() and new.exists()
    tmpfiles.remove([new, tmp_path / "missing"])
    assert not new.exists()
