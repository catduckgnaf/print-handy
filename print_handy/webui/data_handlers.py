"""Data tab and Pasta Wrapped handlers for the Web UI (#1603 item 3).

Moved out of ``webui/server.py``: the ``/data`` and ``/wrapped``
routes, the Wrapped video file/status routes and the Wrapped
video/share form posts. ``_Handler`` keeps thin delegates with the
route-table names. The pasta reel's file, status and form routes are
the video's handlers with ``kind="reel"`` (see :data:`VIDEO_KINDS`).
"""
from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

from . import data as data_module

# The two Wrapped videos: the data.py function names each handler calls
# for a ``kind`` (looked up at call time), and the file route's 404.
VIDEO_KINDS = {
    "video": {"path": "wrapped_video_path", "status": "video_status", "make": "make_video",
              "missing": "no compiled Wrapped video for that year. "
                         "Compile it from the Wrapped page first."},
    "reel": {"path": "wrapped_reel_path", "status": "reel_status", "make": "make_reel",
             "missing": "no pasta reel for that year yet. "
                        "Make it from the Wrapped page first."},
}


def year_param(handler):
    raw = (parse_qs(urlparse(handler.path).query).get("year") or [None])[0]
    if raw is None or raw == "":
        return None, None
    try:
        return int(raw), None
    except ValueError:
        return None, (400, {"ok": False,
                            "reason": "year must be a whole number. Nothing shown."})

def data_route(handler, path):
    """GET /data, /wrapped, /api/data and /api/wrapped (#956).

    Served under the Web UI's own access rules, like every other
    page: open on an install with no login set, behind the login
    when one is set. While a route's own switch is off ([timelapse]
    data_enabled for the Data routes, wrapped_enabled for the
    Wrapped routes), it answers the named 403 refusal from
    data.py, for pages and APIs alike.
    """
    year, error = handler._year_param()
    if error:
        handler._json(error[0], error[1])
        return
    data = handler.server.settings_data
    if path == "/api/data":
        code, payload = data_module.data_view(data, year=year)
        handler._json(code, payload)
        return
    if path == "/api/wrapped":
        code, payload = data_module.wrapped_view(data, year=year)
        handler._json(code, payload)
        return
    if path == "/data":
        code, payload = data_module.data_view(data, year=year)
        if code != 200:
            handler._json(code, payload)
            return
        handler._send(200, data_module.render_data_page(data, payload, theme=handler._theme()),
                   "text/html; charset=utf-8")
        return
    code, payload = data_module.wrapped_view(data, year=year)
    if code != 200:
        handler._json(code, payload)
        return
    if payload.get("sealed"):
        handler._send(200, data_module.render_wrapped_sealed_page(
            data, payload, theme=handler._theme()), "text/html; charset=utf-8")
        return
    done = (parse_qs(urlparse(handler.path).query).get("done") or [""])[0]
    handler._send(200, data_module.render_wrapped_page(
        data, payload, form_token=handler._form_token(), theme=handler._theme(),
        notice=done or None), "text/html; charset=utf-8")

def wrapped_video_file_route(handler, kind="video"):
    """GET /wrapped/video-file?year=<year> (#981 step 4), and
    GET /wrapped/reel-file for the pasta reel.

    Serves the compiled Wrapped video so the Wrapped page can
    embed it. Behind the same login as every other route here
    (do_GET has already authorized), plus the wrapped_enabled
    gate. Only the one file data.py constructs for a whole-number
    year inside the wrapped outputs can be served — the request
    never supplies a file name, so traversal has no input.
    """
    data = handler.server.settings_data
    if not data_module.wrapped_enabled(data):
        handler._json(403, {"ok": False, "reason": data_module.WRAPPED_OFF_REASON})
        return
    year, error = handler._year_param()
    if error:
        handler._json(error[0], error[1])
        return
    if year is None:
        handler._json(400, {"ok": False,
                         "reason": "year must be a whole number. Nothing shown."})
        return
    target = getattr(data_module, VIDEO_KINDS[kind]["path"])(data, year)
    if target is None:
        handler._json(404, {"ok": False, "reason": VIDEO_KINDS[kind]["missing"]})
        return
    handler._stream_file(target, "video/mp4")

