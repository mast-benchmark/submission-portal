#!/usr/bin/env python3
"""Map the registration-form responses (Sheets CSV export) to teams.csv, one row per (team, track).

    python ops/import_roster.py /path/to/responses.csv [--out teams.csv] [--push]

Output defaults to MAST_PRIVATE_DIR/teams.csv (the private ``ops`` directory next to this checkout).

Rule: for every (team, track) pair the latest response row is the registration;
its members are that track's eligible submitters and earlier rows for the pair
are superseded entirely. Team names are compared the way the portal matches
them (case, punctuation and repeated spaces ignored). Hand-edited ``aliases``
in an existing teams.csv are preserved by team name.

Private: this touches participant emails. Never commit its inputs or outputs.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from portal.registrations import rows_to_teams  # noqa: E402
from portal.roster import normalize_name  # noqa: E402

COLUMNS = ["team_name", "track", "contact_email", "member_emails", "aliases", "registered_at"]


def load_aliases(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        out: dict[str, str] = {}
        for r in csv.DictReader(fh):
            if (r.get("aliases") or "").strip():
                out[r["team_name"].strip()] = r["aliases"].strip()
        return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("responses", type=Path)
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("MAST_PRIVATE_DIR") or Path(__file__).resolve().parents[2] / "ops") / "teams.csv")
    ap.add_argument("--push", action="store_true", help="upload to the private submissions repo")
    ap.add_argument("--repo", default="mast-benchmark/mast-2026-submissions")
    args = ap.parse_args()

    with open(args.responses, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    teams, notes = rows_to_teams(rows, load_aliases(args.out))
    for n in notes:
        print("note:", n)
    if not teams:
        print("IMPORT ABORTED: no teams parsed", file=sys.stderr)
        return 1
    seen = {}
    for t in teams:  # the parser already keys on the normalized name; this is a belt-and-braces check
        k = normalize_name(t.name)
        assert k not in seen, f"two teams normalize to the same name: {seen[k]!r} and {t.name!r}"
        seen[k] = t.name
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for t in teams:
            for track in t.tracks:
                emails = t.members[track]
                w.writerow({"team_name": t.name, "track": track, "contact_email": emails[0] if emails else "",
                            "member_emails": ";".join(emails), "aliases": ";".join(t.aliases),
                            "registered_at": t.registered_at.get(track, "")})
    print(f"wrote {args.out}: {len(teams)} teams, {sum(len(t.tracks) for t in teams)} (team, track) registrations")
    if args.push:
        from deploy import load_env  # type: ignore
        from huggingface_hub import HfApi
        HfApi(token=load_env()["HF_TOKEN"]).upload_file(path_or_fileobj=str(args.out), path_in_repo="teams.csv",
                                                        repo_id=args.repo, repo_type="dataset", commit_message="roster update")
        print(f"pushed teams.csv to {args.repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
