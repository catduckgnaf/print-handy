"""Web UI firmware update routes (#1610): confirm, apply, status, dismiss.

The Firmware page stays the read side; these routes are the write side,
and they run the one confirmed apply flow in
:mod:`print_handy.firmware_update` — nothing here sends an upgrade of
its own:

- ``GET /firmware/update?printer=<id>&component=<name>`` validates the
  request against the live printer (``prepare_update``: eligibility,
  the live component, an update actually available, the printer idle)
  and renders the confirmation: the exact version change, the same
  prompt text chat shows, and roughly how long the update takes.
- ``POST /firmware/update`` (session + CSRF, like every browser form
  POST) re-prepares from scratch — a confirmation page is never
  trusted — and starts the update as a background job
  (:mod:`print_handy.webui.update_jobs`), then redirects to the status
  page. Applying additionally requires Web UI printer controls
  (``[webui] controls_enabled = true``, the #708 gate): while off, the
  confirmation page explains and the POST refuses, and nothing is sent.
- ``GET /firmware/update/status?job=<id>`` follows the job: elapsed
  time and the estimate while it runs (the page refreshes itself), and
  the verified outcome — ``send_update``'s own words — when it ends.
- ``POST /notifications/dismiss`` (session+CSRF; fields ``id``, ``back``) dismisses one
  toast and returns to the page it was dismissed from.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, urlparse

from .. import firmware_update as fw
from .. import settings
from . import controls as controls_module
from . import data as data_module
from . import firmware_page as firmware_page_module
from . import notifications as notifications_module
from . import shell
from . import update_jobs as update_jobs_module

_esc = shell._esc


def _send_page(handler, title, body, *, head_extra=""):
    handler._send(200, shell.page(
        title, body, path="/firmware", theme=handler._theme(),
        nav=data_module.nav_for(handler.server.settings_data),
        head_extra=head_extra), "text/html; charset=utf-8")


def _resolve(data, printer_id):
    """(row, entry) for a roster printer id, or (None, None)."""
    for row in settings.printers(data):
        if str(row.get("id")) == str(printer_id):
            return row, fw.printer_entry(row.get("model"))
    return None, None


def _query(handler):
    return parse_qs(urlparse(handler.path).query)


def _prompt_html(prepared) -> str:
    """The chat confirmation prompt as page HTML (** pairs become bold)."""
    text = _esc(fw.update_prompt_text(prepared))
    return "<p>" + re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>",
                          text).replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"


def update_confirm_route(handler):
    """GET /firmware/update: the confirmation page for one component update."""
    data = handler.server.settings_data
    query = _query(handler)
    printer_id = (query.get("printer") or [None])[0]
    component = (query.get("component") or [None])[0]
    row, entry = _resolve(data, printer_id) if printer_id else (None, None)
    if row is None:
        return _send_page(handler, "Firmware update",
                          "<p>That printer is not on the roster, so there is "
                          "nothing to update.</p>"
                          '<p><a href="/firmware">Back to Firmware</a></p>')
    name = str(row.get("name") or printer_id)
    if entry is None or entry.connection != "moonraker":
        return _send_page(handler, "Firmware update",
                          f"<h2>{_esc(name)}</h2><pre>"
                          f"{_esc(fw.guidance_text(entry))}</pre>"
                          '<p><a href="/firmware">Back to Firmware</a></p>')
    if not component:
        return handler._redirect(f"/firmware?printer={quote(str(printer_id), safe='')}")
    if not controls_module.controls_enabled(data):
        return _send_page(handler, "Firmware update",
                          f"<h2>{_esc(name)}</h2>"
                          f"<p>{_esc(controls_module.OFF_REASON)}</p>"
                          "<p>Until controls are on, updates apply from chat "
                          "with <code>/print-firmware update</code>, behind the "
                          "same confirmation.</p>"
                          '<p><a href="/firmware">Back to Firmware</a></p>')
    transport = firmware_page_module.printer_transport(data, str(printer_id))
    try:
        prepared = fw.prepare_update(transport, entry, component, data)
    except (fw.FirmwareRefusal, fw.FirmwareError) as exc:
        return _send_page(handler, "Firmware update",
                          f"<h2>{_esc(name)}</h2><pre>{_esc(str(exc))}</pre>"
                          '<p><a href="/firmware">Back to Firmware</a></p>')
    body = (f"<h2>{_esc(name)}</h2>"
            + _prompt_html(prepared)
            + f"<p>This update takes "
              f"{_esc(fw.update_estimate_text(prepared.component.name))}. "
              "After you confirm, a status page follows it until it "
              "finishes.</p>"
              '<form method="post" action="/firmware/update">'
              f'<input type="hidden" name="_csrf" value="{_esc(handler._form_token())}">'
              f'<input type="hidden" name="printer" value="{_esc(str(printer_id))}">'
              f'<input type="hidden" name="component" '
              f'value="{_esc(prepared.component.name)}">'
              '<button type="submit" class="accent">Update now</button> '
              f'<a class="button" href="/firmware?printer='
              f'{quote(str(printer_id), safe="")}">Cancel</a></form>')
    return _send_page(handler, "Firmware update", body)


def update_apply_route(handler, form):
    """POST /firmware/update: re-prepare, start the job, go to its status."""
    data = handler.server.settings_data
    if not controls_module.controls_enabled(data):
        return handler._html_refusal(403, controls_module.OFF_REASON,
                                     back="/firmware")
    printer_id = (form.get("printer") or [""])[0]
    component = (form.get("component") or [""])[0]
    row, entry = _resolve(data, printer_id) if printer_id else (None, None)
    if row is None or entry is None or entry.connection != "moonraker":
        return handler._html_refusal(
            404, "That printer is not on the roster or cannot be updated "
                 "from here. Nothing was sent.", back="/firmware")
    if not component:
        return handler._html_refusal(400, "No component was named. "
                                          "Nothing was sent.", back="/firmware")
    running = update_jobs_module.running_job_for(printer_id, component)
    if running is not None:
        return handler._redirect(f"/firmware/update/status?job={running.id}")
    transport = firmware_page_module.printer_transport(data, str(printer_id))
    try:
        prepared = fw.prepare_update(transport, entry, component, data)
    except (fw.FirmwareRefusal, fw.FirmwareError) as exc:
        return handler._html_refusal(409, str(exc),
                                     back=f"/firmware?printer={quote(printer_id, safe='')}")
    job = update_jobs_module.start_job(
        data, printer_id=str(printer_id),
        printer_name=str(row.get("name") or printer_id),
        transport=transport, prepared=prepared,
        on_finish=lambda finished: notifications_module.clear_observed_update(
            data, finished.printer_id, finished.component))
    return handler._redirect(f"/firmware/update/status?job={job.id}")


def _elapsed_text(seconds) -> str:
    minutes, secs = divmod(int(seconds), 60)
    if minutes and secs:
        return f"{minutes} min {secs} s"
    if minutes:
        return f"{minutes} min"
    return f"{secs} s"


def update_status_route(handler):
    """GET /firmware/update/status: one job, running or finished."""
    query = _query(handler)
    job = update_jobs_module.get_job((query.get("job") or [""])[0])
    if job is None:
        return handler._html_refusal(
            404, "That update job is not known. It may have finished before "
                 "a Web UI restart — jobs are kept in memory, while the "
                 "update itself is the printer's. The Firmware page's "
                 "installed-version read shows the true state.",
            back="/firmware")
    change = (f"{_esc(job.component)}: "
              f"{_esc(job.current or 'version not reported')} &rarr; "
              f"{_esc(job.remote or 'its latest version')}")
    parts = [f"<h2>{_esc(job.printer_name)}</h2>",
             f"<p>Update: <strong>{change}</strong></p>"]
    head_extra = ""
    if job.running:
        parts.append(f"<p>Updating — {_esc(_elapsed_text(job.elapsed_s))} so "
                     f"far. This update takes {_esc(job.estimate_text)}; the "
                     "printer may restart while it applies. This page "
                     "refreshes itself.</p>")
        head_extra = '<meta http-equiv="refresh" content="15">'
    else:
        parts.append(f"<pre>{_esc(job.outcome or '')}</pre>")
        parts.append(f"<p>Finished after {_esc(_elapsed_text(job.elapsed_s))}. "
                     "To verify, read the printer's installed versions.</p>")
    parts.append(f'<p><a class="button" href="/firmware?printer='
                 f'{quote(job.printer_id, safe="")}">Back to '
                 f"{_esc(job.printer_name)} on the Firmware page</a></p>")
    return _send_page(handler, "Firmware update", "".join(parts),
                      head_extra=head_extra)


def dismiss_route(handler, form):
    """POST /notifications/dismiss (session+CSRF): dismiss one toast, return to the page.

    A POST behind the form token, never a GET link: a dismissal changes
    stored state, so a crafted link must not be able to do it (#1614
    review). The form token is checked by the route policy before this
    runs.
    """
    data = handler.server.settings_data
    notif_id = (form.get("id") or [""])[0]
    back = (form.get("back") or ["/"])[0]
    if not back.startswith("/") or back.startswith("//"):
        back = "/"
    if notif_id:
        notifications_module.dismiss(data, notif_id)
    return handler._redirect(back)


__all__ = ["dismiss_route", "update_apply_route", "update_confirm_route",
           "update_status_route"]
