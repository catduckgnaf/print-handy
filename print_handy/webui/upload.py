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
  remembered sessions. A link expires ``LINK_TTL_S`` after it was
  made, and every link is revoked when the Web UI login is set or
  changed: the link store carries the fingerprint of the credential
  its links were minted under (as the remembered-session store does),
  and a link checked against another credential is not valid. The
  store is written owner-only (mode 0600).

Accepted files: ``.stl`` and ``.3mf`` (the registry's
``DIRECT_FILE_EXTS``). The body is streamed to a spool file inside the
inbox (``parse_multipart_stream``), never read into memory whole. An
upload whose Content-Length is over the shared download budget
(``MAX_UPLOAD_BYTES``) is refused before a byte is read, and a file
part that passes the budget stops the read. An STL over the intake
limit (32 MiB, ``intake_resources.MAX_STL_BYTES``) is refused. At most
``MAX_CONCURRENT_UPLOADS`` uploads are received at once (the rest get
503 straight away), and the inbox as a whole is capped at
``INBOX_QUOTA_BYTES``: an upload that would pass the quota is refused
with a message saying how full the inbox is and how to free it (the
upload page deletes inbox files). File names lose any path, control
and format characters (bidirectional overrides included) and leading
dots before they reach the disk.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
import unicodedata
from email import policy
from email.parser import BytesParser
from pathlib import Path

from .. import settings
from ..core import intake_resources, model_sources
from . import shell

_esc = shell._esc

LINKS_FILE_NAME = "webui_upload_links.json"
MAX_UPLOAD_BYTES = intake_resources.MAX_DOWNLOAD_BYTES
# The multipart framing allowance on top of MAX_UPLOAD_BYTES: the form
# token field, the part headers and the boundaries.
MULTIPART_OVERHEAD_BYTES = 65536
# How many uploads the server receives at once. Each one holds a
# handler thread and a spool file for as long as its client sends;
# the rest are refused straight away (503, nothing read) instead of
# queueing behind them.
MAX_CONCURRENT_UPLOADS = 2
# The whole inbox: the files already there plus the one arriving.
INBOX_QUOTA_BYTES = 2 * 1024 * 1024 * 1024
# How long an upload link stays valid after it was made.
LINK_TTL_S = 7 * 24 * 3600
# The longest name a saved file keeps, extension included.
MAX_NAME_CHARS = 120
# The spool folder inside the inbox: an upload being received lives
# here until it is checked, then moves into place with one rename.
SPOOL_DIR_NAME = ".incoming"
# The link store's reserved key for the credential fingerprint. Link
# keys are SHA-256 hex digests, so this name can never collide with one.
_FINGERPRINT_KEY = "__credential_sha256__"
# Text fields in an upload form are small (the form token, an action).
_MAX_FIELD_BYTES = 8192
_MAX_PART_HEADER_BYTES = 8192
_READ_CHUNK = 64 * 1024

# The start of every inbox-full refusal (the routes answer it with 507).
INBOX_FULL = "The upload inbox is full"
BUSY_MESSAGE = ("Another upload is still being received, so this one was not "
                "started. Try again in a moment; nothing was saved.")

# The Web UI server is threaded, so two uploads can arrive together.
# Name selection and the write that claims the name stay under one
# lock: without it, two same-name uploads can both pass the clash
# check, share one target and one temp path, and overwrite or lose
# each other's file. The quota is re-checked under the same lock.
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


def spool_dir(data) -> Path:
    """Where upload bodies are received before they are checked."""
    return inbox_dir(data) / SPOOL_DIR_NAME


def _links_path(data) -> Path:
    return settings.state_dir(data) / "webui_upload_links.json"


