"""The Web UI Firmware page (#1280 section I): firmware and updates, per printer.

Two views of one question, "what firmware is there, and is mine current":

- **Your printers.** Every roster printer with the firmware lines the
  firmware feed lists for its model: the newest release, when it was
  published, the version Print Handy was audited against, and how that
  firmware is updated. A Moonraker printer has a "Check installed
  versions" link, which reads that one printer's update state on
  request and shows the same report ``/print-firmware`` gives in chat.
- **Every firmware Print Handy follows.** The whole feed.

Both views split the feed into three groups, each its own table:
**Firmware** (community or open firmware such as ZMod, Forge-X,
OpenCreator, Klipper), **Stock firmware** (the printer maker's own, such
as Prusa Buddy or QIDI) and **Plugins** (add-ons that run beside the
firmware, such as HelixScreen, Moonraker or OctoPrint).

The page itself never refreshes a printer's update manager. The
installed-version read happens only for the one printer asked for, so
opening the page never waits on a roster of printers. That read also
feeds the Web UI's update notifications (#1610) and, when Web UI
printer controls are on, offers an update button per component with
one available; the button leads to a confirmation page, and applying
runs the same confirmed flow as ``/print-firmware update`` in chat
(:mod:`print_handy.firmware_update`). The same read also lists each
component's Moonraker update channel with a Switch channel action
(#1634), shown whether or not an update is available, since the
channel decides whether a prerelease ever shows as one. With controls
off the page is read-only and applying stays in chat.
"""
from __future__ import annotations

from urllib.parse import quote

from .. import firmware_alerts as fa
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


def _subscribed_prerelease(row, channel):
    """The row's published prerelease when the owner subscribes to its track.

    Mirrors the chat-notice gate in firmware_alerts.feed_notices: a
    prerelease-channel line (Forge-X) is not part of this, because its
    newest release already is the prerelease it follows.
    """
    pre = getattr(row, "latest_prerelease", None)
    if pre is None or row.channel == "prerelease":
        return None
    track = pre.track or fa.release_track(pre.version)
    if not fa.channel_accepts(channel, track):
        return None
    stable = row.latest_stable
    if stable is not None and fa.version_key(pre.version) <= fa.version_key(stable.version):
        return None
    return pre, track


def _firmware_rows(rows, channel="stable"):
    out = []
    for row in rows:
        notes = []
        if row.channel == "prerelease":
            notes.append("follows prereleases")
        if row.stale:
            notes.append("could not be re-read; showing the last known release")
        pre = _subscribed_prerelease(row, channel)
        pre_cell = ""
        if pre is not None:
            release, track = pre
            pre_cell = (f"<br>Prerelease: {_release_cell(release)} "
                        f"({_esc(track)})<br><span class=\"muted\">"
                        f"{_esc(_date(release))}</span>")
        out.append(
            "<tr>"
            f"<td>{_esc(row.name)}</td>"
            f"<td>{_release_cell(row.latest)}<br><span class=\"muted\">"
            f"{_esc(_date(row.latest))}</span>{pre_cell}</td>"
            f"<td>{_esc(row.audited or 'not audited')}</td>"
            f"<td>{_esc(row.update)}"
            + (f"<br><span class=\"muted\">{_esc('; '.join(notes))}</span>" if notes else "")
            + "</td></tr>")
    return "".join(out)


_FIRMWARE_HEAD = ("<table><tr><th>Firmware</th><th>Newest release</th>"
                  "<th>Audited against</th><th>How it updates</th></tr>")


def _grouped_tables(rows, heading, model_key=None, channel="stable"):
    """One table per kind, in KINDS order: Firmware, Stock firmware, Plugins.

    catduck (2026-11-16): firmware, the maker's stock firmware and plugins
    are three different things and are shown apart. An empty group is left
    out. ``heading`` is the HTML tag for each group's title.
    """
    out = []
    for kind in firmware_feed.KINDS:
        # On one printer, a firmware that is that maker's own (Marlin on a
        # Sovol) is stock there; the full list groups each line by its kind.
        group = [row for row in rows
                 if (row.kind_for(model_key) if model_key else row.kind) == kind]
        if not group:
            continue
        title = firmware_feed.KIND_TITLES[kind]
        head = _FIRMWARE_HEAD.replace("<th>Firmware</th>",
                                      f"<th>{'Plugin' if kind == 'plugin' else 'Firmware'}</th>")
        out.append(f"<{heading}>{_esc(title)}</{heading}>" + head
                   + _firmware_rows(group, channel) + "</table>")
    return "".join(out)


