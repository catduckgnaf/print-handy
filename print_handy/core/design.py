"""AI design with OpenSCAD (#388): create a new part, or edit an existing model.

The engine owns the loop — prompt the model, extract the OpenSCAD source, sandbox-scan it,
render it, validate the mesh, render a preview — and nothing else. It never slices, uploads
or prints: a finished session's STL is handed to the ordinary prepared-intake flow by the
caller, and every gate after that is intake's own.

Stage 2 edit sessions are seeded from an existing model and record their kind and source:
a ``.scad`` is edited in source form (a copy lives in the session; the original is never
touched — parametric changes are made by editing values in the source, never ``-D``
overrides, so the session file always shows the real design), while an ``.stl`` (or a
``.3mf`` converted to STL through the intake's own geometry loader) is edited as a mesh:
the engine composes the render file as a trusted header defining ``base()`` (the only
``import`` in the render, naming the session-local source copy) plus the model's body,
which calls ``base()`` and applies transforms, added shapes and boolean cuts. The sandbox
scan applies to the model's text only, exactly as for creates.

Safety shape (Stage 0 audit on #388, spike-proven):
- Model-authored SCAD may not contain import/include/use/surface statements at all. Those
  are OpenSCAD's only file-access statements, and the spike proved absolute paths, ``../``
  escapes and outside ``include<>`` all execute. The ban is token-level and runs before any
  subprocess starts.
- Every render passes ``--hardwarnings``: without it a warning-class defect (a hallucinated
  import of a missing file) exits 0 and silently produces a wrong part.
- The render runs in the session directory with a minimal environment, a wall-clock timeout
  and POSIX CPU/memory rlimits, following cad_split._run_openscad's discipline.

The engine takes a ``complete(messages) -> str`` callable and never knows the transport:
the transport resolves an OpenAI-compatible HTTP endpoint from [design]/[llm] (#524 Stage 3).
"""
from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MAX_SCAD_BYTES = 64 * 1024  # a single-file part design never needs more source than this
RENDER_TIMEOUT_S = 180
MAX_TURNS = 10
RETENTION_DAYS = 14
SESSIONS_DIRNAME = "design-sessions"

SLICE_LABEL = "Slice it"
CHANGE_LABEL = "Change it"
CANCEL_LABEL = "Cancel"
CHOICES = (SLICE_LABEL, CHANGE_LABEL, CANCEL_LABEL)

# Typed twins of the three buttons (#1206). Whole-message matches only, so
# "cancel the hole" or "slice it thinner" stay design changes. Slice words
# only hand the STL to prepared intake, which still asks before printing.
_TYPED_CHOICES = {
    SLICE_LABEL: {"slice", "slice it", "slice this", "slice that", "print it", "print this"},
    CHANGE_LABEL: {"change", "change it"},
    CANCEL_LABEL: {"cancel", "cancel it", "stop", "never mind", "nevermind", "abort",
                   "forget it"},
}


def typed_choice(text):
    """The button a typed reply to "What next for this design?" names, or None.

    Case and trailing punctuation are ignored, and a bare number picks that
    button (Discord and Telegram users type them too, not only Matrix).
    Anything else is a change to the design, and the caller runs it as one.
    """
    value = " ".join(str(text or "").casefold().strip().rstrip(".!").split())
    if not value:
        return None
    if value.isdigit():
        index = int(value) - 1
        return CHOICES[index] if 0 <= index < len(CHOICES) else None
    for label, words in _TYPED_CHOICES.items():
        if value == label.casefold() or value in words:
            return label
    return None

_FORBIDDEN = (
    ("import", re.compile(r"\bimport\s*\(", re.I)),
    ("include", re.compile(r"\binclude\s*[<(]", re.I)),
    ("use", re.compile(r"\buse\s*<", re.I)),
    ("surface", re.compile(r"\bsurface\s*\(", re.I)),
)


def _scad_token_stream(source: str) -> str:
    """The SCAD source as OpenSCAD's lexer sees it, for the forbidden scan.

    Comments are token separators in OpenSCAD, so ``import/**/("x")`` is the
    same statement as ``import ("x")`` — matching raw text lets a comment
    split a forbidden call past the scan (P1-92, P1-94). Replace every
    comment with a single space and blank every string literal's contents
    (a quoted ``"import("`` is data, not a statement), then match on the
    result. String delimiters are kept so ``surface(file="")`` still scans
    as a call with an argument.
    """
    out: list[str] = []
    i, n = 0, len(source or "")
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            end = source.find("\n", i)
            i = n if end == -1 else end
            out.append(" ")
        elif ch == "/" and nxt == "*":
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
        elif ch == '"':
            out.append('""')
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                elif source[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)
_FENCE = re.compile(r"```(?:openscad|scad)?\s*\n(.*?)```", re.S | re.I)
_SCAD_HINT = re.compile(
    r"\b(cube|sphere|cylinder|polyhedron|translate|rotate|scale|mirror|difference|union|"
    r"intersection|hull|minkowski|linear_extrude|rotate_extrude|module|function|\$fn)\b")


class DesignError(Exception):
    """A design turn failed in a way the user should be told about, in these words."""


class DesignQuestion(DesignError):
    """The model needs one fact it cannot assume before it can design the part.

    Raised at most once per session, only on a create's first turn; the
    session is left awaiting the answer, and the next turn designs with it.
    """

    def __init__(self, question):
        super().__init__(question)
        self.question = question


# --------------------------------------------------------------------- sessions

def sessions_root(cache_dir) -> Path:
    return Path(cache_dir) / SESSIONS_DIRNAME


def _session_path(root: Path, session_id: str) -> Path:
    if not re.fullmatch(r"[a-z0-9-]{8,40}", session_id or ""):
        raise DesignError("that design session id is not valid")
    return root / session_id


def _save(root: Path, session: dict) -> dict:
    folder = _session_path(root, session["id"])
    folder.mkdir(parents=True, exist_ok=True)
    session["updated_at"] = time.time()
    tmp = folder / "session.json.tmp"
    tmp.write_text(json.dumps(session, indent=1) + "\n", encoding="utf-8")
    tmp.replace(folder / "session.json")
    return session


