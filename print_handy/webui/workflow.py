"""The Web UI workflow page: link or upload in, download or print out (#1450).

One on-page workflow for the two ways a browser user hands Print Handy
a model: paste a link (the form here, or the one on the Sources page)
or upload a file (the form here, or the Upload page's per-file start
button). Starting either submits the link or the file to the web chat
service exactly as if it had been dropped in chat, so the SAME intake
runs: same questions, same order, same defaults and refusals, answered
here with the page's own buttons instead of a chat transcript. The
page never slices, prints or approves anything itself; every answer
goes through the prompt machinery the chat page uses, and the print
ending is the intake's own Start button, which records the same
bed-clear approval a chat start records.

The download ending is the one thing web chat cannot do: its adapter
turns an attachment into a note naming the file. Here, the intake's
receipt is bound to this session (a receipt records the chat thread it
ran in, and a web session's scope is that thread), so when the intake
ends sliced-not-started, this page serves the exact sliced files the
receipt names, one download per plate. A receipt from another session
names nothing here.

Leaving and returning reattaches: the page renders from the session's
own chat view and its newest receipt, so a workflow in progress shows
its open question, a finished one shows its downloads, and nothing is
ever submitted twice by a page load. While a workflow is running (a
turn in flight or a question open), the start forms are not offered;
the running workflow is the page.

When the Sliced files page arrives (the #1443 work, draft PR #1444),
its archive-wide downloads will supersede this page's per-receipt
ones; until then the receipt is the record that names the file, the
same way the designs page serves a session's files from its record.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .. import settings
from . import chat as chat_module
from . import shell, sliced, transcript

_esc = shell._esc
REFRESH_SECONDS = 3
WORKING_TEXT = "Working on it…"

_CONTENT_TYPES = {".gcode": "text/plain; charset=utf-8",
                  ".3mf": "application/octet-stream"}


def intake_root(data) -> Path:
    """The folder intake receipts live in, for these settings."""
    return settings.state_dir(data) / "model-link-intakes"


def archive_root(data) -> Path:
    """The job archive sliced plates are written under, for these settings."""
    from . import data as data_module
    return data_module.archive_root(data)


def _receipts(data):
    """Every readable intake receipt, newest first by file mtime."""
    root = intake_root(data)
    try:
        files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except OSError:
        return []
    rows = []
    for path in files:
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(receipt, dict):
            rows.append(receipt)
    return rows


def _for_scope(receipt, scope):
    """A receipt belongs to the web session whose scope it ran in."""
    return (receipt.get("platform") == "webui"
            and str(receipt.get("thread_id") or "") == str(scope or ""))


def latest_receipt(data, scope):
    """This session's newest intake receipt, or None."""
    for receipt in _receipts(data):
        if _for_scope(receipt, scope):
            return receipt
    return None


def receipt_for(data, scope, intake_id):
    """This session's receipt with this intake id, or None.

    The scope check is the authorization: an intake id from another
    session's workflow names nothing here."""
    for receipt in _receipts(data):
        if receipt.get("intake_id") == intake_id and _for_scope(receipt, scope):
            return receipt
    return None


def download_target(data, scope, intake_id, index):
    """The ``index``-th sliced file of this session's receipt, or None.

    Only a file the receipt itself names (its ``gcodes``, recorded at
    slice time) can be served, and only while it is still there. The
    receipt is not trusted for what kind of file that is: the name must
    be a plate file name (.gcode or .gcode.3mf), the rule the Sliced
    files page applies, so a receipt can never hand out anything else
    (review of #1464)."""
    if not re.fullmatch(r"[0-9]{1,4}", str(index or "")):
        return None
    receipt = receipt_for(data, scope, intake_id)
    if receipt is None:
        return None
    gcodes = receipt.get("gcodes") or []
    if int(index) >= len(gcodes):
        return None
    try:
        path = Path(str(gcodes[int(index)])).resolve()
    except OSError:
        return None
    if not sliced._PLATE_NAME.fullmatch(path.name) or not path.is_file():
        return None
    # Receipts record output paths, but may be stale or modified. Only serve
    # a plate that remains inside the configured archive, matching /sliced.
    try:
        # Plates are written under the job archive, not beside the receipts
        # (#1514 checked the receipts folder, so every real download was 404).
        archive = archive_root(data).resolve()
        if not path.is_relative_to(archive):
            return None
    except (OSError, ValueError):
        return None
    return path


def content_type(path):
    """The download's content type: a .gcode.3mf is a package, a .gcode is text."""
    if path.name.endswith(".3mf"):
        return _CONTENT_TYPES[".3mf"]
    return _CONTENT_TYPES[".gcode"]


