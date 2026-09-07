"""MAST @ FIRE 2026 run submission portal (Gradio Space).

One form: upload one ``.jsonl`` (or ``.jsonl.gz``), or an archive (zip or tar)
holding one such file per language. Language, LLM and retriever are read from the records.
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
import tarfile
import zipfile
from typing import Any, Optional

import gradio as gr

from mast_validate import __version__ as validator_version
from mast_validate.languages import TRACKS, display_name
from mast_validate.report import render
from mast_validate.runner import UsageProblem, archive_kind, single_file_report, validate_archive
from portal.config import FIRE_URL, ORGANIZER_EMAIL, SITE_URL, Settings
from portal.mailer import send_receipt
from portal import tmpfiles
from portal.ratelimit import Limiter, client_address
from portal.rejected import RejectedLog
from portal.roster import Roster, Team
from portal.slots import Meta, Prepared, SlotError, SlotService, prepare_stream
from portal.storage import make_storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("portal")

settings = Settings.from_env()
storage = make_storage(settings)
roster = Roster(storage, settings.roster_ttl_seconds)
slots = SlotService(storage, max_slots=settings.max_slots, max_uploads=settings.max_uploads_per_key,
                    attempts=settings.commit_attempts)
rejected = RejectedLog(storage)
limit_validate_ip = Limiter(settings.rate_validate_per_ip)
limit_record_ip = Limiter(settings.rate_record_per_ip)
limit_record_team = Limiter(settings.rate_record_per_team)

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

Up to **3 runs per language per team**. Upload **one `.jsonl` (or `.jsonl.gz`)**, or **one archive** (`.zip`, `.tar`,
`.tar.gz`, `.tgz`) holding one such file per language, named however you like: the records' `language`, `llm` and `retriever` fields say what
each run is. Every file is checked against the official query ids and the corpus before it is stored; a file with
errors is never recorded.
Deadline: **{deadline_text()}**.

Check files offline first with the `mast-validate` command-line tool from the track announcement (same checks,
same output). Format spec: [{SITE_URL}#submission-format]({SITE_URL}#submission-format).

> **Working notes are not submitted here.** Each team submits one working note per track (ACM format,
> 2–4 pages) centrally through the [FIRE 2026]({FIRE_URL}) submission system, as announced by FIRE.
> Teams that submit runs but no working note may be excluded from the final leaderboard.
"""


def _tmp_json(prefix: str, payload: dict[str, Any]) -> str:
    path = tmpfiles.new_path(prefix, ".json")
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


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
        rejected.add(team_name, track)
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
            action = "— (no file in the archive)"
        else:
            action = actions.get(lang or "", "not recorded (errors)")
        rows.append(f"| {display_name(lang) if lang else '?'} | {name} | {fr['records'] if fr['present'] else ''} | {meta} | {_verdict(fr)} | {action} |")
    return "\n".join(rows)


def _prepare_members(path: str, report, rd: dict[str, Any], track: str) -> tuple[dict[str, Prepared], dict[str, str]]:
    """Stream every clean member (or the single file) into a temp gzip. Returns (prepared by language, failures)."""
    items: dict[str, Prepared] = {}
    failed: dict[str, str] = {}
    clean = [fr for fr in report.files if fr.present and fr.language and not fr.errors]

    def sub(fr):
        return {"validator": rd["validator"], "generated_at": rd["generated_at"], "track": track, "source": fr.name,
                "status": "warnings" if fr.warnings else "ok", "files": [fr.to_dict()]}

    def one(fr, opener):
        try:
            with opener() as fh:
                items[fr.language] = prepare_stream(fh, fr.name, sub(fr))
        except Exception as exc:  # a member that validated but cannot be re-read (truncated archive, I/O error)
            failed[fr.language] = f"{type(exc).__name__}: {exc}"
            log.warning("member %s unreadable on second pass: %s", fr.name, exc)

    kind = archive_kind(path)
    try:
        if kind == "zip":
            with zipfile.ZipFile(path) as zf:
                for fr in clean:
                    one(fr, lambda fr=fr: zf.open(fr.name))
        elif kind == "tar":
            with tarfile.open(path, "r:*") as tf:
                for fr in clean:
                    def opener(fr=fr):
                        fh = tf.extractfile(fr.name)
                        if fh is None:
                            raise OSError("not a regular file")
                        return fh
                    one(fr, opener)
        else:
            for fr in clean:
                one(fr, lambda: open(path, "rb"))
    except Exception as exc:
        for fr in clean:
            failed.setdefault(fr.language, f"{type(exc).__name__}: {exc}")
    return items, failed


def _too_many(what: str, limiter: Limiter, retry: int) -> str:
    return (f"**Too many requests:** the limit is {limiter.limit.describe()} for {what}. "
            f"Try again in about {max(1, retry // 60)} minute{'s' if retry >= 120 else ''}. Nothing was recorded.")


