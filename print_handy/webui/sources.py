"""The Web UI Sources page: where model links can be dropped.

One row per entry in the one registry
(:mod:`print_handy.core.model_sources`) — the same list link intake,
the per-site dispatch and the chat ``/print-source`` report read, so
this page cannot drift from what the bot actually accepts. Login sites
show their saved-credential state through
:func:`print_handy.core.site_accounts.credential_state`, which reports
a state word only; a credential itself is never rendered.

The registry table is read-only: connecting a site still happens by
dropping a link in chat and following the walkthrough there. The one
action the page offers is the Start with a link form (#1450), which
hands a pasted link to the workflow page, where the intake runs with
its questions as on-page buttons, ending in a download or a print
start.
"""
from __future__ import annotations

from ..core import model_sources, site_accounts
from . import shell

_esc = shell._esc

_KIND_TEXT = {
    "login": "Full support — a saved login is needed to download",
    "open": "Full support — no login needed",
    "listing": "Listing only — the site's files cannot be downloaded",
    "none": "Not supported",
    "direct": "Full support — the file itself, from any host",
}


def _status(source):
    """The status cell for one registry entry; never a credential."""
    if source.kind == "open" or source.kind == "direct":
        return "Ready"
    if source.kind == "none":
        return "—"
    if source.site not in site_accounts.LOGIN_SITES:
        # A listing site with no saved credential to check (MakerOnline):
        # credential_state only knows the login sites and raises for others.
        return "No key needed"
    state = site_accounts.credential_state(source.site)
    if state == "ready":
        return "Connected" if source.kind == "login" else "Key saved"
    if state == "expired":
        return "Login expired"
    return "Not connected" if source.kind == "login" else "Needs your API key"


def sources_rows():
    """(label, hosts, kind text, status, note) per registry entry."""
    rows = []
    for source in model_sources.SOURCES:
        hosts = ", ".join(source.hosts[::2]) if source.hosts else ""
        rows.append((source.label, hosts, _KIND_TEXT[source.kind],
                     _status(source), source.note))
    direct = model_sources.DIRECT
    rows.append((direct.label, "any host, Dropbox included",
                 _KIND_TEXT[direct.kind], _status(direct), direct.note))
    return rows


def render_sources_page(*, theme=shell.DEFAULT_THEME, nav=None, form_token: str = "",
                        chat_enabled: bool = True):
    """The GET /sources page: every place a model link can come from."""
    parts = [
        "<p>Drop a link to a model on any of these sites in chat and "
        "Print Handy fetches it, slices it and asks before printing. "
        "This list is the same registry the bot's link intake reads, "
        "so what is listed here is what works.</p>",
    ]
    # Start with a link (#1450): the form hands the link to the workflow
    # page, which runs the same intake with its questions as on-page
    # buttons and ends in a download or a print start. Offered only
    # while web chat can run the workflow. (This supersedes the #1443
    # stopgap form that handed the link to web chat; the Sliced files
    # page from #1443 remains the archive-wide download surface.)
    if chat_enabled:
        parts.append(
            "<h2>Start with a link</h2>"
            "<p>Paste a link to a model below and the "
            "<a href=\"/workflow\">workflow</a> runs here in the "
            "browser: the same questions chat asks, as buttons on the "
            "page, ending in a file to download or a print start.</p>"
            "<form method=\"post\" action=\"/workflow/link\">"
            f"<input type=\"hidden\" name=\"_csrf\" value=\"{_esc(form_token)}\">"
            "<p><label>Model link "
            "<input name=\"url\" type=\"url\" required "
            "placeholder=\"https://…\"></label> "
            "<button type=\"submit\">Start the workflow</button></p></form>")
    else:
        parts.append(
            "<h2>Start with a link</h2>"
            "<p>Web chat is not enabled on this install, so a link "
            "cannot be started from this page. Paste the link in "
            "Discord, Telegram or Matrix instead.</p>")
    body = []
    for label, hosts, kind, status, note in sources_rows():
        body.append(
            "<tr>"
            f"<td>{_esc(label)}</td>"
            f"<td>{_esc(kind)}</td>"
            f"<td>{_esc(status)}</td>"
            f"<td>{_esc(hosts)}</td>"
            f"<td>{_esc(note)}</td>"
            "</tr>")
    parts.append(
        '<div class="table-scroll"><table><tr><th>Source</th><th>Support</th>'
        "<th>Status</th><th>Site</th><th>Notes</th></tr>" + "".join(body)
        + "</table></div>")
    parts.append(
        "<p>Uploading a file instead? Use the <a href=\"/upload\">upload "
        "page</a>, or drop the .stl/.3mf file in chat directly.</p>")
    return shell.page("sources", "".join(parts), path="/sources",
                      theme=theme, heading="Model sources", nav=nav)


__all__ = ["render_sources_page", "sources_rows"]
