#!/usr/bin/env python3
"""Print every recorded slot in the private submissions repo, one line per (team, track, language, slot).

    python ops/list_submissions.py                 # table
    python ops/list_submissions.py --json out.json # machine-readable
    python ops/list_submissions.py --download DIR  # also fetch all run.jsonl.gz files into DIR

Reads HF_TOKEN from the private .env (see deploy.py).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ops.deploy import load_env  # noqa: E402

REPO = "mast-benchmark/mast-2026-submissions"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--download", type=Path, help="mirror slots/ (run files + receipts) into this directory")
    args = ap.parse_args()
    from huggingface_hub import HfApi, snapshot_download

    token = load_env()["HF_TOKEN"]
    api = HfApi(token=token)
    files = api.list_repo_files(args.repo, repo_type="dataset")
    manifests = [f for f in files if f.startswith("manifests/") and f.endswith(".json")]
    local = Path(snapshot_download(args.repo, repo_type="dataset", token=token, allow_patterns=["manifests/*", "teams.csv"]))
    rows = []
    for mf in sorted(manifests):
        m = json.loads((local / mf).read_text())
        for n, s in sorted(m.get("slots", {}).items(), key=lambda kv: int(kv[0])):
            rows.append({"team": m.get("team"), "track": m["track"], "language": m["language"], "slot": int(n),
                         "uploaded_at": s.get("uploaded_at"), "llm": s.get("llm"), "retriever": s.get("retriever"),
                         "system_type": s.get("system_type"), "records": s.get("records"), "warnings": s.get("warnings"),
                         "sha256": s.get("sha256"), "receipt_id": s.get("receipt_id"),
                         "path": f"slots/{m['track']}/{m['language']}/{m['team_slug']}/{n}/run.jsonl.gz"})
    teams = 0
    if (local / "teams.csv").is_file():
        import csv
        teams = len({r["team_name"].strip().lower() for r in csv.DictReader(open(local / "teams.csv", encoding="utf-8-sig")) if r.get("team_name")})
    print(f"{args.repo}: {teams} registered teams, {len(rows)} filled slots, "
          f"{sum(1 for f in files if f.startswith('receipts/'))} receipts, {sum(1 for f in files if f.startswith('rejected/'))} rejected names\n")
    if rows:
        w = max(len(r["team"] or "") for r in rows)
        print(f"{'team':{w}s}  track         lang  slot  uploaded (UTC)       llm                   warn  sha256")
        for r in rows:
            print(f"{r['team']:{w}s}  {r['track']:12s}  {r['language']:4s}  {r['slot']:>4}  "
                  f"{(r['uploaded_at'] or '')[:19].replace('T', ' '):19s}  {(r['llm'] or '')[:20]:20s}  {r['warnings']:>4}  {r['sha256'][:12]}")
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2))
        print(f"\nwrote {args.json}")
    if args.download:
        p = snapshot_download(args.repo, repo_type="dataset", token=token, allow_patterns=["slots/*", "manifests/*", "receipts/*"],
                              local_dir=str(args.download))
        print(f"\nmirrored slots/, manifests/, receipts/ into {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
