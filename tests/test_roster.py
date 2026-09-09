from portal.roster import Roster, normalize_name, parse_roster, slugify
from portal.storage import Add, LocalStorage

CSV = """team_name,track,contact_email,member_emails,aliases,registered_at
Waterloo NLP,multilingual,a@uw.ca,a@uw.ca;b@uw.ca,UW NLP;Waterloo-NLP,2026-06-11
Waterloo NLP,indic,c@uw.ca,c@uw.ca,,2026-07-02
Team Bengaluru,indic,x@iisc.in,x@iisc.in,,2026-07-01
"""
LEGACY = """team_name,contact_email,member_emails,tracks,aliases,registered_at
Old Team,o@x.org,o@x.org;p@x.org,multilingual;indic,,t
"""


def test_normalize_and_slug():
    assert normalize_name("  Team  Waterloo-NLP! ") == "waterloo nlp"
    assert normalize_name("TEAM") == "team"
    assert normalize_name("mast_agentic_ai") == normalize_name("mast agentic ai") == "mast agentic ai"
    assert slugify("Waterloo NLP") == "waterloo-nlp" and slugify("mast_agentic_ai") == "mast-agentic-ai"


def test_parse_per_track():
    teams = {t.name: t for t in parse_roster(CSV)}
    w = teams["Waterloo NLP"]
    assert w.tracks == ["multilingual", "indic"] and w.aliases == ["UW NLP", "Waterloo-NLP"]
    assert w.emails_for("multilingual") == {"a@uw.ca", "b@uw.ca"} and w.emails_for("indic") == {"c@uw.ca"}
    assert w.contact_for("indic") == "c@uw.ca" and w.knows_email(" B@UW.CA ", "multilingual") and not w.knows_email("b@uw.ca", "indic")
    assert w.notify_emails("multilingual") == ["a@uw.ca"] and w.notify_emails("nope") == []


def test_parse_legacy_layout():
    t = parse_roster(LEGACY)[0]
    assert t.tracks == ["multilingual", "indic"] and t.emails_for("indic") == {"o@x.org", "p@x.org"}


def test_match_exact_alias_and_suggestion(tmp_path):
    st = LocalStorage(tmp_path)
    st.commit([Add("teams.csv", CSV)], "roster")
    r = Roster(st, ttl_seconds=0)
    assert r.match("waterloo nlp").team.name == "Waterloo NLP"
    assert r.match("Team Waterloo NLP").team.name == "Waterloo NLP"
    assert r.match("uw-nlp").team.name == "Waterloo NLP"
    assert r.match("Bengaluru").team.name == "Team Bengaluru"
    assert r.match("NLP Waterloo").team is None                     # word order matters
    assert r.match("waterloo, nlp!").team.name == "Waterloo NLP"     # punctuation and case do not
    m = r.match("Waterlo NLP")
    assert m.team is None and m.suggestion == "Waterloo NLP"
    assert r.match("Completely Different").team is None and r.match("").team is None


def test_roster_refreshes_after_update(tmp_path):
    st = LocalStorage(tmp_path)
    st.commit([Add("teams.csv", CSV)], "roster")
    r = Roster(st, ttl_seconds=0)
    assert r.match("Late Team").team is None
    st.commit([Add("teams.csv", CSV + "Late Team,multilingual,l@x.org,l@x.org,,2026-09-15\n")], "roster")
    assert r.match("late team").team.name == "Late Team"


def test_missing_roster_is_empty(tmp_path):
    r = Roster(LocalStorage(tmp_path), ttl_seconds=0)
    assert r.size() == 0 and r.match("anyone").team is None
