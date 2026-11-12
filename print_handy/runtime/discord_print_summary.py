#!/usr/bin/env python3
"""Deterministic Discord presentation contract shared with HA owner.

Every print message (post slice summary, start, complete, cancel, failure) is rendered
here from JOB.json and G-code metadata. Hashes and file names stay internal.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import _config

_HASH = re.compile(r"[0-9a-f]{32,}", re.I)
_TIME = re.compile(r"^; estimated printing time \(normal mode\) = (.+)$", re.M)
# Bambu Studio (and Orca's header block) write "; model printing time: 1h 2m; total estimated time: 1h 5m"
# instead of the line above (#271): printing time first, the slicer's total as the fallback.
_BAMBU_TIME = (re.compile(r"^;.*?\bmodel printing time\s*:\s*([^;\n]+)", re.M),
               re.compile(r"^;.*?\btotal estimated time\s*:\s*([^;\n]+)", re.M))
# Bambu Studio's header block spells the total "; total filament weight [g] : X" (#444,
# verified against a real 2.8.2.61 slice); OrcaSlicer/Flash never write it.
_BAMBU_GRAMS = re.compile(r"^;\s*total filament weight \[g\]\s*:\s*([\d.]+)", re.M)
ARCHIVE = _config.archive_dir()
CONTEXT_PATH = _config.state_file("PRINT_HANDY_CONTEXT_PATH", "print_handy_context.json")
WARMUP_S = 5 * 60
ET = _config.tz()  # the configured display timezone (discord.timezone); the name is historical
VERBS = {"started": "Print started", "slice": "Print preparing", "complete": "Print complete", "cancelled": "Print cancelled",
         "error": "Print stopped with an error", "paused": "Print paused"}

# Kinds that end a print. A terminal post pings the person who started
# the print — but the ping is a send-path decision (#1095): the starter
# pings classifier (starter_pings) decides from the post kind and the
# job, and the rendered text here never carries a mention, on any
# platform. Alert posts that need the starter ping through the same
# classifier; start, slice and pause renders mention nobody because no
# send path asks for a ping for them.
TERMINAL_KINDS = ("complete", "cancelled", "error")


def starter_user_id(job, platform="discord"):
    """The ``platform`` user ID of the person who started this job's print, or "".

    #1332: Telegram and Matrix terminal posts mention the starter too.
    Discord and Telegram IDs are digits; a Matrix ID is ``@user:server``.

    The starter is the intake-stamped ``started_by`` record on JOB.json (or
    the start-context sidecar). Anything else — no stamp, a starter from
    another platform, a non-numeric ID — names nobody: a mention is never
    invented for a print Print Handy did not watch someone start.
    """
    if not isinstance(job, dict):
        return ""
    starter = job.get("started_by")
    if not isinstance(starter, dict):
        return ""
    platform = str(platform or "discord")
    if str(starter.get("platform") or "discord") != platform:
        return ""
    user_id = str(starter.get("user_id") or "")
    if platform == "matrix":
        from print_handy.bot.matrix_rest import _MATRIX_USER
        return user_id if _MATRIX_USER.fullmatch(user_id) else ""
    return user_id if user_id.isascii() and user_id.isdigit() else ""


def _inert(text):
    """Third-party text (a model title, a printer-reported filename) with any
    ``MEDIA:`` marker made inert, so it can never name a file to attach."""
    from print_handy.bot.rest import inert_media
    return inert_media(text)


def scrub(text):
    """Last line of defence: no hex run of 32+ characters ever reaches Discord,
    and no rendered text carries a live attachment marker (callers append
    their own MEDIA lines after rendering)."""
    return _inert(_HASH.sub("", str(text)))


def safe_label(value, fallback="Print"):
    text = Path(str(value or "")).name
    text = re.sub(r"\.(?:gcode|3mf|stl|json)$", "", text, flags=re.I)
    text = re.sub(r"[_-]?[0-9a-f]{32,}", "", text, flags=re.I).replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip(" .-/\\")
    return _inert(text) or fallback


def filament_label(name, material, name_fallback="", material_fallback=""):
    """'<name> <material>', without naming the material twice.

    Spoolman filament names often already end with the material ("Coffee
    PLA+"), so appending it again read "Coffee PLA+ PLA+" in alerts.
    """
    name = str(name or "").strip() or name_fallback
    material = str(material or "").strip() or material_fallback
    if name and material and (name.casefold() == material.casefold()
                              or name.casefold().endswith(" " + material.casefold())):
        return name
    return " ".join(part for part in (name, material) if part)


def spool_picker_label(name, material, spool_id, grams):
    """The one label shape every filament/spool picker shows (#792 part 2):
    'color · material · #id · grams left' — e.g. 'Coffee · PLA+ · #21 · 812 g left'.

    ``name`` is the Spoolman filament name (the color, often with the
    material already appended, which is stripped so it is named once).
    Unknown fields are said in place — 'color unknown', 'material
    unknown', 'grams unknown' — never dropped, so two similar spools stay
    tellable apart and a missing weight reads as missing, not as zero.
    """
    name = str(name or "").strip()
    material = str(material or "").strip()
    color = name
    if material and color.casefold() == material.casefold():
        color = ""
    elif material and color.casefold().endswith(" " + material.casefold()):
        color = color[:-(len(material) + 1)].strip()
    if color.casefold().startswith("spool #"):
        color = ""  # the shelf fallback name carries no color at all
    color = color or "color unknown"
    material = material or "material unknown"
    if isinstance(grams, (int, float)) and not isinstance(grams, bool):
        amount = f"{grams:g} g left"
    else:
        amount = "grams unknown"
    return f"{color} · {material} · #{spool_id} · {amount}"


def parse_duration(text):
    """'1d 2h 3m 4s' -> seconds, or None."""
    if not text:
        return None
    found = re.findall(r"(\d+)\s*([dhms])", str(text))
    if not found:
        return None
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in found)


def human(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h {m}m" if h else f"{m}m"


def finish_text(seconds, now=None):
    if not isinstance(seconds, (int, float)) or seconds < 0:
        return None
    now = now or datetime.now(ET)
    return (now + timedelta(seconds=seconds + WARMUP_S)).strftime("%-I:%M %p %a ") + _config.tz_label(now)


def gcode_metadata(gcode):
    try:
        with open(gcode, errors="ignore") as handle:
            text = handle.read()
    except OSError:
        return None, None
    match = _TIME.search(text) or next((m for m in (p.search(text) for p in _BAMBU_TIME) if m), None)
    filament = re.search(r"^;\s*total filament used \[g\]\s*=\s*([\d.]+)", text, re.M) or _BAMBU_GRAMS.search(text)
    return (parse_duration(match.group(1)) if match else None,
            float(filament.group(1)) if filament else None)


def gcode_seconds(gcode):
    return gcode_metadata(gcode)[0]


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _part_names(job, keys=None):
    names = []
    for part in (job.get("color_manifest") or {}).get("parts") or []:
        if keys is not None and part.get("key") not in keys:
            continue
        name = re.sub(r"\.(stl|3mf|obj|step|stp)$", "", safe_label(part.get("name"), ""), flags=re.I)
        if name:
            names.append(name)
    counts = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return [f"{n}× {name}" if n > 1 else name for name, n in counts.items()]


def _filament(job, keys=None, slot=None):
    """(slot, color, material) from the saved decisions; None fields when unknown."""
    dec = job.get("color_decisions") or {}
    sel = job.get("filament_selection") or dec.get("selection")
    if not sel and isinstance(dec.get("choices"), dict):
        pool = [v for k, v in dec["choices"].items() if keys is None or k in keys] or list(dec["choices"].values())
        sel = next((v for v in pool if slot is None or v.get("slot") == slot), None) or (pool[0] if pool else None)
    sel = sel or {}
    return sel.get("slot", slot), sel.get("color"), sel.get("material")


def _filaments(job, keys=None):
    """Every slot the parts in `keys` print with, in slot order, when that is more than one.

    One dict per slot: slot, color, material. A job with one saved selection,
    or whose parts all use one slot, gives [] so its posts keep their single
    "Slot N:" line. _filament alone keeps only the first part's choice, so a
    two-color print used to announce just one of its slots.
    """
    dec = job.get("color_decisions") or {}
    if job.get("filament_selection") or dec.get("selection") or not isinstance(dec.get("choices"), dict):
        return []
    by_slot = {}
    for key, choice in dec["choices"].items():
        if keys is not None and key not in keys and str(key).removeprefix("part:") not in keys:
            continue
        slot = choice.get("slot") if isinstance(choice, dict) else None
        if slot is not None and slot not in by_slot:
            by_slot[slot] = {"slot": slot, "color": choice.get("color"), "material": choice.get("material")}
    if len(by_slot) < 2:
        return []
    return [by_slot[slot] for slot in sorted(by_slot, key=lambda value: (str(type(value)), value))]


def _slot_grams(gcode, mapping):
    """{slot: grams} from the slicer's per-tool '; filament used [g] = a, b' line and the
    tool -> slot mapping; {} when either is missing or they do not line up."""
    if not gcode or not isinstance(mapping, dict) or not mapping:
        return {}
    try:
        with open(gcode, errors="ignore") as handle:
            text = handle.read()
    except OSError:
        return {}
    found = re.search(r"^;\s*filament used \[g\]\s*=\s*([0-9.,\s]+)$", text, re.M)
    if not found:
        return {}
    try:
        grams = [float(value) for value in found.group(1).split(",") if value.strip()]
        result = {}
        for tool, slot in mapping.items():
            index = int(tool)
            if not 0 <= index < len(grams):
                return {}
            result[slot] = round(grams[index], 2)
    except (TypeError, ValueError):
        return {}
    return result


def _filament_lines(filaments, position_noun="Slot"):
    """'Slot 2: bronze PLA+ (~6.8 g)', one per slot of a multi-slot plate; grams only when known."""
    lines = []
    for entry in filaments or ():
        grams = entry.get("filament_g")
        amount = f" (~{grams:g} g)" if isinstance(grams, (int, float)) and not isinstance(grams, bool) else ""
        lines.append(f"{position_noun} {entry.get('slot')}: "
                     + filament_label(safe_label(entry.get("color"), "color unknown"),
                                      safe_label(entry.get("material"), "material unknown")) + amount)
    return lines


def plates_of(job):
    """One dict per sliced plate: gcode, parts, slot, color, material, seconds, plate_id.

    A plate that prints with more than one slot also carries ``filaments``: one
    row per slot (slot, color, material, filament_g when the slicer says).
    """
    rows = []
    mappings = []
    derived = job.get("derived_plates") or []
    for row in derived:
        slot, color, material = _filament(job, set(row.get("part_keys") or []), row.get("slot"))
        rows.append({"gcode": row.get("gcode"), "plate": row.get("plate_id"),
                     "parts": _part_names(job, set(row.get("part_keys") or [])),
                     "slot": slot, "color": color, "material": material})
        mappings.append((set(row.get("part_keys") or []), row.get("tool_mapping")))
    if not rows and job.get("gcode"):
        slot, color, material = _filament(job)
        rows.append({"gcode": job["gcode"], "plate": None, "parts": _part_names(job),
                     "slot": slot, "color": color, "material": material})
        mappings.append((None, job.get("tool_mapping")))
    for row, (keys, mapping) in zip(rows, mappings):
        row["seconds"], row["filament_g"] = gcode_metadata(row["gcode"]) if row.get("gcode") else (None, None)
        filaments = _filaments(job, keys)
        if filaments:
            # Only a multi-slot plate gets the key: a one-slot plate's row and
            # every post built from it stay exactly as they were.
            grams = _slot_grams(row.get("gcode"), mapping)
            row["filaments"] = [{**entry, "filament_g": grams.get(entry["slot"])} for entry in filaments]
    return rows


def _title(job):
    if job.get("title"):
        return safe_label(job["title"])
    return safe_label(Path(str(job.get("downloaded_file") or "")).stem, "Print")


def _job_files(archive):
    for path in sorted(Path(archive).glob("*/JOB.json")):
        try:
            yield path, {**json.loads(path.read_text()), '_job_path': str(path.resolve())}
        except (OSError, ValueError):
            continue


def find_job(filename, archive=None, context_path=None):
    """Resolve a Moonraker filename to (job dict or None, plate row or None).

    Order: the start-time context sidecar (written by record_start), then JOB.json lookup by
    G-code name. Moonraker names are '<stem>_<sha256>.gcode'; the stem is the local file stem.
    """
    name = Path(str(filename or "")).name
    if not name:
        return None, None
    archive = Path(archive) if archive else ARCHIVE
    original_path = Path(str(filename or "")).expanduser()
    requested_is_path = original_path.is_absolute()
    requested_path = original_path
    try:
        requested_path = requested_path.resolve()
    except OSError:
        pass
    for path, job in _job_files(archive):
        candidates = [(None, job.get("gcode"))] + [(row, row.get("gcode")) for row in job.get("derived_plates") or []]
        for row, gcode in candidates:
            if not gcode:
                continue
            local = Path(gcode)
            hit = local.name == name
            if requested_is_path:
                try:
                    hit = local.resolve() == requested_path
                except OSError:
                    hit = False
            if not hit and local.is_file():
                # model_job.remote_name: optional "<Title>_" prefix, the plate stem, then the full SHA256.
                # A .gcode.3mf upload (#564) appends .3mf to that name; its digest is the inner
                # G-code's, so the package name binds to the same local plate.
                tail = re.escape(local.suffix) + (r"(?:\.3mf)?" if local.suffix == ".gcode" else "")
                m = re.fullmatch(rf"(?:.+_)?{re.escape(local.stem)}_([0-9a-f]{{64}}){tail}", name)
                hit = bool(m) and _sha256(local) == m[1]
            if hit:
                plate = next((p for p in plates_of(job) if p.get("gcode") == gcode), None)
                return job, plate
    try:
        ctx = json.loads(Path(context_path or CONTEXT_PATH).read_text())
        entry = (ctx.get("jobs") or {}).get(name)
        if isinstance(entry, dict) and entry.get("title"):
            sl = entry.get("slicer_settings") or {}
            spool = entry.get("active_spool") or {}
            job = {"title": entry["title"], "gcode": sl.get("local_gcode"),
                   "started_by": entry.get("started_by")}
            plate = {"gcode": sl.get("local_gcode"), "plate": None, "parts": [],
                     "slot": None, "color": spool.get("color"), "material": spool.get("material"),
                     "seconds": parse_duration(entry.get("estimated_time"))}
            return job, plate
    except (OSError, ValueError, AttributeError):
        pass
    return None, None


def _print_number(job):
    """Read the print's number without assigning one from a listener render.

    A job dict that carries this launch's own number (a start record context)
    wins; otherwise the job's NEWEST print number from the registry (a job
    printed again has several; its first would name an older print).
    """
    if isinstance(job, dict):
        own = job.get('print_number')
        if isinstance(own, int) and not isinstance(own, bool) and own > 0:
            return own
    path = job.get('_job_path') if isinstance(job, dict) else None
    if not path:
        return None
    try:
        from . import print_control
        return print_control.latest_number_for_job(path)
    except (OSError, ValueError, AttributeError, ImportError):
        return None


def _position_noun(job):
    """'Toolhead' for a multi_tool job's positions, 'Slot' for every other printer (#346)."""
    try:
        from .. import printers
        model = (job or {}).get("printer_model")
        adapter = printers.get(model) if model else printers.current()
        return "Toolhead" if adapter.multi_tool else "Slot"
    except Exception:
        return "Slot"


def announcement_printer_name(job=None):
    """The roster display name a lifecycle announcement is about (#536), or None.

    Only a multi-printer roster names the printer — a one-printer install's
    announcements stay byte-identical. The name comes from the job's stamped
    printer when a job is known, else from the printer this process serves
    (its per-printer listener identity), else the default entry.
    """
    try:
        entries = list((_config.cfg() or {}).get("printers") or [])
    except Exception:
        return None
    if len(entries) <= 1:
        return None
    pid = str((job or {}).get("printer_id") or "")
    if not pid:
        try:
            from .. import listeners
            pid = str(listeners.instance_printer_id() or "")
        except Exception:
            pid = ""
    if pid:
        for entry in entries:
            if str(entry.get("id")) == pid:
                return str(entry.get("name") or entry.get("id"))
        return pid
    first = entries[0]
    return str(first.get("name") or first.get("id"))


def format_confirmed_print(*, model, parts=(), plate=None, slot=None, color=None, material=None,
                           filament_g=None, seconds=None, finish=None, kind="started", plates_total=None,
                           print_number=None, position_noun="Slot", printer_name=None, filaments=None):
    """Human readable, hash free message for one print event.

    ``filaments`` (a multi-slot plate's per-slot rows from plates_of) replaces
    the single slot line with one line per slot.
    """
    details = [f"{VERBS.get(kind, VERBS['started'])}: {safe_label(model)}"]
    if print_number:
        details.append(f"Print #{print_number}")
    pieces = [safe_label(part) for part in parts if str(part or "").strip()]
    if pieces:
        details.append("Parts: " + ", ".join(pieces))
    if plate is not None:
        details.append(f"Plate {plate}" + (f" of {plates_total}" if plates_total and plates_total > 1 else ""))
    if filaments and len(filaments) > 1:
        details.extend(_filament_lines(filaments, position_noun))
    elif slot is not None or color or material:
        prefix =f"{position_noun} {slot}: " if slot is not None else "Filament: "
        details.append(prefix + filament_label(safe_label(color, 'color unknown'),
                                               safe_label(material, 'material unknown')))
    if filament_g is not None:
        details.append(f"Filament: {filament_g:g} g")
    if kind in ("started", "slice"):
        details.append(f"Estimated total: ~{human(seconds + WARMUP_S)} (printing: {human(seconds)})"
                       if isinstance(seconds, (int, float)) and seconds >= 0 else "Estimated total: unknown")
        if finish:
            details.append(f"Approx. finish: {finish}")
    if printer_name:
        # A trailing line, not headline text: first-line consumers (the
        # stop/resume name extraction) keep reading the model line (#536).
        details.append(f"Printer: {printer_name}")
    return scrub("\n".join(details))


def plate_payload(kind, filename, archive=None, context_path=None):
    """Sanitized structured equivalent of the native plate line for HA consumers."""
    job, plate = find_job(filename, archive, context_path)
    if not job:
        return None
    row = plate or {}
    return {
        "model": _title(job),
        "plate": row.get("plate"),
        "parts": list(row.get("parts") or []),
        "slot": row.get("slot"),
        "color": safe_label(row.get("color"), "") or None,
        "material": safe_label(row.get("material"), "") or None,
        "filament_g": row.get("filament_g"),
        "estimated_seconds": row.get("seconds"),
    }


def format_preparation(job, plate=None):
    """Exact bound job/plate summary used before starting; never re-resolves a filename."""
    row = plate or {}
    return format_confirmed_print(
        model=_title(job or {}), parts=row.get("parts") or (), plate=row.get("plate"),
        slot=row.get("slot"), color=row.get("color"), material=row.get("material"),
        filaments=row.get("filaments"),
        filament_g=row.get("filament_g"), seconds=row.get("seconds"), kind="slice",
        plates_total=len((job or {}).get("derived_plates") or []) or None,
        print_number=_print_number(job or {}), position_noun=_position_noun(job),
        printer_name=announcement_printer_name(job))


def format_started(job, plate=None, now=None):
    """Exact bound start summary, avoiding another filename lookup after approval."""
    row = plate or {}
    seconds = row.get("seconds")
    return format_confirmed_print(
        model=_title(job or {}), parts=row.get("parts") or (), plate=row.get("plate"),
        slot=row.get("slot"), color=row.get("color"), material=row.get("material"),
        filaments=row.get("filaments"),
        filament_g=row.get("filament_g"), seconds=seconds,
        finish=finish_text(seconds, now), kind="started",
        plates_total=len((job or {}).get("derived_plates") or []) or None,
        print_number=_print_number(job or {}), position_noun=_position_noun(job),
        printer_name=announcement_printer_name(job))


def format_event(kind, filename, now=None, archive=None, context_path=None, extra=None, emit=True):
    """Message for a Moonraker event on `filename`. Falls back to a scrubbed file label.

    ``emit`` queues the MQTT/ntfy event on the notify worker (never I/O on
    this path). A caller that only wants the name or text passes
    ``emit=False`` so no event is invented.
    """
    job, plate = find_job(filename, archive, context_path)
    printer_name = announcement_printer_name(job)
    if not job:
        message = f"{VERBS.get(kind, VERBS['started'])}: {safe_label(filename)}"
        if printer_name:
            message += f"\nPrinter: {printer_name}"
    else:
        seconds = (plate or {}).get("seconds")
        total = len(job.get("derived_plates") or []) or None
        message = format_confirmed_print(
            model=_title(job), parts=(plate or {}).get("parts") or (), plate=(plate or {}).get("plate"),
            slot=(plate or {}).get("slot"), color=(plate or {}).get("color"),
            material=(plate or {}).get("material"), filament_g=(plate or {}).get("filament_g"),
            filaments=(plate or {}).get("filaments"),
            seconds=seconds, finish=finish_text(seconds, now) if kind == "started" else None,
            kind=kind, plates_total=total, print_number=_print_number(job),
            position_noun=_position_noun(job), printer_name=printer_name)
    if extra:
        message += "\n" + scrub(extra)
    message = scrub(message)
    if emit:
        try:
            from ..notify import emit_queued, event_for
            name = event_for(kind, extra)
            if name:
                emit_queued(name, message)
        except Exception:
            pass
    # No mention is appended here, for any kind (#1095): the notify emit
    # above and every chat platform get the same plain text, and whether
    # the Discord post pings the starter is decided by starter_pings at
    # the send path, from the post kind and this job; the Discord sender
    # then puts the one <@id> in the posted content (bot.rest.starter_mention).
    return message


def _copies(job):
    try:
        return max(1, int(job.get("copies") or 1))
    except (TypeError, ValueError):
        return 1


def _sliced_copies(job):
    """Copies the G-code really contains, as recorded by the slicer path that cloned them.

    Merged, per plate, per part and split slicing never clone copies and record nothing; a record
    from an earlier slice no longer matches the job's current G-code, so it counts as one copy.
    """
    record = job.get("sliced_copies")
    if job.get("derived_plates") or not isinstance(record, dict) or record.get("gcode") != job.get("gcode"):
        return 1
    try:
        return max(1, int(record.get("copies") or 1))
    except (TypeError, ValueError):
        return 1


def _site_plate(job, plate):
    """The model page's own per-plate (seconds, grams) for plate `plate`, or (None, None)."""
    plates = (job.get("selected_source") or {}).get("plates")
    if not isinstance(plates, list) or not isinstance(plate, int) or not 1 <= plate <= len(plates):
        return None, None
    row = plates[plate - 1] if isinstance(plates[plate - 1], dict) else {}

    def num(value):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 1e7
        return float(value) if ok else None
    return num(row.get("prediction_s")), num(row.get("weight_g"))


def _part_rows(job, keys, plate_g):
    """Parts on one plate with their own share of the plate's grams (volume split, else equal).

    Never the extruder or plate total repeated on every part: shares always sum to `plate_g`.
    """
    parts = [p for p in (job.get("color_manifest") or {}).get("parts") or []
             if keys is None or p.get("key") in keys]
    dec = job.get("color_decisions") or {}
    choices = dec.get("choices") if isinstance(dec.get("choices"), dict) else {}
    vols = [p.get("volume_mm3") for p in parts]
    by_volume = bool(parts) and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in vols)
    rows = {}
    for part, vol in zip(parts, vols):
        name = re.sub(r"\.(stl|3mf|obj|step|stp)$", "", safe_label(part.get("name"), ""), flags=re.I) or "Part"
        sel = (choices.get(part.get("key")) or choices.get("part:" + str(part.get("key")))
               or job.get("filament_selection") or dec.get("selection") or {})
        color = safe_label(part.get("author_color") or sel.get("color"), "") or None
        material = safe_label(sel.get("material"), "") or None
        # Same name in different colors or materials stays separate rows.
        row = rows.setdefault((name, color, material), {"name": name, "qty": 0, "weight": 0.0,
                                                        "color": color, "material": material})
        row["qty"] += 1
        row["weight"] += float(vol) if by_volume else 1.0
    total = sum(r["weight"] for r in rows.values())
    for row in rows.values():
        row["filament_g"] = round(plate_g * row.pop("weight") / total, 1) if plate_g is not None and total else None
    return list(rows.values())


