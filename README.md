---
title: MAST 2026 Submission
emoji: 📬
colorFrom: indigo
colorTo: pink
sdk: gradio
sdk_version: 6.26.0
app_file: app.py
pinned: false
license: mit
short_description: Validate and submit MAST @ FIRE 2026 run files
---

# MAST @ FIRE 2026 · run submission portal

Public code for the submission Space. No participant data lives in this repo: the team roster,
uploads, manifests and receipts are in a **private** dataset repo named by `SUBMISSIONS_REPO`.

## Configuration (Space variables and secrets)

| Name | Kind | Meaning |
|---|---|---|
| `HF_TOKEN` | secret | write access to the private submissions repo |
| `SUBMISSIONS_REPO` | variable | e.g. `mast-benchmark/mast-2026-submissions` |
| `STORAGE_BACKEND` | variable | `hf` on the Space; `local` for development |
| `MAST_DEADLINE_UTC` | variable | ISO-8601; default `2026-09-16T11:59:59+00:00` (Sep 15 AoE) |
| `MAX_UPLOADS_PER_SLOT_KEY` | variable | lifetime uploads per (team, track, language); default 10 |
| `RATE_VALIDATE_PER_IP` | variable | validations per client address, `COUNT/SECONDS` or `off`; default `20/600` |
| `RATE_RECORD_PER_IP` | variable | recorded submissions per client address; default `10/3600` |
| `RATE_RECORD_PER_TEAM` | variable | recorded submissions per team, any address; default `15/3600` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | secrets | optional; receipts are emailed only when set |

## Local run

```bash
pip install -r requirements.txt
STORAGE_BACKEND=local STORAGE_ROOT=local-store python app.py
```

Put a `teams.csv` with the columns `team_name,contact_email,member_emails,tracks,aliases,registered_at`
in `local-store/` to have teams to match against. `mast_validate/` is a vendored copy of the
validator package, refreshed by the organizers' deploy script.

## Storage layout (private repo)

```
teams.csv
slots/{track}/{lang}/{team_slug}/{n}/run.jsonl.gz     n in 1..3, always gzipped
slots/{track}/{lang}/{team_slug}/{n}/receipt.json
receipts/{track}/{lang}/{team_slug}/{ts}-{sha8}.json  every upload ever, never deleted
manifests/{track}/{lang}/{team_slug}.json             slot -> metadata
rejected/{ts}-{hash}.json                             team names that matched nothing
```

Each upload (one file or a zip of per-language files) is one atomic commit (blobs + receipts + manifests)
with a compare-and-swap on the repository revision, retried from fresh state on conflict.
