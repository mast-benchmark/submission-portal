"""MAST @ FIRE 2026 run submission portal (Gradio Space).

Two forms: one language at a time (language declared, one slot), or a whole
track as a zip of per-language files (languages read from the records, many
slots in one atomic commit). Files are validated with ``mast_validate`` in
memory; only clean files reach storage. Team names are honor-system against a
private roster that is never rendered.
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
from portal.slots import Meta, SlotError, SlotKey, SlotService, prepare, prepare_bytes
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

Up to **3 runs per language per team**. Upload either **one `.jsonl` per language** or **one zip per track**
holding one `.jsonl` file per language (any filenames; the records' `language`, `llm` and `retriever` fields say
what the run is). Every file is
checked against the official query ids and the corpus before it is stored; a file with errors is never recorded.
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


# ---------------------------------------------------------------- single language

def slot_panel(key: SlotKey, team: Team, manifest: dict[str, Any]) -> str:
    used = slots.filled_slots(manifest)
    rows = []
    for n in range(1, settings.max_slots + 1):
        s = used.get(n)
        if s:
            rows.append(f"| {n} | {s.get('uploaded_at', '')[:19].replace('T', ' ')} UTC | {s.get('llm', '')} | "
                        f"{s.get('retriever', '')} | {s.get('warnings', 0)} | `{s.get('sha256', '')[:8]}` |")
        else:
            rows.append(f"| {n} | *empty* | | | | |")
    table = ("| Slot | Uploaded | LLM | Retriever | Warnings | sha256 |\n|---|---|---|---|---|---|\n" + "\n".join(rows))
    return (f"#### Slots for {team.name} · {key.track} / {display_name(key.lang)}\n\n{table}\n\n"
            f"{_coverage_line(key.track, team)}\n\nUploads used for this language: "
            f"{manifest.get('uploads', 0)} / {settings.max_uploads_per_key}")


def on_validate(team_name, track, system_type, email, file):
    """Validate one file; its language comes from the records. On success show the slot panel."""
    def fail(msg: str, report_path: Optional[str] = None):
        return (msg, gr.update(value=report_path, visible=report_path is not None), gr.update(value="", visible=False),
                gr.update(visible=False, choices=[], value=None), gr.update(visible=False), None)

    err, team, path = _common_checks(team_name, track, system_type, email, file)
    if err:
        return fail(err)
    original = os.path.basename(path)
    if is_zip(path):
        return fail("This is a zip archive. Use the **Whole track (zip)** tab for it, or upload one per-language `.jsonl` here.")
    try:
        report = single_file_report(path, track=track, lang=None, name=original)
    except UsageProblem as exc:
        return fail(f"Could not validate: {exc}")
    lang = report.files[0].language
    log.info("validated team=%s track=%s lang=%s file=%s size=%d status=%s errors=%d warnings=%d",
             team.slug, track, lang, original, os.path.getsize(path), report.status, report.n_errors, report.n_warnings)
    text = render(report, color=False, unicode=True)
    report_path = _tmp_json(f"mast-report-{track}-{lang}-", report.to_dict())
    note = _track_note(team, track)
    if report.status == "errors":
        return fail(f"### ✗ Validation failed — nothing was recorded\nFix the errors below and upload again.\n\n```text\n{text}```{note}", report_path)
    prepared = prepare(path, original, report.to_dict())
    key = SlotKey(track, lang, team.slug)
    manifest, _ = slots.manifest(key, team.name)
    free = slots.next_free_slot(manifest)
    head = ("### ✓ Validation passed" + (" with warnings" if report.n_warnings else "")
            + f" — team **{team.name}**\nRead from the records: language **{display_name(lang)}**, LLM **{report.files[0].llm}**, "
            f"retriever **{report.files[0].retriever}**.\nReview the slots below, then submit.")
    if free is not None:
        submit = gr.update(visible=True, value=f"Submit to slot {free}")
        replace = gr.update(visible=False, choices=[], value=None)
    else:
        used = slots.filled_slots(manifest)
        choices = [(f"Replace slot {n}: {s.get('llm')} uploaded {s.get('uploaded_at', '')[:16].replace('T', ' ')} UTC "
                    f"({s.get('warnings', 0)} warnings)", n) for n, s in sorted(used.items())]
        submit = gr.update(visible=True, value="Replace the selected slot")
        replace = gr.update(visible=True, choices=choices, value=None,
                            label="All 3 slots are filled. Choose which run to replace (its receipt is kept):")
    state = {"team": team, "track": track, "lang": lang, "prepared": prepared, "full": free is None,
             "meta": Meta(system_type=system_type, submitter_email=email.strip())}
    return (f"{head}\n\n```text\n{text}```{note}", gr.update(value=report_path, visible=True),
            gr.update(value=slot_panel(key, team, manifest), visible=True), replace, submit, state)


def _receipt_body(receipt: dict[str, Any], key: SlotKey) -> str:
    replaced = receipt.get("replaced")
    return (f"MAST 2026 submission receipt\n\nTeam: {receipt['team']}\nTrack/language: {key.track}/{key.lang}\n"
            f"Slot: {receipt['slot']}{' (replaced ' + replaced['receipt_id'] + ')' if replaced else ''}\n"
            f"Receipt id: {receipt['receipt_id']}\nsha256 (stored .gz): {receipt['sha256']}\n"
            f"sha256 (content): {receipt['content_sha256']}\nRecords: {receipt['records']}\n"
            f"Warnings: {receipt['warnings']} {receipt['warning_kinds']}\nLLM: {receipt['llm']}\n"
            f"Retriever: {receipt['retriever']}\nSystem type: {receipt['system_type']}\n"
            f"Uploaded at: {receipt['uploaded_at']}\nSubmitted by: {receipt['submitter_email']}\n")


def on_submit(state, replace_choice):
    """Commit the prepared upload. Clears the state afterwards so a double click cannot resubmit."""
    keep = (gr.update(), gr.update(), gr.update())
    if settings.is_closed():
        return closed_message(), gr.update(visible=False), *keep, None
    if not state:
        return "Validate a file first.", gr.update(visible=False), *keep, None
    if state["full"] and replace_choice is None:
        return "Choose which slot to replace before submitting.", gr.update(visible=False), *keep, state
    team: Team = state["team"]
    key = SlotKey(state["track"], state["lang"], team.slug)
    try:
        receipt = slots.submit(key, team.name, state["prepared"], state["meta"],
                               replace_slot=int(replace_choice) if state["full"] else None)
    except SlotError as exc:
        return f"**Not recorded:** {exc}", gr.update(visible=False), *keep, state
    except Exception as exc:
        log.exception("submit failed")
        return (f"**Not recorded:** the storage backend failed ({type(exc).__name__}). Nothing was saved; "
                f"please retry in a minute or email {ORGANIZER_EMAIL}.", gr.update(visible=False), *keep, state)
    log.info("recorded team=%s %s/%s slot=%s receipt=%s", team.slug, key.track, key.lang, receipt["slot"], receipt["receipt_id"])
    receipt_path = _tmp_json(f"mast-receipt-{receipt['receipt_id']}-", receipt)
    body = _receipt_body(receipt, key)
    mailed = send_receipt(settings, [receipt["submitter_email"], *team.notify_emails],
                          f"[MAST 2026] receipt {receipt['receipt_id']} · {team.name} · {key.track}/{key.lang}", body)
    md = (f"### ✓ Recorded in slot {receipt['slot']} · receipt `{receipt['receipt_id']}`\n\n```text\n{body}```\n"
          + ("A copy was emailed to the submitter and the team contact.\n" if mailed else
             "Download the receipt below and keep it; it is your proof of submission.\n"))
    manifest, _ = slots.manifest(key, team.name)
    return (md, gr.update(value=receipt_path, visible=True), gr.update(value=slot_panel(key, team, manifest)),
            gr.update(visible=False), gr.update(visible=False, choices=[], value=None), None)


# ---------------------------------------------------------------- whole track (zip)

def _verdict(fr: dict[str, Any]) -> str:
    if not fr["present"]:
        return "missing"
    if fr["errors"]:
        return f"✗ {fr['errors']} error{'s' if fr['errors'] != 1 else ''}"
    return "✓" + (f" {fr['warnings']} warning{'s' if fr['warnings'] != 1 else ''}" if fr["warnings"] else "")


def _plan_table(files: list[dict[str, Any]], actions: dict[str, str]) -> str:
    rows = ["| Language | File | Records | LLM · retriever | Validation | Action |", "|---|---|---|---|---|---|"]
    for fr in files:
        lang = fr["language"] or "?"
        name = "" if not fr["present"] else f"`{fr['name']}`"
        action = actions.get(lang, "—" if not fr["present"] else "not recorded (errors)")
        if not fr["present"]:
            action = "— (no file in the zip)"
        meta = f"{fr.get('llm') or ''} · {fr.get('retriever') or ''}" if fr["present"] else ""
        rows.append(f"| {display_name(lang) if fr['language'] else '?'} | {name} | {fr['records'] if fr['present'] else ''} | {meta} | {_verdict(fr)} | {action} |")
    return "\n".join(rows)


def on_validate_zip(team_name, track, system_type, email, replace_oldest, file):
    """Validate every member of a track zip and show what would be recorded where."""
    def fail(msg: str, report_path: Optional[str] = None):
        return (msg, gr.update(value=report_path, visible=report_path is not None), gr.update(value="", visible=False),
                gr.update(visible=False), None)

    err, team, path = _common_checks(team_name, track, system_type, email, file)
    if err:
        return fail(err)
    if not is_zip(path):
        return fail("This is not a zip archive. Use the **One language** tab for a single `.jsonl`, or zip your per-language files.")
    try:
        report = validate_zip(path, track=track)
    except UsageProblem as exc:
        return fail(f"Could not validate: {exc}")
    log.info("validated zip team=%s track=%s size=%d status=%s errors=%d warnings=%d files=%d", team.slug, track,
             os.path.getsize(path), report.status, report.n_errors, report.n_warnings, len(report.files))
    rd = report.to_dict()
    report_path = _tmp_json(f"mast-report-{track}-zip-", rd)
    text = render(report, color=False, unicode=True)
    # collect clean members and plan their slots
    items = {}
    with zipfile.ZipFile(path) as zf:
        for fr in report.files:
            if fr.present and fr.language and not fr.errors:
                sub = {"validator": rd["validator"], "generated_at": rd["generated_at"], "track": track,
                       "source": fr.name, "status": "warnings" if fr.warnings else "ok", "files": [fr.to_dict()]}
                items[fr.language] = prepare_bytes(zf.read(fr.name), fr.name, sub)
    manifests, _ = slots.manifests(track, team.slug, team.name, sorted(items)) if items else ({}, "")
    plan = slots.plan(manifests, items, replace_oldest=bool(replace_oldest)) if items else []
    actions = {}
    for it in plan:
        old = it.replaced or {}
        actions[it.lang] = ACTION_TEXT[it.action].format(
            slot=it.slot, note=it.note, old=f"{old.get('llm', '')}, {old.get('uploaded_at', '')[:10]}")
    writable = [it for it in plan if it.writes]
    table = _plan_table(rd["files"], actions)
    note = _track_note(team, track)
    zip_level = "\n".join(f"* {f['message']}" + (f" (e.g. {', '.join(f['examples'][:5])})" if f["examples"] else "")
                          for f in rd["findings"])
    head = (f"### {'✓' if items else '✗'} {len(items)} of {len(TRACKS[track])} languages ready to record — team **{team.name}**\n"
            f"{_coverage_line(track, team)}\n\n{table}\n\n" + (zip_level + "\n\n" if zip_level else ""))
    if not writable:
        why = "Nothing to record: " + ("no member passed validation." if not items else "every clean language is unchanged, skipped or capped.")
        return fail(head + why + f"\n\n<details><summary>Full validator output</summary>\n\n```text\n{text}```\n</details>{note}", report_path)
    state = {"team": team, "track": track, "items": items, "replace_oldest": bool(replace_oldest),
             "meta": Meta(system_type=system_type, submitter_email=email.strip())}
    md = head + (f"Languages with errors are **not** recorded; fix them and upload the zip again (unchanged languages are skipped automatically).\n\n"
                 if any(fr["present"] and fr["errors"] for fr in rd["files"]) else "") + \
        f"<details><summary>Full validator output</summary>\n\n```text\n{text}```\n</details>{note}"
    return (md, gr.update(value=report_path, visible=True), gr.update(value="", visible=False),
            gr.update(visible=True, value=f"Record {len(writable)} language{'s' if len(writable) != 1 else ''}"), state)


def on_submit_zip(state):
    keep = (gr.update(), gr.update())
    if settings.is_closed():
        return closed_message(), gr.update(visible=False), *keep, None
    if not state:
        return "Validate a zip first.", gr.update(visible=False), *keep, None
    team: Team = state["team"]
    track = state["track"]
    try:
        summary = slots.submit_many(track, team.slug, team.name, state["items"], state["meta"],
                                    replace_oldest=state["replace_oldest"])
    except SlotError as exc:
        return f"**Not recorded:** {exc}", gr.update(visible=False), *keep, state
    except Exception as exc:
        log.exception("bulk submit failed")
        return (f"**Not recorded:** the storage backend failed ({type(exc).__name__}). Nothing was saved; "
                f"please retry in a minute or email {ORGANIZER_EMAIL}.", gr.update(visible=False), *keep, state)
    written = [i for i in summary["items"] if i["action"] in ("slot", "replace")]
    log.info("recorded zip team=%s track=%s langs=%s bulk=%s", team.slug, track, [i["language"] for i in written],
             summary["bulk_receipt_id"])
    receipt_path = _tmp_json(f"mast-bulk-receipt-{summary['bulk_receipt_id']}-", summary)
    rows = ["| Language | Result | Slot | Receipt | sha256 |", "|---|---|---|---|---|"]
    for i in summary["items"]:
        res = {"slot": "recorded", "replace": "recorded (replaced " + ((i.get("replaced") or {}).get("receipt_id") or "?") + ")",
               "unchanged": "unchanged", "skipped_full": "skipped: full", "capped": "not recorded: cap"}[i["action"]]
        rows.append(f"| {display_name(i['language'])} | {res} | {i['slot'] or ''} | `{i['receipt_id'] or ''}` | `{(i['sha256'] or '')[:12]}` |")
    body = (f"MAST 2026 zip upload receipt\n\nTeam: {team.name}\nTrack: {track}\nBulk receipt id: {summary['bulk_receipt_id']}\n"
            f"Uploaded at: {summary['uploaded_at']}\n\n"
            + "\n".join(f"{i['language']}: {i['action']}" + (f" slot {i['slot']} receipt {i['receipt_id']} sha256 {i['sha256']} llm {i['llm']} retriever {i['retriever']}" if i['receipt_id'] else f" ({i['note']})")
                        for i in summary["items"]) + "\n")
    mailed = send_receipt(settings, [state["meta"].submitter_email, *team.notify_emails],
                          f"[MAST 2026] zip receipt {summary['bulk_receipt_id']} · {team.name} · {track}", body)
    md = (f"### {'✓' if written else '·'} {len(written)} language{'s' if len(written) != 1 else ''} recorded · bulk receipt `{summary['bulk_receipt_id']}`\n\n"
          + "\n".join(rows) + "\n\n" + _coverage_line(track, team) + "\n\n"
          + ("A copy was emailed to the submitter and the team contact.\n" if mailed else
             "Download the receipt below and keep it; it is your proof of submission.\n"))
    return md, gr.update(value=receipt_path, visible=True), gr.update(), gr.update(visible=False), None


def on_load():
    return gr.update(value=closed_message(), visible=True) if settings.is_closed() else gr.update(visible=False)


RULES = f"""
* Three slots per (team, track, language). On the single-language form, when all three are filled you choose which
  run to replace. On the zip form, full languages are skipped unless you tick *replace the oldest run*.
  A replaced run's receipt is kept, so nothing is lost silently.
