# MAST @ FIRE 2026 · Run submissions

## Where to submit

* Portal: **https://huggingface.co/spaces/sahel-sh/submit**
* Deadline: **15 September 2026, 23:59 AoE** (the portal closes at 2026-09-16 11:59 UTC).
* You need: your team name as registered (case and punctuation do not matter, word order does), the track,
  a contact email, and the file. Language, LLM and retriever are read from the records.
* The track must be one your team registered for, and the contact email must be one of the addresses on your
  team's latest registration for that track (any member's). Anything else is refused before the file is looked at.
* Only agentic runs are accepted.
* Check files offline first with the `mast-validate` CLI: `pip install git+https://github.com/mast-benchmark/mast-validate`.
  It runs the same checks and prints the same report.
* Working notes are **not** submitted here; they go through the FIRE 2026 submission system.

## Where organizers check submissions

Everything lands in the private dataset **https://huggingface.co/datasets/mast-benchmark/mast-2026-submissions**
(org members only). From a checkout of `mast-benchmark/submission-portal`, with the private `.env` holding
`HF_TOKEN` in the `ops` directory next to the checkout (or `MAST_PRIVATE_DIR`):

```
python ops/list_submissions.py                 # one line per (team, track, language, slot)
python ops/list_submissions.py --json out.json
python ops/list_submissions.py --download DIR  # mirror every stored run + receipt for evaluation
```

## The team roster

The portal matches team names and emails against the registration form's responses. Two sources, tried in this
order; both keep the list private and new registrations are recognized within a minute of reaching the portal:

* **Pushed by the form itself (no Google Cloud account needed).** A small Apps Script inside the responses
  spreadsheet (`ops/push_roster.gs`) uploads the sheet as `responses.csv` to the private dataset on every form
  submission or edit of the sheet, plus every 2 hours as a backstop. The portal parses it with the rule below.
  Setup, about five minutes, in your own Google account:
  1. Open the responses spreadsheet → Extensions → Apps Script, paste `ops/push_roster.gs`, save.
  2. Project Settings → Script Properties: `HF_TOKEN` = a Hugging Face **fine-grained** token with write access to
     only `mast-benchmark/mast-2026-submissions` (Settings → Access Tokens → fine-grained → Repositories permissions
     on that one dataset).
  3. Run `pushRoster` once from the editor and accept the authorization prompt (it is your own script).
  4. Triggers → add `pushRoster` three times: "From spreadsheet / On form submit", "From spreadsheet / On change",
     and "Time-driven / every 2 hours". Triggers belong to the account that adds them.
  The upload uses the Hub's commit endpoint; the same call was verified from the organizer machine.
* **`teams.csv` in the private dataset.** The static fallback, one row per (team, track), produced by
  `ops/import_roster.py responses.csv --push` from a manual export.

**Who may submit for a (team, track).** Rows are read in form order. For every team name and track, the
**latest** response row that names that track is the registration: the emails on that row are the eligible
submitters for that track, its first email is the contact that receives receipts, and every earlier row for the
same pair is superseded entirely. A row that names both tracks defines both. So each (team, track) resolves to
exactly one set of submitters. Team names are compared the way the portal matches them: case, punctuation and
repeated spaces are ignored, word order is not. Rows without a recognizable track are skipped and logged.

`responses.csv` serves today and the Apps Script is installed, so registrations flow in automatically.

Hand-maintained spelling aliases go in `aliases.csv` (`team_name,aliases`, `;`-separated) in the private dataset
and apply in both modes.

## The record format

One JSON object per line, one file per (track, language), all 50 official query ids of that language, no more,
no less. This is the format on the MAST website.