def stream_file(handler, target, content_type):
    """Send a file in chunks, honoring one ``Range`` (206) so a browser
    can seek; the whole file is never held in memory."""
    try:
        handle = open(target, "rb")
    except OSError:
        handler._json(404, {"ok": False, "reason": "the Wrapped video could not be read."})
        return
    with handle:
        size = os.fstat(handle.fileno()).st_size
        wanted = data_module.parse_byte_range(handler.headers.get("Range"), size)
        if wanted == "unsatisfiable":
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{size}")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        start, end = wanted if wanted else (0, size - 1)
        length = max(0, end - start + 1)
        handler.send_response(206 if wanted else 200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Length", str(length))
        if wanted:
            handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        handler.end_headers()
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(handler.VIDEO_CHUNK, remaining))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)

def wrapped_video_status_route(handler, kind="video"):
    """GET /api/wrapped/video-status?year=<year> (and reel-status): the
    compile's state (idle, queued, running with a step count, done,
    failed with a short reason), polled by the Wrapped page while a
    video compiles."""
    year, error = handler._year_param()
    if error or year is None:
        handler._json(*(error or (400, {"ok": False, "reason":
                                     "year must be a whole number. Nothing shown."})))
        return
    code, payload = getattr(data_module, VIDEO_KINDS[kind]["status"])(handler.server.settings_data, year)
    handler._json(code, payload)

def wrapped_video_post(handler, form, kind="video"):
    """POST /wrapped/video (#956) and POST /wrapped/reel: the Wrapped
    page's video forms.

    Policy ``session+csrf`` (routes.py): a login session alone never
    authorizes it; the page embeds the session-bound form token
    like every other page (#1588 item 3). The compile itself, its
    two toggles and every named refusal live in data.py /
    wrapped_video.py.
    """
    raw_year = (form.get("year") or [""])[0]
    try:
        year = int(raw_year)
    except (TypeError, ValueError):
        handler._html_refusal(400, "year must be a whole number. Nothing was compiled.",
                           "/wrapped")
        return
    # The compile runs as a background job: this starts it (or joins the
    # one already running for the year) and goes back to the page, which
    # shows the progress and then the finished video.
    code, payload = getattr(data_module, VIDEO_KINDS[kind]["make"])(
        handler.server.settings_data, year,
        export_silent=bool((form.get("export_silent") or [""])[0]))
    if code not in (200, 202):
        handler._html_refusal(code, payload.get("reason", "the video compile failed"),
                           "/wrapped")
        return
    handler._redirect(f"/wrapped?year={year}")

def wrapped_share_post(handler, form):
    """POST /wrapped/share (#981): the Wrapped page's share form.

    Authorized exactly like the video form (routes.py: the page's
    session-bound form token; a login session alone never
    authorizes it). The
    export itself, its wrapped_enabled gate and every named
    refusal live in data.py / share_export.py. The export is a
    local file write only — nothing is uploaded anywhere.
    """
    raw_year = (form.get("year") or [""])[0]
    try:
        year = int(raw_year)
    except (TypeError, ValueError):
        handler._html_refusal(400, "year must be a whole number. Nothing was exported.",
                           "/wrapped")
        return
    # The share form posts what to include as checkboxes (an unticked
    # box is simply absent); a post without the options marker keeps
    # the safe defaults (#1321): titles, printer names and footage out.
    # Only a ticked box on the explicit form opts one back in.
    explicit = (form.get("options") or [""])[0] == "explicit"

    def ticked(name, default):
        return bool((form.get(name) or [""])[0]) if explicit else default

    code, payload = data_module.export_share(
        handler.server.settings_data, year,
        (form.get("visibility") or ["link-only"])[0],
        destination_note=(form.get("destination") or [""])[0],
        passphrase=(form.get("passphrase") or [None])[0],
        include_titles=ticked("include_titles", False),
        include_printers=ticked("include_printers", False),
        include_video=ticked("include_video", False))
    if code != 200:
        handler._html_refusal(code, payload.get("reason", "the export failed"),
                           "/wrapped")
        return
    handler._send(200, data_module.render_share_result(
        handler.server.settings_data, payload, theme=handler._theme()),
        "text/html; charset=utf-8")

