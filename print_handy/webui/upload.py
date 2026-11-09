"""The Web UI upload page and the /u/<token> upload links.

Files land in one place — the inbox, ``<state>/webui-inbox`` — and
nowhere else. An uploaded file is not a print: it becomes printable
only by being picked up in chat, where the normal intake runs it
through the confirm-before-print flow exactly like a file dropped in
chat (``prepared_intake.upload_job`` is that shape). Nothing here
slices, uploads to a printer, or starts anything.

Two ways in:

- ``GET/POST /upload`` — the operator's page, behind the Web UI's
  existing auth, with the usual form token on its posts.
- ``GET/POST /u/<token>`` — a no-login upload link the operator mints
  on the upload page. The token in the address is the whole
  credential: it opens an upload form and nothing else (no navigation,
  no other route honors it), and the operator can revoke it from the
  upload page. Tokens are stored as SHA-256 digests, like the Web UI's
  remembered sessions.

Accepted files: ``.stl`` and ``.3mf`` (the registry's
``DIRECT_FILE_EXTS``). An STL over the intake limit (32 MiB,
``intake_resources.MAX_STL_BYTES``) is refused; any upload over the
shared download budget (``MAX_DOWNLOAD_BYTES``) is refused before it
is read into memory.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from email import policy
from email.parser import BytesParser
from pathlib import Path

from .. import settings
from ..core import intake_resources, model_sources
from . import shell

_esc = shell._esc

LINKS_FILE_NAME = "webui_upload_links.json"
MAX_UPLOAD_BYTES = intake_resources.MAX_DOWNLOAD_BYTES

# The Web UI server is threaded, so two uploads can arrive together.
# Name selection and the write that claims the name stay under one
# lock: without it, two same-name uploads can both pass the clash
# check, share one target and one temp path, and overwrite or lose
# each other's file.
_SAVE_UPLOAD_LOCK = threading.Lock()

# The link store is one JSON file rewritten whole on every change, so
# its read-modify-write operations need the same treatment: without a
# lock, two concurrent operations load the same copy and the later
# save silently drops the earlier one's change — a create can be
# lost, a use count can go backwards, and a note-use save made from a
# stale copy can resurrect a link that was just revoked.
_LINKS_LOCK = threading.Lock()


def inbox_dir(data) -> Path:
    """The one folder uploads land in, for these settings."""
    return settings.state_dir(data) / "webui-inbox"


def _links_path(data) -> Path:
    return settings.state_dir(data) / "webui_upload_links.json"


def _digest(token) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _load_links(data) -> dict:
    try:
        links = json.loads(_links_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return links if isinstance(links, dict) else {}


def _save_links(data, links) -> None:
    path = _links_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp name per call: with one fixed temp path, two
    # concurrent saves write the same file and one replace then fails
    # (or moves the other save's contents).
    tmp = path.parent / f"{path.name}.{secrets.token_hex(8)}.tmp"
    tmp.write_text(json.dumps(links, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def create_link(data) -> str:
    """Mint an upload-link token; only its digest is stored."""
    token = secrets.token_urlsafe(24)
    with _LINKS_LOCK:
        links = _load_links(data)
        links[_digest(token)] = {"created": int(time.time()), "uses": 0}
        _save_links(data, links)
    return token


def link_valid(data, token) -> bool:
    return bool(token) and _digest(token) in _load_links(data)


def _note_link_use_locked(data, token) -> None:
    links = _load_links(data)
    entry = links.get(_digest(token))
    if entry is not None:
        entry["uses"] = int(entry.get("uses") or 0) + 1
        _save_links(data, links)


def note_link_use(data, token) -> None:
    with _LINKS_LOCK:
        _note_link_use_locked(data, token)


def revoke_link(data, digest) -> bool:
    with _LINKS_LOCK:
        links = _load_links(data)
        if links.pop(str(digest or ""), None) is None:
            return False
        _save_links(data, links)
    return True


def list_links(data):
    """(digest, created, uses) per live upload link, oldest first."""
    return sorted(
        ((digest, int(entry.get("created") or 0), int(entry.get("uses") or 0))
         for digest, entry in _load_links(data).items()),
        key=lambda row: row[1])


def save_upload(data, filename, content: bytes):
    """Save one uploaded file into the inbox.

    Returns ``(path, None)`` or ``(None, reason)``. The file name is
    reduced to its bare name first — a path in the submitted name never
    reaches the disk — and a name clash gets a numeric suffix instead
    of overwriting the file already there.
    """
    name = Path(str(filename or "").replace("\\", "/")).name.strip()
    suffix = Path(name).suffix.lower()
    if suffix not in model_sources.DIRECT_FILE_EXTS:
        return None, "only .stl and .3mf files can be uploaded"
    if not Path(name).stem:
        return None, "the file needs a name"
    if suffix == ".stl" and len(content) > intake_resources.MAX_STL_BYTES:
        return None, (f"that STL is over the {intake_resources.MAX_STL_BYTES // (1024 * 1024)} "
                      "MiB intake limit; nothing was saved")
    if len(content) > MAX_UPLOAD_BYTES:
        return None, "that file is over the upload size limit; nothing was saved"
    if not content:
        return None, "the file is empty; nothing was saved"
    folder = inbox_dir(data)
    with _SAVE_UPLOAD_LOCK:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        counter = 1
        while target.exists():
            target = folder / f"{Path(name).stem}-{counter}{suffix}"
            counter += 1
        # A unique temp name per call, so a temp file is never shared
        # with another upload even outside this process's lock.
        tmp = target.with_suffix(
            target.suffix + f".{secrets.token_hex(8)}.tmp")
        tmp.write_bytes(content)
        tmp.replace(target)
    return target, None


def save_upload_for_link(data, token, filename, content: bytes):
    """Save one upload that arrived through a /u/<token> link.

    The validity decision and the write are one atomic step: both
    happen under _LINKS_LOCK, the lock revoke_link takes, so a
    revocation either lands before the decision (and nothing is
    saved) or after the write (and the file was delivered while the
    link was still live). A handler-side link_valid check cannot
    promise that on its own — reading the body takes time, and a
    revocation landing between even a post-read re-check and the
    write would still deliver the file. The use count is recorded in
    the same critical section, so a delivered file is always counted
    and an undelivered one never is.

    Returns ``(path, None)`` on success, ``(None, reason)`` when the
    file itself is refused, and ``(None, None)`` when the link is not
    (or no longer) valid.
    """
    with _LINKS_LOCK:
        if _digest(token) not in _load_links(data):
            return None, None
        saved, reason = save_upload(data, filename, content)
        if saved is None:
            return None, reason
        _note_link_use_locked(data, token)
        return saved, None


def list_inbox(data):
    """(name, size, modified) per inbox file, newest first."""
    folder = inbox_dir(data)
    if not folder.is_dir():
        return []
    rows = []
    for path in folder.iterdir():
        if path.is_file() and path.suffix.lower() in model_sources.DIRECT_FILE_EXTS:
            stat = path.stat()
            rows.append((path.name, stat.st_size, stat.st_mtime))
    return sorted(rows, key=lambda row: -row[2])


def parse_multipart(body: bytes, content_type: str):
    """(fields, files) from a multipart/form-data body, or (None, None).

    ``fields`` maps a field name to its first text value; ``files`` is a
    list of ``(filename, content)`` for the parts that carry a filename.
    Parsed with the standard library's email parser — the body is
    already size-capped by the caller before it gets here.
    """
    if "multipart/form-data" not in (content_type or ""):
        return None, None
    header = (f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n").encode()
    try:
        message = BytesParser(policy=policy.default).parsebytes(header + body)
        fields, files = {}, []
        for part in message.iter_parts() if message.is_multipart() else ():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename:
                files.append((filename, payload))
            elif name:
                fields[name] = payload.decode("utf-8", "replace")
        return fields, files
    except Exception:
        return None, None


def render_upload_page(data, *, form_token, notice=None, error=None,
                       theme=shell.DEFAULT_THEME, nav=None):
    """The GET /upload page: upload form, inbox contents, upload links."""
    esc = _esc
    parts = []
    if notice:
        parts.append(f'<p class="notice">{esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{esc(error)}</p>')
    parts.append(
        "<p>Upload a model file here and it lands in the inbox. An "
        "uploaded file is not a print: pick it up in <a href=\"/chat\">"
        "chat</a> and it goes "
        "through the normal confirm-before-print flow, like a file "
        "dropped in chat.</p>"
        '<form method="post" action="/upload" enctype="multipart/form-data">'
        f'<input type="hidden" name="_csrf" value="{esc(form_token)}">'
        '<div class="field"><label for="u-file">Model file (.stl or .3mf)</label>'
        '<input id="u-file" type="file" name="file" accept=".stl,.3mf" required></div>'
        '<button type="submit" class="accent">Upload to inbox</button></form>')
    rows = list_inbox(data)
    if rows:
        body = "".join(
            f"<tr><td>{esc(name)}</td><td>{size} bytes</td>"
            f"<td>{time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))}</td></tr>"
            for name, size, mtime in rows)
        parts.append("<h2>Inbox</h2><table><tr><th>File</th><th>Size</th>"
                     f"<th>Uploaded</th></tr>{body}</table>")
    else:
        parts.append("<h2>Inbox</h2><p>The inbox is empty.</p>")
    parts.append(
        "<h2>Upload links</h2>"
        "<p>An upload link lets someone send a model file to this inbox "
        "without signing in — the link opens an upload form and nothing "
        "else. Anyone with the link can use it until you revoke it.</p>")
    links = list_links(data)
    if links:
        body = "".join(
            "<tr>"
            f"<td class=\"mono\">link …{esc(digest[:8])}</td>"
            f"<td>{time.strftime('%Y-%m-%d', time.localtime(created))}</td>"
            f"<td>{uses} upload(s)</td>"
            '<td><form method="post" action="/upload">'
            f'<input type="hidden" name="_csrf" value="{esc(form_token)}">'
            f'<input type="hidden" name="action" value="revoke_link">'
            f'<input type="hidden" name="digest" value="{esc(digest)}">'
            '<button type="submit">Revoke</button></form></td></tr>'
            for digest, created, uses in links)
        parts.append("<table><tr><th>Link</th><th>Created</th><th>Used</th>"
                     f"<th></th></tr>{body}</table>")
    parts.append(
        '<form method="post" action="/upload">'
        f'<input type="hidden" name="_csrf" value="{esc(form_token)}">'
        '<input type="hidden" name="action" value="create_link">'
        '<button type="submit">Create an upload link</button></form>')
    return shell.page("upload", "".join(parts), path="/upload", theme=theme,
                      heading="Upload", nav=nav)


def render_token_page(*, notice=None, error=None):
    """The bare /u/<token> page: an upload form and nothing else."""
    parts = []
    if notice:
        parts.append(f'<p class="notice">{_esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{_esc(error)}</p>')
    parts.append(
        "<p>Send a model file (.stl or .3mf) to the Print Handy inbox. "
        "It will be picked up and confirmed there before anything "
        "prints.</p>"
        '<form method="post" enctype="multipart/form-data">'
        '<div class="field"><label for="u-file">Model file</label>'
        '<input id="u-file" type="file" name="file" accept=".stl,.3mf" required></div>'
        '<button type="submit" class="accent">Upload</button></form>')
    return shell.page("upload", "".join(parts), path="/u", chrome=False,
                      heading="Upload a model file")


__all__ = [
    "LINKS_FILE_NAME", "MAX_UPLOAD_BYTES", "create_link",
    "inbox_dir", "link_valid", "list_inbox", "list_links", "note_link_use",
    "parse_multipart", "render_token_page", "render_upload_page",
    "revoke_link", "save_upload", "save_upload_for_link",
]
