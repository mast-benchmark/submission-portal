"""MAST @ FIRE 2026 run submission portal (Gradio Space).

One form: upload one ``.jsonl`` (or ``.jsonl.gz``), or a zip holding one such
file per language. Language, LLM and retriever are read from the records.
Every clean file goes to its language's next free slot in one atomic commit.
Files are validated with ``mast_validate`` in memory; only clean files reach
storage. Team names are honor-system against a private roster never rendered.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
import zipfile
from typing import Any, Optional

import gradio as gr

from mast_validate import __version__ as validator_version
from mast_validate.languages import TRACKS, display_name
from mast_validate.report import render
from mast_validate.runner import UsageProblem, is_zip, single_file_report, validate_zip
from portal.config import FIRE_URL, ORGANIZER_EMAIL, SITE_URL, Settings
from portal.mailer import send_receipt
from portal.roster import Roster, Team
from portal.slots import Meta, SlotError, SlotService, prepare_bytes
from portal.storage import make_storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("portal")

settings = Settings.from_env()
storage = make_storage(settings)
roster = Roster(storage, settings.roster_ttl_seconds)
slots = SlotService(storage, max_slots=settings.max_slots, max_uploads=settings.max_uploads_per_key,
                    attempts=settings.commit_attempts)

TRACK_LABELS = {"multilingual": "MAST Multilingual (15 languages)", "indic": "MAST Indic (9 languages)"}
SYSTEM_TYPES = ["Agentic", "Retrieval-only"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ACTION_TEXT = {"slot": "→ slot {slot}", "replace": "replaces slot {slot} ({old})", "unchanged": "unchanged: {note}",
               "skipped_full": "skipped: {note}", "capped": "not recorded: {note}"}


def deadline_text() -> str:
    d = settings.deadline.astimezone(dt.timezone.utc)
    return f"{d:%A %d %B %Y, %H:%M} UTC (23:59 AoE on {(d - dt.timedelta(hours=12)):%d %B})"


def closed_message() -> str:
    return (f"### Submissions are closed\nThe run submission deadline was {deadline_text()}. "
            f"Contact {ORGANIZER_EMAIL} if you believe this is an error.")


HEADER = f"""
# MAST @ FIRE 2026 · Run submission

Up to **3 runs per language per team**. Upload **one `.jsonl` (or `.jsonl.gz`)**, or **one zip** holding one
such file per language, named however you like: the records' `language`, `llm` and `retriever` fields say what
each run is. Every file is checked against the official query ids and the corpus before it is stored; a file with
errors is never recorded.
Deadline: **{deadline_text()}**.