def plate_estimates(job):
    """One estimate per plate: time and grams from the sliced G-code, else the model page's plate
    estimate, else None (unknown, never zero). Parts carry their own gram share and quantity.

    Copies are cloned into the sliced plate, so G-code figures already cover them; page figures
    are per single copy and are multiplied here.
    """
    copies = _sliced_copies(job)
    derived = job.get("derived_plates") or []
    result = []
    for number, row in enumerate(plates_of(job), 1):
        keys = set(derived[number - 1].get("part_keys") or []) or None if derived else None
        seconds, grams, basis = row.get("seconds"), row.get("filament_g"), "sliced"
        site_s, site_g = _site_plate(job, row.get("plate") or number)
        if seconds is None and site_s is not None:
            seconds, basis = site_s * copies, "model page"
        if grams is None and site_g is not None:
            grams = site_g * copies
        parts = _part_rows(job, keys, grams)
        for part in parts:
            part["qty"] *= copies
        result.append({"plate": row.get("plate") or number, "seconds": seconds, "filament_g": grams,
                       "basis": basis if seconds is not None else "unknown", "parts": parts})
    return result


def format_estimate_breakdown(job):
    """Per-plate time, per-part grams and quantity, then aggregate totals. Warmup is noted separately."""
    plates = plate_estimates(job)
    if not plates:
        return "Estimate unknown"
    many = len(plates) > 1
    copies = _sliced_copies(job)
    lines = []
    for row in plates:
        if row["seconds"] is not None:
            time_text = f"~{human(row['seconds'])}" + (" (model page estimate)" if row["basis"] == "model page" else "")
        else:
            time_text = "time unknown"
        parts = []
        for part in row["parts"]:
            look = " ".join(v for v in (part["color"], part["material"]) if v)
            grams = f"~{part['filament_g']:g} g" if part["filament_g"] is not None else "grams unknown"
            qty = f" x{part['qty']}" if part["qty"] > 1 else ""
            parts.append(f"{part['name']}{qty} ({', '.join(v for v in (look, grams) if v)})")
        shown = "; ".join(parts[:4]) + (f"; +{len(parts) - 4} more" if len(parts) > 4 else "")
        if not parts and row["filament_g"] is not None:
            shown = f"~{row['filament_g']:g} g"
        lines.append(f"{'Plate ' + str(row['plate']) if many else 'Plate'}: {time_text}" + (f" · {shown}" if shown else ""))
    if len(lines) > 10:  # many plates: keep Discord short; the totals below still cover every plate
        lines = lines[:10] + [f"+{len(lines) - 10} more plates"]
    known = [r["seconds"] for r in plates if r["seconds"] is not None]
    grams = [r["filament_g"] for r in plates if r["filament_g"] is not None]
    unknown = len(plates) - len(known)
    total = sum(known)
    warm = f"+~{WARMUP_S // 60} min warmup" + (" per plate" if many else "")
    if not known:
        head = f"Total: time unknown ({warm})"
    elif unknown:
        head = f"Total: at least ~{human(total)} printing, {unknown} plate{'s' if unknown > 1 else ''} unknown ({warm})"
    else:
        head = f"Total: ~{human(total)} printing ({warm})"
    head += (f", {'at least ' if len(grams) < len(plates) else '~'}{sum(grams):g} g filament"
             if grams else ", filament unknown")
    if copies > 1:
        head += f" for {copies} copies"
    lines.append(head)
    return scrub("\n".join(lines))