def installed_report(printer_id, entry, transport_for):
    """The ``/print-firmware`` report for one printer, or a plain reason it has none."""
    text, _status = installed_report_and_status(printer_id, entry, transport_for)
    return text


def installed_report_and_status(printer_id, entry, transport_for):
    """The installed report plus the live UpdateStatus behind it (#1610).

    One read of the one printer: the status the report was built from
    also drives the update offers and the notification observation, so
    checking a printer never reads it twice. ``status`` is None when
    there is no Moonraker report to build from.
    """
    if entry is None:
        return ("This printer's model is not in the printer registry, so "
                "there is no report.", None)
    if entry.connection != "moonraker":
        return fw.guidance_text(entry), None
    try:
        return fw.firmware_report_with_status(
            transport_for(printer_id), entry, refresh=False)
    except fw.FirmwareError as exc:
        return f"{fw.COULD_NOT_READ_TEXT}\n({exc})", None


def _update_actions(printer_id, entry, status, *, controls, cfg):
    """The update offers under one printer's installed-version read (#1610).

    One row per component with an update available: what would change,
    roughly how long it takes, and — only when this printer may be
    updated at all and Web UI controls are on — the link to that
    component's confirmation page. Every refusal is named in words,
    never a dead button.
    """
    updates = status.updates_available
    if not updates:
        return ""
    parts = ["<h3>Updates available</h3>"]
    block = fw.apply_block_reason(entry, cfg)
    for component in updates:
        parts.append(
            f"<p><strong>{_esc(component.name)}</strong> "
            f"{_esc(component.current or 'version not reported')} &rarr; "
            f"{_esc(component.remote)}"
            f"<br><span class=\"muted\">Takes "
            f"{_esc(fw.update_estimate_text(component.name))}.</span>")
        if block is None and controls:
            url = (f"/firmware/update?printer={quote(printer_id, safe='')}"
                   f"&component={quote(component.name, safe='')}")
            parts.append(
                f' <a class="button" href="{_esc(url)}">'
                f"Update {_esc(component.name)}</a>")
        parts.append("</p>")
    if block is not None:
        parts.append(f"<p>{_esc(block)} Updates stay available from the "
                     "printer's own update screen.</p>")
    elif not controls:
        parts.append("<p>To update from this page, turn on Web UI printer "
                     "controls (<code>[webui] controls_enabled = true</code>). "
                     "Until then, updates apply from chat with "
                     "<code>/print-firmware update</code>, behind a "
                     "confirmation.</p>")
    return "".join(parts)


def _channel_actions(printer_id, entry, status, *, controls, cfg):
    """The channel-switch offers under one printer's installed read (#1634).

    One row per component Moonraker reports a channel for, whether or
    not an update is available: the channel is what decides whether a
    prerelease ever shows as an update at all. The switch link shows
    only when this printer may be updated at all and Web UI controls
    are on; every refusal is named in words, never a dead button.
    """
    channeled = [c for c in status.components if c.channel]
    if not channeled:
        return ""
    parts = ["<h3>Update channels</h3>",
             "<p class=\"muted\">A component's channel decides which releases "
             "Moonraker offers it: stable takes tagged stable releases, beta "
             "also takes prereleases, and git components may also take dev "
             "(the latest commits). Switching a channel installs nothing and "
             "downgrades nothing.</p>"]
    block = fw.apply_block_reason(entry, cfg)
    for component in channeled:
        parts.append(
            f"<p><strong>{_esc(component.name)}</strong> is on the "
            f"{_esc(component.channel)} channel.")
        if block is None and controls:
            url = (f"/firmware/channel?printer={quote(printer_id, safe='')}"
                   f"&component={quote(component.name, safe='')}")
            parts.append(
                f' <a class="button" href="{_esc(url)}">Switch channel</a>')
        parts.append("</p>")
    if block is not None:
        parts.append(f"<p>{_esc(block)} Channels stay switchable from the "
                     "printer's own Moonraker config.</p>")
    elif not controls:
        parts.append("<p>To switch a channel from this page, turn on Web UI "
                     "printer controls (<code>[webui] controls_enabled = "
                     "true</code>). Until then, channels switch from chat "
                     "with <code>/print-firmware channel</code>, behind a "
                     "confirmation.</p>")
    return "".join(parts)


