"""MAST @ FIRE 2026 run submission portal (Gradio Space).

One upload = one (team, track, language) run file. The file is validated with
``mast_validate`` in memory; only clean files reach storage. Team names are
honor-system against a private roster that is never rendered.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
from typing import Any, Optional

import gradio as gr

from mast_validate import __version__ as validator_version
from mast_validate.languages import TRACKS, display_name
from mast_validate.report import render
from mast_validate.runner import UsageProblem, is_zip, single_file_report
from portal.config import FIRE_URL, ORGANIZER_EMAIL, SITE_URL, Settings
from portal.mailer import send_receipt
from portal.roster import Roster
from portal.slots import Meta, SlotError, SlotKey, SlotService, prepare
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


def lang_choices(track: Optional[str]) -> list[tuple[str, str]]:
    codes = TRACKS.get(track or "") or sorted({c for langs in TRACKS.values() for c in langs})
    return [(display_name(code), code) for code in codes]


def deadline_text() -> str:
    d = settings.deadline.astimezone(dt.timezone.utc)
    return f"{d:%A %d %B %Y, %H:%M} UTC (23:59 AoE on {(d - dt.timedelta(hours=12)):%d %B})"


def closed_message() -> str:
    return (f"### Submissions are closed\nThe run submission deadline was {deadline_text()}. "
            f"Contact {ORGANIZER_EMAIL} if you believe this is an error.")


HEADER = f"""
# MAST @ FIRE 2026 · Run submission

Upload **one `.jsonl` (or `.jsonl.gz`) file per language**, up to **3 runs per language per team**.
The file is checked against the official query ids and the corpus before it is stored; a file with
errors is not recorded. Deadline: **{deadline_text()}**.

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


def slot_panel(key: SlotKey, team_name: str, manifest: dict[str, Any]) -> str:
    used = slots.filled_slots(manifest)
    rows = []
    for n in range(1, settings.max_slots + 1):
        s = used.get(n)
        if s:
            rows.append(f"| {n} | {s.get('uploaded_at', '')[:19].replace('T', ' ')} UTC | {s.get('llm', '')} | "
                        f"{s.get('retriever', '')} | {s.get('warnings', 0)} | `{s.get('sha256', '')[:8]}` |")
        else:
            rows.append(f"| {n} | *empty* | | | | |")
    table = ("| Slot | Uploaded | LLM | Retriever | Warnings | sha256 |\n|---|---|---|---|---|---|\n"
             + "\n".join(rows))
    done = slots.languages_submitted(key.track, key.team_slug)
    total = len(TRACKS[key.track])
    strip = f"**{key.track}** coverage for *{team_name}*: **{len(done)} / {total}** languages have at least one run"
    if done:
        strip += f" ({', '.join(sorted(done))})"
    uploads = manifest.get("uploads", 0)
    return (f"#### Slots for {team_name} · {key.track} / {display_name(key.lang)}\n\n{table}\n\n{strip}\n\n"
            f"Uploads used for this language: {uploads} / {settings.max_uploads_per_key}")


def _hidden(*n: int):
    return [gr.update(visible=False)] * n[0] if n else gr.update(visible=False)


def on_track_change(track: Optional[str]):
    return gr.update(choices=lang_choices(track), value=None)