def render_workflow_disabled_page(*, theme=shell.DEFAULT_THEME, nav=None):
    """The GET /workflow page while [webui] chat_enabled is false.

    The workflow runs on the web chat service, which does not exist
    behind a disabled chat page, so there is nothing to start: the
    page says the state and the way back on, like the chat page."""
    body = ('<p>Web chat is turned off, and the workflow runs through '
            'web chat, so no workflow can be started here. The workflow '
            'is disabled by the <code>webui.chat_enabled</code> setting.</p>'
            '<p>To turn it on, set <strong>Web chat</strong> on in Settings '
            '(or <code>webui.chat_enabled</code> / '
            '<code>PRINT_HANDY__WEBUI__CHAT_ENABLED</code> to '
            '<code>true</code>) and restart Print Handy.</p>')
    return shell.page("workflow", body, path="/workflow", theme=theme,
                      heading="Print workflow", nav=nav)


def _start_forms(form_token):
    return (
        "<h2>Start with a link</h2>"
        "<p>Paste a link to a model (MakerWorld, Printables, Thingiverse, "
        "or a direct file link). Print Handy fetches it and walks you "
        "through the same questions chat asks, right here.</p>"
        '<form method="post" action="/workflow/link">'
        f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
        '<div class="field"><label for="w-url">Model link</label>'
        '<input id="w-url" name="url" type="url" required '
        'placeholder="https://…"></div>'
        '<button type="submit" class="accent">Start the workflow</button></form>'
        "<h2>Start with a file</h2>"
        "<p>Upload a model file (.stl or .3mf) and the workflow starts "
        "for it at once. Files already in the "
        '<a href="/upload">upload inbox</a> can be started from there '
        "instead.</p>"
        '<form method="post" action="/workflow/upload" '
        'enctype="multipart/form-data">'
        f'<input type="hidden" name="_csrf" value="{_esc(form_token)}">'
        '<div class="field"><label for="w-file">Model file</label>'
        '<input id="w-file" type="file" name="file" accept=".stl,.3mf" '
        'required></div>'
        '<button type="submit" class="accent">Upload and start</button></form>')


def _result_section(receipt):
    """The finished workflow's ending: downloads, or the recorded start."""
    if not receipt:
        return ""
    parts = []
    if receipt.get("bed_clear_approval"):
        parts.append(
            "<h2>Result</h2><p>This workflow sent the print to the "
            "printer: the bed-clear approval is recorded on the intake "
            "receipt, the same record a start from chat makes.</p>")
    gcodes = receipt.get("gcodes") or []
    if receipt.get("status") == "SLICED_NOT_STARTED" and gcodes:
        links = "".join(
            f'<p><a href="/workflow/download/'
            f'{_esc(str(receipt.get("intake_id") or ""))}/{index}">'
            f'Download {_esc(Path(str(path)).name)}</a></p>'
            for index, path in enumerate(gcodes))
        parts.append(
            "<h2>Your sliced file</h2>"
            "<p>Sliced and not printed. Download the file to print it "
            "by other means: a USB stick, an SD card, or the printer's "
            "own screen.</p>" + links)
    return "".join(parts)


def render_workflow_page(view, receipt, *, form_token, notice=None, error=None,
                         theme=shell.DEFAULT_THEME, nav=None):
    """The GET /workflow page for one session's ScopedChat view.

    ``view`` is the session's web chat view (entries, busy, open_prompt);
    ``receipt`` is the session's newest intake receipt, or None."""
    prompt = view.open_prompt()
    pending = view.busy() and not prompt
    entries = view.entries()
    parts = []
    if pending:
        seq = entries[-1]["seq"] if entries else 0
        parts.append(f'<noscript><meta http-equiv="refresh" content="{REFRESH_SECONDS}"></noscript>')
        # The chat page's poll, against this page's own state route.
        parts.append(chat_module.poll_script(seq, "/workflow/state"))
    if notice:
        parts.append(f'<p class="notice">{_esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    if not prompt and not view.busy():
        parts.append(_start_forms(form_token))
    log = transcript.render_log(entries, open_prompt=prompt, working=pending)
    steps = bool(log)
    if log:
        parts.append("<h2>Progress</h2>" + log)
    if prompt:
        parts.append("<h2>Current step</h2>"
                     + transcript.render_prompt(prompt, action="/workflow/choice",
                                                form_token=form_token))
    parts.append(_result_section(receipt))
    if not steps and not prompt and not view.busy():
        parts.append("<p>Nothing is running. Start above with a link or "
                     "a file, and the questions appear here as buttons, "
                     "ending in a download or a print start.</p>")
    return shell.page("workflow", "".join(parts), path="/workflow",
                      theme=theme, heading="Print workflow", nav=nav)


__all__ = ["content_type", "download_target", "intake_root", "latest_receipt",
           "receipt_for", "render_workflow_disabled_page", "render_workflow_page"]