def _conversion_record(holder):
    """The conversion provenance a job or derived plate carries, or None.

    The slicer adapter stores None when building the record failed, and older
    or hand-edited JOB.json files may hold anything: a missing, None or
    non-dict record at either level is no record, never an error.
    """
    provenance = holder.get('provenance') if isinstance(holder, dict) else None
    record = provenance.get('conversion_provenance') if isinstance(provenance, dict) else None
    return record if isinstance(record, dict) else None


def _conversion_line(records):
    """One concise 'Conversion:' line for the consequential facts in `records`, or None."""
    records = [record for record in records if record]
    # Unknown intent is only worth a line for a foreign project; a plain STL
    # or native job (source_format 'unknown', or intent 'not_applicable') has
    # no designer settings whose intent could be unknown. Records without a
    # format predate the field and were all written for projects.
    unknown = any(record.get('intent_status') == 'unknown'
                  and record.get('source_format') != 'unknown' for record in records)
    conflicts = sum(len(record['unresolved_conflicts']) for record in records
                    if isinstance(record.get('unresolved_conflicts'), list))
    if not (unknown or conflicts):
        return None
    details = []
    if unknown:
        details.append('designer intent unknown for some settings')
    if conflicts:
        details.append(f'{conflicts} designer setting conflict(s) resolved by destination profile')
    artifacts = sorted({record['artifact'] for record in records
                        if isinstance(record.get('artifact'), str) and record['artifact']})
    suffix = f' (full record: {artifacts[0]})' if artifacts else ''
    return 'Conversion: ' + '; '.join(details) + suffix