def _digest(token) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _open_private(path):
    """A new file opened for binary writing, created owner-only (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                 0o600)
    return os.fdopen(fd, "wb")


# -- upload links -------------------------------------------------------------

def _load_links(data) -> dict:
    """The raw link store: digest -> entry, plus the fingerprint key."""
    try:
        links = json.loads(_links_path(data).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return links if isinstance(links, dict) else {}


def _save_links(data, links) -> None:
    """Write the link store atomically and owner-only (mode 0600).

    The temp file is created 0600 before a byte is written, so the
    digests are never readable by other users even for a moment, and
    the mode is set again after the write for filesystems that ignore
    the creation mode."""
    path = _links_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp name per call: with one fixed temp path, two
    # concurrent saves write the same file and one replace then fails
    # (or moves the other save's contents).
    tmp = path.parent / f"{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        with _open_private(tmp) as handle:
            handle.write((json.dumps(links, indent=2) + "\n").encode("utf-8"))
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _expires(entry) -> int:
    """When a link entry stops working. A link made before links
    expired (no ``expires`` field) runs out LINK_TTL_S after it was
    made, like a new one."""
    try:
        return int(entry["expires"])
    except (KeyError, TypeError, ValueError):
        try:
            return int(entry.get("created") or 0) + LINK_TTL_S
        except (AttributeError, TypeError, ValueError):
            return 0


def _live_links(links, fingerprint=None, now=None) -> dict:
    """The entries of a raw store that still work: not expired and, when
    a credential ``fingerprint`` is given, minted under that credential.
    A store minted under another credential (or under none recorded)
    has no live links at all."""
    if fingerprint is not None and links.get(_FINGERPRINT_KEY) != fingerprint:
        return {}
    now = time.time() if now is None else now
    return {digest: entry for digest, entry in links.items()
            if digest != _FINGERPRINT_KEY and isinstance(entry, dict)
            and _expires(entry) > now}


def create_link(data, fingerprint=None) -> str:
    """Mint an upload-link token; only its digest is stored.

    ``fingerprint`` names the credential the link is minted under (the
    server passes ``Access.credential_fingerprint()``). Links minted
    under another credential are discarded first, and expired links are
    pruned."""
    token = secrets.token_urlsafe(24)
    now = int(time.time())
    with _LINKS_LOCK:
        raw = _load_links(data)
        links = _live_links(raw, fingerprint, now)
        if fingerprint is not None:
            links[_FINGERPRINT_KEY] = fingerprint
        elif _FINGERPRINT_KEY in raw:
            links[_FINGERPRINT_KEY] = raw[_FINGERPRINT_KEY]
        links[_digest(token)] = {"created": now, "expires": now + LINK_TTL_S,
                                 "uses": 0}
        _save_links(data, links)
    return token


def link_valid(data, token, fingerprint=None) -> bool:
    """True while the link exists, has not expired and (with a
    ``fingerprint``) was minted under the credential now in force."""
    return bool(token) and _digest(token) in _live_links(_load_links(data), fingerprint)


def _note_link_use_locked(data, token) -> None:
    links = _load_links(data)
    entry = links.get(_digest(token))
    if isinstance(entry, dict):
        entry["uses"] = int(entry.get("uses") or 0) + 1
        _save_links(data, links)


def note_link_use(data, token) -> None:
    with _LINKS_LOCK:
        _note_link_use_locked(data, token)


def revoke_link(data, digest) -> bool:
    digest = str(digest or "")
    if digest == _FINGERPRINT_KEY:
        return False
    with _LINKS_LOCK:
        links = _load_links(data)
        if links.pop(digest, None) is None:
            return False
        _save_links(data, links)
    return True


def revoke_all_links(data, fingerprint=None) -> None:
    """Revoke every upload link: the Web UI login was set or changed.

    The store is rewritten empty, bound to the new credential's
    ``fingerprint`` when one is given. The fingerprint check in
    link_valid already refuses links minted under the old credential;
    this makes the revocation final on disk and on the upload page."""
    with _LINKS_LOCK:
        if fingerprint is None and not _links_path(data).exists():
            return
        _save_links(data, {_FINGERPRINT_KEY: fingerprint} if fingerprint else {})


def rebind_links(data, old_fingerprint, new_fingerprint) -> None:
    """Carry the links over to a re-hashed copy of the same login.

    A password re-hashed at a higher cost (access.rehash_password) is
    the same credential, but its fingerprint changes with the stored
    hash; links minted under the old fingerprint stay valid under the
    new one. Nothing changes when the store names another credential."""
    with _LINKS_LOCK:
        links = _load_links(data)
        if links and links.get(_FINGERPRINT_KEY) == old_fingerprint:
            links[_FINGERPRINT_KEY] = new_fingerprint
            _save_links(data, links)


def list_links(data, fingerprint=None):
    """(digest, created, uses, expires) per live upload link, oldest first."""
    return sorted(
        ((digest, int(entry.get("created") or 0), int(entry.get("uses") or 0),
          _expires(entry))
         for digest, entry in _live_links(_load_links(data), fingerprint).items()),
        key=lambda row: row[1])


# -- receiving ----------------------------------------------------------------

class _UploadSlots:
    """A non-blocking cap on the uploads being received at once."""

    def __init__(self, limit):
        self.limit = limit
        self._lock = threading.Lock()
        self._busy = 0

    def try_acquire(self) -> bool:
        with self._lock:
            if self._busy >= self.limit:
                return False
            self._busy += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._busy = max(0, self._busy - 1)


UPLOAD_SLOTS = _UploadSlots(MAX_CONCURRENT_UPLOADS)


class UploadError(ValueError):
    """An upload body refused while it was being received: ``status``
    is the HTTP code, the message the plain reason."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _boundary(content_type):
    if "multipart/form-data" not in (content_type or ""):
        return None
    match = re.search(r'boundary=(?:"([^"]{1,200})"|([^\s;]{1,200}))', content_type)
    if not match:
        return None
    return (match.group(1) or match.group(2)).encode("latin-1", "replace")


