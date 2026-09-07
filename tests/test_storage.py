import pytest

from portal.storage import Add, Conflict, LocalStorage


def test_revision_cas(tmp_path):
    st = LocalStorage(tmp_path)
    _, rev0 = st.read_versioned("manifests/x.json")
    assert rev0 == "0"
    rev1 = st.commit([Add("manifests/x.json", "{}")], "a", parent=rev0)
    assert rev1 == "1"
    with pytest.raises(Conflict):
        st.commit([Add("manifests/x.json", "{}")], "b", parent=rev0)
    st.commit([Add("other.txt", b"x")], "no parent check")
    files, rev = st.read_many(["manifests/x.json", "missing.json", "other.txt"])
    assert files == {"manifests/x.json": b"{}", "missing.json": None, "other.txt": b"x"} and rev == "2"
    with pytest.raises(ValueError):
        st.read("../escape")
