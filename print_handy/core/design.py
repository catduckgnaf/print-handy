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
every mode resolves an OpenAI-compatible HTTP endpoint from [design]/[llm] first; only
when no endpoint resolves does Hermes mode fall back to the plugin's ``ctx.llm`` lane
with its registered ``print_design`` auxiliary task (#524 Stage 3).
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
    try:
        if "cults3d.com" in host:
            # Metadata-only (#462 Stage 3): Cults' API exposes no files at all,
            # so there is nothing design could fetch — name the gap instead of
            # falling through to the Printables lookup.
            raise DesignError(
                "Print Handy cannot download files from Cults3D: its API exposes a "
                "listing's details only, never its 3D files, which are downloaded "
                "through the signed-in website. Download the file from the model "
                "page in a browser and drop the file here instead.")
        if "makerworld.com" in host:
            job = mj.maker_inventory(str(url))
            from .makerworld_profiles import choose as choose_profile
            hint = re.fullmatch(r"profileId-(\d+)", urlsplit(str(url)).fragment or "")
            chosen = choose_profile(job["sources"], hint[1] if hint else None)
            if chosen is None:
                raise DesignError(
                    f"that MakerWorld link has {len(job['sources'])} profiles and no clear "
                    "default, so design cannot pick one; drop the file you want edited")
            return Path(mj.maker_download(job, chosen, target_dir))
        if "thingiverse.com" in host:
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
        if "crealitycloud.com" in host:
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
    return f"{float(value):g}"


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
        exact = next((key for key in params if key.casefold() == word), None)
        candidates = [key for key in params if word in key.casefold()]
        name = exact or (candidates[0] if len(candidates) == 1 else None)
        if name is None:
            return None
        value = params[name] + delta
        new_source = _set_scad_param(source, name, value)
        return new_source, [(name, params[name], value)]
    return None


_SYSTEM_PROMPT = (
    "You are the design engine for a 3D printer helper. Answer with ONE OpenSCAD file and "
    "nothing else: no explanation, no markdown fences. Rules: millimetres; one self-contained "
    "file; put every dimension in a named top-level variable so it can be adjusted; use a "
    "sane $fn (24-64) for curves; the part must be a single solid resting on Z=0 unless the "
    "request says otherwise. You may NOT use import, include, use or surface statements — "
    "the file may not read other files. If you are given previous OpenSCAD and a change "
    "request, return the complete updated file. If you are given an OpenSCAD error, return "
    "the complete corrected file."
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


def _messages(session, instruction, previous_scad=None, error=None):
    mesh = session.get("edit_mode") == "mesh"
    parts = [f"Design request: {session['description']}"]
    if previous_scad:
        label = ("Current edit body (it calls base(); return the complete new body)"
                 if mesh else f"Current OpenSCAD (turn {session['turns']})")
        parts.append(f"{label}:\n{previous_scad}")
        parts.append(f"Change requested: {instruction}")
    elif mesh or session.get("edit_mode") == "scad":
        parts.append(f"Edit requested: {instruction}")
    if session.get("scad_deps"):
        parts.append("Note: the original file references external files that are not "
                     "available here; return a complete self-contained file.")
    if error:
        parts.append(f"The previous file failed to render. OpenSCAD said:\n{error}\n"
                     "Return the complete corrected file.")
    return [{"role": "system",
             "content": _MESH_SYSTEM_PROMPT if mesh else _SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)}]


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
        # The first ERROR: line names the actual defect; a long warning tail
        # can push it out of the last-6 window, and neither the model's
        # repair prompt nor the user would ever see it (#841 extra).
        first_error = next((line for line in lines if "ERROR:" in line), None)
        if first_error and first_error not in tail:
            tail = [first_error, *tail[-5:]]
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
    transport (the Hermes lane counts as available), else whether the
    standalone HTTP endpoint resolves. Explicit True is on (the caller
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
    key = os.environ.get(resolved["api_key_env"]) or ""
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


async def hermes_complete(llm, messages, *, timeout_s=180):
    """One design model call through the Hermes plugin LLM lane (ctx.llm).

    This lane is the legacy fallback (#524 Stage 3): it serves a design
    turn only when no [design]/[llm] HTTP endpoint resolves. The model is
    the plugin's registered ``print_design`` auxiliary task: the
    operator pins provider/model/reasoning for it in the profile's
    ``auxiliary.print_design`` block; with no pin it follows the profile's
    active model. A trust-gate refusal (an operator who has not granted a
    pin the call needs) surfaces as a plain DesignError, never a crash.
    """
    kwargs = {"messages": messages, "task": "print_design", "purpose": "print-design",
              "timeout": timeout_s, "max_tokens": 4000, "temperature": 0.3}
    try:
        acomplete = getattr(llm, "acomplete", None)
        if callable(acomplete):
            result = await acomplete(**kwargs)
        elif callable(getattr(llm, "complete", None)):
            result = await asyncio.to_thread(llm.complete, **kwargs)
        else:
            raise DesignError("this Hermes version does not offer the plugin LLM lane "
                              "(ctx.llm), so design has no model in Hermes mode")
    except DesignError:
        raise
    except Exception as exc:
        if "Trust" in type(exc).__name__ or "trust" in str(exc).lower():
            raise DesignError(
                "Hermes refused the design model call (plugin LLM trust gate). "
                "Pin the design model in the profile's auxiliary.print_design block, "
                "or grant the print-feedback plugin a model override in config.yaml") from None
        raise DesignError("the design service did not answer — wait a moment and try again") from None
    text = getattr(result, "text", result)
    return str(text or "")


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
        self.repair_attempts = int(design.get("repair_attempts", 3))

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

    def _render_and_validate(self, session, source):
        folder = _session_path(self.root, session["id"])
        folder.mkdir(parents=True, exist_ok=True)
        turn = session["turns"] + 1
        scad_path = folder / f"turn-{turn}.scad"
        stl_path = folder / f"turn-{turn}.stl"
        if session.get("edit_mode") == "mesh":
            # The model authored a body, never a file: the render file is the
            # driver header (the only import) plus that body, and the body is
            # kept separately so the next turn shows the model its own text.
            body_path = folder / f"turn-{turn}.body.scad"
            body_path.write_text(source, encoding="utf-8")
            session["body_file"] = body_path.name
            source = _MESH_HEADER.format(name=session["base_stl"]) + source
        scad_path.write_text(source, encoding="utf-8")
        ok, error = render_scad(scad_path, stl_path, timeout_s=self.render_timeout_s,
                                runner=self.runner)
        if not ok:
            return None, error
        try:
            info = validate_stl(stl_path)
        except DesignError as exc:
            return None, str(exc)
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
        session["turns"] = turn
        session["scad"] = scad_path.name
        session["stl"] = stl_path.name
        session["preview"] = Path(preview_path).name if preview_path else None
        session["preview_error"] = preview_error
        session["geometry"] = info
        session["status"] = "active"
        _save(self.root, session)
        return {"session": session, "scad_path": scad_path, "stl_path": stl_path,
                "preview_path": preview_path, "preview_error": preview_error,
                "param_changes": [], "geometry": info, "scad": source}, ""

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
        if not mesh and previous:
            edit = deterministic_param_edit(previous, instruction)
            if edit is not None:
                new_source, changes = edit
                scan_scad(new_source)
                result, det_error = await asyncio.to_thread(
                    self._render_and_validate, session, new_source)
                if result is None:
                    raise DesignError(
                        "that parameter change could not be rendered: "
                        f"{det_error}")
                result["param_changes"] = changes
                return result
        old_params = scad_params(previous) if previous and not mesh else {}
        error = None
        attempts = 1 + self.repair_attempts
        last_error = ""
        for attempt in range(attempts):
            reply = await self._call(_messages(session, instruction, previous, error))
            needs = re.match(r"\s*NEEDS_SOURCE:\s*(.+)", str(reply or ""))
            if needs:
                # Not a render failure and not repairable by asking again:
                # the change needs the source design, said in plain words.
                raise DesignError(
                    f"that change needs the original design source: {needs[1].strip()} "
                    "Mesh editing can scale, mirror, move, add shapes and cut holes; "
                    "drop the original .scad file if you have it and design can edit "
                    "that directly.")
            source = extract_scad(reply)
            scan_scad(source)  # a sandbox refusal is not repaired by asking again
            result, last_error = await asyncio.to_thread(
                self._render_and_validate, session, source)
            if result is not None:
                if not mesh:
                    # Name what the rewrite actually changed, so a model
                    # that quietly moved a dimension is visible (#841).
                    result["param_changes"] = param_changes(
                        old_params, scad_params(source))
                return result
            error = last_error
            previous = source
            if attempt + 1 >= attempts:
                break
        raise DesignError(
            f"OpenSCAD could not render the design after {attempts} attempt(s). "
            f"Last error: {last_error}")

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


def summary_text(result) -> str:
    """The measured-facts line posted with a design preview."""
    info = result["geometry"]
    dims = " × ".join(f"{float(v):g}" for v in info["dimensions_mm"])
    volume = info.get("abs_volume_mm3")
    return (f"{dims} mm, {float(volume):.0f} mm³, {info['shell_count']} shell(s), "
            f"{'watertight' if info['watertight'] else 'NOT watertight'}, "
            f"{'manifold' if info['manifold'] else 'NOT manifold'}")