def _part_disposition(header_block: bytes):
    """(field name, filename or None) from one part's header block."""
    message = BytesParser(policy=policy.default).parsebytes(header_block + b"\r\n\r\n")
    name = message.get_param("name", header="content-disposition")
    filename = message.get_filename()
    return (str(name) if name else None), (str(filename) if filename is not None else None)


def parse_multipart_stream(rfile, length: int, content_type: str, spool):
    """(fields, files) from a multipart/form-data body read from ``rfile``.

    The body is read in chunks and never held whole: each file part is
    written straight to a fresh spool file in ``spool`` (created 0600),
    and text fields are kept in memory up to a small limit. At most
    ``length`` bytes are read. ``files`` is a list of
    ``(filename, spool path)``; the caller owns those files and removes
    the ones it does not keep (discard_spooled). A file part passing
    MAX_UPLOAD_BYTES stops the read with UploadError(413); a body that
    is not well-formed multipart is UploadError(400). On any error the
    spool files written so far are removed.
    """
    boundary = _boundary(content_type)
    if boundary is None:
        raise UploadError(400, "could not read the upload form")
    spool = Path(spool)
    spool.mkdir(parents=True, exist_ok=True)
    delimiter = b"\r\n--" + boundary
    fields, files = {}, []
    remaining = max(0, int(length))
    # Read as if the body began with CRLF, so the first boundary line
    # matches the same delimiter as every later one.
    buf = bytearray(b"\r\n")

    def fill():
        nonlocal remaining
        if remaining <= 0:
            return False
        chunk = rfile.read(min(_READ_CHUNK, remaining))
        if not chunk:
            remaining = 0
            return False
        remaining -= len(chunk)
        buf.extend(chunk)
        return True

    def bad():
        return UploadError(400, "could not read the upload form")

    sink = None
    try:
        while True:  # the preamble, up to the first delimiter
            at = buf.find(delimiter)
            if at >= 0:
                del buf[:at + len(delimiter)]
                break
            if len(buf) > len(delimiter):
                del buf[:len(buf) - len(delimiter)]
            if not fill():
                raise bad()
        while True:
            # After a delimiter: "--" ends the body, CRLF starts a part.
            while len(buf) < 2 and fill():
                pass
            if buf[:2] == b"--":
                while fill():  # the epilogue, read and dropped
                    del buf[:]
                return fields, files
            if buf[:2] != b"\r\n":
                raise bad()
            del buf[:2]
            while (end := buf.find(b"\r\n\r\n")) < 0:
                if len(buf) > _MAX_PART_HEADER_BYTES or not fill():
                    raise bad()
            if end > _MAX_PART_HEADER_BYTES:
                raise bad()
            name, filename = _part_disposition(bytes(buf[:end]))
            del buf[:end + 4]
            value = written = None
            if filename is not None:
                path = spool / f"upload-{secrets.token_hex(12)}.part"
                sink = _open_private(path)
                files.append((filename, path))
                written = 0
            else:
                value = bytearray()
            while True:
                at = buf.find(delimiter)
                cut = at if at >= 0 else max(0, len(buf) - len(delimiter))
                if cut:
                    if sink is not None:
                        written += cut
                        if written > MAX_UPLOAD_BYTES:
                            raise UploadError(413, "upload too large")
                        sink.write(buf[:cut])
                    else:
                        value.extend(buf[:cut])
                        if len(value) > _MAX_FIELD_BYTES:
                            raise bad()
                    del buf[:cut]
                if at >= 0:
                    del buf[:len(delimiter)]
                    break
                if not fill():
                    raise bad()
            if sink is not None:
                sink.close()
                sink = None
            elif name and name not in fields:
                fields[name] = value.decode("utf-8", "replace")
    except BaseException:
        if sink is not None:
            sink.close()
        discard_spooled(files)
        raise


