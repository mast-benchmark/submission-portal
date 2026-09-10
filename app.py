"""MAST @ FIRE 2026 run submission portal (Gradio Space).

One form: upload one ``.jsonl`` (or ``.jsonl.gz``), or an archive (zip or tar)
holding one such file per language. Language, LLM and retriever are read from the records.
One button validates and records: every clean file goes to its language's next
free slot in one atomic commit, and the page shows either the errors or the receipt.
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
from portal.config import FIRE_URL, ORGANIZER_EMAIL, REGISTRATION_URL, SITE_URL, Settings
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
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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

Check files offline first with [`mast-validate`](https://github.com/mast-benchmark/mast-validate) (same checks,
same output). Format and guidelines: [{SITE_URL}#submission-format]({SITE_URL}#submission-format).

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


def _common_checks(team_name, track, email, file):
    """Shared preamble. Returns (error_message or None, team, path)."""
    if settings.is_closed():
        return closed_message(), None, None
    fields = [("team name", team_name), ("track", track), ("contact email", email), ("file", file)]
    missing = [label for label, v in fields if not v or (isinstance(v, str) and not v.strip())]
    if missing:
        return f"Please fill in: {', '.join(missing)}.", None, None
    if not EMAIL_RE.match(email.strip()):
        return "The contact email does not look like an email address.", None, None
    m = roster.match(team_name)
    if not m.team:
        rejected.add(team_name, track)
        hint = (f" Did you mean **{m.suggestion}**?" if m.suggestion
                else " Make sure the team name is exactly as you registered it.")
        return (f"No registered team matches **{team_name.strip()}**.{hint} "
                f"If your team is new, [register here]({REGISTRATION_URL}) and try again in two minutes so the "
                f"registration propagates. Still not recognized? Email {ORGANIZER_EMAIL}.", None, None)
    team = m.team
    if team.tracks and track not in team.tracks:
        log.warning("track refused team=%s registered=%s requested=%s", team.slug, team.tracks, track)
        return (f"**{team.name}** is registered for the **{' and '.join(team.tracks)}** track"
                f"{'s' if len(team.tracks) > 1 else ''} only; this upload is for **{track}**. "
                f"Email {ORGANIZER_EMAIL} to add a track to your registration. Nothing was recorded.", None, None)
    if not team.knows_email(email, track):
        log.warning("email refused team=%s", team.slug)
        return (f"The contact email is not one of the addresses registered for **{team.name}** on the {track} track. "
                f"Use an address from your team's latest registration, or email {ORGANIZER_EMAIL} to update it. "
                f"Nothing was recorded.",
                None, None)
    path = _upload_path(file)
    if not path:
        return "The upload did not arrive; please try again.", team, None
    return None, team, path


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


def _results_table(files: list[dict[str, Any]], results: dict[str, str]) -> str:
    rows = ["| Language | File | Records | LLM · retriever | Validation | Result |", "|---|---|---|---|---|---|"]
    for fr in files:
        lang = fr["language"]
        if not fr["present"]:
            rows.append(f"| {display_name(lang)} | | | | missing | — (no file in the archive) |")
            continue
        meta = f"{fr.get('llm') or ''} · {fr.get('retriever') or ''}"
        result = results.get(lang or "", "not submitted (errors)")
        rows.append(f"| {display_name(lang) if lang else '?'} | `{fr['name']}` | {fr['records']} | {meta} | {_verdict(fr)} | {result} |")
    return "\n".join(rows)


RESULT_TEXT = {"slot": "**submitted** → slot {slot}", "replace": "**submitted** → slot {slot}, replaced the oldest run",
               "unchanged": "unchanged: {note}", "skipped_full": "not submitted: {note}", "capped": "not submitted: {note}"}


def on_submit(team_name, track, email, replace_oldest, file, request: gr.Request = None):
    """One step: validate the upload and record every clean file. Shows either the errors or the receipt."""
    hidden = gr.update(visible=False)

    def fail(msg: str, report_path: Optional[str] = None):
        return msg, gr.update(value=report_path, visible=report_path is not None), hidden

    tmpfiles.sweep()
    addr = client_address(request)
    allowed, retry = limit_validate_ip.hit(addr)
    if not allowed:
        log.warning("rate limit: submit addr=%s", addr)
        return fail(_too_many("submissions from one address", limit_validate_ip, retry))
    err, team, path = _common_checks(team_name, track, email, file)
    if err:
        return fail(err)
    original = os.path.basename(path)
    try:
        report = validate_archive(path, track=track) if archive_kind(path) else single_file_report(path, track=track, lang=None, name=original)
    except UsageProblem as exc:
        return fail(f"Could not validate: {exc}")
    log.info("validated team=%s track=%s upload=%s size=%d status=%s errors=%d warnings=%d files=%d", team.slug, track,
             original, os.path.getsize(path), report.status, report.n_errors, report.n_warnings, len(report.files))
    rd = report.to_dict()
    report_path = _tmp_json(f"mast-report-{track}-", rd)
    text = render(report, color=False, unicode=True)
    present = [fr for fr in rd["files"] if fr["present"]]
    zip_level = "\n".join(f"* {f['message']}" + (f" (e.g. {', '.join(f['examples'][:5])})" if f["examples"] else "")
                          for f in rd["findings"])
    details = f"<details><summary>Full validator output</summary>\n\n```text\n{text}```\n</details>"
    items, unreadable = _prepare_members(path, report, rd, track)
    results = {lang: f"not submitted (unreadable: {why[:60]})" for lang, why in unreadable.items()}

    def page(headline: str, note: str = "") -> str:
        return (f"### {headline}\n\n" + (note + "\n\n" if note else "") + f"{_coverage_line(track, team)}\n\n"
                f"{_results_table(rd['files'], results)}\n\n" + (zip_level + "\n\n" if zip_level else "") + details)

    if not items:
        tmpfiles.remove(p.gz_path for p in items.values())
        return fail(page(f"✗ Not submitted — team **{team.name}**",
                         "**No file passed validation.** Fix the errors listed in the table and the validator output, then submit again."), report_path)
    allowed, retry = limit_record_ip.hit(addr)
    what, limiter = "submissions from one address", limit_record_ip
    if allowed:
        allowed, retry = limit_record_team.hit(team.slug)
        what, limiter = "submissions by one team", limit_record_team
    if not allowed:
        log.warning("rate limit: record addr=%s team=%s", addr, team.slug)
        tmpfiles.remove(p.gz_path for p in items.values())
        return fail(page(f"✗ Not submitted — team **{team.name}**", _too_many(what, limiter, retry)), report_path)
    try:
        summary = slots.submit_many(track, team.slug, team.name, items, Meta(submitter_email=email.strip()),
                                    replace_oldest=bool(replace_oldest))
    except SlotError as exc:
        tmpfiles.remove(p.gz_path for p in items.values())
        return fail(page(f"✗ Not submitted — team **{team.name}**", f"**Not submitted:** {exc}"), report_path)
    except Exception as exc:
        log.exception("submit failed")
        tmpfiles.remove(p.gz_path for p in items.values())
        return fail(page(f"✗ Not submitted — team **{team.name}**",
                         f"**Not submitted:** the storage backend failed ({type(exc).__name__}). Nothing was saved; "
                         f"please retry in a minute or email {ORGANIZER_EMAIL}."), report_path)
    tmpfiles.remove(p.gz_path for p in items.values())
    for i in summary["items"]:
        results[i["language"]] = RESULT_TEXT[i["action"]].format(slot=i["slot"], receipt=i["receipt_id"], note=i["note"])
    written = [i for i in summary["items"] if i["action"] in ("slot", "replace")]
    if not summary.get("stored") or not written:
        why = ("every file that passed is already recorded (identical to a stored run), or its language is full "
               "(tick *replace the oldest run* to overwrite) or capped.")
        return fail(page(f"· Nothing new to submit — team **{team.name}**", f"**Nothing was written:** {why}"), report_path)
    log.info("recorded team=%s track=%s langs=%s receipt=%s", team.slug, track, [i["language"] for i in written], summary["bulk_receipt_id"])
    receipt_path = _tmp_json(f"mast-receipt-{summary['bulk_receipt_id']}-", summary)
    body = (f"MAST 2026 submission receipt\n\nTeam: {team.name}\nTrack: {track}\nReceipt id: {summary['bulk_receipt_id']}\n"
            f"Uploaded at: {summary['uploaded_at']}\nSubmitted by: {summary['submitter_email']}\n\n"
            + "\n".join(f"{i['language']}: {i['action']}" + (f" slot {i['slot']} receipt {i['receipt_id']} sha256 {i['sha256']} llm {i['llm']} retriever {i['retriever']}"
                                                              if i['receipt_id'] else f" ({i['note']})") for i in summary["items"]) + "\n")
    mailed = send_receipt(settings, [summary["submitter_email"], *team.notify_emails(track)],
                          f"[MAST 2026] receipt {summary['bulk_receipt_id']} · {team.name} · {track}", body)
    n = len(written)
    headline = f"✓ Submitted: {n} language{'s' if n != 1 else ''} recorded — team **{team.name}** · receipt `{summary['bulk_receipt_id']}`"
    note = ("A copy of the receipt was emailed to the submitter and the team contact." if mailed
            else "Download the receipt below and keep it; it is your proof of submission.")
    if any(fr["errors"] for fr in present):
        note += " Files with errors were **not** submitted; fix them and submit again (unchanged languages are skipped automatically)."
    return page(headline, note), gr.update(value=report_path, visible=True), gr.update(value=receipt_path, visible=True)


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
  The track must be one your team registered for, and the contact email must be one of the addresses on the
  registration form.
* Only agentic runs are accepted.
* Every upload gets a receipt id and a sha256 per language. Keep the receipt. Organizers evaluate exactly the
  stored bytes.
* One button: Submit validates the upload and records every file that passes. Files with errors are never
  stored; fix them and submit again, unchanged languages are skipped automatically.
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
            email = gr.Textbox(label="Contact email", placeholder="an address from your team's registration")
        with gr.Column():
            upload = gr.File(label="Run file(s): one .jsonl / .jsonl.gz, or a .zip / .tar / .tar.gz / .tgz with one per language",
                             file_types=[".jsonl", ".gz", ".zip", ".tar", ".tgz"], type="filepath")
            replace_oldest = gr.Checkbox(label="Replace the oldest run for languages whose 3 slots are already full", value=False)
    submit_btn = gr.Button("Submit", variant="primary")
    status = gr.Markdown()
    with gr.Row():
        report_dl = gr.DownloadButton("Download validation report (JSON)", visible=False)
        receipt_dl = gr.DownloadButton("Download receipt (JSON)", visible=False)
    with gr.Accordion("Rules and help", open=False):
        gr.Markdown(RULES)
    gr.Markdown("<small>Registration closes with the run deadline. New registrations are recognized within a minute.</small>")

    demo.load(on_load, outputs=[closed_banner])
    submit_btn.click(on_submit, inputs=[team_name, track, email, replace_oldest, upload],
                     outputs=[status, report_dl, receipt_dl])

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch(max_file_size="200mb", show_error=True)