* Every upload gets a receipt id and a sha256. Keep the receipt. Organizers evaluate exactly the stored bytes.
* Errors block a file; warnings do not, but each one costs score. `Exact Answer:` must appear in the final
  `output_text`; unknown docids score as misses; unprefixed `query_id`s are accepted and reconstructed.
* A file's language is read from its records: every record's `language` must agree, and query-id prefixes must
  match it. Filenames are ignored.
* Overlap languages (bn, hi, ta) exist in both tracks; submit to each track you take part in.
* Questions: {ORGANIZER_EMAIL}. Validator version {validator_version}.
"""

with gr.Blocks(title="MAST 2026 submission", analytics_enabled=False) as demo:
    gr.Markdown(HEADER)
    closed_banner = gr.Markdown(visible=False)
    with gr.Tabs():
        with gr.Tab("Whole track (zip)"):
            gr.Markdown("One zip with one `.jsonl` (or `.jsonl.gz`) per language. Each clean language goes to its next free slot.")
            with gr.Row():
                with gr.Column():
                    z_team = gr.Textbox(label="Team name", placeholder="exactly as on the registration form")
                    z_track = gr.Radio(choices=[(v, k) for k, v in TRACK_LABELS.items()], label="Track", value=None)
                    z_system = gr.Radio(choices=SYSTEM_TYPES, label="System type", value=None)
                    z_replace = gr.Checkbox(label="Replace the oldest run for languages whose 3 slots are already full", value=False)
                with gr.Column():
                    z_email = gr.Textbox(label="Contact email", placeholder="receipt goes here and to the team contact")
                    z_file = gr.File(label="Track zip", file_types=[".zip"], type="filepath")
            z_validate = gr.Button("Validate zip", variant="primary")
            z_status = gr.Markdown()
            z_report_dl = gr.DownloadButton("Download validation report (JSON)", visible=False)
            z_unused = gr.Markdown(visible=False)
            z_submit = gr.Button("Record", variant="primary", visible=False)
            z_result = gr.Markdown()
            z_receipt_dl = gr.DownloadButton("Download bulk receipt (JSON)", visible=False)
            z_state = gr.State(None)
        with gr.Tab("One language"):
            with gr.Row():
                with gr.Column():
                    team_name = gr.Textbox(label="Team name", placeholder="exactly as on the registration form")
                    track = gr.Radio(choices=[(v, k) for k, v in TRACK_LABELS.items()], label="Track", value=None)
                    system_type = gr.Radio(choices=SYSTEM_TYPES, label="System type", value=None)
                with gr.Column():
                    email = gr.Textbox(label="Contact email", placeholder="receipt goes here and to the team contact")
                    upload = gr.File(label="Run file (.jsonl or .jsonl.gz, one language; the records say which)", file_types=[".jsonl", ".gz"], type="filepath")
            validate_btn = gr.Button("Validate", variant="primary")
            status = gr.Markdown()
            report_dl = gr.DownloadButton("Download validation report (JSON)", visible=False)
            slot_md = gr.Markdown(visible=False)
            replace_radio = gr.Radio(choices=[], visible=False, value=None)
            submit_btn = gr.Button("Submit", variant="primary", visible=False)
            receipt_md = gr.Markdown()
            receipt_dl = gr.DownloadButton("Download receipt (JSON)", visible=False)
            state = gr.State(None)
    with gr.Accordion("Rules and help", open=False):
        gr.Markdown(RULES)
    gr.Markdown("<small>Registration closes with the run deadline. The team roster refreshes within a minute of an update.</small>")

    demo.load(on_load, outputs=[closed_banner])
    z_validate.click(on_validate_zip, inputs=[z_team, z_track, z_system, z_email, z_replace, z_file],
                     outputs=[z_status, z_report_dl, z_unused, z_submit, z_state])
    z_submit.click(on_submit_zip, inputs=[z_state], outputs=[z_result, z_receipt_dl, z_unused, z_submit, z_state])
    validate_btn.click(on_validate, inputs=[team_name, track, system_type, email, upload],
                       outputs=[status, report_dl, slot_md, replace_radio, submit_btn, state])
    submit_btn.click(on_submit, inputs=[state, replace_radio],
                     outputs=[receipt_md, receipt_dl, slot_md, submit_btn, replace_radio, state])

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch(max_file_size="200mb", show_error=True)