def on_validate(team_name, track, lang, llm, retriever, system_type, email, file):
    """Validate the upload; on success show the slot panel and enable submission."""
    no_submit = dict(submit=gr.update(visible=False), replace=gr.update(visible=False, choices=[], value=None),
                     state=None)

    def fail(msg: str, report_path: Optional[str] = None):
        return (msg, gr.update(value=report_path, visible=report_path is not None), gr.update(value="", visible=False),
                no_submit["replace"], no_submit["submit"], None)

    if settings.is_closed():
        return fail(closed_message())
    missing = [label for label, v in [("team name", team_name), ("track", track), ("language", lang), ("LLM", llm),
                                      ("retriever", retriever), ("system type", system_type),
                                      ("contact email", email), ("file", file)] if not v or (isinstance(v, str) and not v.strip())]
    if missing:
        return fail(f"Please fill in: {', '.join(missing)}.")
    if not EMAIL_RE.match(email.strip()):
        return fail("The contact email does not look like an email address.")
    if lang not in TRACKS[track]:
        return fail(f"'{lang}' is not a language of the {track} track.")
    if system_type == "Retrieval-only":
        return fail("**Retrieval-only submissions are not supported yet.** The submission format for retrieval-only "
                    f"runs is still being defined; watch the mailing list or email {ORGANIZER_EMAIL}. Nothing was recorded.")

    m = roster.match(team_name)
    if not m.team:
        slots.log_rejected_name(team_name, track, lang)
        hint = f" Did you mean **{m.suggestion}**?" if m.suggestion else ""
        return fail(f"No registered team matches **{team_name.strip()}**.{hint} Team names must match the "
                    f"registration form. If you registered under another spelling, email {ORGANIZER_EMAIL}.")
    team = m.team
    path = file if isinstance(file, str) else getattr(file, "name", None)
    if not path or not os.path.isfile(path):
        return fail("The upload did not arrive; please try again.")
    original = os.path.basename(path)
    if is_zip(path):
        return fail("This is a zip archive. The portal takes **one per-language `.jsonl` file per upload**; "
                    "please upload each language separately (the offline validator accepts zips, the portal does not).")

    try:
        report = single_file_report(path, track=track, lang=lang, name=original)
    except UsageProblem as exc:
        return fail(f"Could not validate: {exc}")
    log.info("validated team=%s track=%s lang=%s file=%s size=%d status=%s errors=%d warnings=%d",
             team.slug, track, lang, original, os.path.getsize(path), report.status, report.n_errors, report.n_warnings)
    text = render(report, color=False, unicode=True)
    report_path = _tmp_json(f"mast-report-{track}-{lang}-", report.to_dict())
    notes = []
    if track not in team.tracks and team.tracks:
        notes.append(f"Note: the registration for *{team.name}* lists only the **{', '.join(team.tracks)}** track; "
                     f"this upload is for **{track}**. Proceeding.")
    if report.status == "errors":
        head = "### ✗ Validation failed — nothing was recorded\nFix the errors below and upload again."
        return fail(f"{head}\n\n```text\n{text}```\n" + "\n".join(notes), report_path)

    prepared = prepare(path, original, report.to_dict())
    key = SlotKey(track, lang, team.slug)
    manifest, _ = slots.manifest(key, team.name)
    panel = slot_panel(key, team.name, manifest)
    free = slots.next_free_slot(manifest)
    head = ("### ✓ Validation passed" + (" with warnings" if report.n_warnings else "")
            + f" — matched team **{team.name}**\nReview the slots below, then submit.")
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
    state = {"team_name": team.name, "team_slug": team.slug, "notify": team.notify_emails, "track": track,
             "lang": lang, "prepared": prepared,
             "meta": Meta(llm=llm.strip(), retriever=retriever.strip(), system_type=system_type,
                          submitter_email=email.strip()),
             "full": free is None}
    return (f"{head}\n\n```text\n{text}```\n" + "\n".join(notes), gr.update(value=report_path, visible=True),
            gr.update(value=panel, visible=True), replace, submit, state)