| Field | Type | Rule |
|---|---|---|
| `query_id` | string or integer | `"zh-798"` as in the released data (the prefix must be exactly the file's language code, lowercase), or the bare number `798` / `"798"` as in the website example; a bare number is combined with the file's language (one warning per file) |
| `language` | string | code or name (`"hi"`, `"Hindi"`, `"hindi"`); every record of the file must name the same language, in any of these forms |
| `retriever` | string | non-empty; identical in every record of the file, compared exactly (case-sensitive, whitespace trimmed); shown on the leaderboard |
| `llm` | string | non-empty; same rule; shown on the leaderboard |
| `tool_call_counts` | object | string → non-negative integer; `"search"` is compared with the number of rounds |
| `retrieved_docids` | list of lists of strings | one inner list per search round, docids as non-blank strings, in rank order; an empty round is a warning |
| `result` | list of steps | non-empty; step = `{type, tool_name, arguments, output}` with `type` in `reasoning` / `tool_call` / `output_text`, `tool_name` and `arguments` string or null, `output` a string (null tolerated except on `output_text`) |

The last `output_text` step's `output` should contain `Exact Answer:`; that is what the exact-match scorer parses.
A file without it is accepted with a warning, and those queries score zero.

JSON Schema (draft 2020-12) for one record. The validator enforces exactly these constraints in code, plus
the cross-record checks listed below that a schema cannot express.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "MAST 2026 run record",
  "type": "object",
  "required": ["query_id", "language", "retriever", "llm", "tool_call_counts", "retrieved_docids", "result"],
  "properties": {
    "query_id": { "oneOf": [
      { "type": "string", "pattern": "^[a-z]{2}-[0-9]+$" },
      { "type": "string", "pattern": "^[0-9]+$" },
      { "type": "integer", "minimum": 0 } ] },
    "language": { "type": "string", "minLength": 1 },
    "retriever": { "type": "string", "minLength": 1 },
    "llm": { "type": "string", "minLength": 1 },
    "tool_call_counts": { "type": "object", "additionalProperties": { "type": "integer", "minimum": 0 } },
    "retrieved_docids": { "type": "array", "items": { "type": "array", "items": { "type": "string", "minLength": 1 } } },
    "result": { "type": "array", "minItems": 1, "items": {
      "type": "object",
      "required": ["type", "output"],
      "properties": {
        "type": { "enum": ["reasoning", "tool_call", "output_text"] },
        "tool_name": { "type": ["string", "null"] },
        "arguments": { "type": ["string", "null"] },
        "output": { "type": ["string", "null"] } },
      "if": { "properties": { "type": { "const": "output_text" } } },
      "then": { "properties": { "output": { "type": "string" } } } } }
  },
  "additionalProperties": true
}
```

Unknown top-level keys are allowed and reported once per file as a warning.

## What is validated

**File level, before anything else.** A file describes exactly one run: every record must carry the **same**
`language`, the **same** `llm` and the **same** `retriever` (the latter two compared exactly, case-sensitive),
and every prefixed `query_id` must start with exactly that language code. The validator reads the three values
from the records and refuses the file if any record disagrees.
Filenames are ignored, and the form has no language, LLM or retriever field. The track you pick only says which
track the run is for (Hindi, Bengali and Tamil exist in both), and the file's language must belong to it.

**Errors** (the file is refused, nothing is recorded):

| Kind | Meaning |
|---|---|
| `json.invalid_line`, `json.not_object` | a line is not JSON / not an object |
| `schema.missing_field`, `schema.wrong_type` | required field absent / wrong type (a flat `retrieved_docids` list is reported here with a how-to-nest hint) |
| `schema.result_empty`, `schema.step_unknown_type` | empty `result` / step type outside the three allowed |
| `lang.undetermined`, `lang.not_in_track`, `lang.mismatch` | no recognizable language / language not in the declared track / a record disagrees with the file's language |
| `qid.malformed`, `qid.prefix_mismatch`, `qid.unknown`, `qid.duplicate` | unparseable id / prefix from another language / not an official id / same id twice |
| `coverage.missing` | fewer than the 50 official ids present |
| `docid.bad_entry` | a docid that is not a non-empty string |
| `meta.inconsistent` | the file mixes different `llm` or `retriever` values |
| `io.oversize`, `io.unreadable`, `io.not_jsonl` | over 200 MB compressed or 2 GB decompressed / unreadable / an archive where a `.jsonl` was expected |
| `archive.unsafe_member`, `archive.duplicate_language` | archives only: path traversal, symlink, hard link, absolute path / two members with the same language |

**Warnings** (recorded, but each one costs score): `docid.unknown` and `docid.unknown_majority` (docids not
in the corpus; over half means the wrong corpus was indexed), `qid.reconstructed` (bare numeric ids),
`answer.no_exact_answer`, `answer.no_output_text`, `rounds.count_mismatch`, `rounds.empty`, `keys.unknown`,
`track.language_missing` and `archive.member_ignored` (archives only).

CLI exit codes: `0` clean, `1` warnings only, `2` errors, `3` usage or I/O problem. `--strict` turns warnings
into errors. `--json` writes the full report.

## The form

One form, one button. Inputs: team name, track, contact email, the file, and a *replace the oldest run* checkbox.
The file is either **one `.jsonl` / `.jsonl.gz`** or **one archive** (`.zip`, `.tar`, `.tar.gz`, `.tgz`) holding
one such file per language, named however you like. Everything else (language, LLM, retriever) is read from the
records.

**Submit** validates the upload and, in the same step, records every file that passed. The page then shows one of
three outcomes, always with the full validator output and a JSON report download:

* **Submitted.** Headline "✓ Submitted: N languages recorded · receipt <id>", one row per file with its result
  ("submitted → slot 1 · receipt …", "submitted → slot 1, replaced the oldest run", "unchanged: identical to
  slot 2", "not submitted: all 3 slots are filled …", "not submitted (errors)"), the coverage line, and a receipt
  download. Files with errors in the same archive are simply not stored; fix them and submit again, and the
  unchanged languages are skipped automatically.
* **Nothing new to submit.** Every file that passed is already recorded, or its language is full (tick *replace
  the oldest run*) or capped. Nothing is written.
* **Not submitted.** No file passed validation, or the request was refused (rate limit, storage failure). The
  table shows each file's errors; nothing is written.

## What the page says, case by case

**Refused before the file is looked at** (nothing is validated or recorded):

| Case | Message |
|---|---|
| After the deadline | *Submissions are closed. The run submission deadline was Wednesday 16 September 2026, 11:59 UTC (23:59 AoE on 15 September). Contact mast-organizers@googlegroups.com if you believe this is an error.* |
| A field left empty | *Please fill in: team name.* (lists every missing field) |
| Email not an address | *The contact email does not look like an email address.* |
| Team not registered | *No registered team matches **Ghost Team**. Make sure the team name is exactly as you registered it. If your team is new, register here (link) and try again in two minutes so the registration propagates. Still not recognized? Email …* |
| Team name close to a registered one | *No registered team matches **Test Sahle**. Did you mean **Test Sahel**? If your team is new, register here …* |
| Track not in the registration | ***Test Sahel** is registered for the **indic** track only; this upload is for **multilingual**. Email … to add a track to your registration. Nothing was recorded.* |
| Email not on the registration for that track | *The contact email is not one of the addresses registered for **Test Sahel** on the indic track. Use an address from your team's latest registration, or email … to update it. Nothing was recorded.* |
| Too many requests | ***Too many requests:** the limit is 20 per 10 minutes for submissions from one address. Try again in about N minutes. Nothing was recorded.* |
| Wrong file extension | Gradio's own notice: *Invalid file type. Please upload a file that is one of these formats: .jsonl, .gz, .zip, .tar, .tgz* |

**After Submit** (one row per file; for an archive, also one row per language of the track with no file):

| Case | Headline and rows |
|---|---|
| Single file passes | *✓ Submitted: 1 language recorded — team **Sahel Test** · receipt `…`*; row `Hindi (hi) · file · 50 · llm · retriever · ✓ · submitted → slot 1 · receipt …`; receipt download shown |
| Single file passes with warnings | same, `✓ 2 warnings` in the Validation column; the warnings are spelled out in the full validator output |
| Single file fails | *✗ Not submitted — team …* then *No file passed validation. Fix the errors listed …*; row `… ✗ 1 error · not submitted (errors)`; each error is in the full validator output (e.g. *coverage: 1 official query_id missing (found 49 of 50) (e.g. hi-10)*); no receipt |
| Archive, some pass, some fail | *✓ Submitted: 2 languages recorded …* plus *Files with errors were not submitted; fix them and submit again*; passing rows `submitted → slot N`; failing rows `not submitted (errors)`; missing languages `missing`; stray members listed as ignored |
| Same upload again | *· Nothing new to submit — team …*; passing rows `unchanged: identical to slot 1 (uploaded …)`; no receipt |
| A language whose 3 slots are full | row `not submitted: all 3 slots are filled; tick 'replace the oldest run' to replace one`; with the box ticked, `submitted → slot 1, replaced the oldest run` |
| A member that validated but could not be re-read | row `not submitted (unreadable: …)`; other rows proceed |
| Team or address over the submission rate limit | *✗ Not submitted …* with ***Too many requests:** the limit is 15 per 1 hour for submissions by one team …*; nothing written |
| Storage failure | *✗ Not submitted …* with ***Not submitted:** the storage backend failed (…). Nothing was saved; please retry in a minute or email …* |

The full validator output (the same text the CLI prints) is always available under *Full validator output*, and
the JSON report is always downloadable, pass or fail. The receipt download appears only when something was recorded.

## Slots and replacement

* Three slots per (team, track, language). A new upload takes the next free slot.
* When all three are filled, the *replace the oldest run* checkbox replaces the oldest; without it the language
  is skipped. The replaced upload's receipt is kept forever, and the new receipt names what it replaced.
* An upload identical to a run already in a slot is reported as unchanged and not written again.
* Lifetime cap of 10 uploads per (team, track, language), so the repository history stays bounded.
* Rate limits per client address and per team (see *All limits in one place*). A refused request records
  nothing and says when to retry. They slow abuse; they do not prevent someone who knows a team name from
  submitting under it.
* Every recorded upload gets a receipt id and two sha256 values (stored gzip and decompressed content).
  Organizers evaluate exactly the stored bytes; the receipt lets a team prove what they submitted.

## All limits in one place

| Limit | Value | Where set |
|---|---|---|
| Deadline | 15 Sep 2026 23:59 AoE = 2026-09-16 11:59:59 UTC; the form refuses after it | `MAST_DEADLINE_UTC` Space variable (ISO-8601) |
| Records per file | exactly the 50 official query ids of the language, no more, no less | fixed |
| Runs per (team, track, language) | 3 slots; a 4th upload is skipped unless *replace the oldest run* is ticked | fixed |
| Lifetime uploads per (team, track, language) | 10, replacements included; then the language is refused until an organizer raises it | `MAX_UPLOADS_PER_SLOT_KEY` |
| Upload size | 200 MB per upload and per archive member as stored (gzip/zip/tar); the browser refuses larger uploads before validation | fixed (`limits.py`, Gradio `max_file_size`) |
| Decompressed size | 2 GB per file | fixed (`limits.py`) |
| Single record | 64 MB per line | fixed (`limits.py`) |
| Files per archive | no limit beyond size; one file per language (a second file for the same language refuses both) | fixed |
| Validations | 20 per 10 minutes per client address | `RATE_VALIDATE_PER_IP` (`COUNT/SECONDS` or `off`) |
| Recorded submissions | 10 per hour per client address | `RATE_RECORD_PER_IP` |
| Recorded submissions per team | 15 per hour from any address | `RATE_RECORD_PER_TEAM` |
| Concurrent validations on the Space | 4; further requests queue | fixed (`app.py`) |
| Roster refresh | within 60 seconds of a `teams.csv` change | `ROSTER_TTL_SECONDS` |
| Rejected-name log | at most 200 distinct names per 10-minute window, written as one file | fixed (`rejected.py`) |

Changing a Space variable restarts the Space (about 30 seconds) and resets the in-memory rate counters.
The evaluation uses exactly the stored bytes; none of these limits alters a file.

## Where the data lands

Private dataset `mast-benchmark/mast-2026-submissions`:

```
teams.csv                                             roster fallback: team_name, track, contact_email, member_emails, aliases, registered_at
responses.csv                                         raw registration export pushed by the Apps Script (preferred roster source)
manifests/{track}/{lang}/{team}.json                  current state: slot -> {sha256, uploaded_at, llm, retriever, warnings, receipt_id, replaced}
slots/{track}/{lang}/{team}/{1|2|3}/run.jsonl.gz       the run, always stored gzipped
slots/{track}/{lang}/{team}/{1|2|3}/receipt.json       its receipt, including the full validation report
receipts/{track}/{lang}/{team}/{ts}-{sha8}.json        every upload ever, replaced ones included, never deleted
receipts/{track}/bulk/{team}/{ts}-{sha8}.json          one per upload (file or archive): what went where
rejected/{ts}-{hash}.json                              rejected team names, one file per ~10-minute window with counts (spot a locked-out team, add an alias)
```

`{team}` is the slug of the registered name (`test-sahel`). Each upload is one atomic commit (every clean
file's run + receipt + manifest together, plus the upload receipt) with a compare-and-swap on the repository
revision, so two uploads racing on the same manifest cannot overwrite each other; the loser retries from fresh
state. Overwriting a file does not free its old blob in git history, which is why the upload cap exists.

## Next steps and open items

**1. The submission URL.** The Space runs on a personal PRO account because Hugging Face will not host a
Gradio Space for an organization without a Team plan, nor for a free personal account.

| Option | URL teams see | Cost |
|---|---|---|
| PRO + redirect from the MAST site | `https://mast-benchmark.github.io/submit/` | $9 / month |
| PRO only | `https://huggingface.co/spaces/sahel-sh/…` | $9 / month |
| Team plan for the org | `https://huggingface.co/spaces/mast-benchmark/…` | $20 / seat / month, 3 seats today |

