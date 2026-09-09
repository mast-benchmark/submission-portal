from portal.registrations import rows_to_teams, tracks_from_cell
from portal.roster import Roster
from portal.storage import Add, LocalStorage

HEAD = ["Timestamp", "Team Name", "Member #1 (Full Name, Affiliation)\ne.g. Nandan Thakur, University of Waterloo",
        "Member #1 Email Address", "Member #2 (Full Name, Affiliation)", "Member #2 Email Address",
        "Member #3 (Full Name, Affiliation)", "Member #3 Email Address", "Member #4 (Full Name, Affiliation)",
        "Member #4 Email Address", "Which MAST Track will you participate in?\n(You can participate in both tracks)",
        "Any Feedback or Questions?"]
ML = "MAST Multilingual (16 diverse languages: Chinese, German, Yoruba, ...)"
IN = "Mast Indic (10 Indian languages: Hindi, Kannada, Bengali, Punjabi, ....)"
ROWS = [HEAD,
        ["8/17/2026 23:34:00", "ZeroOne", "A", "a@x.org", "B", "b@x.org", "", "", "", "", ML, ""],
        ["8/18/2026 0:03:05", "ZeroOne", "A", "a@x.org", "B", "b@x.org", "C", "c@x.org", "", "", IN],  # short row: the API drops trailing blanks
        ["8/19/2026 0:03:05", "ZeroOne", "A", "a@x.org", "D", "d@x.org"],                                  # re-registration without a track still merges
        ["8/20/2026 12:52:56", "BhashaVidya", "P, JU", "p@ju.in", "", "", "", "", "", "", IN, ""],
        ["8/21/2026 1:00:00", "No Track", "X", "x@x.org", "", "", "", "", "", "", "", ""],
        ["8/22/2026 1:00:00", "", "", "", "", "", "", "", "", "", ML, ""]]


def test_tracks_from_cell():
    assert tracks_from_cell(f"{ML}, {IN}") == ["multilingual", "indic"] and tracks_from_cell(IN) == ["indic"] and tracks_from_cell("") == []


def test_rows_to_teams_merges_and_skips():
    teams, notes = rows_to_teams(ROWS, {"BhashaVidya": "bhasha vidya;BV"})
    by = {t.name: t for t in teams}
    assert set(by) == {"ZeroOne", "BhashaVidya"}
    assert by["ZeroOne"].member_emails == ["a@x.org", "b@x.org", "c@x.org", "d@x.org"] and by["ZeroOne"].tracks == ["multilingual", "indic"]
    assert by["BhashaVidya"].aliases == ["bhasha vidya", "BV"] and by["BhashaVidya"].tracks == ["indic"]
    assert any("duplicate registration merged" in n for n in notes) and any("No Track" in n for n in notes)


def test_rows_to_teams_bad_headers():
    assert rows_to_teams([["a", "b"], ["1", "2"]]) == ([], ["unexpected headers: ['a', 'b']..."])


class FakeSheet:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.calls = rows, fail, 0

    def fetch(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("sheet down")
        return self.rows


def test_roster_from_sheet_and_fallbacks(tmp_path):
    st = LocalStorage(tmp_path)
    st.commit([Add("teams.csv", "team_name,contact_email,member_emails,tracks,aliases,registered_at\nCsvTeam,c@x.org,c@x.org,indic,,t\n"),
               Add("aliases.csv", "team_name,aliases\nZeroOne,zero one;01\n")], "seed")
    sheet = FakeSheet(ROWS)
    r = Roster(st, ttl_seconds=0, sheet=sheet)
    assert r.match("zeroone").team.name == "ZeroOne" and r.match("01").team.name == "ZeroOne" and r.source == "sheet"
    assert r.match("CsvTeam").team is None                     # the sheet is the source, not the csv
    sheet.fail = True
    assert r.match("ZeroOne").team is not None and "sheet down" in r.last_error   # last good roster kept
    r2 = Roster(st, ttl_seconds=0, sheet=FakeSheet(fail=True))
    assert r2.match("CsvTeam").team.name == "CsvTeam" and r2.source == "teams.csv"  # nothing cached: csv fallback
    r3 = Roster(st, ttl_seconds=0)
    assert r3.match("CsvTeam").team is not None and r3.source == "teams.csv"