def on_submit(state, replace_choice):
    """Commit the prepared upload. Clears the state afterwards so a double click cannot resubmit."""
    keep = (gr.update(), gr.update(), gr.update())
    if settings.is_closed():
        return closed_message(), gr.update(visible=False), *keep, None
    if not state:
        return "Validate a file first.", gr.update(visible=False), *keep, None
    if state["full"] and replace_choice is None:
        return ("Choose which slot to replace before submitting.", gr.update(visible=False), gr.update(),
                gr.update(), gr.update(), state)
    key = SlotKey(state["track"], state["lang"], state["team_slug"])
    try:
        receipt = slots.submit(key, state["team_name"], state["prepared"], state["meta"],
                               replace_slot=int(replace_choice) if state["full"] else None)
    except SlotError as exc:
        return f"**Not recorded:** {exc}", gr.update(visible=False), gr.update(), gr.update(), gr.update(), state
    except Exception as exc:  # storage failure: say so plainly
        log.exception("submit failed")
        return (f"**Not recorded:** the storage backend failed ({type(exc).__name__}). Nothing was saved; "
                f"please retry in a minute or email {ORGANIZER_EMAIL}.", gr.update(visible=False),
                gr.update(), gr.update(), gr.update(), state)
    log.info("recorded team=%s %s/%s slot=%s receipt=%s", key.team_slug, key.track, key.lang,
             receipt["slot"], receipt["receipt_id"])
    public = {k: v for k, v in receipt.items() if k != "report"}
    receipt_path = _tmp_json(f"mast-receipt-{receipt['receipt_id']}-", receipt)
    replaced = receipt.get("replaced")
    body = (f"MAST 2026 submission receipt\n\nTeam: {receipt['team']}\nTrack/language: {key.track}/{key.lang}\n"
            f"Slot: {receipt['slot']}{' (replaced ' + replaced['receipt_id'] + ')' if replaced else ''}\n"
            f"Receipt id: {receipt['receipt_id']}\nsha256 (stored .gz): {receipt['sha256']}\n"
            f"sha256 (content): {receipt['content_sha256']}\nRecords: {receipt['records']}\n"
            f"Warnings: {receipt['warnings']} {receipt['warning_kinds']}\nLLM: {receipt['llm']}\n"
            f"Retriever: {receipt['retriever']}\nSystem type: {receipt['system_type']}\n"
            f"Uploaded at: {receipt['uploaded_at']}\nSubmitted by: {receipt['submitter_email']}\n")
    mailed = send_receipt(settings, [receipt["submitter_email"], *state["notify"]],
                          f"[MAST 2026] receipt {receipt['receipt_id']} · {receipt['team']} · {key.track}/{key.lang}",
                          body)
    md = (f"### ✓ Recorded in slot {receipt['slot']} · receipt `{receipt['receipt_id']}`\n\n"
          f"```text\n{body}```\n"
          + ("A copy was emailed to the submitter and the team contact.\n" if mailed else
             "Download the receipt below and keep it; it is your proof of submission.\n"))
    manifest, _ = slots.manifest(key, state["team_name"])
    return (md, gr.update(value=receipt_path, visible=True), gr.update(value=slot_panel(key, state["team_name"], manifest)),
            gr.update(visible=False), gr.update(visible=False, choices=[], value=None), None)


def on_load():
    return gr.update(value=closed_message(), visible=True) if settings.is_closed() else gr.update(visible=False)


with gr.Blocks(title="MAST 2026 submission", analytics_enabled=False) as demo:
    gr.Markdown(HEADER)
    closed_banner = gr.Markdown(visible=False)
    with gr.Row():
        with gr.Column(scale=1):
            team_name = gr.Textbox(label="Team name", placeholder="exactly as on the registration form")
            track = gr.Radio(choices=[(v, k) for k, v in TRACK_LABELS.items()], label="Track", value=None)
            language = gr.Dropdown(choices=lang_choices(None), label="Language", value=None)  # all 21 until a track narrows it
            system_type = gr.Radio(choices=SYSTEM_TYPES, label="System type", value=None)
        with gr.Column(scale=1):
            llm = gr.Textbox(label="LLM", placeholder="e.g. Alibaba-NLP/Tongyi-DeepResearch-30B-A3B")
            retriever = gr.Textbox(label="Retriever", placeholder="e.g. Qwen/Qwen3-Embedding-8B or BM25")
            email = gr.Textbox(label="Contact email", placeholder="receipt goes here and to the team contact")
            upload = gr.File(label="Run file (.jsonl or .jsonl.gz, one language)", file_types=[".jsonl", ".gz"],
                             type="filepath")
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
        gr.Markdown(f"""
* Three slots per (team, track, language). When all three are filled you choose which run to replace; the
  replaced run's receipt is kept, so nothing is lost silently.
* Every upload gets a receipt id and a sha256. Keep the receipt. Organizers evaluate exactly the stored bytes.
* Errors block a submission; warnings do not, but each one costs score. `Exact Answer:` must appear in the final
  `output_text`; unknown docids score as misses; unprefixed `query_id`s are accepted and reconstructed.
* Overlap languages (bn, hi, ta) exist in both tracks; submit to each track you take part in.
* Questions: {ORGANIZER_EMAIL}. Validator version {validator_version}.
""")
    gr.Markdown(f"<small>Registration closes with the run deadline. The team roster refreshes within a minute of an update.</small>")

    demo.load(on_load, outputs=[closed_banner])
    track.change(on_track_change, inputs=[track], outputs=[language])
    validate_btn.click(on_validate,
                       inputs=[team_name, track, language, llm, retriever, system_type, email, upload],
                       outputs=[status, report_dl, slot_md, replace_radio, submit_btn, state])
    submit_btn.click(on_submit, inputs=[state, replace_radio],
                     outputs=[receipt_md, receipt_dl, slot_md, submit_btn, replace_radio, state])

if __name__ == "__main__":
    demo.queue(default_concurrency_limit=4).launch(max_file_size="200mb", show_error=True)
