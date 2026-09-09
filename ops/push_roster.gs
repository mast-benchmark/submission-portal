/**
 * MAST 2026: push the registration responses to the private submissions dataset on every form submission.
 *
 * Setup (no Google Cloud project needed):
 *   1. Open the responses spreadsheet -> Extensions -> Apps Script, paste this file, save.
 *   2. Project Settings -> Script Properties -> add HF_TOKEN = a Hugging Face *fine-grained* token with
 *      write access to only the dataset mast-benchmark/mast-2026-submissions.
 *   3. Run pushRoster once from the editor and accept the authorization prompt (your own account, own script).
 *   4. Triggers (clock icon) -> Add trigger: function pushRoster, event source "From spreadsheet", type "On form submit";
 *      another "From spreadsheet" / "On change" (catches edits to the sheet); and a "Time-driven" one every 2 hours
 *      as a backstop. Triggers are per Google account: only the account that added them sees them.
 *   The portal reads responses.csv within a minute of each push. Nothing is published anywhere.
 */
const REPO = "mast-benchmark/mast-2026-submissions";
const PATH = "responses.csv";
const SHEET = "Form Responses 1";

function pushRoster() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET);
  const values = sheet.getDataRange().getValues();
  const tz = Session.getScriptTimeZone();
  const csv = values.map(row => row.map(cell => {
    const s = cell instanceof Date ? Utilities.formatDate(cell, tz, "yyyy-MM-dd HH:mm:ss") : String(cell);
    return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }).join(",")).join("\n") + "\n";
  const token = PropertiesService.getScriptProperties().getProperty("HF_TOKEN");
  if (!token) throw new Error("Script property HF_TOKEN is not set");
  const body =
    JSON.stringify({key: "header", value: {summary: "roster: registration responses (" + (values.length - 1) + " rows)"}}) + "\n" +
    JSON.stringify({key: "file", value: {path: PATH, encoding: "base64", content: Utilities.base64Encode(csv, Utilities.Charset.UTF_8)}}) + "\n";
  const resp = UrlFetchApp.fetch("https://huggingface.co/api/datasets/" + REPO + "/commit/main", {
    method: "post", contentType: "application/x-ndjson",
    headers: {Authorization: "Bearer " + token}, payload: body, muteHttpExceptions: true,
  });
  if (resp.getResponseCode() >= 300) throw new Error("upload failed: " + resp.getResponseCode() + " " + resp.getContentText());
  console.log("pushed " + (values.length - 1) + " registration rows to " + REPO + "/" + PATH);
}