def format_slice_summary(job, now=None):
    """Post slice summary straight from JOB.json: model, parts per plate, filament, estimates."""
    rows = plates_of(job)
    lines = [f"Sliced: {_title(job)}"]
    line = _conversion_line([_conversion_record(job)])
    if line:
        lines.append(line)
    derived = job.get('derived_plates')
    if isinstance(derived, list) and derived:
        line = _conversion_line([_conversion_record(row) for row in derived])
        if line:
            lines.append(line)
    total = 0
    total_filament = 0.0
    filament_known = True
    known = True
    noun = _position_noun(job).lower()
    for row in rows:
        label = f"Plate {row['plate']}" if row.get("plate") is not None else "Plate"
        parts = ", ".join(row["parts"]) or "all parts"
        slot = f"{noun} {row['slot']}" if row.get("slot") is not None else f"{noun} unknown"
        fil = (filament_label(safe_label(row['color'], 'color unknown'),
                              safe_label(row['material'], 'material unknown')) + f", {slot}")
        if row.get("filaments"):
            fil = "; ".join(filament_label(safe_label(entry.get("color"), "color unknown"),
                                           safe_label(entry.get("material"), "material unknown"))
                            + f", {noun} {entry.get('slot')}" for entry in row["filaments"])
        est = human(row["seconds"]) if row.get("seconds") is not None else "estimate unknown"
        grams = row.get("filament_g")
        amount = f" | {grams:g} g" if grams is not None else ""
        lines.append(f"{label}: {parts} | {fil}{amount} | {est}")
        if grams is None:
            filament_known = False
        else:
            total_filament += grams
        if row.get("seconds") is None:
            known = False
        else:
            total += row["seconds"]
    if rows and filament_known:
        lines.append(f"Total filament: {total_filament:g} g")
    if rows and known:
        lines.append(f"Estimated total: ~{human(total + WARMUP_S)} (printing: {human(total)})")
        lines.append(f"Approx. finish if started now: {finish_text(total, now)}")
    auto_scale = job.get('auto_scale')
    if isinstance(auto_scale, dict):
        original = auto_scale.get('original_mm') or ()
        final = auto_scale.get('final_mm') or ()
        if len(original) == 3 and len(final) == 3:
            original_text = ' x '.join(f'{float(value):g}' for value in original)
            final_text = ' x '.join(f'{float(value):g}' for value in final)
            lines.append(f'Auto-scale: {original_text} mm treated as inches, scaled 25.4x -> {final_text} mm')
    else:
        provenance = job.get('unit_provenance')
        if isinstance(provenance, dict) and provenance.get('source') == 'user_confirmation' \
                and provenance.get('units') == 'mm':
            original = provenance.get('original_mm') or ()
            if len(original) == 3:
                original_text = ' x '.join(f'{float(value):g}' for value in original)
                lines.append(f'Units: millimeters confirmed, printed at authored size {original_text} mm')
    # Thermal assumptions (issue #720): registry facts for the job's model,
    # unknown shown as unknown. Only when the job names a registered model;
    # jobs without one keep the summary they had. This reports facts only —
    # it sends no fan, temperature or chamber command.
    try:
        from ..knowledge.thermal import thermal_lines_for_model
        lines.extend(thermal_lines_for_model(job.get("printer_model"), printer_id=job.get("printer_id")))
    except Exception:
        pass
    return scrub("\n".join(lines))
