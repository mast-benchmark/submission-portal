"""pytest ops/test_import_roster.py  (private helper; not part of either public repo)."""
import csv
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HEADERS = ["Timestamp", "Team Name", "Member #1 (Full Name, Affiliation)", "Member #1 Email Address",
           "Member #2 (Full Name, Affiliation)", "Member #2 Email Address", "Member #3 (Full Name, Affiliation)",
           "Member #3 Email Address", "Member #4 (Full Name, Affiliation)", "Member #4 Email Address",
           "Which MAST Track will you participate in?", "Any Feedback or Questions?"]
ML = "MAST Multilingual (16 diverse languages: Chinese, German, Yoruba, ...)"
IN = "Mast Indic (10 Indian languages: Hindi, Kannada, Bengali, Punjabi, ....)"


def write_responses(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(HEADERS); w.writerows(rows)


def run(*args):
    return subprocess.run([sys.executable, str(HERE / "import_roster.py"), *map(str, args)], capture_output=True, text=True)


def test_latest_row_per_track_and_alias_preservation(tmp_path):
    src = tmp_path / "responses.csv"; out = tmp_path / "teams.csv"
    write_responses(src, [
        ["2026/08/17 23:34:00", "ZeroOne", "A", "a@x.org", "B", "b@x.org", "", "", "", "", f"{ML}, {IN}", ""],
        ["2026/08/18 00:03:05", "ZeroOne", "A", "a@x.org", "C", "c@x.org", "", "", "", "", IN, ""],
        ["2026/07/01 09:00:00", "Bengaluru", "X, IISc", "x@iisc.in", "", "", "", "", "", "", IN, "hi"],
        ["2026/07/02 09:00:00", "No Track", "Y", "y@x.org", "", "", "", "", "", "", "", ""],
    ])
    r = run(src, "--out", out)
    assert r.returncode == 0 and "supersedes row 2" in r.stdout and "No Track" in r.stdout
    rows = {(r["team_name"], r["track"]): r for r in csv.DictReader(open(out))}
    assert rows[("ZeroOne", "multilingual")]["member_emails"] == "a@x.org;b@x.org"
    assert rows[("ZeroOne", "indic")]["member_emails"] == "a@x.org;c@x.org" and rows[("ZeroOne", "indic")]["registered_at"] == "2026/08/18 00:03:05"
    assert ("No Track", "indic") not in rows and len(rows) == 3
    # an organizer adds an alias by hand; re-import keeps it
    lines = open(out).read().replace("Bengaluru,indic,x@iisc.in,x@iisc.in,,", "Bengaluru,indic,x@iisc.in,x@iisc.in,IISc team,")
    open(out, "w").write(lines)
    assert run(src, "--out", out).returncode == 0
    rows = {(r["team_name"], r["track"]): r for r in csv.DictReader(open(out))}
    assert rows[("Bengaluru", "indic")]["aliases"] == "IISc team"