def discard_spooled(files) -> None:
    """Remove the spool files of an upload that was not (fully) kept."""
    for _name, path in files or ():
        if isinstance(path, (bytes, bytearray, memoryview)):
            continue
        try:
            Path(path).unlink(missing_ok=True)
        except (OSError, TypeError):
            continue


# -- checks and saving --------------------------------------------------------

def clean_name(filename) -> str:
    """The bare file name an upload is saved under.

    Any path is dropped (a path in the submitted name never reaches the
    disk); control and format characters are removed, so a newline, a
    terminal escape or a right-to-left override cannot ride in a name
    that is later shown in chat, a log or a shell; leading dots and
    spaces go (no hidden files); and an over-long stem is shortened so
    the name stays within MAX_NAME_CHARS, extension included."""
    raw = "".join(ch for ch in str(filename or "").replace("\\", "/")
                  if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Zl", "Zp"))
    name = Path(raw).name.strip().lstrip(". ").strip()
    suffix = Path(name).suffix
    if len(name) > MAX_NAME_CHARS:
        stem = name[:-len(suffix)] if suffix else name
        name = stem[:max(1, MAX_NAME_CHARS - len(suffix))].rstrip() + suffix
    return name


def inbox_usage(data) -> int:
    """Bytes held in the inbox now, uploads being received included."""
    folder = inbox_dir(data)
    total = 0
    for base in (folder, folder / SPOOL_DIR_NAME):
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for path in entries:
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


def _mib(count) -> str:
    return f"{count / (1024 * 1024):.0f} MiB"


def quota_refusal(data, incoming: int, *, already_counted: int = 0):
    """The inbox-full message when ``incoming`` more bytes would pass the
    quota, else None. ``already_counted`` is the part of ``incoming``
    inbox_usage already sees (the spool file being checked)."""
    used = max(0, inbox_usage(data) - already_counted)
    if used + incoming <= INBOX_QUOTA_BYTES:
        return None
    return (f"{INBOX_FULL}: {_mib(used)} of its {_mib(INBOX_QUOTA_BYTES)} "
            f"are in use and this file needs {_mib(incoming)}. Delete files from the "
            "inbox on the Upload page before uploading more; nothing was saved.")


def _is_bytes(content) -> bool:
    return isinstance(content, (bytes, bytearray, memoryview))


def _content_size(content) -> int:
    """The size of an upload given as bytes or as a spool file path."""
    if _is_bytes(content):
        return len(content)
    try:
        return Path(content).stat().st_size
    except (OSError, TypeError):
        return 0


def _upload_refusal(data, filename, content):
    """Why this upload would be refused, or None if it may be saved.

    Checks only — the cleaned name, the file type, the sizes, the inbox
    quota — so a caller can run them before committing anything:
    save_upload_for_link counts the link use before the write, and a
    file refused for one of these reasons must not be counted.
    ``content`` is the file's bytes or the spool file holding them.
    """
    name = clean_name(filename)
    suffix = Path(name).suffix.lower()
    if suffix not in model_sources.DIRECT_FILE_EXTS:
        return "only .stl and .3mf files can be uploaded"
    if not Path(name).stem:
        return "the file needs a name"
    size = _content_size(content)
    if suffix == ".stl" and size > intake_resources.MAX_STL_BYTES:
        return (f"that STL is over the {intake_resources.MAX_STL_BYTES // (1024 * 1024)} "
                "MiB intake limit; nothing was saved")
    if size > MAX_UPLOAD_BYTES:
        return "that file is over the upload size limit; nothing was saved"
    if not size:
        return "the file is empty; nothing was saved"
    return quota_refusal(data, size, already_counted=0 if _is_bytes(content) else size)


def save_upload(data, filename, content):
    """Save one uploaded file into the inbox.

    ``content`` is the file's bytes, or the spool file
    parse_multipart_stream wrote (moved into place with one rename,
    never copied through memory). Returns ``(path, None)`` or
    ``(None, reason)``. The name is cleaned first (clean_name) and a
    name clash gets a numeric suffix instead of overwriting the file
    already there. The quota is checked again under the save lock, so
    two uploads arriving together cannot both squeeze past it.
    """
    refusal = _upload_refusal(data, filename, content)
    if refusal is not None:
        return None, refusal
    name = clean_name(filename)
    suffix = Path(name).suffix.lower()
    folder = inbox_dir(data)
    spooled = not _is_bytes(content)
    with _SAVE_UPLOAD_LOCK:
        size = _content_size(content)
        refusal = quota_refusal(data, size, already_counted=size if spooled else 0)
        if refusal is not None:
            return None, refusal
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / name
        counter = 1
        while target.exists():
            target = folder / f"{Path(name).stem}-{counter}{suffix}"
            counter += 1
        if spooled:
            os.replace(content, target)
            return target, None
        # A unique temp name per call, so a temp file is never shared
        # with another upload even outside this process's lock.
        tmp = target.with_suffix(
            target.suffix + f".{secrets.token_hex(8)}.tmp")
        tmp.write_bytes(content)
        tmp.replace(target)
    return target, None


def save_upload_for_link(data, token, filename, content, fingerprint=None):
    """Save one upload that arrived through a /u/<token> link.

    The validity decision and the delivery are one atomic step
    against revocation: everything happens under _LINKS_LOCK, the
    lock revoke_link takes, so a revocation either lands before the
    decision (and nothing is saved) or after the write (and the file
    was delivered while the link was still live). A handler-side
    link_valid check cannot promise that on its own — reading the
    body takes time, and a revocation landing between even a
    post-read re-check and the write would still deliver the file.

    Inside that section the use is counted and persisted BEFORE the
    file is written, which makes the delivery failure-atomic too:
    if the count cannot be persisted, the upload is refused and
    nothing is saved; a write that fails after the count leaves a
    counted use with no file — never a file in the inbox behind a
    failed, uncounted request, which a retry could silently
    duplicate.

    Returns ``(path, None)`` on success, ``(None, reason)`` when the
    upload is refused, and ``(None, None)`` when the link is not (or
    no longer) valid: revoked, expired, or (with ``fingerprint``)
    minted under a login that has since been set or changed.
    """
    with _LINKS_LOCK:
        if _digest(token) not in _live_links(_load_links(data), fingerprint):
            return None, None
        refusal = _upload_refusal(data, filename, content)
        if refusal is not None:
            return None, refusal
        try:
            _note_link_use_locked(data, token)
        except OSError:
            return None, ("the upload could not be recorded; "
                          "nothing was saved")
        return save_upload(data, filename, content)


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


def delete_inbox_file(data, name) -> bool:
    """Delete one inbox file by the name the upload page lists.

    Only a name list_inbox shows is accepted, so no path, spool file
    or other state file can be named here."""
    name = str(name or "")
    if name not in {row[0] for row in list_inbox(data)}:
        return False
    with _SAVE_UPLOAD_LOCK:
        try:
            (inbox_dir(data) / name).unlink()
        except FileNotFoundError:
            return False
    return True


def parse_multipart(body: bytes, content_type: str):
    """(fields, files) from a multipart/form-data body, or (None, None).

    ``fields`` maps a field name to its first text value; ``files`` is a
    list of ``(filename, content)`` for the parts that carry a filename.
    Parsed in memory with the standard library's email parser, for a
    small body already in hand; the upload routes stream instead
    (parse_multipart_stream).
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
                       theme=shell.DEFAULT_THEME, nav=None, fingerprint=None):
    """The GET /upload page: upload form, inbox contents, upload links."""
    esc = _esc
    parts = []
    if notice:
        parts.append(f'<p class="notice">{esc(notice)}</p>')
    if error:
        parts.append(f'<p class="error">{esc(error)}</p>')
    parts.append(
        "<p>Upload a model file here and it lands in the inbox. An "
        "uploaded file is not a print: pick it up in chat and it goes "
        "through the normal confirm-before-print flow, like a file "
        "dropped in chat.</p>"
        '<form method="post" action="/upload" enctype="multipart/form-data">'
        f'<input type="hidden" name="_csrf" value="{esc(form_token)}">'
        '<div class="field"><label for="u-file">Model file (.stl or .3mf)</label>'
        '<input id="u-file" type="file" name="file" accept=".stl,.3mf" required></div>'
        '<button type="submit" class="accent">Upload to inbox</button></form>')
    rows = list_inbox(data)
    usage = (f"<p>{esc(_mib(inbox_usage(data)))} of the inbox's "
             f"{esc(_mib(INBOX_QUOTA_BYTES))} are in use.</p>")
    if rows:
        body = "".join(
            f"<tr><td>{esc(name)}</td><td>{size} bytes</td>"
            f"<td>{time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))}</td>"
            '<td><form method="post" action="/upload">'
            f'<input type="hidden" name="_csrf" value="{esc(form_token)}">'
            '<input type="hidden" name="action" value="delete_file">'
            f'<input type="hidden" name="name" value="{esc(name)}">'
            '<button type="submit">Delete</button></form></td></tr>'
            for name, size, mtime in rows)
        parts.append("<h2>Inbox</h2><table><tr><th>File</th><th>Size</th>"
                     f"<th>Uploaded</th><th></th></tr>{body}</table>{usage}")
    else:
        parts.append(f"<h2>Inbox</h2><p>The inbox is empty.</p>{usage}")
    parts.append(
        "<h2>Upload links</h2>"
        "<p>An upload link lets someone send a model file to this inbox "
        "without signing in — the link opens an upload form and nothing "
        f"else. Anyone with the link can use it for {LINK_TTL_S // 86400} days, "
        "until you revoke it, or until the Web UI login is set or changed, "
        "whichever comes first.</p>")
    links = list_links(data, fingerprint)
    if links:
        body = "".join(
            "<tr>"
            f"<td class=\"mono\">link …{esc(digest[:8])}</td>"
            f"<td>{time.strftime('%Y-%m-%d', time.localtime(created))}</td>"
            f"<td>{time.strftime('%Y-%m-%d %H:%M', time.localtime(expires))}</td>"
            f"<td>{uses} upload(s)</td>"
            '<td><form method="post" action="/upload">'
            f'<input type="hidden" name="_csrf" value="{esc(form_token)}">'
            f'<input type="hidden" name="action" value="revoke_link">'
            f'<input type="hidden" name="digest" value="{esc(digest)}">'
            '<button type="submit">Revoke</button></form></td></tr>'
            for digest, created, uses, expires in links)
        parts.append("<table><tr><th>Link</th><th>Created</th><th>Expires</th>"
                     f"<th>Used</th><th></th></tr>{body}</table>")
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
    "BUSY_MESSAGE", "INBOX_FULL", "INBOX_QUOTA_BYTES", "LINKS_FILE_NAME", "LINK_TTL_S",
    "MAX_CONCURRENT_UPLOADS", "MAX_UPLOAD_BYTES", "MULTIPART_OVERHEAD_BYTES",
    "UPLOAD_SLOTS", "UploadError", "clean_name", "create_link",
    "delete_inbox_file", "discard_spooled", "inbox_dir", "inbox_usage",
    "link_valid", "list_inbox", "list_links", "note_link_use",
    "parse_multipart", "parse_multipart_stream", "quota_refusal",
    "rebind_links", "render_token_page", "render_upload_page",
    "revoke_all_links", "revoke_link", "save_upload", "save_upload_for_link",
    "spool_dir",
]