Recommended: keep PRO, add `submit/index.html` with a meta-refresh redirect to the MAST website repo, and
announce the `mast-benchmark.github.io/submit/` address. If the org later takes the Team plan, only the
redirect changes. Rename the Space to something self-describing (e.g. `mast-2026-submissions`) **before**
announcing; the deploy script takes the name as a flag. PRO is only needed while the page is up; pause the Space
and cancel after results.

**2. Confirmation emails: not configured.** The code is in place and switches on when five secrets exist on
the Space; nothing else changes. After each recorded upload it emails a plain-text receipt to the submitter and
to the team's registered contact: one summary email per upload, its subject carrying the receipt id. Failures
are logged and never block a submission.

What is needed:

* An account that can send mail over SMTP. Simplest: a Gmail account with 2-Step Verification on and an
  *App Password* generated at `myaccount.google.com/apppasswords` (the Google Group address cannot send).
  Consumer Gmail allows about 500 messages a day, far more than needed. A university SMTP relay works too if it
  accepts logins from outside the campus network.
* The five Space secrets, set in the Space settings page or added to `ops/.env` and pushed by `ops/deploy.py`:

  | Secret | Value for Gmail |
  |---|---|
  | `SMTP_HOST` | `smtp.gmail.com` |
  | `SMTP_PORT` | `587` |
  | `SMTP_USER` | the Gmail address |
  | `SMTP_PASSWORD` | the 16-character app password |
  | `SMTP_FROM` | the same Gmail address |

* One test upload as a test team afterwards, to confirm delivery and check the spam folder. Mail from a
  brand-new address is more likely to be filtered than mail from an organizer's existing account.

**3. Before opening.** Install the Apps Script (above) so late registrations work, clear the test teams' data
from the dataset, make `mast-benchmark/mast-validate` public, merge the website branch `submission-guidelines`,
and send the announcement with the format corrections (50 queries per language, 1,200 total, prefixed string
query ids, 15 + 9 languages).
