"""The Web UI Firmware page (#1280 section I): firmware and updates, per printer.

Two views of one question, "what firmware is there, and is mine current":

- **Your printers.** Every roster printer with the firmware lines the
  firmware feed lists for its model: the newest release, when it was
  published, the version Print Handy was audited against, and how that
  firmware is updated. A Moonraker printer has a "Check installed
  versions" link, which reads that one printer's update state on
  request and shows the same report ``/print-firmware`` gives in chat.
- **Every firmware Print Handy follows.** The whole feed as a table.

Read-only. The page never refreshes a printer's update manager, never
applies an update and has no update button: applying stays the
confirmed ``/print-firmware update`` path in chat. The installed-version
read happens only for the one printer asked for, so opening the page
never waits on a roster of printers.
"""
from __future__ import annotations

from urllib.parse import quote

from .. import firmware_feed, printers, settings
from .. import firmware_update as fw
from . import shell

_esc = shell._esc

FEED_UNAVAILABLE = (
    "The firmware feed could not be read just now, so the newest releases are "
    "not shown. Print Handy reads it again within the hour; nothing else on "
    "this page depends on it.")


def _date(release):
    stamp = (release.published_at or "") if release is not None else ""
    return stamp[:10] if stamp else "date not published"


def _release_cell(release):
    if release is None:
        return "not known"
    label = _esc(release.version)
    if release.url:
        return f'<a href="{_esc(release.url)}" rel="noopener noreferrer">{label}</a>'
    return label


def _model_label(model_key):
    model = printers.MODELS.get(str(model_key or ""))
    return model.label if model is not None else str(model_key or "unknown model")


def _firmware_rows(rows):
    out = []
    for row in rows:
        notes = []
        if row.channel == "prerelease":
            notes.append("follows prereleases")
        if row.stale:
            notes.append("could not be re-read; showing the last known release")
        out.append(
            "<tr>"
            f"<td>{_esc(row.name)}</td>"
            f"<td>{_release_cell(row.latest)}<br><span class=\"muted\">"
            f"{_esc(_date(row.latest))}</span></td>"
            f"<td>{_esc(row.audited or 'not audited')}</td>"
            f"<td>{_esc(row.update)}"
            + (f"<br><span class=\"muted\">{_esc('; '.join(notes))}</span>" if notes else "")
            + "</td></tr>")
    return "".join(out)


_FIRMWARE_HEAD = ("<table><tr><th>Firmware</th><th>Newest release</th>"
                  "<th>Audited against</th><th>How it updates</th></tr>")


def installed_report(printer_id, entry, transport_for):
    """The ``/print-firmware`` report for one printer, or a plain reason it has none."""
    if entry is None:
        return "This printer's model is not in the printer registry, so there is no report."
    if entry.connection != "moonraker":
        return fw.guidance_text(entry)
    try:
        return fw.firmware_report(transport_for(printer_id), entry, refresh=False)
    except fw.FirmwareError as exc:
        return f"{fw.COULD_NOT_READ_TEXT}\n({exc})"


def _printer_section(row, feed, *, checked, transport_for):
    printer_id, model_key = str(row.get("id")), row.get("model")
    name = str(row.get("name") or printer_id)
    entry = fw.printer_entry(model_key)
    parts = [f"<h2>{_esc(name)}</h2>",
             f"<p class=\"muted\">{_esc(_model_label(model_key))}</p>"]
    lines = feed.for_model(model_key) if feed is not None else []
    if lines:
        parts.append(_FIRMWARE_HEAD + _firmware_rows(lines) + "</table>")
    elif feed is not None:
        parts.append("<p>The firmware feed lists no firmware for this model. "
                     "Either its maker publishes no release feed Print Handy can "
                     "read, or it has not been added yet.</p>")
    if checked == printer_id:
        parts.append("<h3>Installed versions</h3>"
                     f"<pre>{_esc(installed_report(printer_id, entry, transport_for))}</pre>")
    elif entry is not None and entry.connection == "moonraker":
        parts.append(f'<p><a href="/firmware?printer={quote(printer_id, safe="")}">'
                     "Check installed versions</a> reads this printer's update "
                     "state now. Nothing is changed on the printer.</p>")
    elif entry is not None:
        parts.append(f"<pre>{_esc(fw.guidance_text(entry))}</pre>")
    return "".join(parts)


def _default_transport(data):
    def transport_for(printer_id):
        from ..runtime import spool_sync
        return spool_sync.moonraker_for(
            None if printer_id == settings.default_printer_id(data) else printer_id)
    return transport_for


def render_firmware_page(data, *, theme=shell.DEFAULT_THEME, nav=None, checked=None,
                         feed=firmware_feed.current, transport_for=None):
    """The Firmware page document for a settings mapping.

    ``checked`` is the roster printer whose installed versions were
    asked for (the ``printer`` query value); an id that is not on the
    roster is ignored. ``feed`` and ``transport_for`` are seams for tests.
    """
    current = feed() if callable(feed) else feed
    roster = [row for row in settings.printers(data) if row.get("id") is not None]
    ids = {str(row["id"]) for row in roster}
    checked = checked if checked in ids else None
    transport_for = transport_for or _default_transport(data)
    parts = [
        "<p>The newest release of the firmware each of your printers runs, the "
        "version Print Handy was last checked against, and how that firmware is "
        "updated. Print Handy tells you in chat when a new release appears "
        "(<code>[firmware] update_alerts</code>). Updates are applied only from "
        "chat with <code>/print-firmware update</code>, behind a confirmation, "
        "and only for the firmware that has a safe path; this page changes "
        "nothing.</p>"]
    if current is None:
        parts.append(f"<p>{_esc(FEED_UNAVAILABLE)}</p>")
    if not roster:
        parts.append("<p>No printers are set up yet. Add one on the "
                     '<a href="/printers">Printers</a> page.</p>')
    for row in roster:
        parts.append(_printer_section(row, current, checked=checked,
                                      transport_for=transport_for))
    if current is not None and current.firmware:
        parts.append("<h2>Every firmware Print Handy follows</h2>")
        if current.generated_at:
            parts.append(f"<p class=\"muted\">Feed last changed "
                         f"{_esc(current.generated_at[:10])}.</p>")
        parts.append(_FIRMWARE_HEAD + _firmware_rows(current.firmware) + "</table>")
        parts.append("<p class=\"muted\">\"Audited against\" is the upstream version "
                     "Print Handy's code for that firmware was last checked with. A "
                     "newer release is one it has not been checked with yet, not one "
                     "it is known to break on.</p>")
    return shell.page("Firmware", "".join(parts), path="/firmware", theme=theme, nav=nav)


__all__ = ["FEED_UNAVAILABLE", "installed_report", "render_firmware_page"]