Check files offline first with [`mast-validate`]({SITE_URL}#submission-format) (same checks, same output).
Format spec: [{SITE_URL}#submission-format]({SITE_URL}#submission-format).

> **Working notes are not submitted here.** Each team submits one working note per track (ACM format,
> 2–4 pages) centrally through the [FIRE 2026]({FIRE_URL}) submission system, as announced by FIRE.
> Teams that submit runs but no working note may be excluded from the final leaderboard.
"""


def _tmp_json(prefix: str, payload: dict[str, Any]) -> str:
    fh = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", prefix=prefix, encoding="utf-8")
    json.dump(payload, fh, indent=2, ensure_ascii=False)
    fh.close()
    return fh.name


def _upload_path(file) -> Optional[str]:
    path = file if isinstance(file, str) else getattr(file, "name", None)
    return path if path and os.path.isfile(path) else None


def _common_checks(team_name, track, system_type, email, file):
    """Shared preamble. Returns (error_message or None, team, path)."""
    if settings.is_closed():
        return closed_message(), None, None
    fields = [("team name", team_name), ("track", track), ("system type", system_type),
              ("contact email", email), ("file", file)]
    missing = [label for label, v in fields if not v or (isinstance(v, str) and not v.strip())]
    if missing:
        return f"Please fill in: {', '.join(missing)}.", None, None
    if not EMAIL_RE.match(email.strip()):
        return "The contact email does not look like an email address.", None, None
    if system_type == "Retrieval-only":
        return ("**Retrieval-only submissions are not supported yet.** The submission format for retrieval-only "
                f"runs is still being defined; watch the mailing list or email {ORGANIZER_EMAIL}. Nothing was recorded.",
                None, None)
    m = roster.match(team_name)
    if not m.team:
        slots.log_rejected_name(team_name, track, None)
        hint = f" Did you mean **{m.suggestion}**?" if m.suggestion else ""
        return (f"No registered team matches **{team_name.strip()}**.{hint} Team names must match the "
                f"registration form. If you registered under another spelling, email {ORGANIZER_EMAIL}.", None, None)
    path = _upload_path(file)
    if not path:
        return "The upload did not arrive; please try again.", m.team, None
    return None, m.team, path


def _track_note(team: Team, track: str) -> str:
    if team.tracks and track not in team.tracks:
        return (f"\n\nNote: the registration for *{team.name}* lists only the **{', '.join(team.tracks)}** track; "
                f"this upload is for **{track}**. Proceeding.")
    return ""


def _coverage_line(track: str, team: Team) -> str:
    done = slots.languages_submitted(track, team.slug)
    total = len(TRACKS[track])
    line = f"**{track}** coverage for *{team.name}*: **{len(done)} / {total}** languages have at least one run"
    return line + (f" ({', '.join(sorted(done))})" if done else "")


# ---------------------------------------------------------------- one form for one file or a zip

def _verdict(fr: dict[str, Any]) -> str:
    if not fr["present"]:
        return "missing"
    if fr["errors"]:
        return f"✗ {fr['errors']} error{'s' if fr['errors'] != 1 else ''}"
    return "✓" + (f" {fr['warnings']} warning{'s' if fr['warnings'] != 1 else ''}" if fr["warnings"] else "")


def _plan_table(files: list[dict[str, Any]], actions: dict[str, str]) -> str:
    rows = ["| Language | File | Records | LLM · retriever | Validation | Action |", "|---|---|---|---|---|---|"]
    for fr in files:
        lang = fr["language"]
        name = f"`{fr['name']}`" if fr["present"] else ""
        meta = f"{fr.get('llm') or ''} · {fr.get('retriever') or ''}" if fr["present"] else ""
        if not fr["present"]:
            action = "— (no file in the zip)"
        else:
            action = actions.get(lang or "", "not recorded (errors)")
        rows.append(f"| {display_name(lang) if lang else '?'} | {name} | {fr['records'] if fr['present'] else ''} | {meta} | {_verdict(fr)} | {action} |")
    return "\n".join(rows)


def _read_members(path: str, report) -> dict[str, "bytes"]:
    """Raw bytes of every clean member (or of the single file), keyed by language."""
    out: dict[str, bytes] = {}
    if is_zip(path):
        with zipfile.ZipFile(path) as zf:
            for fr in report.files:
                if fr.present and fr.language and not fr.errors:
                    out[fr.language] = zf.read(fr.name)
    else:
        fr = report.files[0]
        if fr.present and fr.language and not fr.errors:
            with open(path, "rb") as fh:
                out[fr.language] = fh.read()
    return out


def on_validate(team_name, track, system_type, email, replace_oldest, file):
    """Validate one .jsonl(.gz) or a zip of them; show what would be recorded where."""
    def fail(msg: str, report_path: Optional[str] = None):
        return msg, gr.update(value=report_path, visible=report_path is not None), gr.update(visible=False), None

    err, team, path = _common_checks(team_name, track, system_type, email, file)
    if err:
        return fail(err)
    original = os.path.basename(path)
    try:
        if is_zip(path):
            report = validate_zip(path, track=track)
        else:
            report = single_file_report(path, track=track, lang=None, name=original)
    except UsageProblem as exc:
        return fail(f"Could not validate: {exc}")
    log.info("validated team=%s track=%s upload=%s size=%d status=%s errors=%d warnings=%d files=%d", team.slug, track,
             original, os.path.getsize(path), report.status, report.n_errors, report.n_warnings, len(report.files))
    rd = report.to_dict()
    report_path = _tmp_json(f"mast-report-{track}-", rd)
    text = render(report, color=False, unicode=True)
    by_name = {fr.name: fr for fr in report.files}
    items = {}
    for lang, raw in _read_members(path, report).items():
        fr = next(f for f in report.files if f.language == lang and f.present and not f.errors)
        sub = {"validator": rd["validator"], "generated_at": rd["generated_at"], "track": track, "source": fr.name,
               "status": "warnings" if fr.warnings else "ok", "files": [fr.to_dict()]}
        items[lang] = prepare_bytes(raw, fr.name, sub)
    manifests, _ = slots.manifests(track, team.slug, team.name, sorted(items)) if items else ({}, "")
    plan = slots.plan(manifests, items, replace_oldest=bool(replace_oldest)) if items else []
    actions = {}
    for it in plan:
        old = it.replaced or {}
        actions[it.lang] = ACTION_TEXT[it.action].format(
            slot=it.slot, note=it.note, old=f"{old.get('llm', '')}, {old.get('uploaded_at', '')[:10]}")
    writable = [it for it in plan if it.writes]
    present = [fr for fr in rd["files"] if fr["present"]]
    table = _plan_table(rd["files"], actions)
    zip_level = "\n".join(f"* {f['message']}" + (f" (e.g. {', '.join(f['examples'][:5])})" if f["examples"] else "")
                          for f in rd["findings"])
    head = (f"### {'✓' if items else '✗'} {len(items)} of {len(present)} file{'s' if len(present) != 1 else ''} ready to record — team **{team.name}**\n"
            f"{_coverage_line(track, team)}\n\n{table}\n\n" + (zip_level + "\n\n" if zip_level else ""))
    details = f"<details><summary>Full validator output</summary>\n\n```text\n{text}```\n</details>{_track_note(team, track)}"
    if not writable:
        why = "**Nothing to record:** " + ("no file passed validation. Fix the errors and upload again." if not items
                                            else "every clean language is unchanged, skipped (full) or capped.")
        return fail(head + why + "\n\n" + details, report_path)
    note = ("Files with errors are **not** recorded; fix them and upload again (unchanged languages are skipped automatically).\n\n"
            if any(fr["errors"] for fr in present) else "")
    state = {"team": team, "track": track, "items": items, "replace_oldest": bool(replace_oldest),
             "meta": Meta(system_type=system_type, submitter_email=email.strip())}
    return (head + note + details, gr.update(value=report_path, visible=True),
            gr.update(visible=True, value=f"Record {len(writable)} language{'s' if len(writable) != 1 else ''}"), state)


def on_submit(state):
    """Commit every writable language in one atomic commit; clear the state so a double click does nothing."""
    if settings.is_closed():
        return closed_message(), gr.update(visible=False), gr.update(visible=False), None
    if not state:
        return "Validate a file first.", gr.update(visible=False), gr.update(visible=False), None
    team: Team = state["team"]
    track = state["track"]
    try:
        summary = slots.submit_many(track, team.slug, team.name, state["items"], state["meta"],
                                    replace_oldest=state["replace_oldest"])
    except SlotError as exc:
        return f"**Not recorded:** {exc}", gr.update(visible=False), gr.update(), state
    except Exception as exc:
        log.exception("submit failed")
        return (f"**Not recorded:** the storage backend failed ({type(exc).__name__}). Nothing was saved; "
                f"please retry in a minute or email {ORGANIZER_EMAIL}.", gr.update(visible=False), gr.update(), state)
    written = [i for i in summary["items"] if i["action"] in ("slot", "replace")]
    log.info("recorded team=%s track=%s langs=%s receipt=%s", team.slug, track, [i["language"] for i in written],
             summary["bulk_receipt_id"])
    receipt_path = _tmp_json(f"mast-receipt-{summary['bulk_receipt_id']}-", summary)
    rows = ["| Language | Result | Slot | Receipt | sha256 |", "|---|---|---|---|---|"]
    for i in summary["items"]:
        res = {"slot": "recorded", "replace": "recorded (replaced " + ((i.get("replaced") or {}).get("receipt_id") or "?") + ")",
               "unchanged": "unchanged", "skipped_full": "skipped: full", "capped": "not recorded: cap"}[i["action"]]
        rows.append(f"| {display_name(i['language'])} | {res} | {i['slot'] or ''} | `{i['receipt_id'] or ''}` | `{(i['sha256'] or '')[:12]}` |")
    body = (f"MAST 2026 submission receipt\n\nTeam: {team.name}\nTrack: {track}\nReceipt id: {summary['bulk_receipt_id']}\n"
            f"Uploaded at: {summary['uploaded_at']}\nSystem type: {summary['system_type']}\nSubmitted by: {summary['submitter_email']}\n\n"
            + "\n".join(f"{i['language']}: {i['action']}" + (f" slot {i['slot']} receipt {i['receipt_id']} sha256 {i['sha256']} llm {i['llm']} retriever {i['retriever']}"
                                                              if i['receipt_id'] else f" ({i['note']})") for i in summary["items"]) + "\n")
    mailed = send_receipt(settings, [summary["submitter_email"], *team.notify_emails],
                          f"[MAST 2026] receipt {summary['bulk_receipt_id']} · {team.name} · {track}", body)
    md = (f"### {'✓' if written else '·'} {len(written)} language{'s' if len(written) != 1 else ''} recorded · receipt `{summary['bulk_receipt_id']}`\n\n"
          + "\n".join(rows) + "\n\n" + _coverage_line(track, team) + "\n\n"
          + ("A copy was emailed to the submitter and the team contact.\n" if mailed else
             "Download the receipt below and keep it; it is your proof of submission.\n"))
    return md, gr.update(value=receipt_path, visible=True), gr.update(visible=False), None


def on_load():
    return gr.update(value=closed_message(), visible=True) if settings.is_closed() else gr.update(visible=False)


RULES = f"""
* Three slots per (team, track, language). A new run takes the next free slot. When all three are filled the
  language is skipped, unless you tick *replace the oldest run*; a replaced run's receipt is kept, so nothing is
  lost silently.
* A file's language, LLM and retriever are read from its records; every record must carry the same values.
  Filenames carry no meaning. A zip may hold one file per language, named however you like.
* Every upload gets a receipt id and a sha256 per language. Keep the receipt. Organizers evaluate exactly the
  stored bytes.
* Errors block a file; warnings do not, but each one costs score. `Exact Answer:` must appear in the final
  `output_text`; unknown docids score as misses; unprefixed `query_id`s are accepted and reconstructed.
* Overlap languages (bn, hi, ta) exist in both tracks; submit to each track you take part in.
* Questions: {ORGANIZER_EMAIL}. Validator version {validator_version}.
"""

with gr.Blocks(title="MAST 2026 submission", analytics_enabled=False) as demo:
    gr.Markdown(HEADER)
    closed_banner = gr.Markdown(visible=False)
    with gr.Row():
        with gr.Column():
            team_name = gr.Textbox(label="Team name", placeholder="as on the registration form")
            track = gr.Radio(choices=[(v, k) for k, v in TRACK_LABELS.items()], label="Track", value=None)
            system_type = gr.Radio(choices=SYSTEM_TYPES, label="System type", value=None)
        with gr.Column():
            email = gr.Textbox(label="Contact email", placeholder="receipt goes here and to the team contact")
            upload = gr.File(label="Run file(s): one .jsonl / .jsonl.gz, or a .zip with one per language",
                             file_types=[".jsonl", ".gz", ".zip"], type="filepath")
            replace_oldest = gr.Checkbox(label="Replace the oldest run for languages whose 3 slots are already full", value=False)
    validate_btn = gr.Button("Validate", variant="primary")
    status = gr.Markdown()
    report_dl = gr.DownloadButton("Download validation report (JSON)", visible=False)
    submit_btn = gr.Button("Record", variant="primary", visible=False)
    result = gr.Markdown()
    receipt_dl = gr.DownloadButton("Download receipt (JSON)", visible=False)
    state = gr.State(None)
    with gr.Accordion("Rules and help", open=False):
        gr.Markdown(RULES)
    gr.Markdown("<small>Registration closes with the run deadline. The team roster refreshes within a minute of an update.</small>")

    demo.load(on_load, outputs=[closed_banner])
    validate_btn.click(on_validate, inputs=[team_name, track, system_type, email, replace_oldest, upload],
                       outputs=[status, report_dl, submit_btn, state])
    submit_btn.click(on_submit, inputs=[state], outputs=[result, receipt_dl, submit_btn, state])

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch(max_file_size="200mb", show_error=True)