def _printer_section(row, feed, *, checked, transport_for, controls=False,
                     cfg=None, observe=None,
                     channel="stable"):
    printer_id, model_key = str(row.get("id")), row.get("model")
    name = str(row.get("name") or printer_id)
    entry = fw.printer_entry(model_key)
    parts = [f"<h2>{_esc(name)}</h2>",
             f"<p class=\"muted\">{_esc(_model_label(model_key))}</p>"]
    lines = feed.for_model(model_key) if feed is not None else []
    if lines:
        parts.append(_grouped_tables(lines, "h3", model_key, channel))
    elif feed is not None:
        parts.append("<p>The firmware feed lists no firmware for this model. "
                     "Either its maker publishes no release feed Print Handy can "
                     "read, or it has not been added yet.</p>")
    if checked == printer_id:
        report, status = installed_report_and_status(printer_id, entry,
                                                     transport_for)
        parts.append("<h3>Installed versions</h3>"
                     f"<pre>{_esc(report)}</pre>")
        if status is not None:
            if observe is not None:
                observe(printer_id, name, status)
            parts.append(_update_actions(printer_id, entry, status,
                                         controls=controls, cfg=cfg))
            parts.append(_channel_actions(printer_id, entry, status,
                                          controls=controls, cfg=cfg))
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


def printer_transport(data, printer_id):
    """The Moonraker transport for one roster printer (the update flow's)."""
    return _default_transport(data)(printer_id)


def render_firmware_page(data, *, theme=shell.DEFAULT_THEME, nav=None, checked=None,
                         feed=firmware_feed.current, transport_for=None,
                         controls=False, observe=None):
    """The Firmware page document for a settings mapping.

    ``checked`` is the roster printer whose installed versions were
    asked for (the ``printer`` query value); an id that is not on the
    roster is ignored. ``feed`` and ``transport_for`` are seams for tests.
    ``controls`` is whether Web UI printer controls are on (update
    buttons show only then) and ``observe`` an optional callable
    ``(printer_id, name, status)`` receiving the checked printer's live
    update status — the route passes the notification store's recorder.
    """
    current = feed() if callable(feed) else feed
    channel = str((data.get("firmware") or {}).get("update_channel") or "stable")
    channel = channel.strip().lower()
    if channel not in settings.FIRMWARE_UPDATE_CHANNELS:
        channel = "stable"
    roster = [row for row in settings.printers(data) if row.get("id") is not None]
    ids = {str(row["id"]) for row in roster}
    checked = checked if checked in ids else None
    transport_for = transport_for or _default_transport(data)
    parts = [
        "<p>The newest release of the firmware each of your printers runs, the "
        "version Print Handy was last checked against, and how that firmware is "
        "updated. Print Handy tells you when a new release appears — in chat "
        "(<code>[firmware] update_alerts</code>) and, once a printer's installed "
        "versions have been checked below, as a notification in this Web UI. "
        "A prerelease is shown under the newest release when you "
        "subscribe to its track "
        "(<code>[firmware] update_channel</code>). "
        "An "
        "update is applied only behind a confirmation, from chat with "
        "<code>/print-firmware update</code> or from this page when Web UI "
        "printer controls are on, and only for the firmware that has a safe "
        "path. A component's Moonraker update channel can be switched the "
        "same way (<code>/print-firmware channel</code>), behind a "
        "confirmation that shows the exact one-line config change.</p>"]
    if current is None:
        parts.append(f"<p>{_esc(FEED_UNAVAILABLE)}</p>")
    if not roster:
        parts.append("<p>No printers are set up yet. Add one on the "
                     '<a href="/printers">Printers</a> page.</p>')
    for row in roster:
        parts.append(_printer_section(row, current, checked=checked,
                                      transport_for=transport_for,
                                      controls=controls, cfg=data,
                                      observe=observe,
                                      channel=channel))
    if current is not None and current.firmware:
        parts.append("<h2>Every firmware Print Handy follows</h2>")
        if current.generated_at:
            parts.append(f"<p class=\"muted\">Feed last changed "
                         f"{_esc(current.generated_at[:10])}.</p>")
        parts.append(_grouped_tables(current.firmware, "h3", channel=channel))
        parts.append("<p class=\"muted\">\"Audited against\" is the upstream version "
                     "Print Handy's code for that firmware was last checked with. A "
                     "newer release is one it has not been checked with yet, not one "
                     "it is known to break on.</p>")
    return shell.page("Firmware", "".join(parts), path="/firmware", theme=theme, nav=nav)


__all__ = ["FEED_UNAVAILABLE", "installed_report",
           "installed_report_and_status", "printer_transport",
           "render_firmware_page"]
