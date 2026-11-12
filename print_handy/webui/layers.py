"""Read-only sliced-layer view (#624, stage 1).

Draws one plate of G-code Print Handy already wrote. Extrusion is colored
by role. Travel is faint. The page has no print, pause, or exclude control,
and it does not touch the chat confirm card. A file that is not G-code, or
that has no extrusion, is an error and no drawing.

Only a plate named by a job's own JOB.json, inside the archive, can be
opened, addressed by its position in that job (0 is ``gcode``, then each
derived plate), since every plate file has the same name. Stage 2 overrides and a remote slicer desktop are out of scope.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from . import shell

MAX_BYTES = 8_000_000
MAX_MOVES = 200_000
# A job id is an archive folder name. The longest producer is the chat
# intake flow (core/model_job.intake_folder): a slug of up to 70 chars
# plus "--" plus a 64-char intake hash — 136 in all.
_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,136}$")
_PLATE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}\.gcode$")

ROLES = ("perimeter", "infill", "support", "other", "travel")
COLORS = {
    "perimeter": "#3d8bfd",
    "infill": "#e6b325",
    "support": "#b57bff",
    "other": "#6fbf73",
    "travel": "#8b93a1",
}


def archive_root(data) -> Path:
    """The job archive for these settings, or a folder under the home dir."""
    paths = (data or {}).get("paths") or {}
    archive = paths.get("archive")
    if archive:
        return Path(str(archive)).expanduser()
    return Path(str(paths.get("state_home") or ".")) / "archive"


def classify_type(label: str) -> str:
    """Map a slicer role comment (``;TYPE:`` or Bambu ``; FEATURE:``) onto a drawing role."""
    text = str(label or "").strip().lower()
    if "support" in text or "raft" in text:
        return "support"
    if "infill" in text or "gap fill" in text:
        return "infill"
    if any(word in text for word in ("wall", "perimeter", "skin")):
        return "perimeter"
    return "other"


def clamp_layer(count: int, requested) -> int:
    """A layer index that stays inside ``0 .. count-1``."""
    if count <= 0:
        return 0
    try:
        index = int(requested)
    except (TypeError, ValueError):
        index = 0
    if index < 0:
        return 0
    if index >= count:
        return count - 1
    return index


def _words(parts):
    found = {}
    for part in parts[1:]:
        if len(part) < 2:
            continue
        letter = part[0].upper()
        if letter < "A" or letter > "Z":
            continue
        try:
            found[letter] = float(part[1:])
        except ValueError:
            continue
    return found


def parse_gcode(text) -> dict:
    """Layers of extrusion and travel, or ``{"ok": False, "error": ...}``.

    A failure has no ``layers`` key, so a caller cannot draw a partial toolpath
    that looks like the file.
    """
    if not isinstance(text, str) or "\x00" in text:
        return {"ok": False, "error": "this file is not G-code"}
    if len(text.encode("utf-8", "replace")) > MAX_BYTES:
        return {"ok": False, "error": "this G-code is too large to draw here"}
    role = "other"
    x = y = z = e = None
    rel_xyz = False
    rel_e = False
    want_break = False
    layers = []
    moves = 0
    saw_motion = False

    def ensure():
        nonlocal want_break
        if want_break or not layers:
            layers.append({"z": z, "moves": []})
            want_break = False
        return layers[-1]

    for raw_line in text.splitlines():
        raw = raw_line.strip()
        if not raw:
            continue
        if raw.startswith(";"):
            upper = raw.upper()
            if upper.startswith(";TYPE:") or upper.startswith("; FEATURE:"):
                # Role markers in both dialects: ";TYPE:" (Prusa/Orca) and
                # "; FEATURE:" (Bambu profiles, printers/bambulab/facts.py).
                role = classify_type(raw.split(":", 1)[1])
            elif (upper.startswith(";LAYER_CHANGE") or upper.startswith(";LAYER:")
                    or upper.startswith("; CHANGE_LAYER")):
                # Layer markers likewise: ";LAYER_CHANGE" / ";LAYER:" and
                # the Bambu dialect's "; CHANGE_LAYER".
                if layers and layers[-1]["moves"]:
                    want_break = True
            continue
        code = raw.split(";", 1)[0].strip()
        if not code:
            continue
        parts = code.split()
        cmd = parts[0].upper()
        if cmd == "M82":
            rel_e = False
            continue
        if cmd == "M83":
            rel_e = True
            continue
        if cmd == "G90":
            rel_xyz = False
            continue
        if cmd == "G91":
            rel_xyz = True
            continue
        if cmd == "G92":
            words = _words(parts)
            if "X" in words:
                x = words["X"]
            if "Y" in words:
                y = words["Y"]
            if "Z" in words:
                z = words["Z"]
            if "E" in words:
                e = words["E"]
            continue
        if cmd not in ("G0", "G00", "G1", "G01"):
            continue
        saw_motion = True
        words = _words(parts)
        nx, ny, nz, ne = x, y, z, e
        if "X" in words:
            nx = (0.0 if x is None else x) + words["X"] if rel_xyz else words["X"]
        if "Y" in words:
            ny = (0.0 if y is None else y) + words["Y"] if rel_xyz else words["Y"]
        if "Z" in words:
            nz = (0.0 if z is None else z) + words["Z"] if rel_xyz else words["Z"]
        extruding = False
        if "E" in words:
            if rel_e:
                extruding = words["E"] > 0
                ne = (0.0 if e is None else e) + words["E"]
            else:
                extruding = e is not None and words["E"] > e
                ne = words["E"]
                if e is None and words["E"] > 0:
                    extruding = True
        if x is not None and y is not None and nx is not None and ny is not None and (nx != x or ny != y):
            kind = role if extruding else "travel"
            if extruding or kind == "travel":
                layer = ensure()
                if layer["z"] is None:
                    layer["z"] = nz
                layer["moves"].append({
                    "role": kind, "x1": x, "y1": y, "x2": nx, "y2": ny,
                })
                moves += 1
                if moves > MAX_MOVES:
                    return {"ok": False, "error": "this G-code is too large to draw here"}
        x, y, z, e = nx, ny, nz, ne
    while layers and not layers[-1]["moves"]:
        layers.pop()
    extruded = [move for layer in layers for move in layer["moves"] if move["role"] != "travel"]
    if not saw_motion or not extruded:
        return {"ok": False, "error": "this G-code has no extrusion to draw"}
    return {"ok": True, "layers": layers, "roles": sorted({move["role"] for layer in layers for move in layer["moves"]})}


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _page(title, body, *, theme=shell.DEFAULT_THEME) -> str:
    """Through the one shell (#813); the header chrome carries the nav."""
    return shell.page(title, body, path="/layers", theme=theme)


def _bounds(moves):
    xs = [value for move in moves for value in (move["x1"], move["x2"])]
    ys = [value for move in moves for value in (move["y1"], move["y2"])]
    return min(xs), min(ys), max(xs), max(ys)


def svg_for(parsed, index) -> str:
    """One layer as SVG. Caller must pass a successful parse."""
    chosen = clamp_layer(len(parsed["layers"]), index)
    moves = parsed["layers"][chosen]["moves"]
    min_x, min_y, max_x, max_y = _bounds(moves)
    min_y, max_y = -max_y, -min_y
    pad = 2.0
    width = max(max_x - min_x, 1.0) + pad * 2
    height = max(max_y - min_y, 1.0) + pad * 2
    lines = []
    for move in moves:
        opacity = "0.35" if move["role"] == "travel" else "1"
        y1, y2 = -move["y1"], -move["y2"]
        lines.append(
            f'<line data-role="{move["role"]}" stroke="{COLORS[move["role"]]}" '
            f'stroke-opacity="{opacity}" stroke-width="0.4" '
            f'x1="{move["x1"]}" y1="{y1}" x2="{move["x2"]}" y2="{y2}"/>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{min_x - pad} {min_y - pad} {width} {height}" '
        'width="720" height="480" style="background:#111;max-width:100%;touch-action:none">'
        f'<g id="view">{"".join(lines)}</g></svg>'
    )


_PAN = (
    "<script>(function(){var svg=document.querySelector('svg'),group=document.getElementById('view');"
    "if(!svg||!group)return;var x=0,y=0,drag=null;"
    "svg.addEventListener('pointerdown',function(ev){svg.setPointerCapture(ev.pointerId);"
    "drag={x:ev.clientX,y:ev.clientY,ox:x,oy:y};});"
    "svg.addEventListener('pointermove',function(ev){if(!drag)return;"
    "var rect=svg.getBoundingClientRect(),vb=svg.viewBox.baseVal;"
    "var sx=rect.width?vb.width/rect.width:1,sy=rect.height?vb.height/rect.height:1;"
    "x=drag.ox+(ev.clientX-drag.x)*sx;y=drag.oy+(ev.clientY-drag.y)*sy;"
    "group.setAttribute('transform','translate('+x+' '+y+')');});"
    "function stop(){drag=null;}"
    "svg.addEventListener('pointerup',stop);svg.addEventListener('pointercancel',stop);"
    "var k=1;function draw(){group.setAttribute('transform','translate('+x+' '+y+') scale('+k+')');}"
    "svg.addEventListener('pointermove',function(){if(drag)draw();});"
    "document.querySelectorAll('[data-zoom]').forEach(function(b){b.addEventListener('click',function(){"
    "var z=b.getAttribute('data-zoom');if(z==='reset'){k=1;x=0;y=0;}else{k*=parseFloat(z);}draw();});});"
    "svg.addEventListener('wheel',function(ev){ev.preventDefault();k*=ev.deltaY<0?1.1:0.9;draw();},{passive:false});"
    "})();</script>"
)


def render_error(message, *, theme=shell.DEFAULT_THEME) -> str:
    """An error page with no drawing."""
    return _page("layers", f"<p>{_esc(message)}</p>", theme=theme)


def render_list(rows, *, theme=shell.DEFAULT_THEME) -> str:
    parts = ["<p>Sliced plates already in the archive. This view is read-only. "
             "Nothing here starts a job, and chat confirmation is unchanged.</p>"]
    if not rows:
        parts.append("<p>No sliced plates are in the archive yet.</p>")
        return _page("layers", "".join(parts), theme=theme)
    body = "".join(
        "<tr>"
        f'<td><a href="/layers/{_esc(row["job_id"])}/{int(row["index"])}">{_esc(row["title"])}</a></td>'
        f"<td>{_esc(row['file'])}</td>"
        "</tr>"
        for row in rows)
    parts.append("<table><tr><th>Plate</th><th>File</th></tr>" + body + "</table>")
    return _page("layers", "".join(parts), theme=theme)


def render_plate(parsed, index, *, title, theme=shell.DEFAULT_THEME) -> str:
    """One plate, or the error page when ``parsed`` is not ok."""
    if not parsed or not parsed.get("ok"):
        return render_error((parsed or {}).get("error") or "this G-code could not be drawn",
                            theme=theme)
    count = len(parsed["layers"])
    chosen = clamp_layer(count, index)
    links = []
    if chosen > 0:
        links.append(f'<a href="?layer={chosen - 1}">Previous layer</a>')
    else:
        links.append('<span class="note">Previous layer (this is the first layer)</span>')
    if chosen + 1 < count:
        links.append(f'<a href="?layer={chosen + 1}">Next layer</a>')
    else:
        links.append('<span class="note">Next layer (this is the last layer)</span>')
    links.append(
        '<form method="get" class="layer-jump" style="display:inline">'
        f'<label>Layer 1 <input type="range" name="layer" min="0" max="{count - 1}" '
        f'value="{chosen}" aria-valuetext="Layer {chosen + 1} of {count}" '
        'onchange="this.form.submit()"> '
        f'{count}</label> <button type="submit">Go</button></form>')
    links.append('<span class="view-tools">'
                 '<button type="button" data-zoom="1.25">Zoom in</button> '
                 '<button type="button" data-zoom="0.8">Zoom out</button> '
                 '<button type="button" data-zoom="reset">Fit / reset</button></span>')
    legend = " ".join(
        f'<span data-role="{role}" style="color:{COLORS[role]}">{role}</span>'
        for role in ROLES if role in parsed["roles"])
    z = parsed["layers"][chosen].get("z")
    height = f" · Z {float(z):g} mm" if isinstance(z, (int, float)) else ""
    body = (
        "<p>Read-only. Nothing here starts a job. "
        + " ".join(links) + "</p>"
        + f"<p>Layer {chosen + 1} of {count}{height}. {legend}</p>"
        + svg_for(parsed, chosen)
        + _PAN
    )
    return _page(title, body, theme=theme)


def _safe_job(job_id) -> bool:
    return bool(_JOB_ID.fullmatch(str(job_id or ""))) and job_id not in (".", "..")


def list_plates(archive: Path) -> list:
    """Plates named by JOB.json files inside ``archive``. Unreadable jobs are skipped."""
    root = Path(archive)
    if not root.is_dir():
        return []
    rows = []
    for job in sorted(root.glob("*/JOB.json")):
        job_id = job.parent.name
        if not _safe_job(job_id):
            continue
        try:
            payload = json.loads(job.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        title = str(payload.get("title") or job_id)
        seen = set()
        for index, candidate in enumerate(_candidates(payload)):
            path = _plate_path(job, candidate)
            if path is None or path in seen:
                continue
            seen.add(path)
            rows.append({"job_id": job_id, "index": index, "file": str(path.relative_to(job.parent.resolve())),
                         "title": title})
    return rows


def _candidates(payload) -> list:
    """A job's plate G-code entries in order: ``gcode``, then each derived plate's.

    A plate's position here is its address: every plate is named plate_1.gcode
    in its own folder, so names do not tell plates apart.
    """
    return [payload.get("gcode")] + [row.get("gcode") for row in payload.get("derived_plates") or []
                                     if isinstance(row, dict)]


def _plate_path(job: Path, gcode) -> Path | None:
    if not gcode:
        return None
    path = Path(str(gcode))
    if not path.is_absolute():
        path = job.parent / path
    try:
        path = path.resolve()
        root = job.parent.resolve()
    except OSError:
        return None
    if path.suffix != ".gcode" or not _PLATE_NAME.fullmatch(path.name):
        return None
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path


def resolve_plate(archive: Path, job_id: str, index) -> Path | None:
    """The job's ``index``-th plate file, or None when it is not that job's own G-code."""
    if not _safe_job(job_id) or not re.fullmatch(r"[0-9]{1,4}", str(index or "")):
        return None
    root = Path(archive).resolve()
    job = (root / job_id / "JOB.json")
    try:
        job = job.resolve()
    except OSError:
        return None
    if not job.is_relative_to(root) or not job.is_file():
        return None
    try:
        payload = json.loads(job.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    candidates = _candidates(payload)
    if int(index) >= len(candidates):
        return None
    return _plate_path(job, candidates[int(index)])


def load_plate(path: Path) -> dict:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return {"ok": False, "error": "this G-code could not be read"}
    if len(raw) > MAX_BYTES or b"\x00" in raw:
        return {"ok": False, "error": "this file is not G-code" if b"\x00" in raw
                else "this G-code is too large to draw here"}
    return parse_gcode(raw.decode("utf-8", "replace"))


def respond(archive: Path, rest: str, layer, theme=shell.DEFAULT_THEME) -> tuple:
    """``(status, content_type, body)`` for ``GET /layers`` and ``GET /layers/<job>/<n>``."""
    if not rest:
        return 200, "text/html; charset=utf-8", render_list(list_plates(archive), theme=theme)
    segments = [segment for segment in rest.split("/") if segment != ""]
    if len(segments) != 2:
        return 404, "application/json", {"ok": False, "reason": "no such sliced plate"}
    path = resolve_plate(archive, segments[0], segments[1])
    if path is None:
        return 404, "application/json", {"ok": False, "reason": "no such sliced plate"}
    parsed = load_plate(path)
    title = path.stem
    return 200, "text/html; charset=utf-8", render_plate(parsed, layer, title=title, theme=theme)
