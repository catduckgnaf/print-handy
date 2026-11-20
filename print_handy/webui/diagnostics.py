"""The sanitized diagnostics download (#1719).

``GET /diagnostics/download`` hands the operator the diagnostics
bundle — the same Markdown file ``/print-handy bundle`` in chat and
``python3 -m print_handy.diagnostics_bundle`` produce — as a browser
download, so it can be attached to a GitHub issue. The link lives on
the Settings page's Logging section.

The bundle is :mod:`print_handy.diagnostics_bundle`'s, unchanged:
settings are summarized with secret-shaped keys redacted, the roster
carries no hosts or credentials, and the whole document passes through
``print_handy.sanitize_logs`` (internal addresses, platform IDs, local
paths, emails, serials and URL query strings removed, on top of
``logs.redact`` for secrets). This module adds no content of its own —
it only serves that text as an attachment — and the sanitization is
pinned by tests that build a bundle from settings carrying planted
tokens, keys and passwords and assert none of them reach the download.
"""
from __future__ import annotations

import logging

from .. import diagnostics_bundle

log = logging.getLogger(__name__)

FILENAME = diagnostics_bundle.BUNDLE_NAME


def build_download(data=None):
    """(code, text): the bundle for ``data`` (the server's settings).

    ``build_bundle`` never raises on a section — a broken setup is
    bundle content, not a reason to produce no bundle — so a failure
    here is a genuine server error and answers 500 with no body.
    """
    try:
        return 200, diagnostics_bundle.build_bundle(data)
    except Exception:
        log.exception("diagnostics bundle could not be built")
        return 500, ""


def diagnostics_route(handler):
    """GET /diagnostics/download: the bundle as a file download."""
    code, text = build_download(handler.server.settings_data)
    if code != 200:
        handler._json(500, {"ok": False,
                            "reason": "the diagnostics bundle could not be built"})
        return
    handler._send(200, text, "text/markdown; charset=utf-8",
                  headers=[("Content-Disposition",
                            f'attachment; filename="{FILENAME}"')])


__all__ = ["FILENAME", "build_download", "diagnostics_route"]