def on_validate(team_name, track, system_type, email, replace_oldest, file, request: gr.Request = None):
    """Validate one .jsonl(.gz) or a zip of them; show what would be recorded where."""
    def fail(msg: str, report_path: Optional[str] = None):
        return msg, gr.update(value=report_path, visible=report_path is not None), gr.update(visible=False), None

    tmpfiles.sweep()
    addr = client_address(request)
    allowed, retry = limit_validate_ip.hit(addr)
    if not allowed:
        log.warning("rate limit: validate addr=%s", addr)
        return fail(_too_many("validations from one address", limit_validate_ip, retry))
    err, team, path = _common_checks(team_name, track, system_type, email, file)
    if err:
        return fail(err)
    original = os.path.basename(path)
    try:
        if archive_kind(path):
            report = validate_archive(path, track=track)
        else:
            report = single_file_report(path, track=track, lang=None, name=original)
    except UsageProblem as exc:
        return fail(f"Could not validate: {exc}")
    log.info("validated team=%s track=%s upload=%s size=%d status=%s errors=%d warnings=%d files=%d", team.slug, track,
             original, os.path.getsize(path), report.status, report.n_errors, report.n_warnings, len(report.files))
    rd = report.to_dict()
    report_path = _tmp_json(f"mast-report-{track}-", rd)
    text = render(report, color=False, unicode=True)
    items, unreadable = _prepare_members(path, report, rd, track)
    manifests, _ = slots.manifests(track, team.slug, team.name, sorted(items)) if items else ({}, "")
    plan = slots.plan(manifests, items, replace_oldest=bool(replace_oldest)) if items else []
    actions = {lang: f"not recorded (unreadable: {why[:60]})" for lang, why in unreadable.items()}
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
        tmpfiles.remove(p.gz_path for p in items.values())
        why = "**Nothing to record:** " + ("no file passed validation. Fix the errors and upload again." if not items
                                            else "every clean language is unchanged, skipped (full) or capped.")
        return fail(head + why + "\n\n" + details, report_path)
    note = ("Files with errors are **not** recorded; fix them and upload again (unchanged languages are skipped automatically).\n\n"
            if any(fr["errors"] for fr in present) else "")
    state = {"team": team, "track": track, "items": items, "replace_oldest": bool(replace_oldest),
             "meta": Meta(system_type=system_type, submitter_email=email.strip())}
    return (head + note + details, gr.update(value=report_path, visible=True),
            gr.update(visible=True, value=f"Record {len(writable)} language{'s' if len(writable) != 1 else ''}"), state)


def on_submit(state, request: gr.Request = None):
    """Commit every writable language in one atomic commit; clear the state so a double click does nothing."""
    if settings.is_closed():
        return closed_message(), gr.update(visible=False), gr.update(visible=False), None
    if not state:
        return "Validate a file first.", gr.update(visible=False), gr.update(visible=False), None
    team: Team = state["team"]
    track = state["track"]
    addr = client_address(request)
    allowed, retry = limit_record_ip.hit(addr)
    if allowed:
        allowed, retry = limit_record_team.hit(team.slug)
        what = "submissions by one team"
        limiter = limit_record_team
    else:
        what, limiter = "submissions from one address", limit_record_ip
    if not allowed:
        log.warning("rate limit: record addr=%s team=%s", addr, team.slug)
        return _too_many(what, limiter, retry), gr.update(visible=False), gr.update(), state
    if any(not p.gz_path.is_file() for p in state["items"].values()):
        return ("**Not recorded:** this validation is too old (prepared files are kept for an hour). "
                "Validate again, then record.", gr.update(visible=False), gr.update(visible=False), None)
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
    tmpfiles.remove(p.gz_path for p in state["items"].values())
    if not summary.get("stored") or not written:
        log.info("nothing recorded team=%s track=%s (state changed between validate and record)", team.slug, track)
        return ("**Nothing was recorded.** The slots changed between *Validate* and *Record* (every language is now "
                "unchanged, full or capped). Validate again to see the current state.",
                gr.update(visible=False), gr.update(visible=False), None)
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
    md = (f"### ✓ {len(written)} language{'s' if len(written) != 1 else ''} recorded · receipt `{summary['bulk_receipt_id']}`\n\n"
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
* A file's language, LLM and retriever are read from its records; every record must carry the same values
  (`llm` and `retriever` are compared exactly, case-sensitive). Filenames carry no meaning. An archive (zip or
  tar) may hold one file per language, named however you like.
* Team names are matched ignoring case and punctuation, but word order matters: "Sahel Test" is not "Test Sahel".
* Every upload gets a receipt id and a sha256 per language. Keep the receipt. Organizers evaluate exactly the
  stored bytes.
* Errors block a file; warnings do not, but each one costs score. `Exact Answer:` must appear in the final
  `output_text`; unknown docids score as misses; unprefixed `query_id`s are accepted and reconstructed.
* Overlap languages (bn, hi, ta) exist in both tracks; submit to each track you take part in.
* Rate limits: {settings.rate_validate_per_ip.describe() if settings.rate_validate_per_ip.enabled else 'none'} for validations per address,
  {settings.rate_record_per_ip.describe() if settings.rate_record_per_ip.enabled else 'none'} for submissions per address,
  {settings.rate_record_per_team.describe() if settings.rate_record_per_team.enabled else 'none'} for submissions per team.
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
            upload = gr.File(label="Run file(s): one .jsonl / .jsonl.gz, or a .zip / .tar / .tar.gz / .tgz with one per language",
                             file_types=[".jsonl", ".gz", ".zip", ".tar", ".tgz"], type="filepath")
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
