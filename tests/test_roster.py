from portal.roster import Roster, normalize_name, parse_roster, slugify
from portal.storage import Add, LocalStorage

CSV = """team_name,contact_email,member_emails,tracks,aliases,registered_at
Waterloo NLP,a@uw.ca,a@uw.ca;b@uw.ca,multilingual;indic,UW NLP;Waterloo-NLP,2026-06-11
Team Bengaluru,x@iisc.in,x@iisc.in,indic,,2026-07-01
"""


def test_normalize_and_slug():
    assert normalize_name("  Team  Waterloo-NLP! ") == "waterloo nlp"
    assert normalize_name("TEAM") == "team"  # a team literally called Team keeps its name
    assert normalize_name("mast_agentic_ai") == normalize_name("mast agentic ai") == "mast agentic ai"
    assert slugify("mast_agentic_ai") == "mast-agentic-ai"
    assert slugify("Waterloo NLP") == "waterloo-nlp"
    assert slugify("Team Bengaluru") == "bengaluru"


def test_parse():
    teams = parse_roster(CSV)
    assert [t.name for t in teams] == ["Waterloo NLP", "Team Bengaluru"]
    assert teams[0].tracks == ["multilingual", "indic"] and teams[0].aliases == ["UW NLP", "Waterloo-NLP"]
    assert teams[0].member_emails == ["a@uw.ca", "b@uw.ca"]


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
    m = r.match("Completely Different")
    assert m.team is None and m.suggestion is None
    assert r.match("").team is None


def test_roster_refreshes_after_update(tmp_path):
    st = LocalStorage(tmp_path)
    st.commit([Add("teams.csv", CSV)], "roster")
    r = Roster(st, ttl_seconds=0)
    assert r.match("Late Team").team is None
    st.commit([Add("teams.csv", CSV + "Late Team,l@x.org,l@x.org,multilingual,,2026-09-15\n")], "roster")
    assert r.match("late team").team.name == "Late Team"


def test_missing_roster_is_empty(tmp_path):
    r = Roster(LocalStorage(tmp_path), ttl_seconds=0)
    assert r.size() == 0 and r.match("anyone").team is None


def test_registered_emails():
    t = parse_roster(CSV)[0]
    assert t.all_emails == {"a@uw.ca", "b@uw.ca"}
    assert t.knows_email(" B@UW.CA ") and not t.knows_email("x@uw.ca") and not t.knows_email("")