def load_session(root, session_id):
    folder = _session_path(Path(root), session_id)
    try:
        data = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def find_session_for_owner(root, owner_key):
    """The newest non-finished session owned by this key, or None."""
    root = Path(root)
    if not root.is_dir():
        return None
    best = None
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        try:
            data = json.loads((folder / "session.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("owner_key") != owner_key:
            continue
        if data.get("status") in ("cancelled", "handed_off"):
            continue
        if best is None or data.get("updated_at", 0) > best.get("updated_at", 0):
            best = data
    return best


# ------------------------------------------------------- read-only catalog

# Catalog kind -> the session-record key carrying its filename. "base" is
# a mesh edit's original mesh (source.stl): the composed .scad imports it
# through base(), so the source download is only reproducible with it.
_CATALOG_FILES = {"scad": "scad", "stl": "stl", "preview": "preview",
                  "base": "base_stl"}


def list_sessions(root, *, days=RETENTION_DAYS, now=None):
    """Session records inside the retention window, newest first.

    The read side of the store (the Web UI designs catalog renders this):
    only records whose ``updated_at`` is within ``days`` are listed — the
    same window :func:`prune_old_sessions` enforces — and a folder whose
    record is unreadable is skipped, never fatal.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    cutoff = (now or time.time()) - days * 86400
    found = []
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        try:
            data = json.loads((folder / "session.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("id"):
            continue
        if float(data.get("updated_at") or 0) < cutoff:
            continue
        found.append(data)
    found.sort(key=lambda s: (float(s.get("updated_at") or 0),
                              float(s.get("created_at") or 0), str(s.get("id"))),
               reverse=True)
    return found


def session_file(root, session_id, kind):
    """The path of one catalog file of a session, or None.

    ``kind`` is one of ``scad`` / ``stl`` / ``preview`` / ``base`` and
    selects the filename the session record itself carries for it (the
    record keys are the kind names, except ``base``, whose key is
    ``base_stl``). The name must be a bare filename, so what is served
    always lives directly inside the session's own folder — a tampered
    record naming ``../`` or an absolute path resolves to None, never to
    a file outside.
    """
    key = _CATALOG_FILES.get(kind)
    if key is None:
        return None
    try:
        session = load_session(root, session_id)
    except DesignError:  # a malformed id names no session
        return None
    if session is None:
        return None
    name = session.get(key)
    if not isinstance(name, str) or not name or Path(name).name != name:
        return None
    path = _session_path(Path(root), session_id) / name
    return path if path.is_file() else None


def prune_old_sessions(root, days=RETENTION_DAYS, now=None):
    """Drop session folders whose last update is older than the retention window."""
    root = Path(root)
    if not root.is_dir():
        return 0
    cutoff = (now or time.time()) - days * 86400
    dropped = 0
    for folder in list(root.iterdir()):
        if not folder.is_dir():
            continue
        try:
            updated = json.loads((folder / "session.json").read_text(encoding="utf-8")).get("updated_at", 0)
        except (OSError, ValueError):
            updated = folder.stat().st_mtime
        if updated < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            dropped += 1
    return dropped


# --------------------------------------------------------------------- source handling

def extract_scad(reply) -> str:
    """The OpenSCAD source from a model reply: a fenced block, else the whole reply."""
    text = str(reply or "").strip()
    for block in _FENCE.findall(text):
        if _SCAD_HINT.search(block):
            return block.strip()
    if _SCAD_HINT.search(text) and "```" not in text:
        return text
    raise DesignError("the design model did not return any OpenSCAD code")


def external_refs(source: str) -> list[str]:
    """The file-access statement kinds a SCAD text uses (empty when self-contained)."""
    tokens = _scad_token_stream(source or "")
    return [name for name, pattern in _FORBIDDEN if pattern.search(tokens)]


def convert_to_stl(source_path, dest_path) -> Path:
    """Convert a 3MF to a binary STL through the intake geometry loader.

    Uses geometry_layout.load_parts (the same loader prepared intake inspects
    sources with, unit handling included) and write_binary_stl — there is no
    second 3MF reader in the design path.
    """
    from .geometry_layout import load_parts, write_binary_stl
    parts, *_ = load_parts(Path(source_path))
    triangles = []
    for part in parts:
        triangles.extend(part["triangles"])
    if not triangles:
        raise DesignError("that 3MF contains no printable mesh to edit")
    return write_binary_stl(triangles, Path(dest_path),
                            header_text="Print Handy design 3MF conversion")


def fetch_link_source(url, target_dir) -> Path:
    """Fetch a model link's source file into target_dir; returns the file path.

    The minimal existing fetch-to-file path (the same inventory + download
    pair prepared intake drives): MakerWorld picks its profile with
    makerworld_profiles.choose (the URL's profileId when present);
    Printables, Thingiverse and Creality Cloud must offer exactly one
    STL/3MF file, except that a Creality Cloud link's print profile names
    its file when it has one; Thangs, Nexprint and MyMiniFactory must
    offer exactly one too; Cults3D and MakerOnline stop with their named
    download gaps. Ambiguity is a plain refusal, never a silent pick.
    """
    from . import model_job as mj
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    host = urlsplit(str(url)).hostname or ""
    # Which site this is comes from the one registry (via model_job's
    # SITE_HOSTS, which model_sources builds); no host lists here.
    site = mj.SITE_HOSTS.get(host.lower())
    try:
        if site == "cults3d":
            # Metadata-only (#462 Stage 3): Cults' API exposes no files at all,
            # so there is nothing design could fetch — name the gap instead of
            # falling through to the Printables lookup.
            raise DesignError(
                "Print Handy cannot download files from Cults3D: its API exposes a "
                "listing's details only, never its 3D files, which are downloaded "
                "through the signed-in website. Download the file from the model "
                "page in a browser and drop the file here instead.")
        if site == "makerworld":
            job = mj.maker_inventory(str(url))
            from .makerworld_profiles import choose as choose_profile
            hint = re.fullmatch(r"profileId-(\d+)", urlsplit(str(url)).fragment or "")
            chosen = choose_profile(job["sources"], hint[1] if hint else None)
            if chosen is None:
                raise DesignError(
                    f"that MakerWorld link has {len(job['sources'])} profiles and no clear "
                    "default, so design cannot pick one; drop the file you want edited")
            return Path(mj.maker_download(job, chosen, target_dir))
        if site == "thingiverse":
            job = mj.thingiverse_inventory(str(url))
            rows = [row for row in job["sources"]
                    if Path(str(row.get("name") or "")).suffix.lower() in (".3mf", ".stl")]
            if not rows:
                raise DesignError("that Thingiverse link has no STL or 3MF file to edit")
            if len(rows) > 1:
                raise DesignError(
                    f"that Thingiverse link has {len(rows)} model files, so design cannot pick "
                    "one; drop the file you want edited")
            return Path(mj.thingiverse_download(job, rows[0], target_dir))
        if site == "crealitycloud":
            job = mj.crealitycloud_inventory(str(url))
            rows = [row for row in job["sources"]
                    if Path(str(row.get("name") or "")).suffix.lower() in (".3mf", ".stl")]
            if not rows:
                raise DesignError("that Creality Cloud link has no STL or 3MF file to edit")
            chosen = next((row for row in rows if row.get("suggested_by_link")), None)
            if chosen is None and len(rows) == 1:
                chosen = rows[0]
            if chosen is None:
                raise DesignError(
                    f"that Creality Cloud link has {len(rows)} model files, so design cannot pick "
                    "one; drop the file you want edited")
            try:
                return Path(mj.crealitycloud_download(job, chosen, target_dir))
            except RuntimeError as exc:
                # The download's own wording names the cause and remedy
                # (the login-token file, a refused token), so keep it.
                raise DesignError(str(exc)) from None
        login_site = mj.SITE_HOSTS.get(host.lower()) if host else None
        if login_site == "makeronline":
            raise DesignError(mj.MAKERONLINE_DOWNLOAD_GAP)
        if login_site in ("thangs", "nexprint", "myminifactory"):
            label = {"thangs": "Thangs", "nexprint": "Nexprint",
                     "myminifactory": "MyMiniFactory"}[login_site]
            job = mj.inventory_for_url(str(url))
            rows = [row for row in job["sources"]
                    if Path(str(row.get("name") or "")).suffix.lower() in (".3mf", ".stl")]
            if not rows:
                raise DesignError(f"that {label} link has no STL or 3MF file to edit")
            if len(rows) != 1:
                raise DesignError(
                    f"that {label} link has {len(rows)} model files, so design cannot pick "
                    "one; drop the file you want edited")
            try:
                return Path(mj.download_for_site(job, rows[0], target_dir))
            except RuntimeError as exc:
                # The download's own wording names the cause and remedy
                # (the login-token file, a refused token).
                raise DesignError(str(exc)) from None
        job = mj.printables_inventory(str(url))
        rows = [row for row in job["sources"]
                if Path(str(row.get("name") or "")).suffix.lower() in (".3mf", ".stl")]
        if not rows:
            raise DesignError("that Printables link has no STL or 3MF file to edit")
        if len(rows) > 1:
            raise DesignError(
                f"that Printables link has {len(rows)} model files, so design cannot pick "
                "one; drop the file you want edited")
        return Path(mj.printables_download(job, rows[0], target_dir))
    except DesignError:
        raise
    except Exception as exc:
        # Keep a curated, operator-fixable cause (a missing or refused site
        # token, a robot check, a site download gap, an intake size limit):
        # its text names the remedy. Only an unrecognised failure gets the
        # generic wording, and the cause stays chained for the log.
        from .. import diagnostics
        from .intake_resources import IntakeLimitExceeded
        if isinstance(exc, IntakeLimitExceeded) or diagnostics.known_cause(exc) is not None:
            raise DesignError(str(exc)) from exc
        raise DesignError("the linked model could not be downloaded — check the link is public and try again") from exc


def source_note(session) -> str:
    """One plain line naming what an edit session is working on and its limits."""
    source = session.get("source") or {}
    name = source.get("name") or "the model"
    if session.get("edit_mode") == "scad":
        note = f"Editing `{name}` in source form — its parameters can be changed directly."
        deps = session.get("scad_deps") or []
        if deps:
            note += (f" Note: the file also references other files ({', '.join(deps)}) that "
                     "were not dropped with it; a design here must be one self-contained "
                     "file, so those parts cannot be rendered as-is.")
        return note
    if session.get("edit_mode") == "mesh":
        via = " (converted from the 3MF)" if source.get("converted_from") else ""
        return (f"Editing `{name}`{via} as a mesh: scaling, mirroring, moving, adding "
                "shapes and cutting holes work; changes that need the original design's "
                "parameters do not — the design will say so if you ask for one.")
    return ""


def scan_scad(source: str) -> None:
    """Refuse file-access statements before execution (Stage 0 sandbox spec)."""
    if len(source.encode("utf-8")) > MAX_SCAD_BYTES:
        raise DesignError(
            f"the design model returned {len(source.encode('utf-8'))} bytes of OpenSCAD; "
            f"the limit for one design file is {MAX_SCAD_BYTES} bytes, so nothing was run")
    tokens = _scad_token_stream(source)
    for name, pattern in _FORBIDDEN:
        if pattern.search(tokens):
            raise DesignError(
                f"the design uses a '{name}' statement, which design files may not use: "
                "a design is one self-contained file and may not read other files. "
                "Nothing was run")


# --------------------------------------------------------------------- parameters (#841)

_PARAM_ASSIGN = re.compile(
    r"([A-Za-z_$][\w$]*)\s*=\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*;")


def _scad_param_spans(source: str):
    """Top-level numeric assignments as (name, value, value_start, value_end).

    A single pass over the original source that skips comments and string
    literals (the same lexing rules as :func:`_scad_token_stream`) and only
    matches at brace/paren/bracket depth zero, so a commented-out or
    module-local assignment is never a design parameter. The spans index
    into the original source so a value can be substituted in place.
    """
    spans = []
    i, n = 0, len(source or "")
    depth = 0
    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            end = source.find("\n", i)
            i = n if end == -1 else end
        elif ch == "/" and nxt == "*":
            end = source.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch == '"':
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                elif source[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
        elif ch in "{([":
            depth += 1
            i += 1
        elif ch in "})]":
            depth = max(0, depth - 1)
            i += 1
        elif depth == 0 and (ch.isalpha() or ch in "_$"):
            match = _PARAM_ASSIGN.match(source, i)
            if match:
                spans.append((match.group(1), float(match.group(2)),
                              match.start(2), match.end(2)))
                i = match.end()
            else:
                i += 1
        else:
            i += 1
    return spans


def scad_params(source: str) -> dict:
    """The design's top-level numeric parameters: {name: value}.

    The last assignment wins, matching OpenSCAD's compile-time variables.
    """
    return {name: value for name, value, _start, _end in _scad_param_spans(source)}


def _format_number(value) -> str:
    # 12 significant digits: enough for any value a person types, and
    # never the 6-digit rounding (1234.5678 -> 1234.57) of a bare :g.
    return f"{float(value):.12g}"


def _set_scad_param(source: str, name: str, value) -> str | None:
    """The source with the (last) top-level assignment to name set to value."""
    spans = [span for span in _scad_param_spans(source) if span[0] == name]
    if not spans:
        return None
    _name, _old, start, end = spans[-1]
    return source[:start] + _format_number(value) + source[end:]


def param_changes(before: dict, after: dict):
    """(name, old, new) for every parameter that changed, was added or removed."""
    changes = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old != new:
            changes.append((name, old, new))
    return changes


def format_param_changes(changes) -> list:
    """The posted diff lines: 'plate_z: 3 → 5' (added/removed named as such)."""
    lines = []
    for name, old, new in changes:
        if old is None:
            lines.append(f"{name}: (new) → {_format_number(new)}")
        elif new is None:
            lines.append(f"{name}: {_format_number(old)} → (removed)")
        else:
            lines.append(f"{name}: {_format_number(old)} → {_format_number(new)}")
    return lines


_DET_NAMED = re.compile(
    r"^\s*(?:make|set)\s+([A-Za-z_$][\w$]*)\s*(?:to|=)\s*"
    r"(-?\d+(?:\.\d+)?)\s*(?:mm)?\s*[.!]?\s*$", re.I)
_DET_RELATIVE = re.compile(
    r"^\s*(?:make\s+it\s+)?(-?\d+(?:\.\d+)?)\s*mm\s+(taller|wider|longer)\b[.!]?\s*$",
    re.I)
_RELATIVE_PARAMS = {"taller": "height", "wider": "width", "longer": "length"}


_REVERT_PREVIOUS = re.compile(
    r"^\s*(?:please\s+)?(?:undo(?:\s+(?:that|it|this|the\s+last\s+(?:change|one)))?|"
    r"revert(?:\s+(?:that|it|this))?|go\s+back(?:\s+one)?|"
    r"(?:go\s+)?back\s+to\s+(?:the\s+)?(?:previous|last|earlier)\s+(?:one|version|design))"
    r"\s*[.!]?\s*$", re.I)
_REVERT_FIRST = re.compile(
    r"^\s*(?:please\s+)?(?:go\s+back|revert|back)\s+to\s+(?:the\s+)?(?:first|original)"
    r"(?:\s+(?:one|version|design))?\s*[.!]?\s*$", re.I)
_REVERT_NUMBER = re.compile(
    r"^\s*(?:please\s+)?(?:go\s+back|revert|back)\s+to\s+(?:version|turn|v)\s*(\d{1,2})"
    r"\s*[.!]?\s*$", re.I)


def parse_revert(instruction):
    """"previous", a turn number, or None for an undo/go-back instruction.

    Narrow and anchored on purpose: "undo", "go back", "back to the
    previous version", "go back to the first one", "revert to version 2".
    Anything else ("undo the chamfer but keep the holes") is a real change
    and takes the model path.
    """
    text = str(instruction or "")
    if _REVERT_PREVIOUS.match(text):
        return "previous"
    if _REVERT_FIRST.match(text):
        return 1
    match = _REVERT_NUMBER.match(text)
    return int(match.group(1)) if match else None


def deterministic_param_edit(source, instruction):
    """A plain parameter edit applied without a model call, or None.

    Handles 'make <name> to <number>' / 'set <name> = <number>' (absolute:
    the parameter is set to the given value) and
    '<number> mm taller|wider|longer' (relative: the given amount is
    added to the parameter's current value, mapped to the
    height/width/length parameter). Returns (new_source, [(name, old, new)]).
    Anything else — an unknown or ambiguous parameter name — returns None
    and the caller runs a normal model turn.
    """
    if not source:
        return None
    params = scad_params(source)
    if not params:
        return None
    match = _DET_NAMED.match(instruction or "")
    if match:
        wanted, value = match.group(1), float(match.group(2))
        name = wanted if wanted in params else next(
            (key for key in params if key.casefold() == wanted.casefold()), None)
        if name is None:
            return None
        new_source = _set_scad_param(source, name, value)
        return new_source, [(name, params[name], value)]
    match = _DET_RELATIVE.match(instruction or "")
    if match:
        delta = float(match.group(1))
        word = _RELATIVE_PARAMS[match.group(2).casefold()]
        # Only the exact height/width/length parameter: a name that merely
        # contains the word ("hole_height") is a feature, not the part's
        # overall size, so anything else takes the model path.
        name = next((key for key in params if key.casefold() == word), None)
        if name is None:
            return None
        value = params[name] + delta
        new_source = _set_scad_param(source, name, value)
        return new_source, [(name, params[name], value)]
    return None


# Every design prompt lives here. The model's file opens with a small
# comment header (Part / Size / Assumes) that design_notes() reads back: the
# declared Size is checked against the measured render, and Part/Assumes
# become the reply's description, so the user hears what was made and what
# was assumed in the model's words while the numbers stay measured.
_SYSTEM_PROMPT = (
    "You design 3D-printable parts in OpenSCAD for an FDM printer. Answer with ONE complete "
    "OpenSCAD file (OpenSCAD 2021.01 syntax — no newer language features) and nothing "
    "outside it.\n"
    "Start the file with these comment lines, filled in:\n"
    "// Part: <one short line saying what this is>\n"
    "// Size: <X> x <Y> x <Z> mm  (the overall bounding box you intend, as it sits on the bed)\n"
    "// Assumes: <one assumption the user should know about>  (one line per assumption; "
    "omit when there are none)\n"
    "Then the design:\n"
    "- Millimeters. Every dimension is a named top-level variable with a trailing comment "
    "saying what it is, e.g. 'wall = 2.4; // wall thickness'. Derive the geometry from those "
    "variables; no unexplained numbers inside the shapes. Name the overall dimensions "
    "width, length and height where the part has them, so 'make it 5 mm taller' can be "
    "applied to the variable directly.\n"
    "- Proceed on sensible assumptions instead of asking: choose realistic sizes for the "
    "object, and state each choice that matters in an Assumes line.\n"
    "- Print it well: one connected solid lying on its largest flat face, resting on Z=0 "
    "(nothing below Z=0, nothing floating); avoid overhangs steeper than 45 degrees from "
    "vertical where the shape allows (use 45-degree chamfers instead of flat ledges; "
    "chamfer bottom edges, do not round them); walls at least 1.2 mm, 2 mm or more where "
    "the part carries load; screw and pin holes 0.2-0.3 mm larger than the nominal "
    "diameter; 0.2-0.3 mm clearance per side between mating parts; details at least "
    "0.6 mm; $fn between 32 and 96 for curves.\n"
    "- You may NOT use import, include, use or surface statements: the file may not read "
    "other files.\n"
    "- Given current OpenSCAD and a change: return the complete updated file, change only "
    "what was asked, keep the existing variable names, and update the Size and Assumes "
    "lines to match.\n"
    "- Given a render error or a failed check: fix that exact problem and return the "
    "complete corrected file.\n"
    "- Ask only when a missing fact would change the part and cannot reasonably be assumed "
    "(for example the size of the thing it must fit, when the request gives none and there "
    "is no standard size). Then answer with exactly one line, 'QUESTION: <the question>', "
    "and nothing else. Never ask about what you can choose yourself: material, wall "
    "thickness, style or cosmetic details."
)

_MESH_SYSTEM_PROMPT = (
    "You are the design engine for a 3D printer helper, editing an existing 3D model that "
    "exists only as a mesh. Answer with ONLY OpenSCAD code (no explanation, no markdown "
    "fences) that builds the edited model by calling base(): base() is the original model "
    "as one solid, already defined for you — do not redefine it and do not try to load any "
    "file yourself. You may transform base() (scale, mirror, rotate, translate), add "
    "primitive shapes, and combine with union/difference/intersection — e.g. "
    "'difference() { scale([1, 1, 2]) base(); translate([0, 0, -1]) cylinder(h = 99, d = 6); }'. "
    "Millimetres; use a sane $fn (24-64). Return the complete body every time, not a diff. "
    "You may NOT use import, include, use or surface statements. If the requested change "
    "needs the original design's internal parameters or generated structure (a thread "
    "pitch, a pattern, a wall built by the source code), you cannot do it from a mesh: "
    "answer with exactly 'NEEDS_SOURCE: <one short line saying what is needed>' and "
    "nothing else."
)

_MESH_HEADER = (
    "// Print Handy design render: composed by the engine, not written by the model.\n"
    "// base() is the model being edited; this header carries the only import in the\n"
    "// render and it names the session-local source copy.\n"
    "module base() {{ import(\"{name}\"); }}\n"
)


def build_volume(cfg):
    """[x, y, z] mm of the configured printer, or None when the bed is unknown.

    Settings keep ``bed_mm`` as two values and the height separately
    (``max_print_height_mm``); a missing height is None in the third slot.
    """
    printer = (cfg or {}).get("printer") or {}
    bed = list(printer.get("bed_mm") or [])[:2]
    if len(bed) != 2:
        return None
    try:
        x, y = float(bed[0]), float(bed[1])
        z = printer.get("max_print_height_mm")
        z = float(z) if z else None
    except (TypeError, ValueError):
        return None
    return [x, y, z]


def _volume_line(cfg):
    volume = build_volume(cfg)
    if not volume:
        return ""
    x, y, z = volume
    size = (f"{_format_number(x)} x {_format_number(y)} x {_format_number(z)} mm" if z
            else f"{_format_number(x)} x {_format_number(y)} mm (bed)")
    return f"Printer build volume: {size}; the part must fit inside it."


# The earlier change requests shown to the model on a revision: enough to
# resolve "the other side" or "like before", never the whole history.
_HISTORY_IN_PROMPT = 5


def _messages(session, instruction, previous_scad=None, error=None, *, cfg=None,
              feedback=None):
    mesh = session.get("edit_mode") == "mesh"
    parts = [f"Design request: {session['description']}"]
    asked = session.get("question")
    if asked and session.get("question_answer"):
        parts.append(f"You asked: {asked}\nThe user answered: {session['question_answer']}\n"
                     "Do not ask anything else: design it now, stating any further "
                     "assumptions in Assumes lines.")
    volume = _volume_line(cfg)
    if volume:
        parts.append(volume)
    if previous_scad:
        earlier = [entry.get("instruction") for entry in session.get("history") or []
                   if entry.get("instruction")][1:]
        if earlier:
            parts.append("Earlier changes in this session, oldest first:\n" + "\n".join(
                f"- {text}" for text in earlier[-_HISTORY_IN_PROMPT:]))
        label = ("Current edit body (it calls base(); return the complete new body)"
                 if mesh else f"Current OpenSCAD (turn {session['turns']})")
        parts.append(f"{label}:\n{previous_scad}")
        parts.append(f"Change requested: {instruction}")
    elif mesh or session.get("edit_mode") == "scad":
        parts.append(f"Edit requested: {instruction}")
    elif instruction and str(instruction).strip() != str(session["description"]).strip():
        # A retry of a first design (after a failed turn) may add detail;
        # without this it was silently dropped (#1206).
        parts.append(f"The user also said: {instruction}")
    if session.get("scad_deps"):
        parts.append("Note: the original file references external files that are not "
                     "available here; return a complete self-contained file.")
    if error:
        parts.append(f"The previous file failed to render. OpenSCAD said:\n{error}\n"
                     "Return the complete corrected file.")
    if feedback:
        parts.append("The previous file rendered, but the measured part failed these "
                     "checks:\n" + "\n".join(f"- {line}" for line in feedback) +
                     "\nFix the design (or the Size line, if the line was wrong) and return "
                     "the complete corrected file.")
    return [{"role": "system",
             "content": _MESH_SYSTEM_PROMPT if mesh else _SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)}]


# --------------------------------------------------------------------- design notes and checks

_NOTE_LINE = re.compile(r"^\s*//\s*(part|size|assumes?|assumption)\s*:\s*(.+?)\s*$",
                        re.I | re.M)
_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:mm)?\s*[x×*]\s*(\d+(?:\.\d+)?)\s*(?:mm)?\s*"
                   r"[x×*]\s*(\d+(?:\.\d+)?)", re.I)


def design_notes(source) -> dict:
    """The header comments the design prompt asks for, read back from a file.

    Returns {"part": str|None, "size_mm": [x, y, z]|None, "assumptions": [...]}.
    Only comment lines are read, so a file without the header (an older
    session, a dropped .scad, a mesh body) simply has no notes.
    """
    notes = {"part": None, "size_mm": None, "assumptions": []}
    for key, value in _NOTE_LINE.findall(str(source or "")):
        key = key.casefold()
        if key == "part" and notes["part"] is None:
            notes["part"] = value[:160]
        elif key == "size" and notes["size_mm"] is None:
            match = _SIZE.search(value)
            if match:
                notes["size_mm"] = [float(v) for v in match.groups()]
        elif key.startswith("assum") and len(notes["assumptions"]) < 6:
            notes["assumptions"].append(value[:200])
    return notes


_PARAM_COMMENT = re.compile(r"[ \t]*;?[ \t]*//[ \t]*(.+?)[ \t]*$", re.M)


def scad_param_notes(source) -> dict:
    """{name: trailing comment} for the top-level parameters that carry one."""
    source = str(source or "")
    notes = {}
    for name, _value, _start, end in _scad_param_spans(source):
        line_end = source.find("\n", end)
        tail = source[end:line_end if line_end != -1 else len(source)]
        match = _PARAM_COMMENT.match(tail.lstrip(";"))
        if match:
            notes[name] = match.group(1)[:80]
    return notes


# A declared size within this of the measured one is a match: curves are
# polygons ($fn), so a cylinder measures slightly under its diameter.
SIZE_TOLERANCE_MM = 0.5
SIZE_TOLERANCE_REL = 0.03


def check_design(info, *, notes=None, cfg=None, declared=True) -> list:
    """Checks of a rendered part against its declaration and the printer.

    Returns [(level, text)] with level "fail" (never handed over: the mesh is
    not a printable solid) or "warn" (printable, but not what was intended,
    or not proven to fit). The texts are written for the model's repair
    prompt and are shown to the user when a finding survives the repairs.
    ``declared=False`` skips the Size comparison (a deterministic parameter
    edit leaves the header's Size line stale by design).
    """
    from .assembly_check import PartReport, check_bed_fit, check_part
    findings = []
    part = PartReport("design", "structural", dict(info or {}))
    for finding in check_part(part):
        if finding.code == "mesh_topology":
            problems = finding.message.split(": ", 1)[-1]
            findings.append((finding.level, f"the mesh has problems: {problems}"))
        elif finding.code in ("empty_mesh", "no_volume"):
            findings.append(("fail", finding.message))
    shells = int((info or {}).get("shell_count") or 1)
    findings = [(level, text.replace("more than one shell",
                                     f"{shells} separate pieces instead of one solid"))
                for level, text in findings]
    low = (info or {}).get("bounds_min_mm") or []
    if len(low) == 3 and abs(float(low[2])) > 0.01:
        findings.append(("warn", f"the part's lowest point is at Z={float(low[2]):g} mm, "
                                 "not on the bed at Z=0"))
    dims = [float(v) for v in (info or {}).get("dimensions_mm") or []]
    volume = build_volume(cfg)
    if volume and len(dims) == 3:
        bed = [volume[0], volume[1], volume[2] if volume[2] else float("inf")]
        for finding in check_bed_fit(part, bed, min_margin_mm=0.0):
            if finding.level == "fail":
                findings.append(("warn", f"it does not fit the printer: {finding.message}"))
    size = (notes or {}).get("size_mm")
    if declared and size and len(dims) == 3:
        def close(a, b):
            return abs(a - b) <= max(SIZE_TOLERANCE_MM, SIZE_TOLERANCE_REL * max(a, b))
        if not (all(close(a, b) for a, b in zip(size, dims)) or
                all(close(a, b) for a, b in zip(sorted(size), sorted(dims)))):
            fmt = lambda v: " x ".join(f"{x:g}" for x in v)
            findings.append(("warn", f"it measures {fmt(dims)} mm, not the {fmt(size)} mm on "
                                     "the Size line"))
    return findings


# --------------------------------------------------------------------- rendering

def _limits(timeout_s):
    def apply():
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (int(timeout_s) + 5, int(timeout_s) + 10))
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 ** 3, 2 * 1024 ** 3))
    return apply


def render_scad(scad_path: Path, stl_path: Path, *, timeout_s=RENDER_TIMEOUT_S, runner=None):
    """Render one SCAD file to STL. Returns (ok, error_text). Never raises for a bad design."""
    exe = shutil.which("openscad")
    if not exe:
        if runner is subprocess.run:
            raise DesignError("OpenSCAD is not installed on this machine (no openscad on PATH), "
                              "so designs cannot be rendered")
        exe = "openscad"  # an injected runner (tests) stands in for the binary
    cmd = [exe, "--hardwarnings", "-o", str(stl_path), "--export-format", "binstl", str(scad_path)]
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(scad_path.parent)}
    run = runner or subprocess.run
    kwargs = {"capture_output": True, "text": True, "timeout": timeout_s,
              "cwd": str(scad_path.parent), "env": env}
    if runner is None and os.name == "posix":
        kwargs["preexec_fn"] = _limits(timeout_s)
    try:
        proc = run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        return False, f"OpenSCAD did not finish within {timeout_s} seconds (timeout)"
    except TypeError:
        # An injected test runner may not accept every subprocess keyword.
        proc = run(cmd)
    combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    if proc.returncode != 0 or "ERROR:" in combined or not stl_path.is_file() \
            or stl_path.stat().st_size < 84:
        lines = [line for line in combined.splitlines() if line.strip()]
        tail = lines[-6:]
        # The first WARNING:/ERROR: lines name the actual defect; the
        # statistics tail can push them out of the last-6 window, and
        # neither the model's repair prompt nor the user would ever see
        # them (#841 extra). Under --hardwarnings the WARNING: line is
        # often the whole cause (e.g. an unknown variable), with no ERROR:
        # line at all (#1046).
        def _first_index(marker):
            return next((i for i, line in enumerate(lines) if marker in line), None)

        named = (_first_index("WARNING:"), _first_index("ERROR:"))
        head = [lines[i] for i in sorted(i for i in named if i is not None)
                if lines[i] not in tail]
        if head:
            keep = 6 - len(head)
            tail = [*head, *tail[-keep:]] if keep > 0 else head
        return False, "\n".join(tail) or f"OpenSCAD exited with code {proc.returncode}"
    return True, ""


def validate_stl(stl_path: Path) -> dict:
    """Geometry facts for a rendered STL; raises DesignError on an unusable mesh."""
    from .geometry_layout import describe_triangles
    from .orca_adapter import read_stl_triangles
    info = describe_triangles(read_stl_triangles(stl_path))
    volume = info.get("abs_volume_mm3")
    if not info.get("dimensions_mm") or not volume or float(volume) <= 0:
        raise DesignError("the rendered file has no solid volume (empty or non-watertight "
                          "mesh), so there is nothing to print")
    return info


# --------------------------------------------------------------------- transports

def resolve_http_config(cfg) -> dict | None:
    """The effective design endpoint (the normal path in every mode): [design] first, [llm] as the fallback."""
    design = (cfg or {}).get("design") or {}
    llm = (cfg or {}).get("llm") or {}
    chat_url = str(design.get("chat_url") or llm.get("chat_url") or "").strip()
    model = str(design.get("model") or llm.get("model") or "").strip()
    if not chat_url or not model:
        return None
    return {"chat_url": chat_url, "model": model,
            "api_key_env": str(design.get("api_key_env") or llm.get("api_key_env")
                               or "LLM_API_KEY"),
            "timeout_s": int(design.get("timeout_s") or 180)}


def endpoint_configured(cfg) -> bool:
    """True when a design endpoint resolves ([design] inheriting [llm])."""
    return resolve_http_config(cfg) is not None


def effective_enabled(cfg, *, endpoint_available=None) -> bool:
    """The tri-state [design] enabled switch, resolved (#388 close-out ruling).

    ``enabled`` absent/None is AUTO: design is on exactly when an endpoint
    is available — ``endpoint_available`` when the caller knows its
    transport, else whether the HTTP endpoint resolves. Explicit True is on (the caller
    still reports an unconfigured endpoint separately), explicit False is
    off in all cases.
    """
    raw = (cfg or {}).get("design", {}).get("enabled")
    if raw is False:
        return False
    if raw is True:
        return True
    if endpoint_available is None:
        endpoint_available = endpoint_configured(cfg)
    return bool(endpoint_available)


def mode_name(cfg, *, endpoint_available=None) -> str:
    """The design posture in bot-check words: off / on / auto-on / auto-off."""
    raw = (cfg or {}).get("design", {}).get("enabled")
    if raw is False:
        return "off"
    if raw is True:
        return "on"
    return "auto-on" if effective_enabled(cfg, endpoint_available=endpoint_available) \
        else "auto-off"


def http_complete(cfg, messages, *, opener=None):
    """One design model call over an OpenAI-compatible endpoint. Returns the reply text."""
    import urllib.request
    resolved = resolve_http_config(cfg)
    if resolved is None:
        raise DesignError("design is enabled but no model endpoint is configured: "
                          "set [design] chat_url and model, or the [llm] values they inherit")
    from .. import credential_binding
    # The key is bound to the host of the setting the address came from (#1192).
    slot = ("design.chat_url" if str(((cfg or {}).get("design") or {}).get("chat_url") or "").strip()
            else "llm.chat_url")
    key = credential_binding.key_for(slot, resolved["chat_url"],
                                     os.environ.get(resolved["api_key_env"]) or "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = json.dumps({"model": resolved["model"], "messages": messages,
                       "max_tokens": 4000, "temperature": 0.3}).encode()
    req = urllib.request.Request(resolved["chat_url"], data=body, headers=headers, method="POST")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=float(resolved["timeout_s"])) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        raise DesignError("the design service did not answer — wait a moment and try again") from None
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        raise DesignError("the design model returned a reply with no message content") from None


# --------------------------------------------------------------------- the engine

class DesignEngine:
    """Runs design sessions against an injected model callable and OpenSCAD."""

    def __init__(self, cache_dir, complete, *, cfg=None, runner=None, preview=None,
                 render_timeout_s=None):
        self.root = sessions_root(cache_dir)
        self.complete = complete
        self.cfg = cfg or {}
        self.runner = runner
        self.preview = preview  # callable(stl_path) -> preview path; None uses render_preview
        design = self.cfg.get("design") or {}
        if render_timeout_s is None:
            # The documented [design] timeout_s bounds rendering as well as
            # the model call (P2-152, P2-157); an explicit constructor
            # argument still wins, and the constant is the last resort.
            configured = design.get("timeout_s")
            render_timeout_s = (configured if isinstance(configured, (int, float))
                                and not isinstance(configured, bool) and configured > 0
                                else RENDER_TIMEOUT_S)
        self.render_timeout_s = render_timeout_s
        # A bad value must not leave a turn with no model call at all (#1206).
        try:
            attempts = int(design.get("repair_attempts", 3))
        except (TypeError, ValueError):
            attempts = 3
        self.repair_attempts = max(0, attempts)

    # -- session lifecycle -------------------------------------------------

    def start_session(self, owner_key, chat_id, description, *, kind="create",
                      source=None):
        prune_old_sessions(self.root)
        session = {"id": uuid.uuid4().hex[:12], "owner_key": owner_key,
                   "chat_id": str(chat_id), "description": str(description)[:500],
                   "kind": kind, "source": source, "edit_mode": None,
                   "base_stl": None, "body_file": None, "scad_deps": [],
                   "status": "active", "turns": 0, "created_at": time.time(),
                   "updated_at": time.time(), "scad": None, "stl": None,
                   "preview": None, "preview_error": None, "geometry": None}
        return _save(self.root, session)

    def start_edit_session(self, owner_key, chat_id, description, source_path=None, *,
                           link_url=None, fetcher=None):
        """Seed an edit session from a dropped file or a model link.

        The source is copied into the session folder (a link is fetched there
        first, through the existing link machinery); the user's original is
        never modified. A ``.scad`` seeds source-form editing, an ``.stl``
        seeds mesh editing directly, and a ``.3mf`` is converted to a
        session-local STL first.
        """
        if (source_path is None) == (link_url is None):
            raise DesignError("an edit session needs exactly one source: a file or a link")
        session = self.start_session(owner_key, chat_id, description, kind="edit")
        folder = _session_path(self.root, session["id"])
        try:
            origin = "file"
            if link_url is not None:
                origin = "link"
                fetched = (fetcher or fetch_link_source)(
                    link_url, folder / "incoming")
                source_path = Path(fetched)
            source_path = Path(source_path)
            suffix = source_path.suffix.lower()
            if suffix not in (".stl", ".3mf", ".scad"):
                raise DesignError(
                    f"design can edit .stl, .3mf and .scad files, not "
                    f"{suffix or 'a file with no extension'}")
            if not source_path.is_file():
                raise DesignError("the model file to edit could not be read")
            record = {"origin": origin, "name": source_path.name,
                      "type": suffix.lstrip(".")}
            session["source"] = record
            if suffix == ".scad":
                text = source_path.read_text(encoding="utf-8", errors="replace")
                (folder / "source.scad").write_text(text, encoding="utf-8")
                session["edit_mode"] = "scad"
                session["scad"] = "source.scad"  # the baseline the first turn edits
                session["scad_deps"] = external_refs(text)
            else:
                if suffix == ".stl":
                    shutil.copyfile(source_path, folder / "source.stl")
                else:
                    convert_to_stl(source_path, folder / "source.stl")
                    record["converted_from"] = "3mf"
                session["edit_mode"] = "mesh"
                session["base_stl"] = "source.stl"
        except DesignError:
            self.cancel(session)
            raise
        except Exception as exc:
            self.cancel(session)
            raise DesignError("that model could not be prepared for editing — try a single .stl or .3mf file") from None
        return _save(self.root, session)

    def cancel(self, session):
        session["status"] = "cancelled"
        return _save(self.root, session)

    def mark_handed_off(self, session):
        session["status"] = "handed_off"
        return _save(self.root, session)

    def awaiting_revision(self, session):
        session["status"] = "awaiting_revision"
        return _save(self.root, session)

    def current_scad(self, session):
        name = session.get("scad")
        if not name:
            return None
        path = _session_path(self.root, session["id"]) / name
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def current_body(self, session):
        """The model's last mesh-edit body (the text it actually authored), if any."""
        name = session.get("body_file")
        if not name:
            return None
        path = _session_path(self.root, session["id"]) / name
        return path.read_text(encoding="utf-8") if path.is_file() else None

    # -- one turn ----------------------------------------------------------

    async def _call(self, messages):
        result = self.complete(messages)
        if inspect.isawaitable(result):
            result = await result
        return str(result or "")

    def _render_attempt(self, session, source, attempt=0):
        """Render one candidate source into attempt files; nothing is committed.

        Returns (attempt dict, "") or (None, error text). A candidate whose
        composed render file is byte-identical to a version this session
        already rendered reuses that version's STL instead of running
        OpenSCAD again (an undo, or a model that repeats itself).
        """
        import hashlib
        folder = _session_path(self.root, session["id"])
        folder.mkdir(parents=True, exist_ok=True)
        turn = session["turns"] + 1
        stem = f"turn-{turn}-try{attempt}"
        scad_path = folder / f"{stem}.scad"
        stl_path = folder / f"{stem}.stl"
        body_path = None
        authored = source
        if session.get("edit_mode") == "mesh":
            # The model authored a body, never a file: the render file is the
            # driver header (the only import) plus that body, and the body is
            # kept separately so the next turn shows the model its own text.
            body_path = folder / f"{stem}.body.scad"
            body_path.write_text(source, encoding="utf-8")
            source = _MESH_HEADER.format(name=session["base_stl"]) + source
        scad_path.write_text(source, encoding="utf-8")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        cached = next((folder / entry["stl"] for entry in session.get("history") or []
                       if entry.get("hash") == digest and entry.get("stl")
                       and (folder / entry["stl"]).is_file()), None)
        if cached is not None:
            shutil.copyfile(cached, stl_path)
            logger.info("design session %s: reusing the render of an identical earlier "
                        "version (%s)", session["id"], cached.name)
        else:
            ok, error = render_scad(scad_path, stl_path, timeout_s=self.render_timeout_s,
                                    runner=self.runner)
            if not ok:
                return None, error
        try:
            info = validate_stl(stl_path)
        except DesignError as exc:
            return None, str(exc)
        return {"scad_path": scad_path, "stl_path": stl_path, "body_path": body_path,
                "source": source, "authored": authored, "info": info, "hash": digest}, ""

    def _commit(self, session, attempt, *, instruction=None, findings=(), attempts=1,
                reverted_to=None, declared=True):
        """Make one rendered attempt the session's new turn (files, preview, history)."""
        folder = _session_path(self.root, session["id"])
        turn = session["turns"] + 1
        scad_path = folder / f"turn-{turn}.scad"
        stl_path = folder / f"turn-{turn}.stl"
        attempt["scad_path"].replace(scad_path)
        attempt["stl_path"].replace(stl_path)
        body_name = None
        if attempt.get("body_path") is not None:
            body_path = folder / f"turn-{turn}.body.scad"
            attempt["body_path"].replace(body_path)
            body_name = body_path.name
            session["body_file"] = body_name
        for stale in folder.glob(f"turn-{turn}-try*"):
            stale.unlink(missing_ok=True)
        info = attempt["info"]
        preview_path, preview_error = None, None
        try:
            preview_path = self._make_preview(session, stl_path, turn, info)
        except Exception as exc:
            # A failed preview never fails the turn, but it is never silent
            # either (#841): the reason is logged, kept on the session and
            # returned so the caller can post it next to the dimensions.
            logger.warning("design preview failed for session %s turn %s: %s",
                           session["id"], turn, exc)
            preview_error = str(exc)[:200]
        findings = [list(f) for f in findings]
        session["turns"] = turn
        session["scad"] = scad_path.name
        session["stl"] = stl_path.name
        session["preview"] = Path(preview_path).name if preview_path else None
        session["preview_error"] = preview_error
        session["geometry"] = info
        session["findings"] = findings
        session["status"] = "active"
        history = list(session.get("history") or [])
        history.append({"turn": turn, "hash": attempt["hash"], "scad": scad_path.name,
                        "stl": stl_path.name, "body": body_name,
                        "instruction": (str(instruction)[:300] if instruction else None),
                        "reverted_to": reverted_to, "declared": bool(declared)})
        session["history"] = history[-MAX_TURNS:]
        _save(self.root, session)
        return {"session": session, "scad_path": scad_path, "stl_path": stl_path,
                "preview_path": preview_path, "preview_error": preview_error,
                "param_changes": [], "geometry": info, "scad": attempt["source"],
                "notes": design_notes(attempt["authored"]), "findings": findings,
                "attempts": attempts, "reverted_to": reverted_to}

    def _discard_attempts(self, session):
        folder = _session_path(self.root, session["id"])
        for stale in folder.glob(f"turn-{session['turns'] + 1}-try*"):
            stale.unlink(missing_ok=True)

    def _render_and_validate(self, session, source, *, instruction=None):
        """Render and commit one source with no model in the loop (baseline turns)."""
        attempt, error = self._render_attempt(session, source)
        if attempt is None:
            self._discard_attempts(session)
            return None, error
        findings = check_design(attempt["info"], notes=design_notes(attempt["authored"]),
                                cfg=self.cfg, declared=False)
        return self._commit(session, attempt, instruction=instruction,
                            findings=findings, declared=False), ""

    def _make_preview(self, session, stl_path, turn, info=None):
        if self.preview is not None:
            return self.preview(stl_path)
        from . import render_preview
        bed = (self.cfg.get("printer") or {}).get("bed_mm")
        folder = _session_path(self.root, session["id"])
        dest = folder / f"turn-{turn}.png"
        dims = (info or {}).get("dimensions_mm")
        try:
            made = render_preview.render_sheet(str(stl_path), bed=bed, dimensions=dims)
            shutil.copyfile(made, dest)
        except Exception as exc:
            logger.warning(
                "design preview render failed for session %s: %s; "
                "falling back to the plate rasterizer", session["id"], exc)
            self._fallback_preview(stl_path, dest, info)
        if turn > 1:
            previous = folder / f"turn-{turn - 1}.png"
            if previous.is_file():
                return self._comparison_preview(previous, dest, turn)
        return str(dest)

    def _fallback_preview(self, stl_path, dest, info=None):
        """The dependency-free preview: plate_thumbnail's rasterizer (#841).

        The triangles are placed the way render_preview places the model —
        centered on the bed, resting on Z0 — and the measured dimensions
        are drawn on the image, since this sheet carries no OpenSCAD views.
        """
        import numpy as np
        from PIL import Image, ImageDraw
        from . import plate_thumbnail
        from .orca_adapter import read_stl_triangles
        tris = np.asarray(read_stl_triangles(stl_path), dtype=float).reshape(-1, 3, 3)
        if not len(tris):
            raise RuntimeError("the rendered STL has no triangles to preview")
        points = tris.reshape(-1, 3)
        lo, hi = points.min(axis=0), points.max(axis=0)
        bed = (self.cfg.get("printer") or {}).get("bed_mm")
        if bed:
            shift = [float(bed[0]) / 2 - (lo[0] + hi[0]) / 2,
                     float(bed[1]) / 2 - (lo[1] + hi[1]) / 2, -lo[2]]
        else:
            shift = [-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]]
        png = plate_thumbnail.render_plate(tris + shift, size=640, bed_mm=bed)
        image = Image.open(io.BytesIO(png)).convert("RGBA")
        dims = (info or {}).get("dimensions_mm")
        if dims:
            text = " × ".join(f"{float(v):g}" for v in dims) + " mm"
            draw = ImageDraw.Draw(image, "RGBA")
            box = draw.textbbox((0, 0), text)
            draw.rectangle([4, 4, box[2] + 14, box[3] + 12], fill=(0, 0, 0, 170))
            draw.text((9, 8), text, fill=(255, 255, 255, 255))
        image.save(dest)

    @staticmethod
    def _comparison_preview(previous_path, current_path, turn):
        """Previous turn's sheet beside this turn's, for a before/after look."""
        from PIL import Image, ImageDraw
        previous = Image.open(previous_path).convert("RGB")
        current = Image.open(current_path).convert("RGB")
        height = max(previous.height, current.height)
        sheet = Image.new("RGB", (previous.width + current.width, height), (24, 28, 34))
        sheet.paste(previous, (0, 0))
        sheet.paste(current, (previous.width, 0))
        draw = ImageDraw.Draw(sheet, "RGBA")
        for x, label in ((6, f"Turn {turn - 1}"), (previous.width + 6, f"Turn {turn}")):
            box = draw.textbbox((0, 0), label)
            draw.rectangle([x - 2, 4, x + box[2] + 8, box[3] + 12], fill=(0, 0, 0, 170))
            draw.text((x + 3, 8), label, fill=(255, 255, 255, 255))
        compare = current_path.with_name(f"turn-{turn}-compare.png")
        sheet.save(compare)
        return str(compare)

    async def run_turn(self, session, instruction):
        """One design turn: model -> scan -> render -> validate, with bounded repairs.

        Returns the turn result dict. Raises DesignError with user-ready words
        when the turn fails; the session keeps its previous good turn, if any.
        """
        if session["turns"] >= MAX_TURNS and session.get("scad"):
            raise DesignError(f"this design session has had its {MAX_TURNS} turns; "
                              "slice it or cancel and start a new design")
        mesh = session.get("edit_mode") == "mesh"
        previous = self.current_body(session) if mesh else self.current_scad(session)
        # A plain parameter edit ("make plate_z to 5", "make it 5 mm taller")
        # is applied to the source directly — no model call, so nothing else
        # in the file can drift (#841 gap 2). Mesh bodies have no parameters
        # to patch, so they always take the model path.
        if (session.get("question") and not session.get("question_answer")
                and not previous):
            # The reply to the one clarifying question the model asked: keep
            # the answer beside the original request and design that.
            session["question_answer"] = str(instruction)[:500]
            instruction = session["description"]
            # Kept on disk at once: if this turn fails, the next message is a
            # retry, not a second answer (#1206).
            _save(self.root, session)
        target = parse_revert(instruction) if previous else None
        if target is not None:
            return await asyncio.to_thread(self._revert, session, target, instruction,
                                           previous)
        if not mesh and previous:
            edit = deterministic_param_edit(previous, instruction)
            if edit is not None:
                new_source, changes = edit
                scan_scad(new_source)
                attempt, det_error = await asyncio.to_thread(
                    self._render_attempt, session, new_source)
                if attempt is None:
                    self._discard_attempts(session)
                    raise DesignError(
                        "that parameter change could not be rendered: "
                        f"{det_error}")
                # The header's Size line is the model's and is now stale by
                # design, so only the measured checks apply here.
                findings = check_design(attempt["info"], cfg=self.cfg, declared=False)
                self._refuse_unprintable(session, findings)
                result = await asyncio.to_thread(
                    lambda: self._commit(session, attempt, instruction=instruction,
                                         findings=findings, declared=False))
                result["param_changes"] = changes
                return result
        old_params = scad_params(previous) if previous and not mesh else {}
        # A first turn has no earlier source: every parameter would read as
        # "(new)", which is noise, not a diff — report changes only after one.
        had_previous = bool(previous)
        may_ask = (not previous and session.get("kind") == "create"
                   and not session.get("question"))
        error, feedback = None, None
        budget = 1 + self.repair_attempts
        last_error = ""
        best = None  # (rank, attempt, findings, number)
        calls = questions = renders = 0
        for number in range(budget):
            calls += 1
            reply = await self._call(_messages(session, instruction, previous, error,
                                               cfg=self.cfg, feedback=feedback))
            needs = re.match(r"\s*NEEDS_SOURCE:\s*(.+)", str(reply or ""))
            if needs:
                # Not a render failure and not repairable by asking again:
                # the change needs the source design, said in plain words.
                raise DesignError(
                    f"that change needs the original design source: {needs[1].strip()} "
                    "Mesh editing can scale, mirror, move, add shapes and cut holes; "
                    "drop the original .scad file if you have it and design can edit "
                    "that directly.")
            asked = re.match(r"\s*QUESTION:\s*(.+)", str(reply or ""), re.S)
            if asked and "```" not in str(reply):
                question = " ".join(asked[1].split())[:300]
                if may_ask:
                    session["question"] = question
                    session["status"] = "awaiting_revision"
                    _save(self.root, session)
                    logger.info("design session %s asked: %s", session["id"], question)
                    raise DesignQuestion(question)
                # At most one question per design: anything later is answered
                # by an assumption, and the retry spends the same budget.
                questions += 1
                error, feedback = None, [
                    f"you asked '{question}', but no further questions are possible: "
                    "make a sensible assumption, state it in an Assumes line, and "
                    "return the complete file"]
                continue
            try:
                source = extract_scad(reply)
            except DesignError as exc:
                # A chatty reply with no file is asked again within the same
                # budget, like a render error (#1206), not the end of the turn.
                last_error = str(exc)
                logger.info("design session %s attempt %s/%s returned no OpenSCAD code",
                            session["id"], number + 1, budget)
                error, feedback = None, [
                    "your reply contained no OpenSCAD code: answer with the complete "
                    "OpenSCAD file only"]
                continue
            scan_scad(source)  # a sandbox refusal is not repaired by asking again
            renders += 1
            attempt, last_error = await asyncio.to_thread(
                self._render_attempt, session, source, number)
            if attempt is None:
                logger.info("design session %s attempt %s/%s did not render: %s",
                            session["id"], number + 1, budget, last_error.splitlines()[-1:])
                error, feedback = last_error, None
                previous = source
                continue
            findings = check_design(attempt["info"], notes=design_notes(source), cfg=self.cfg)
            logger.info("design session %s attempt %s/%s rendered; findings: %s",
                        session["id"], number + 1, budget,
                        "; ".join(text for _level, text in findings) or "none")
            rank = (sum(level == "fail" for level, _ in findings), len(findings), -number)
            if best is None or rank < best[0]:
                best = (rank, attempt, findings, number)
            if not findings:
                break
            error, feedback = None, [text for _level, text in findings]
            previous = source
        if best is None:
            self._discard_attempts(session)
            if not renders and questions:
                # Nothing was rendered: say what really happened (#1206).
                raise DesignError(
                    f"the design model kept asking questions instead of designing "
                    f"({calls} tries), so nothing was made. Say the size or detail it is "
                    "missing in your next message, or start over with more detail")
            if not renders:
                raise DesignError(
                    f"the design model did not return a usable OpenSCAD file after {calls} "
                    f"attempt(s): {last_error}")
            raise DesignError(
                f"OpenSCAD could not render the design after {calls} attempt(s). "
                f"Last error: {last_error}")
        _rank, attempt, findings, _number = best
        self._refuse_unprintable(session, findings)
        result = await asyncio.to_thread(
            lambda: self._commit(session, attempt, instruction=instruction,
                                 findings=findings, attempts=calls))
        if not mesh and had_previous:
            # Name what the rewrite actually changed, so a model that
            # quietly moved a dimension is visible (#841).
            result["param_changes"] = param_changes(old_params,
                                                    scad_params(attempt["source"]))
        return result

    def _refuse_unprintable(self, session, findings):
        failed = [text for level, text in findings if level == "fail"]
        if failed:
            self._discard_attempts(session)
            raise DesignError("the design rendered, but it is not a printable solid: "
                              + "; ".join(failed))

    def _revert(self, session, target, instruction, current):
        """Go back to an earlier version: a new turn with that version's source.

        No model call; the earlier render is reused through the source-hash
        cache. ``target`` is "previous" or a turn number.
        """
        history = list(session.get("history") or [])
        if target == "previous":
            # Undo is a stack (#1206): a revert entry stands for the version
            # it restored, so undo steps back from there. Taking the entry
            # before the newest one made a second undo go forward again.
            by_turn = {e.get("turn"): e for e in history}
            index = len(history) - 1
            seen = set()
            while index >= 0 and history[index].get("reverted_to") in by_turn \
                    and index not in seen:
                seen.add(index)
                index = history.index(by_turn[history[index]["reverted_to"]])
            if index < 1:
                raise DesignError("there is no earlier version of this design to go back to")
            entry = history[index - 1]
        else:
            entry = next((e for e in history if e.get("turn") == target), None)
            if entry is None:
                known = ", ".join(str(e.get("turn")) for e in history) or "none"
                raise DesignError(f"this design has no version {target} to go back to "
                                  f"(versions kept: {known})")
        folder = _session_path(self.root, session["id"])
        name = entry.get("body") if session.get("edit_mode") == "mesh" else entry.get("scad")
        path = folder / name if name else None
        if path is None or not path.is_file():
            raise DesignError(f"version {entry.get('turn')} of this design is no longer on disk")
        source = path.read_text(encoding="utf-8")
        attempt, error = self._render_attempt(session, source)
        if attempt is None:
            self._discard_attempts(session)
            raise DesignError(f"version {entry.get('turn')} could not be rendered again: {error}")
        # Checked the way the version was checked when it was made: a
        # parameter edit's Size line is stale by design.
        declared = entry.get("declared", True)
        findings = check_design(attempt["info"], notes=design_notes(attempt["authored"]),
                                cfg=self.cfg, declared=declared)
        result = self._commit(session, attempt, instruction=instruction, findings=findings,
                              attempts=0, reverted_to=entry.get("turn"), declared=declared)
        if session.get("edit_mode") != "mesh" and current:
            result["param_changes"] = param_changes(scad_params(current), scad_params(source))
        return result

    def baseline_turn(self, session):
        """Preview a dropped model exactly as it is — no model call (#841 gap 4).

        Mesh sessions render the body ``base();``; SCAD sessions render the
        dropped source as-is. The result is the ordinary turn result, so the
        caller posts the same preview card as for a model turn.
        """
        if session.get("edit_mode") == "mesh":
            source = "base();"
        elif session.get("edit_mode") == "scad":
            source = self.current_scad(session)
            if source is None:
                raise DesignError("the dropped model's source could not be read back")
            # The dropped source is user-supplied, not model-generated, so
            # it gets the same sandbox scan run_turn applies before any
            # render: file-access statements (include/import/use/surface)
            # must never reach OpenSCAD, absolute paths included.
            scan_scad(source)
        else:
            raise DesignError("a baseline preview needs a dropped model; "
                              "describe what to make instead")
        result, error = self._render_and_validate(session, source)
        if result is None:
            raise DesignError(f"the dropped model could not be rendered as-is: {error}")
        return result


_REPLY_PARAMS = 6  # key dimensions named in a reply; the .scad has the rest


def reply_lines(result) -> list:
    """The design reply, in the same words on every platform.

    What was made (the model's Part line), its measured size, what was
    assumed, the key named dimensions, anything a check still found, and
    how to change it. Numbers are always the measured ones; only the
    description and the assumptions are the model's.
    """
    session = result["session"]
    info = result.get("geometry") or {}
    notes = result.get("notes") or {}
    title = notes.get("part") or ("Your design" if session.get("kind") == "create"
                                  else "The edited model")
    if result.get("reverted_to"):
        title = f"Back to version {result['reverted_to']}: {title[0].lower() + title[1:]}"
    lines = [f"**Design turn {session['turns']}** — {title.rstrip('.')}."]
    lines.append(f"Measured: {summary_text(result)}.")
    assumptions = notes.get("assumptions") or []
    if assumptions:
        lines.append("Assumed: " + "; ".join(a.rstrip(".") for a in assumptions) + ".")
    scad = result.get("scad") or ""
    if session.get("edit_mode") != "mesh" and scad:
        params = scad_params(scad)
        described = scad_param_notes(scad)
        names = [n for n in params if not n.startswith("$")]
        names = sorted(names, key=lambda n: (n not in described, list(params).index(n)))
        shown = [f"{n} = {_format_number(params[n])}"
                 + (f" ({described[n]})" if n in described else "")
                 for n in names[:_REPLY_PARAMS]]
        if shown:
            more = len(names) - len(shown)
            lines.append("Key parameters: " + ", ".join(shown)
                         + (f", and {more} more in the .scad" if more > 0 else "") + ".")
    findings = [text for _level, text in result.get("findings") or []]
    if findings:
        lines.append("Heads-up: " + "; ".join(findings) + ".")
    return lines


def change_hint(session) -> str:
    """How to change the design, phrased for the next message."""
    if session.get("edit_mode") == "mesh":
        return ('To change it, just say so — "scale it to 120%", "cut a 5 mm hole through '
                'the middle" — or "undo" to go back.')
    return ('To change it, just say so — "make it 5 mm taller", "set wall to 3", "add a '
            'second hook" — or "undo" / "go back to version 1".')


def summary_text(result) -> str:
    """The measured-facts line posted with a design preview."""
    info = result["geometry"]
    dims = " × ".join(f"{float(v):g}" for v in info["dimensions_mm"])
    volume = info.get("abs_volume_mm3")
    return (f"{dims} mm, {float(volume):.0f} mm³, {info['shell_count']} shell(s), "
            f"{'watertight' if info['watertight'] else 'NOT watertight'}, "
            f"{'manifold' if info['manifold'] else 'NOT manifold'}")
