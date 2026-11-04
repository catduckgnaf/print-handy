"""Spoolman -> printer one-way spool sync and the /spool command logic.

Spoolman is the source of truth. Which spool sits in which slot lives in the
Spoolman extra field ``ph_slot`` (``<printer_id>:<slot>``, #798); ``location``
stays human-readable (`IFS Slot N` / `Shelf`) and is written but never read
as truth, and ``printer_hardware.json``'s ``filament_slots`` is a cache that
:func:`reconcile` rebuilds from Spoolman. The AD5X screen is *told* what
Spoolman says, through the Z-Mod `CHANGE_ZCOLOR SLOT= TYPE= HEX=` macro and
through the Moonraker ``lane_data`` record written on a bind (#791). The
inbound direction — screen-initiated changes read back from ``lane_data``
and converged through :func:`reconcile` — lives in ``lane_data_sync``.

Usage accounting: Moonraker charges extrusion to its active Spoolman spool.
`suspend_usage()` clears that so an unidentified mid-print swap charges no one
until a human says which spool is loaded; `resume_usage()` re-points it.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .. import printers
from ..core import filament_color, spool_canon, spoolman_client
from . import _config

HARDWARE_STATE = _config.state_file("PRINTER_HARDWARE_STATE", "printer_hardware.json")
PENDING_SCREEN = _config.state_file("SCREEN_SYNC_PENDING", "screen_sync_pending.json")
MOONRAKER = _config.moonraker_url()
SPOOLMAN = _config.service_url("spoolman_url")
SHELF = "Shelf"
SLOTS = (1, 2, 3, 4)
# Stamped on every spool create_spool makes; the Spoolman new-spool watcher
# reads it to tell Print Handy's own spools from ones added elsewhere (#483).
CREATED_SPOOL_COMMENT = "added from Discord /spool new"

Client = Callable[[str, str, Any], Any]


class SpoolError(RuntimeError):
    """A refused /spool action. Nothing was changed when this is raised before a write."""


# ---------------------------------------------------------------- transports

# _http is the Moonraker transport only. Spoolman never goes through it: spoolman()
# below routes every Spoolman call through core.spoolman_client (#387, #798), and
# tests/pipeline/test_spoolman_wrapper_contract.py fails if _http is given a Spoolman base.
def _http(base: str, method: str, path: str, body: Any = None, timeout: float = 8) -> Any:
    if not base:
        raise SpoolError("the service for this request is not configured "
                         "(services.moonraker_url / services.spoolman_url in config/local.yaml or config/local.toml)")
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if method == "POST" and data is None:
        req.data = b"{}"
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode() or "{}"
    return json.loads(raw)


_SPOOL_GET = re.compile(r"/api/v1/spool/(\d+)")


def _unavailable_error(method: str, path: str, exc: Exception) -> SpoolError:
    """The /spool wording for a Spoolman that did not answer (#839).

    A 404 on a single-spool GET is not an outage: the id is wrong or the
    spool was deleted, so say that. Everything else names Spoolman, the
    configured URL, and the remedy — the raw transport string
    ("HTTP Error 502: Bad Gateway") named none of those.

    "Nothing changed" is promised only for a read: a GET writes nothing.
    A failed write is worded as unconfirmed instead — the request may
    have reached Spoolman before the failure, and a multi-write action
    (a bind patches the new spool before the replaced one, a swap
    patches both spools) may have partly applied, so the operator is
    told to check before retrying rather than told no reconciliation
    is needed.
    """
    match = _SPOOL_GET.fullmatch(path or "")
    if getattr(exc, "status", None) == 404 and str(method).upper() == "GET" and match:
        return SpoolError(f"Spoolman has no spool #{match.group(1)}. "
                          "Check the id with /spool list. Nothing changed")
    remedy = "Check that the Spoolman service is running and that services.spoolman_url points at it."
    # Never post credentials embedded in the URL (user:pass@host) to chat.
    where = f" at {re.sub(r'//[^/@]*@', '//', str(SPOOLMAN))}" if SPOOLMAN else ""
    if str(method).upper() == "GET":
        return SpoolError(f"Spoolman did not answer{where} ({exc}). Nothing changed. {remedy}")
    return SpoolError(f"Spoolman did not answer{where} ({exc}). This change may have partly "
                      "applied in Spoolman — check /spool list or run /spool sync before "
                      f"trying again. {remedy}")


def spoolman(method: str, path: str, body: Any = None) -> Any:
    """The /spool command layer's Spoolman transport: the one wrapper (#387).

    An unavailable Spoolman surfaces as SpoolError, this module's contract
    for a refused /spool action — the operator asked for a Spoolman
    operation, so the failure is reported, never swallowed.
    """
    try:
        return spoolman_client.request(method, path, body, base_url=SPOOLMAN, timeout=8)
    except spoolman_client.SpoolmanUnavailable as exc:
        raise _unavailable_error(method, path, exc) from exc


def moonraker(method: str, path: str, body: Any = None, timeout: float | None = None) -> Any:
    # The default stays read-sized (15s); a caller whose request Moonraker
    # answers only after long work (the firmware upgrade POST) passes an
    # upgrade-sized timeout instead.
    return _http(MOONRAKER, method, path, body, timeout=15 if timeout is None else timeout)


def moonraker_for(printer_id: str | None) -> Client:
    """A Moonraker client bound to one roster printer's Moonraker (#335); the configured one when no printer is named."""
    if not printer_id:
        return moonraker
    from print_handy import settings
    base = str(settings.view_for(printer_id)["services"]["moonraker_url"])

    def call(method: str, path: str, body: Any = None, timeout: float | None = None) -> Any:
        return _http(base, method, path, body, timeout=15 if timeout is None else timeout)

    return call


# ---------------------------------------------------------------- state file

def _hardware_path(path: Path | None = None, printer_id: str | None = None) -> Path:
    """The hardware record to write: an explicit path, else one printer's (#334).

    Per-printer records live at <state>/printers/<id>/printer_hardware.json;
    the env override behind HARDWARE_STATE wins over the layout, as it does
    for the singleton path today. No printer named: HARDWARE_STATE as before.
    """
    if path is not None:
        return Path(path)
    if printer_id is None or os.environ.get("PRINTER_HARDWARE_STATE"):
        return Path(HARDWARE_STATE)
    return Path(HARDWARE_STATE).parent / "printers" / printer_id / "printer_hardware.json"


def _hardware_read_path(path: Path | None = None, printer_id: str | None = None) -> Path:
    """Where a load reads: the per-printer record, or the legacy record it adopts.

    Adoption is read-only and the default printer's alone; a second printer
    must never inherit the singleton's loaded spool (design §2.4).
    """
    target = _hardware_path(path, printer_id)
    if printer_id is None or path is not None or target.exists():
        return target
    try:
        default_id = _config.cfg()["printers"][0]["id"]
    except Exception:
        default_id = None
    if printer_id == default_id and Path(HARDWARE_STATE).is_file():
        return Path(HARDWARE_STATE)
    return target


def load_state(path: Path | None = None, *, printer_id: str | None = None) -> dict[str, Any]:
    state = json.loads(_hardware_read_path(path, printer_id).read_text())
    # Same load fix-ups as printer_state.load: adopt the pre-#672 slot-map key,
    # then backfill slot color codes (a slot with no color code gets one from
    # its name; a valid hex is never touched).
    filament_color.adopt_renamed_slots_key(state)
    filament_color.fill_missing_slot_hexes(state)
    return state


def save_state(state: dict[str, Any], path: Path | None = None, *, printer_id: str | None = None) -> None:
    target = _hardware_path(path, printer_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, target)


def _loaded_spool_ids(state: dict[str, Any]) -> set[int]:
    """Every spool id a hardware record says is loaded: the active spool and the IFS slots."""
    ids = set()
    active = (state.get("spool") or {}).get("spoolman_id")
    if active is not None:
        ids.add(int(active))
    for rec in (state.get("filament_slots") or {}).values():
        sid = (rec or {}).get("spoolman_id")
        if sid is not None:
            ids.add(int(sid))
    return ids


def spool_loaded_elsewhere(spool_id: int, *, state_path: Path | None = None,
                           printer_id: str | None = None) -> str | None:
    """The name of another printer whose record already loads this spool, or None (#334).

    One physical spool sits on one printer. The scan covers the legacy
    root-level record and every per-printer record under the same state
    root, excluding the target printer's own record. Best-effort: a record
    that cannot be read proves nothing and is skipped.
    """
    target = _hardware_path(state_path, printer_id)
    root = target.parent
    if target.parent.name and target.parent.parent.name == "printers":
        root = target.parent.parent.parent  # <state>/printers/<id>/file -> <state>
    candidates = [root / "printer_hardware.json"]
    printers_dir = root / "printers"
    if printers_dir.is_dir():
        candidates += sorted(printers_dir.glob("*/printer_hardware.json"))
    try:
        default_id = _config.cfg()["printers"][0]["id"]
    except Exception:
        default_id = None
    if target == root / "printer_hardware.json":
        # The legacy root record IS the default printer's record (pre-adoption
        # spelling of it): its per-printer copy is the same printer, not another.
        try:
            candidates.remove(root / "printers" / default_id / "printer_hardware.json")
        except Exception:
            pass
    elif default_id is not None and target == root / "printers" / default_id / "printer_hardware.json":
        # The reverse direction (P2-127): binding the default printer's
        # per-printer record, the root record is that same printer's legacy
        # spelling (retained as the rollback copy after adoption), not
        # another printer — a spool it loads is this printer's own.
        try:
            candidates.remove(root / "printer_hardware.json")
        except Exception:
            pass
    for candidate in candidates:
        if candidate == target:
            continue
        try:
            other = json.loads(candidate.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(other, dict):
            # Another printer's record may predate the #672 rename: adopt its
            # legacy slot-map key or only its active spool is seen (P2-267).
            filament_color.adopt_renamed_slots_key(other)
            if int(spool_id) in _loaded_spool_ids(other):
                return candidate.parent.name if candidate.parent != root else "the default printer"
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- mapping

def screen_hex(value: Any) -> str | None:
    """``RRGGBB`` for one hex color in any form filament_color.parse_hex reads, else None."""
    return filament_color.normalize_hex(value)


def screen_command(slot: int, material: str, hex_code: str, adapter=None) -> str:
    """CHANGE_ZCOLOR line for the printer screen; the printer's adapter builds it."""
    return (adapter or printers.current()).screen_command(slot, material, hex_code)


def screen_fallback_note(material: str, adapter=None) -> str | None:
    """'screen shows ABS for ASA' when the screen label is a stand-in for the material, else None (#798).

    Purely informational, so any failure to work it out reads as no note.
    """
    try:
        note = (adapter or printers.current()).screen_fallback_note(material)
    except Exception:
        return None
    return note if isinstance(note, str) and note else None


def _printer_adapter(printer_id: str | None):
    """One roster entry's adapter bound to its own settings view (#335); None when no printer is named."""
    if not printer_id:
        return None
    from print_handy import settings
    return printers.get(settings.by_id(printer_id)["model"], settings.view_for(printer_id))


def _pending_path(pending_path: Path | None, printer_id: str | None) -> Path:
    """The deferred-screen queue for one printer (#335): per-printer, so a queued
    update can never be flushed to another printer's screen."""
    if pending_path is not None:
        return Path(pending_path)
    if not printer_id:
        return Path(PENDING_SCREEN)
    return Path(PENDING_SCREEN).parent / "printers" / printer_id / Path(PENDING_SCREEN).name


# ---------------------------------------------------------------- printer side

def printer_state(mr: Client = moonraker) -> str:
    try:
        res = mr("GET", "/printer/objects/query?print_stats=state", None)
        return str(res["result"]["status"]["print_stats"]["state"])
    except Exception:
        return "unknown"


def push_screen(slot: int, material: str, hex_code: str, *, mr: Client = moonraker,
                pending_path: Path | None = None, adapter=None) -> str:
    """Tell the screen what Spoolman says. Deferred while printing; flushed when idle.

    Returns 'pushed', 'deferred', 'unsupported' (the printer has no screen), or raises SpoolError for unmappable data.
    """
    printer = adapter or printers.current()
    if not printer.multi_slot:
        return "unsupported"  # stock Klipper has no screen to tell
    command = screen_command(slot, material, hex_code, adapter=printer)
    if command is None:
        # A multi_slot adapter with no screen sync (Bambu AMS, #671): the class
        # alone no longer implies a screen, so say "unsupported" instead of
        # queueing or sending an empty command.
        return "unsupported"
    pending = Path(pending_path or PENDING_SCREEN)
    if printer_state(mr) in ("printing", "unknown"):
        queued = _read_json(pending)
        queued[str(slot)] = {"command": command, "queued_at": time.time()}
        pending.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(json.dumps(queued, indent=2) + "\n")
        return "deferred"
    res = mr("POST", "/printer/gcode/script?script=" + urllib.parse.quote(command), None)
    if res.get("result") != "ok":
        raise SpoolError(f"printer refused {command}: {res}")
    queued = _read_json(pending)
    if queued.pop(str(slot), None) is not None:
        pending.write_text(json.dumps(queued, indent=2) + "\n")
    return "pushed"


def flush_pending_screen(*, mr: Client = moonraker, pending_path: Path | None = None) -> list[int]:
    """Send screen updates that were deferred during a print. Safe to call often."""
    pending = Path(pending_path or PENDING_SCREEN)
    queued = _read_json(pending)
    if not queued or printer_state(mr) in ("printing", "unknown"):
        return []
    done = []
    for slot, item in sorted(queued.items()):
        res = mr("POST", "/printer/gcode/script?script=" + urllib.parse.quote(item["command"]), None)
        if res.get("result") == "ok":
            done.append(int(slot))
    remaining = {k: v for k, v in queued.items() if int(k) not in done}
    pending.write_text(json.dumps(remaining, indent=2) + "\n")
    return done


# ---------------------------------------------------------------- lane_data (#791)

LANE_DATA_NAMESPACE = "lane_data"
LANE_DATA_PATH = "/server/database/item?namespace=lane_data"


def read_lane_data(mr: Client = moonraker) -> dict[str, Any] | None:
    """The raw Moonraker ``lane_data`` namespace (``laneN`` -> record), or None when unknown.

    The namespace is shared and cooperative: HelixScreen and other writers
    keep one record per IFS lane there. Any failure — Moonraker unreachable,
    the namespace absent, a payload in an unexpected shape — reads as
    unknown (None), never as an empty namespace, so a caller must not
    converge slots toward "unloaded" from a None here.
    """
    try:
        res = mr("GET", LANE_DATA_PATH, None)
    except Exception:
        return None
    if not isinstance(res, dict):
        return None
    result = res.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    return value if isinstance(value, dict) else None


def build_lane_record(slot: int, spool: dict[str, Any], existing: dict[str, Any] | None = None,
                      *, name: str | None = None, now: float | None = None) -> dict[str, Any]:
    """The ``lane_data`` record Print Handy writes for one bound slot (#791).

    The outer key is ``lane<slot>`` (1-based); the inner ``lane`` field is
    0-based. ``spool_id`` is the Spoolman id. The ``helix_locked_*`` fields
    are written false — Print Handy declares nothing on the user's behalf —
    while every other ``helix_*`` key the screen already keeps on the record
    is preserved untouched.
    """
    slot = int(slot)
    filament = spool.get("filament") or {}
    vendor = filament.get("vendor") or {}
    vendor_id = filament.get("vendor_id") or (vendor.get("id") if isinstance(vendor, dict) else None)
    color = (screen_hex(filament.get("color_hex"))
             or screen_hex(first_hex_of(filament))
             or str(filament.get("name") or "").strip())
    material = str(filament.get("material") or "").strip()
    display = name or " ".join(part for part in
                               (str(filament.get("name") or "").strip(), material) if part)
    record: dict[str, Any] = {}
    if isinstance(existing, dict):
        for key, value in existing.items():
            if isinstance(key, str) and key.startswith("helix_") \
                    and key not in ("helix_locked_color", "helix_locked_material"):
                record[key] = value
    record.update({
        "spool_id": int(spool["id"]),
        "spoolman_vendor_id": vendor_id,
        "lane": slot - 1,
        "material": material,
        "color": color,
        "name": display or f"Spool #{spool['id']}",
        "scan_time": int(time.time() if now is None else now),
        "helix_locked_color": False,
        "helix_locked_material": False,
    })
    return record


def write_lane_record(slot: int, spool: dict[str, Any], *, mr: Client = moonraker,
                      name: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Write one slot's ``lane_data`` record through Moonraker's database POST (#791).

    Best effort by the caller: this raises whatever Moonraker raises, and
    ``bind_slot`` turns that into a named "not updated" note rather than
    failing the bind. The ``CHANGE_ZCOLOR`` that clears a stale screen
    override is not sent here — ``push_screen`` already sends it on the
    bind path, beside this write.
    """
    slot = int(slot)
    existing = None
    raw = read_lane_data(mr)
    if isinstance(raw, dict) and isinstance(raw.get(f"lane{slot}"), dict):
        existing = raw[f"lane{slot}"]
    record = build_lane_record(slot, spool, existing, name=name, now=now)
    mr("POST", "/server/database/item",
       {"namespace": LANE_DATA_NAMESPACE, "key": f"lane{slot}", "value": record})
    return record


def active_usage_spool(mr: Client = moonraker) -> int | None:
    return (mr("GET", "/server/spoolman/spool_id", None).get("result") or {}).get("spool_id")


def suspend_usage(mr: Client = moonraker) -> int | None:
    """Stop Moonraker charging any spool. Returns the spool that was being charged."""
    prior = active_usage_spool(mr)
    mr("POST", "/server/spoolman/spool_id", {})
    if active_usage_spool(mr) is not None:
        raise SpoolError("Moonraker still reports an active spool after clearing it")
    return prior


def resume_usage(spool_id: int, mr: Client = moonraker) -> None:
    mr("POST", "/server/spoolman/spool_id", {"spool_id": int(spool_id)})
    back = active_usage_spool(mr)
    if back != int(spool_id):
        raise SpoolError(f"Moonraker active spool readback {back!r} != #{spool_id}")


# ---------------------------------------------------------------- ph_slot (#798)

PH_SLOT_FIELD = "ph_slot"
PH_SLOT_FIELD_NAME = "Print Handy slot"


def _default_printer_id() -> str | None:
    """The default roster printer's id, derived from config like the rest of this module."""
    try:
        return str(_config.cfg()["printers"][0]["id"])
    except Exception:
        return None


def format_ph_slot(printer_id: str, slot: int) -> str:
    """The ``ph_slot`` value naming one slot of one printer: ``<printer_id>:<slot>``."""
    return f"{printer_id}:{int(slot)}"


def _decode_extra_value(raw: Any) -> str | None:
    """One Spoolman extra value as plain text.

    Spoolman stores extra values JSON-encoded (a text value ``ad5x:2`` comes
    back as the string ``'"ad5x:2"'``); decode when quoted, tolerate a raw
    string from a client or a hand edit.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith('"'):
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, str):
            text = decoded.strip()
    return text or None


def parse_ph_slot(value: Any) -> tuple[str, int] | None:
    """(printer_id, slot) for a ``ph_slot`` value in any spelling Spoolman returns, else None."""
    text = _decode_extra_value(value)
    if not text:
        return None
    match = re.fullmatch(r"(.+):(\d+)", text)
    if not match:
        return None
    slot = int(match.group(2))
    if slot not in SLOTS:
        return None
    return match.group(1), slot


def spool_ph_slot(spool: dict[str, Any] | None) -> tuple[str, int] | None:
    """The (printer_id, slot) a spool record claims through ``ph_slot``, else None."""
    if not isinstance(spool, dict):
        return None
    extra = spool.get("extra")
    if not isinstance(extra, dict):
        return None
    return parse_ph_slot(extra.get(PH_SLOT_FIELD))


def spool_ph_slot_text(spool: dict[str, Any] | None) -> str | None:
    """The raw ``ph_slot`` text a spool record carries (even an unparseable one), else None."""
    if not isinstance(spool, dict):
        return None
    extra = spool.get("extra")
    if not isinstance(extra, dict):
        return None
    return _decode_extra_value(extra.get(PH_SLOT_FIELD))


def _call_client(client: Any, method: str, path: str, body: Any = None) -> Any:
    """Call a Spoolman client in whichever shape it offers (callable, or an object with ``request``)."""
    if callable(client):
        return client(method, path, body)
    request = getattr(client, "request", None)
    if callable(request):
        return request(method, path, body)
    raise TypeError(f"Spoolman client {client!r} is not callable")


def ensure_ph_slot_field(client: Client = spoolman) -> bool:
    """Create the ``ph_slot`` extra field definition in Spoolman when it is missing (#798).

    Best effort by design: returns True when the field is defined (or was
    just created), False when Spoolman did not answer or the client does not
    speak the field endpoints — and never raises into a caller, so a fake or
    partial client in a test, or a Spoolman without field support, costs
    only the field, never the bind or reconcile that asked.
    """
    try:
        fields = _call_client(client, "GET", "/api/v1/field/spool", None)
    except Exception:
        return False
    if not isinstance(fields, list):
        return False
    if any(isinstance(row, dict) and row.get("key") == PH_SLOT_FIELD for row in fields):
        return True
    try:
        _call_client(client, "POST", f"/api/v1/field/spool/{PH_SLOT_FIELD}",
                     {"name": PH_SLOT_FIELD_NAME, "field_type": "text"})
    except Exception:
        return False
    return True


def _set_ph_slot(client: Client, spool_id: int, value: str | None) -> bool:
    """Write (or, with None, clear) one spool's ``ph_slot``. Best effort: False on any failure."""
    ensure_ph_slot_field(client)
    try:
        _call_client(client, "PATCH", f"/api/v1/spool/{int(spool_id)}",
                     {"extra": {PH_SLOT_FIELD: json.dumps(value) if value is not None else None}})
    except Exception:
        return False
    return True


def _clear_ph_slot_claims(client: Client, printer_id: str, slot: int, *, keep_spool_id: int | None) -> list[int]:
    """Clear the ``ph_slot`` claim on (printer, slot) from every spool except ``keep_spool_id``.

    Best effort: a Spoolman that does not answer the list call leaves the
    claims alone (reconcile names them later); returns the spool ids cleared.
    """
    cleared: list[int] = []
    try:
        rows = _call_client(client, "GET", "/api/v1/spool?allow_archived=true", None)
    except Exception:
        return cleared
    if not isinstance(rows, list):
        return cleared
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        if keep_spool_id is not None and int(row["id"]) == int(keep_spool_id):
            continue
        if spool_ph_slot(row) == (printer_id, int(slot)):
            if _set_ph_slot(client, int(row["id"]), None):
                cleared.append(int(row["id"]))
    return cleared


# ---------------------------------------------------------------- slot binding

def _slot_record(slot: int, spool: dict[str, Any], old: dict[str, Any] | None, note: str) -> dict[str, Any]:
    filament = spool.get("filament") or {}
    remaining = spool.get("remaining_weight")
    raw_multi = filament.get("multi_color_hexes")
    multi_color = isinstance(raw_multi, str) and "," in raw_multi
    same_spool = (old or {}).get("spoolman_id") == spool.get("id")
    raw_name = str(filament.get("name") or ((old or {}).get("color") if same_spool else None) or "unknown").strip()
    material = str(filament.get("material") or ((old or {}).get("material") if same_spool else None) or "unknown").strip()
    # A trailing material token, or a name that is only that token, is not a color.
    # "Pink PLA" + PLA -> Pink; "PLA" + PLA stays one PLA, not "PLA PLA".
    if material and raw_name.casefold() == material.casefold():
        color = ""
    elif material:
        color = re.sub(rf"\s+{re.escape(material)}$", "", raw_name, flags=re.IGNORECASE).strip()
    else:
        color = raw_name
    display_name = " ".join(x for x in (color, material) if x)
    # An exact material name has no color token. Do not store the material as the color.
    stored_color = color.lower() if color else ((old or {}).get("color") if same_spool else "") or ""
    # A null/invalid Spoolman color is not permission to reuse cached color: that
    # can silently assign another spool's swatch to the physical slot. The
    # color code comes from this spool alone: Spoolman's color_hex, else the
    # first of its multi_color_hexes (a multi-color spool's representative
    # swatch), else its color name, else the neutral default — which
    # hex_source records and chat says out loud. A slot is never left without
    # a code: slicing used to die on exactly that.
    # An invalid color_hex ("0C6283FF" was one, "#abc" still is not six digits
    # to old readers) must not hide a valid multi_color_hexes: resolve() takes
    # them as separate candidates.
    hex_code, hex_source = filament_color.resolve(
        None, spoolman_hex=filament.get("color_hex"), spoolman_multi=raw_multi,
        name=raw_name, material=material, warn_label=f"Spool #{spool.get('id')}")
    return {
        "slot": slot,
        "material": material,
        "color": stored_color,
        "hex": hex_code,
        "hex_source": hex_source,
        "spoolman_id": int(spool["id"]),
        "name": display_name or f"Spool #{spool['id']}",
        "remaining_grams": round(float(remaining), 1) if isinstance(remaining, (int, float)) else None,
        "quantity_status": "spoolman",
        "weight_confidence": "spoolman",
        "physical_color_verified": bool(hex_code),
        "multi_color": multi_color,
        # The blend's full color list rides on the record (#838): the launch
        # color check accepts a screen set to any of these as the same spool,
        # where the representative hex alone would warn or refuse.
        "multi_color_hexes": raw_multi if isinstance(raw_multi, str) and raw_multi.strip() else None,
        "representative_swatch": multi_color,
        "physical_loading_verified": True,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "verification_note": note,
    }


def bind_slot(slot: int, spool_id: int, *, client: Client = spoolman, mr: Client = moonraker,
              state_path: Path | None = None, pending_path: Path | None = None,
              prior_disposition: str = "shelf", note: str = "Bound from Discord /spool",
              push_screen_update: bool = True, printer_id: str | None = None,
              expected_prior_spool_id: int | None = None) -> dict[str, Any]:
    """Make `spool_id` the spool in `slot`, everywhere, with Spoolman winning.

    prior_disposition: 'shelf' puts the replaced spool on the shelf; 'keep' leaves
    its Spoolman record alone (the caller already handled it, e.g. depleted);
    'archive' archives the replaced spool; 'depleted' archives it with 0 g
    left at the 'depleted' location (the runout pattern); 'delete' removes
    the replaced spool's Spoolman record entirely (#512). The guided change
    flow (#475) and the conversational intake (#512) only pass
    archive/depleted/delete after the operator picked that fate on a button.
    `printer_id` binds in that printer's own hardware record (#334) and refuses
    a spool another printer's record already loads.
    `expected_prior_spool_id` is the replaced spool the operator's confirmation
    named: when a flow supplies it, the slot's current mapping is re-checked
    before any write and the bind aborts on a mismatch, so a slot rebound
    while the confirmation was open can never make a disposition — above all
    'delete' — land on a different spool than the one confirmed (#515).
    """
    if prior_disposition not in ("shelf", "keep", "archive", "depleted", "delete"):
        raise SpoolError(f"unknown prior disposition {prior_disposition!r}. Nothing changed")
    slot = int(slot)
    if slot not in SLOTS:
        raise SpoolError(f"slot {slot} is not 1-4")
    spool = client("GET", f"/api/v1/spool/{int(spool_id)}", None)
    if not isinstance(spool, dict) or spool.get("id") is None:
        raise SpoolError(f"Spoolman has no spool #{spool_id}. Check the id with /spool list. Nothing changed")
    if spool.get("archived"):
        raise SpoolError(f"spool #{spool_id} is archived (used up); pick a live one")
    state = load_state(state_path, printer_id=printer_id)
    slots = state.setdefault("filament_slots", {})
    for key, rec in slots.items():
        if str(key) != str(slot) and (rec or {}).get("spoolman_id") == int(spool_id):
            raise SpoolError(f"spool #{spool_id} is already mapped to slot {key}. Move that slot first. Nothing changed")
    elsewhere = spool_loaded_elsewhere(int(spool_id), state_path=state_path, printer_id=printer_id)
    if elsewhere is not None:
        raise SpoolError(f"spool #{spool_id} is already loaded on {elsewhere}. "
                         "One spool cannot be loaded on two printers. Nothing changed")
    old = slots.get(str(slot)) or {}
    prior = old.get("spoolman_id")
    if expected_prior_spool_id is not None and \
            int(prior or 0) != int(expected_prior_spool_id):
        actual = f"spool #{prior}" if prior else "no spool"
        raise SpoolError(f"slot {slot} was expected to hold spool "
                         f"#{int(expected_prior_spool_id)} (the spool in the confirmation), "
                         f"but it now holds {actual}. Nothing changed — the confirmed "
                         "spool was not touched. Start the change again")
    # The ph_slot identity this bind writes (#798): the named printer, else
    # the default roster printer derived from config. No derivable printer
    # means no ph_slot writes — a value is never invented.
    effective_printer = printer_id or _default_printer_id()
    _prior_cache: dict[str, Any] = {}

    def prior_record() -> dict[str, Any]:
        """The replaced spool's Spoolman record, best effort ({} when it cannot be read)."""
        if "rec" not in _prior_cache:
            try:
                got = client("GET", f"/api/v1/spool/{int(prior)}", None)
            except Exception:
                got = None
            _prior_cache["rec"] = got if isinstance(got, dict) else {}
        return _prior_cache["rec"]

    def prior_claims_slot() -> bool:
        """True when the replaced spool still claims this slot through ph_slot."""
        return bool(effective_printer) and \
            spool_ph_slot(prior_record()) == (effective_printer, slot)

    client("PATCH", f"/api/v1/spool/{int(spool_id)}", {"location": f"IFS Slot {slot}"})
    if effective_printer:
        # Spoolman is the source of truth for the assignment: the new spool
        # claims the slot, and any stale claim by another spool for the same
        # printer:slot is cleared. Both are best effort — the location write
        # above is the bind's contract, and reconcile repairs a missed claim.
        _set_ph_slot(client, int(spool_id), format_ph_slot(effective_printer, slot))
        _clear_ph_slot_claims(client, effective_printer, slot, keep_spool_id=int(spool_id))
    if prior and int(prior) != int(spool_id) and prior_disposition == "shelf":
        prior_rec = prior_record()
        if isinstance(prior_rec, dict) and not prior_rec.get("archived") and \
                str(prior_rec.get("location") or "") in ("", f"IFS Slot {slot}"):
            body: dict[str, Any] = {"location": SHELF}
            if prior_claims_slot():
                body["extra"] = {PH_SLOT_FIELD: None}
            client("PATCH", f"/api/v1/spool/{int(prior)}", body)
        elif prior_claims_slot():
            _set_ph_slot(client, int(prior), None)
    if prior and int(prior) != int(spool_id) and prior_disposition == "archive":
        # The operator picked this fate in the guided change flow (#475):
        # the old spool leaves the live inventory, its record kept. An
        # archived spool must not keep claiming the slot (#798), so a claim
        # it holds is cleared in the same write.
        body = {"archived": True}
        if prior_claims_slot():
            body["extra"] = {PH_SLOT_FIELD: None}
        client("PATCH", f"/api/v1/spool/{int(prior)}", body)
    if prior and int(prior) != int(spool_id) and prior_disposition == "depleted":
        # Same shape as the runout pattern: archived, 0 g left, depleted.
        body = {"archived": True, "remaining_weight": 0, "location": "depleted"}
        if prior_claims_slot():
            body["extra"] = {PH_SLOT_FIELD: None}
        client("PATCH", f"/api/v1/spool/{int(prior)}", body)
    if prior and int(prior) != int(spool_id) and prior_disposition == "delete":
        # The operator explicitly picked Delete for the replaced spool
        # (#512): its Spoolman record is removed, not archived. This is the
        # one destructive disposition, so no flow passes it by default.
        # The record going away takes any ph_slot claim with it (#798).
        client("DELETE", f"/api/v1/spool/{int(prior)}", None)
    if prior and int(prior) != int(spool_id) and prior_disposition == "keep":
        # The caller already handled the replaced spool's fate; its ph_slot
        # claim on this slot is still Print Handy's to clear (#798).
        if prior_claims_slot():
            _set_ph_slot(client, int(prior), None)
    record = _slot_record(slot, spool, old, note)
    slots[str(slot)] = record
    if (state.get("spool") or {}).get("spoolman_id") == prior and prior is not None:
        # The live hardware check compares this name with Spoolman: carry the new spool's facts,
        # not just its id, or a swapped spool reads as "Spoolman #7 is 'Coffee PLA+', state says 'Pink PLA'".
        filament = spool.get("filament") or {}
        old_spool = state["spool"]
        state["spool"] = {**old_spool, "spoolman_id": int(spool_id),
                          "name": filament.get("name") or old_spool.get("name"),
                          "material": filament.get("material") or old_spool.get("material"),
                          "density": filament.get("density") or old_spool.get("density"),
                          "diameter_mm": filament.get("diameter") or old_spool.get("diameter_mm")}
        # temps_c belonged to the previous spool: mark it so slicing resolves this one's (#692).
        state["temperature_resolution"] = {"source": "spool changed by slot sync; resolved per slice"}
    save_state(state, state_path, printer_id=printer_id)
    usage = "unchanged"
    try:
        if prior is not None and active_usage_spool(mr) == prior and int(prior) != int(spool_id):
            resume_usage(int(spool_id), mr)
            usage = f"now charged to #{spool_id}"
    except Exception as exc:
        usage = "not updated — the spool change itself is saved; run `/spool sync` to fix the charging"
    lane_data = "not requested"
    if push_screen_update:
        # The lane_data side of the screen converge (#791): write this slot's
        # record beside the CHANGE_ZCOLOR push_screen sends below (the gcode
        # itself is not duplicated here). Best effort, like the screen push:
        # a Moonraker database failure names itself and never fails the bind.
        try:
            write_lane_record(slot, spool, mr=mr, name=record["name"])
            lane_data = "written"
        except Exception:
            lane_data = "not updated — run `/spool sync` to try again"
        try:
            if printer_id:
                screen = push_screen(slot, record["material"], record["hex"], mr=mr,
                                     pending_path=_pending_path(pending_path, printer_id),
                                     adapter=_printer_adapter(printer_id))
            else:
                screen = push_screen(slot, record["material"], record["hex"], mr=mr, pending_path=pending_path)
        except Exception as exc:
            screen = "not updated — run `/spool sync` to try again"
    else:
        screen = "not requested"
    screen_note = (screen_fallback_note(record["material"], _printer_adapter(printer_id))
                   if screen in ("pushed", "deferred") else None)
    return {"slot": slot, "spool_id": int(spool_id), "prior_spool_id": prior, "record": record,
            "screen": screen, "screen_note": screen_note, "usage": usage,
            "lane_data": lane_data,
            "prior_disposition": prior_disposition}


def unbind_slot(slot: int, *, client: Client = spoolman, mr: Client = moonraker,
                state_path: Path | None = None, pending_path: Path | None = None,
                printer_id: str | None = None,
                note: str = "Unbound from the Web UI spools page") -> dict[str, Any]:
    """Remove the spool in `slot`, everywhere, with Spoolman winning (#789).

    The unassign primitive the chat flows never needed (Discord changes a
    slot's spool; it never empties one): the spool's ``ph_slot`` claim is
    cleared and its location set to the shelf — the same two writes
    bind_slot makes for a replaced spool with the 'shelf' disposition —
    and the slot's record is removed from the local cache. That is the
    reconcile-consistent shape: Spoolman (``ph_slot``) is the source of
    truth, so the next reconcile rebuild keeps the slot empty, and the
    cache removal happens first so a not-yet-migrated printer's first
    reconcile cannot resurrect the assignment from the stale cache.

    The spool the slot holds is read from the cache AND from Spoolman's
    claims for this printer and slot; when they name different spools the
    unassign refuses and names the conflict (run the sync first) instead
    of picking one. When the removed spool is also the record's active
    spool, that reference is cleared too (it is part of what "loaded"
    means), and Moonraker's charging pointer is suspended if it was
    charging the removed spool.
    """
    slot = int(slot)
    if slot not in SLOTS:
        raise SpoolError(f"slot {slot} is not 1-4")
    try:
        state = load_state(state_path, printer_id=printer_id)
    except FileNotFoundError:
        state = {"filament_slots": {}}
    slots = state.setdefault("filament_slots", {})
    old = slots.get(str(slot)) or {}
    cached_id = old.get("spoolman_id")
    effective_printer = printer_id or _default_printer_id()
    claimed_ids: set[int] = set()
    spool_row: dict[str, Any] | None = None
    if effective_printer:
        try:
            rows = _call_client(client, "GET", "/api/v1/spool?allow_archived=true", None)
        except Exception:
            rows = None
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("id") is not None \
                        and spool_ph_slot(row) == (effective_printer, slot):
                    claimed_ids.add(int(row["id"]))
                    if cached_id is not None and int(row["id"]) == int(cached_id):
                        spool_row = row
    named = {int(cached_id)} if cached_id is not None else set()
    named |= claimed_ids
    if not named:
        raise SpoolError(f"slot {slot} has no spool assigned. Nothing changed")
    if len(named) > 1:
        ids = " and ".join(f"#{sid}" for sid in sorted(named))
        raise SpoolError(f"slot {slot} is claimed by spools {ids} — the local record and "
                         "Spoolman disagree, so nothing was changed. Run the spool sync "
                         "to name the conflict, fix it, and try again")
    spool_id = named.pop()
    if spool_row is None:
        try:
            got = client("GET", f"/api/v1/spool/{spool_id}", None)
        except Exception:
            got = None
        spool_row = got if isinstance(got, dict) else {}
    if spool_row.get("id") is not None:
        body: dict[str, Any] = {"location": SHELF}
        if effective_printer and spool_ph_slot(spool_row) == (effective_printer, slot):
            body["extra"] = {PH_SLOT_FIELD: None}
        client("PATCH", f"/api/v1/spool/{spool_id}", body)
    slots.pop(str(slot), None)
    # The active-spool record is part of "loaded": leaving it naming the
    # removed spool would keep that spool classified as loaded forever
    # (_loaded_spool_ids includes it), blocking its use on another printer
    # and making the usage pointer look correct when it points at a
    # shelved spool. Clear the reference, and stop Moonraker charging the
    # removed spool — the same suspension an unidentified swap uses.
    usage = "unchanged"
    if (state.get("spool") or {}).get("spoolman_id") == spool_id:
        state["spool"] = {}
        try:
            if active_usage_spool(mr) == spool_id:
                suspend_usage(mr)
                usage = f"charging stopped: spool #{spool_id} is no longer loaded"
        except Exception:
            usage = ("not updated — the unassign itself is saved; run `/spool sync` "
                     "to fix the charging")
    save_state(state, state_path, printer_id=printer_id)
    return {"slot": slot, "spool_id": spool_id, "record": old or None,
            "usage": usage, "note": note}


def unbind_text(result: dict[str, Any]) -> str:
    return (f"Slot {result['slot']} no longer holds spool #{result['spool_id']}; "
            f"spool #{result['spool_id']} is on the shelf.")


def swap_slots(slot_a: int, slot_b: int, *, client: Client = spoolman, mr: Client = moonraker,
               state_path: Path | None = None, pending_path: Path | None = None,
               printer_id: str | None = None,
               note: str = "Swapped from the Web UI spools page") -> dict[str, Any]:
    """Swap the spools in two slots, everywhere, with Spoolman winning (#789).

    Two bind_slot calls cannot do this (each refuses a spool that is
    already mapped to the other slot), so the swap writes both sides
    itself in bind_slot's shape: each spool's location and ``ph_slot``
    claim move to its new slot, stale claims for the two slots are
    cleared, both cache records are rebuilt from the Spoolman records
    through _slot_record, and both screens are pushed through
    push_screen (which defers while printing, as a bind does). One side
    may be empty: the swap is then a move. Both empty refuses. A spool
    Spoolman no longer has, or an archived one, refuses by name before
    any write, as a bind does.
    """
    slot_a, slot_b = int(slot_a), int(slot_b)
    for slot in (slot_a, slot_b):
        if slot not in SLOTS:
            raise SpoolError(f"slot {slot} is not 1-4")
    if slot_a == slot_b:
        raise SpoolError(f"slot {slot_a} cannot be swapped with itself. Nothing changed")
    try:
        state = load_state(state_path, printer_id=printer_id)
    except FileNotFoundError:
        state = {"filament_slots": {}}
    slots = state.setdefault("filament_slots", {})
    old_a = slots.get(str(slot_a)) or {}
    old_b = slots.get(str(slot_b)) or {}
    id_a, id_b = old_a.get("spoolman_id"), old_b.get("spoolman_id")
    effective_printer = printer_id or _default_printer_id()
    if effective_printer:
        # Validate against Spoolman's current claims before any write, as
        # unbind_slot does: the cache may be stale (the picker shows
        # Spoolman's claims), and swapping only the cached ids could move
        # a spool nobody picked and clear another spool's legitimate
        # claim. A disagreement refuses by name; a claim the cache has
        # not caught up with is the spool that moves.
        claims: dict[int, set[int]] = {slot_a: set(), slot_b: set()}
        try:
            rows = _call_client(client, "GET", "/api/v1/spool?allow_archived=true", None)
        except Exception:
            rows = None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or row.get("id") is None:
                    continue
                parsed = spool_ph_slot(row)
                if parsed is not None and parsed[0] == effective_printer \
                        and parsed[1] in claims:
                    claims[parsed[1]].add(int(row["id"]))
            resolved: dict[int, int | None] = {}
            for slot, old in ((slot_a, old_a), (slot_b, old_b)):
                named = set(claims[slot])
                if old.get("spoolman_id") is not None:
                    named.add(int(old["spoolman_id"]))
                if len(named) > 1:
                    ids = " and ".join(f"#{sid}" for sid in sorted(named))
                    raise SpoolError(f"slot {slot} is claimed by spools {ids} — the local "
                                     "record and Spoolman disagree, so nothing was changed. "
                                     "Run the spool sync to name the conflict, fix it, "
                                     "and try again")
                resolved[slot] = named.pop() if named else None
            id_a, id_b = resolved[slot_a], resolved[slot_b]
            if id_a is not None and id_a == id_b:
                raise SpoolError(f"spool #{id_a} is claimed by both slot {slot_a} and "
                                 f"slot {slot_b} in Spoolman, so nothing was changed. "
                                 "Run the spool sync to name the conflict, fix it, "
                                 "and try again")
    if id_a is None and id_b is None:
        raise SpoolError(f"slots {slot_a} and {slot_b} are both empty. Nothing changed")
    spools: dict[int, dict[str, Any]] = {}
    for spool_id in (id_a, id_b):
        if spool_id is None:
            continue
        spool = client("GET", f"/api/v1/spool/{int(spool_id)}", None)
        if not isinstance(spool, dict) or spool.get("id") is None:
            raise SpoolError(f"Spoolman has no spool #{spool_id}. Check the id with /spool list. Nothing changed")
        if spool.get("archived"):
            raise SpoolError(f"spool #{spool_id} is archived (used up); pick a live one")
        elsewhere = spool_loaded_elsewhere(int(spool_id), state_path=state_path,
                                           printer_id=printer_id)
        if elsewhere is not None:
            raise SpoolError(f"spool #{spool_id} is already loaded on {elsewhere}. "
                             "One spool cannot be loaded on two printers. Nothing changed")
        spools[int(spool_id)] = spool
    # (spool now in slot_a, spool now in slot_b) is (id_b, id_a).
    moves = ((slot_a, id_b, old_a), (slot_b, id_a, old_b))
    for new_slot, spool_id, _old in moves:
        if spool_id is None:
            continue
        client("PATCH", f"/api/v1/spool/{int(spool_id)}", {"location": f"IFS Slot {new_slot}"})
        if effective_printer:
            _set_ph_slot(client, int(spool_id), format_ph_slot(effective_printer, new_slot))
    if effective_printer:
        for new_slot, spool_id, _old in moves:
            _clear_ph_slot_claims(client, effective_printer, new_slot,
                                  keep_spool_id=int(spool_id) if spool_id is not None else None)
    records: dict[int, dict[str, Any] | None] = {}
    for new_slot, spool_id, old in moves:
        if spool_id is None:
            slots.pop(str(new_slot), None)
            records[new_slot] = None
            continue
        record = _slot_record(new_slot, spools[int(spool_id)], old, note)
        slots[str(new_slot)] = record
        records[new_slot] = record
    save_state(state, state_path, printer_id=printer_id)
    screens: dict[int, str] = {}
    for new_slot, record in records.items():
        if record is None:
            screens[new_slot] = "not updated: the slot is empty"
            continue
        try:
            if printer_id:
                screens[new_slot] = push_screen(
                    new_slot, record["material"], record["hex"], mr=mr,
                    pending_path=_pending_path(pending_path, printer_id),
                    adapter=_printer_adapter(printer_id))
            else:
                screens[new_slot] = push_screen(new_slot, record["material"], record["hex"],
                                                mr=mr, pending_path=pending_path)
        except Exception:
            screens[new_slot] = "not updated — run `/spool sync` to try again"
    return {"slot_a": slot_a, "slot_b": slot_b,
            "spool_in_a": int(id_b) if id_b is not None else None,
            "spool_in_b": int(id_a) if id_a is not None else None,
            "records": records, "screens": screens}


def swap_text(result: dict[str, Any]) -> str:
    def held(slot, spool_id):
        return f"slot {slot} holds spool #{spool_id}" if spool_id is not None \
            else f"slot {slot} is empty"
    return (f"Swapped: {held(result['slot_a'], result['spool_in_a'])}; "
            f"{held(result['slot_b'], result['spool_in_b'])}.")


def backfill_slot_colors(*, client: Client = spoolman, mr: Client = moonraker,
                          state_path: Path | None = None,
                          printer_id: str | None = None) -> list[dict[str, Any]]:
    """Give every loaded slot a color code from the best source that answers.

    Run at listener startup and on each reconnect, without user action.
    Fallback order: the slot's own valid hex (never overwritten), Spoolman's
    ``color_hex``, the hex the printer reports for the slot (ZMod
    ``zmod_color``), the color name, then the neutral default. A code this
    module derived earlier (``hex_source`` name/default) is upgraded when
    Spoolman or the printer now answers. A Spoolman filament with no color
    gets the slot's code filled in (never replaced). Spoolman and the
    printer are each best effort: one being down only narrows the sources.
    Returns the changes made (slot, hex, source, previous); saves only then.
    """
    path = _hardware_read_path(state_path, printer_id)
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    state = json.loads(json.dumps(raw))
    # A record written before the #672 rename carries only ifs_slots;
    # adopt it like both state loaders do, or legacy slots never
    # backfill (P2-268). The save below then writes the current name.
    filament_color.adopt_renamed_slots_key(state)
    slots = state.get("filament_slots") or {}
    pending = {str(key): rec for key, rec in slots.items() if isinstance(rec, dict) and rec.get("spoolman_id")
               and (not filament_color.normalize_hex(rec.get("hex"))
                    or rec.get("hex_source") in filament_color.DERIVED_SOURCES)}
    if not pending:
        return []
    spoolman_hexes: dict[Any, str] = {}
    filaments: dict[Any, dict[str, Any]] = {}
    for rec in pending.values():
        try:
            spool = client("GET", f"/api/v1/spool/{int(rec['spoolman_id'])}", None)
        except Exception:
            continue
        filament = (spool or {}).get("filament") or {}
        filaments[rec["spoolman_id"]] = filament
        hx = first_hex_of(filament)
        if hx:
            spoolman_hexes[rec["spoolman_id"]] = hx
    printer_hexes: dict[str, str] = {}
    try:
        reply = mr("GET", "/printer/objects/query?zmod_color", None) or {}
        printer_hexes = filament_color.printer_slot_hexes(
            ((reply.get("result") or {}).get("status") or {}).get("zmod_color"))
    except Exception:
        pass
    changes = filament_color.fill_missing_slot_hexes(
        state, spoolman_hexes=spoolman_hexes, printer_hexes=printer_hexes)
    for change in changes:
        # The printer's own hex wins when it reports one (fill_missing_slot_hexes),
        # so the screen already shows this code. Any other source leaves the
        # screen label different from the record, and mapped launch would
        # refuse the slot ("live RGB mapping mismatch"). This runs at listener
        # startup, possibly mid-print, and is not a screen-sync point, so it
        # sends nothing: it flags the slot for the existing screen sync
        # (/spool sync, or the next bind), which defers while printing.
        change["screen"] = ("matches" if filament_color.same_color(printer_hexes.get(change["slot"]), change["hex"])
                            else "needs_sync")
    for change in changes:
        rec = slots.get(change["slot"]) or {}
        filament = filaments.get(rec.get("spoolman_id"))
        if (filament and filament.get("id") is not None and not first_hex_of(filament)
                and change["source"] in ("printer", "name")):
            try:
                client("PATCH", f"/api/v1/filament/{int(filament['id'])}", {"color_hex": change["hex"]})
            except Exception:
                pass
    if state != raw:
        save_state(state, state_path, printer_id=printer_id)
    return changes


def bind_text(result: dict[str, Any]) -> str:
    r = result["record"]
    grams = f", {r['remaining_grams']:g} g left" if r.get("remaining_grams") is not None else ""
    prior = result.get("prior_spool_id")
    moved = ""
    if prior and prior != result["spool_id"]:
        disposition = result.get("prior_disposition") or "shelf"
        if disposition == "archive":
            moved = f" Spool #{prior} was archived."
        elif disposition == "depleted":
            moved = f" Spool #{prior} was marked as depleted (0 g left)."
        elif disposition == "delete":
            moved = f" Spool #{prior} was deleted from Spoolman."
        elif disposition == "shelf":
            moved = f" Spool #{prior} went back to the shelf."
    screen = {"pushed": "Printer screen updated.",
              "deferred": "Printer screen will update when this print ends."}.get(result["screen"],
                                                                                   f"Printer screen {result['screen']}.")
    if result.get("screen_note"):
        screen = screen[:-1] + f" ({result['screen_note']})."
    usage = "" if result["usage"] == "unchanged" else f" Filament usage {result['usage']}."
    return f"Slot {result['slot']} → spool #{result['spool_id']} ({r['name']}{grams}).{moved} {screen}{usage}"


# ---------------------------------------------------------------- reconcile (#798)

def _reconcile_lock(printer_id: str):
    """The existing per-printer control lock for one printer, or None when it cannot be built.

    Same discipline as print_control._lock_for: the shared flock-backed lock
    from core.control_lock, keyed by printer under this install's state dir,
    so a reconcile never interleaves with a print start, a slot load or
    another reconcile for the same printer in another process.
    """
    try:
        from ..core import control_lock as _cl
        return _cl.control_lock(str(printer_id), state_dir=_config.state_dir())
    except Exception:
        return None


def reconcile(printer_id: str | None = None, *, client: Client = spoolman, mr: Client = moonraker,
              state_path: Path | None = None, pending_path: Path | None = None) -> dict[str, Any]:
    """Make Spoolman, the local slot cache and the screen agree, with Spoolman winning (#798).

    The single writer of the ``filament_slots`` cache: slot assignments are
    read from the Spoolman ``ph_slot`` extra field, never from ``location``.
    On the first run — when Spoolman carries no ``ph_slot`` values at all —
    the existing local cache is migrated into Spoolman instead. Conflicts
    (two spools claiming one slot, a slot claimed by an archived spool, a
    local record naming a spool Spoolman no longer has, an identity
    cross-check mismatch) are named in the result, never silently picked.
    Screens are converged through push_screen (which defers while printing)
    and Moonraker's usage pointer through repair_usage_spool.

    Idempotent: a second run over the same facts writes nothing. Runs under
    the per-printer control lock; a held lock is a named busy result, not a
    queue. An unreachable Spoolman is a named failure and the local cache is
    left untouched. Never raises for a Spoolman or printer failure.
    """
    pid = printer_id or _default_printer_id()
    result: dict[str, Any] = {"ok": False, "printer_id": pid, "migrated": False,
                              "assignments": {}, "conflicts": [], "screens": [],
                              "usage": None, "error": None, "changed": False}
    if not pid:
        result["error"] = ("Spool reconcile: no printer could be derived from the "
                           "configuration, so nothing was changed")
        return result
    lock = _reconcile_lock(pid)
    if lock is not None:
        try:
            acquired = lock.acquire(blocking=False)
        except Exception:
            acquired, lock = True, None  # a lock that cannot be taken must not block the reconcile
        if not acquired:
            result["error"] = (f"Spool reconcile for {pid}: another printer control action is "
                               "in flight, so nothing was changed — run it again when the printer is free")
            return result
    try:
        return _reconcile_locked(pid, result, client=client, mr=mr,
                                 state_path=state_path, pending_path=pending_path)
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


def _reconcile_locked(pid: str, result: dict[str, Any], *, client: Client, mr: Client,
                      state_path: Path | None, pending_path: Path | None) -> dict[str, Any]:
    ensure_ph_slot_field(client)
    try:
        rows = _call_client(client, "GET", "/api/v1/spool?allow_archived=true", None)
    except Exception:
        rows = None
    if not isinstance(rows, list):
        result["error"] = ("Spool reconcile: Spoolman did not answer, so the local slot "
                           "record was left unchanged")
        return result
    spools = [row for row in rows if isinstance(row, dict) and row.get("id") is not None]
    by_id = {int(row["id"]): row for row in spools}
    try:
        state = load_state(state_path, printer_id=pid)
    except FileNotFoundError:
        state = {"filament_slots": {}}  # no cache yet: a rebuild from Spoolman, not a conflict
    except Exception:
        state = {"filament_slots": {}}
        result["conflicts"].append("The local slot record could not be read; it was rebuilt from Spoolman")
    old_slots = {str(key): dict(rec) for key, rec in (state.get("filament_slots") or {}).items()
                 if isinstance(rec, dict)}
    claims: dict[int, list[dict[str, Any]]] = {}
    for row in spools:
        parsed = spool_ph_slot(row)
        if parsed is not None and parsed[0] == pid:
            claims.setdefault(parsed[1], []).append(row)
    # Migration is a per-printer one-time step, tracked in this printer's own
    # record — never inferred from whether any spool currently carries a
    # ph_slot value: another printer sharing this Spoolman may already have
    # values while this one has not migrated yet, and clearing this printer's
    # last assignment after migration must not resurrect it from the stale
    # cache on the next run.
    migrated_done = bool(state.get("ph_slot_migrated"))
    if not migrated_done and not claims:
        assignments = _reconcile_migrate(pid, result, client=client, by_id=by_id, old_slots=old_slots,
                                         state=state, state_path=state_path)
    else:
        assignments = _reconcile_rebuild(pid, result, client=client, state=state, old_slots=old_slots,
                                         claims=claims, by_id=by_id, state_path=state_path)
    result["assignments"] = {slot: assignments[slot] for slot in sorted(assignments)}
    # Converge the screens for the slots Spoolman settled (push_screen itself
    # defers while printing), then repair the usage pointer like /spool sync.
    try:
        adapter = _printer_adapter(pid)
    except Exception:
        adapter = None
    for slot in sorted(assignments):
        rec = (result.get("_records") or {}).get(str(slot)) or {}
        if not rec.get("hex"):
            spool = by_id.get(assignments[slot]) or {}
            rec = _slot_record(slot, spool, old_slots.get(str(slot)), "Reconciled from Spoolman (#798)")
        try:
            pushed = push_screen(slot, rec.get("material") or "", rec.get("hex") or "", mr=mr,
                                 pending_path=_pending_path(pending_path, pid), adapter=adapter)
            note = (screen_fallback_note(rec.get("material") or "", adapter)
                    if pushed in ("pushed", "deferred") else None)
            result["screens"].append(f"Slot {slot}: {pushed}" + (f" ({note})" if note else ""))
        except Exception:
            result["screens"].append(f"Slot {slot}: the screen could not be updated — try again")
    result.pop("_records", None)
    try:
        result["usage"] = repair_usage_spool(mr=mr, state_path=state_path, printer_id=pid)
    except Exception:
        result["usage"] = "Filament charging: could not be checked — try again"
    result["ok"] = True
    return result


def _reconcile_migrate(pid: str, result: dict[str, Any], *, client: Client,
                       by_id: dict[int, dict[str, Any]],
                       old_slots: dict[str, dict[str, Any]],
                       state: dict[str, Any], state_path: Path | None) -> dict[int, int]:
    """First run for this printer: write the local cache's assignments into Spoolman ``ph_slot`` (#798).

    Completion is persisted in this printer's own record (``ph_slot_migrated``)
    even when there was nothing to migrate, so a later run takes the rebuild
    branch for this printer regardless of what other printers sharing this
    Spoolman have done, and a cleared assignment is never resurrected.
    """
    assignments: dict[int, int] = {}
    records: dict[str, dict[str, Any]] = {}
    for key in sorted(old_slots, key=lambda k: int(k) if k.isdigit() else 0):
        rec = old_slots[key]
        sid = rec.get("spoolman_id")
        if sid is None or not key.isdigit():
            continue
        slot = int(key)
        spool = by_id.get(int(sid))
        if spool is None:
            result["conflicts"].append(
                f"Slot {slot}: the local record names spool #{sid}, which Spoolman no longer "
                "has — it was not migrated")
            continue
        if spool.get("archived"):
            result["conflicts"].append(
                f"Slot {slot}: the local record names spool #{sid}, which is archived in "
                "Spoolman — it was not migrated")
            continue
        if _set_ph_slot(client, int(sid), format_ph_slot(pid, slot)):
            assignments[slot] = int(sid)
            records[key] = rec
        else:
            result["conflicts"].append(
                f"Slot {slot}: spool #{sid} could not be written to Spoolman — it was not migrated")
    if assignments:
        result["migrated"] = True
        result["changed"] = True
    if not state.get("ph_slot_migrated"):
        state["ph_slot_migrated"] = True
        save_state(state, state_path, printer_id=pid)
    result["_records"] = records
    return assignments


def _reconcile_rebuild(pid: str, result: dict[str, Any], *, client: Client, state: dict[str, Any],
                       old_slots: dict[str, dict[str, Any]],
                       claims: dict[int, list[dict[str, Any]]], by_id: dict[int, dict[str, Any]],
                       state_path: Path | None) -> dict[int, int]:
    """Rebuild the local cache from Spoolman's ``ph_slot`` assignments (#798)."""
    assignments: dict[int, int] = {}
    records: dict[str, dict[str, Any]] = {}
    new_slots: dict[str, dict[str, Any]] = {}
    for slot in sorted(claims):
        claimants = claims[slot]
        live = [row for row in claimants if not row.get("archived")]
        for row in claimants:
            if row.get("archived"):
                result["conflicts"].append(
                    f"Slot {slot}: spool #{row['id']} is archived but still claims this slot in "
                    "Spoolman — the claim was cleared and the slot was not taken from it")
                _set_ph_slot(client, int(row["id"]), None)
        if len(live) > 1:
            ids = " and ".join(f"#{row['id']}" for row in sorted(live, key=lambda r: int(r["id"])))
            result["conflicts"].append(
                f"Slot {slot}: spools {ids} both claim this slot in Spoolman — neither was "
                "picked; fix the claim in Spoolman and run the sync again")
            if str(slot) in old_slots:
                new_slots[str(slot)] = old_slots[str(slot)]
            continue
        if not live:
            continue
        spool = live[0]
        sid = int(spool["id"])
        old = old_slots.get(str(slot))
        record = _slot_record(slot, spool, old, "Rebuilt from Spoolman ph_slot by reconcile (#798)")
        if old is not None and old.get("spoolman_id") == sid:
            # Identity cross-check: the cache and Spoolman name the same spool
            # for this slot, so a material or color disagreement is a fact
            # worth naming — Spoolman's record still wins the rebuild.
            filament = spool.get("filament") or {}
            old_material = str(old.get("material") or "").strip()
            new_material = str(filament.get("material") or "").strip()
            if old_material and new_material and old_material.casefold() != new_material.casefold() \
                    and old_material.casefold() != "unknown" and new_material.casefold() != "unknown":
                result["conflicts"].append(
                    f"Slot {slot}: spool #{sid} is {new_material} in Spoolman but the local "
                    f"record said {old_material} — the Spoolman record was used")
            old_color = str(old.get("color") or "").strip()
            if old_color and record.get("color") and old_color.casefold() != str(record["color"]).casefold():
                result["conflicts"].append(
                    f"Slot {slot}: spool #{sid} is {record['color']} in Spoolman but the local "
                    f"record said {old_color} — the Spoolman record was used")
            # Idempotence: an unchanged assignment keeps its existing record
            # (and its timestamp) instead of being rewritten every run.
            if all(old.get(field) == record.get(field)
                   for field in ("spoolman_id", "name", "material", "color", "hex", "remaining_grams")):
                record = old
        assignments[slot] = sid
        records[str(slot)] = record
        new_slots[str(slot)] = record
    for key, rec in old_slots.items():
        sid = rec.get("spoolman_id")
        if sid is not None and int(sid) not in by_id and key not in new_slots:
            result["conflicts"].append(
                f"Slot {key}: the local record named spool #{sid}, which Spoolman no longer "
                "has (it may have been deleted) — the slot was rebuilt from Spoolman")
    marker_dirty = not state.get("ph_slot_migrated")
    if marker_dirty:
        # A printer whose claims already exist (binds wrote them before the
        # first reconcile) still records migration as done, so clearing its
        # last assignment later cannot re-enter the migration branch.
        state["ph_slot_migrated"] = True
    if new_slots != old_slots or marker_dirty:
        state["filament_slots"] = new_slots
        active = state.get("spool") or {}
        active_id = active.get("spoolman_id")
        replaced_by = None
        if active_id is not None and int(active_id) not in assignments.values():
            # The slot that held the active spool now holds a different one
            # in Spoolman: carry the replacement into the active record, as
            # bind_slot does, or usage repair keeps charging the displaced
            # spool because the stale active record still counts it loaded.
            for key, rec in old_slots.items():
                if rec.get("spoolman_id") == active_id and str(key).isdigit() \
                        and int(key) in assignments and assignments[int(key)] != int(active_id):
                    replaced_by = assignments[int(key)]
                    break
        target_id = replaced_by if replaced_by is not None else (
            int(active_id) if active_id is not None and int(active_id) in assignments.values() else None)
        if target_id is not None:
            spool = by_id.get(int(target_id)) or {}
            filament = spool.get("filament") or {}
            state["spool"] = {**active, "spoolman_id": int(target_id),
                              "name": filament.get("name") or active.get("name"),
                              "material": filament.get("material") or active.get("material"),
                              "density": filament.get("density") or active.get("density"),
                              "diameter_mm": filament.get("diameter") or active.get("diameter_mm")}
            if replaced_by is not None:
                # temps_c belonged to the displaced spool (#692, as in bind_slot).
                state["temperature_resolution"] = {"source": "spool changed by slot sync; resolved per slice"}
        save_state(state, state_path, printer_id=pid)
        result["changed"] = True
    result["_records"] = records
    return assignments


def reconcile_text(result: dict[str, Any]) -> str:
    """The /spool sync and log wording for one reconcile result (#798)."""
    if result.get("error"):
        return str(result["error"])
    pid = result.get("printer_id") or "the printer"
    assignments = result.get("assignments") or {}
    if assignments:
        named = ", ".join(f"slot {slot} → spool #{assignments[slot]}" for slot in sorted(assignments))
        lead = f"Spool reconcile for {pid}: {named} (from Spoolman)."
    else:
        lead = f"Spool reconcile for {pid}: no spools are assigned to slots in Spoolman."
    if result.get("migrated"):
        lead += " The local slot record was migrated into Spoolman (first run)."
    lines = [lead]
    for conflict in result.get("conflicts") or []:
        lines.append(f"Conflict: {conflict}")
    lines.extend(result.get("screens") or [])
    if result.get("usage"):
        lines.append(str(result["usage"]))
    return "\n".join(lines)


def repair_usage_spool(*, mr: Client = moonraker, state_path: Path | None = None,
                       printer_id: str | None = None) -> str:
    """Re-point Moonraker's active spool at the recorded spool when the pointer is stale.

    The recovery bind_slot's failure message sends the user to (/spool sync,
    P2-179): the new slot mapping was saved but re-pointing usage failed, so
    Moonraker still charges a spool that is loaded nowhere. A pointer naming
    a spool no record loads is stale; the recorded active spool replaces it,
    with resume_usage's verified readback. A pointer that names a loaded
    spool is consistent and is left alone, an empty pointer charges no one
    and is left alone (a deliberate suspension is not sync's to undo), and
    nothing is touched while the printer is busy — re-pointing mid-print
    would corrupt the running print's accounting.
    """
    if printer_state(mr) in ("printing", "unknown"):
        return "Filament charging: left alone while the printer is busy"
    try:
        active = active_usage_spool(mr)
    except Exception:
        return "Filament charging: could not be checked — try again"
    if active is None:
        return "Filament charging: no spool is being charged"
    state = load_state(state_path, printer_id=printer_id)
    if active in _loaded_spool_ids(state):
        return f"Filament charging: spool #{active} — matches the record"
    target = (state.get("spool") or {}).get("spoolman_id")
    if not isinstance(target, int) or isinstance(target, bool):
        return (f"Filament charging: spool #{active} is no longer loaded and no replacement "
                "is recorded — load the new spool with `/spool change`")
    try:
        resume_usage(target, mr)
    except Exception:
        return f"Filament charging: could not be re-pointed to spool #{target} — try again"
    return f"Filament charging: was pointing at spool #{active} (no longer loaded); re-pointed to #{target}"


def sync_slot_screen(slot: int, *, printer_id: str | None = None) -> str:
    """Push one slot's Spoolman material and color to the printer screen (#795 'Set screen')."""
    rec = (load_state(printer_id=printer_id).get("filament_slots") or {}).get(str(slot)) or {}
    if not rec.get("spoolman_id") or not rec.get("hex"):
        return "unsupported"
    if printer_id:
        return push_screen(slot, rec.get("material") or "", rec["hex"], mr=moonraker_for(printer_id),
                           pending_path=_pending_path(None, printer_id), adapter=_printer_adapter(printer_id))
    return push_screen(slot, rec.get("material") or "", rec["hex"])


def sync_all_screens(*, mr: Client = moonraker, state_path: Path | None = None,
                     pending_path: Path | None = None, printer_id: str | None = None) -> list[str]:
    state = load_state(state_path, printer_id=printer_id)
    out = []
    for slot in SLOTS:
        rec = (state.get("filament_slots") or {}).get(str(slot)) or {}
        if not rec.get("spoolman_id"):
            out.append(f"Slot {slot}: no spool mapped, screen left alone")
            continue
        try:
            if printer_id:
                pushed = push_screen(slot, rec.get('material') or '', rec.get('hex') or '', mr=mr,
                                      pending_path=_pending_path(pending_path, printer_id),
                                      adapter=_printer_adapter(printer_id))
            else:
                pushed = push_screen(slot, rec.get('material') or '', rec.get('hex') or '', mr=mr, pending_path=pending_path)
            note = (screen_fallback_note(rec.get('material') or '', _printer_adapter(printer_id))
                    if pushed in ("pushed", "deferred") else None)
            out.append(f"Slot {slot}: {pushed}" + (f" ({note})" if note else ""))
        except Exception as exc:
            out.append(f"Slot {slot}: the screen could not be updated — try again")
    # /spool sync is also the recovery bind_slot names when re-pointing usage
    # failed (P2-179): it must repair the charging, not just the screens.
    out.append(repair_usage_spool(mr=mr, state_path=state_path, printer_id=printer_id))
    return out


# ---------------------------------------------------------------- inventory

def list_text(*, client: Client = spoolman, state_path: Path | None = None,
              printer_id: str | None = None) -> str:
    state = load_state(state_path, printer_id=printer_id)
    spools = client("GET", "/api/v1/spool?allow_archived=false", None) or []
    by_id = {s.get("id"): s for s in spools if isinstance(s, dict)}
    lines = ["**Loaded**"]
    loaded = set()
    for slot in SLOTS:
        rec = (state.get("filament_slots") or {}).get(str(slot)) or {}
        sid = rec.get("spoolman_id")
        live = by_id.get(sid) or {}
        grams = live.get("remaining_weight")
        g = f", {grams:.0f} g" if isinstance(grams, (int, float)) else ""
        lines.append(f"Slot {slot}: #{sid} {rec.get('name') or '?'}{g}" if sid else f"Slot {slot}: empty")
        if sid:
            loaded.add(sid)
    shelf = [s for s in spools if s.get("id") not in loaded]
    lines.append("**Shelf**")
    for s in sorted(shelf, key=lambda s: s.get("id") or 0):
        f = s.get("filament") or {}
        grams = s.get("remaining_weight")
        g = f", {grams:.0f} g" if isinstance(grams, (int, float)) else ""
        from .discord_print_summary import filament_label
        lines.append(f"#{s['id']} {filament_label(f.get('name'), f.get('material'), '?')}{g}".rstrip())
    if not shelf:
        lines.append("(nothing on the shelf)")
    lines.append("Guided: `/spool` walks you through adding or changing a spool with buttons · "
                 "One-shot: `/spool change <slot> <spool#>` · `/spool new brand, material, color, grams[, hex]`")
    return "\n".join(lines)


def _loaded_line(label: str, rec: dict[str, Any], live: dict[str, Any], amounts_known: bool) -> str:
    """One loaded location's line for loaded_text (#744).

    The live Spoolman spool wins for identity and amounts; the hardware
    record is the fallback for both, and is all there is when Spoolman
    did not answer. A percent is shown only when the spool's initial
    weight is known — it is never estimated from the record's snapshot.
    """
    from .discord_print_summary import filament_label
    sid = rec.get("spoolman_id")
    filament = live.get("filament") or {}
    brand = str((filament.get("vendor") or {}).get("name") or "").strip()
    name = filament_label(filament.get("name"), filament.get("material")) or str(rec.get("name") or "").strip()
    if not name and (rec.get("color") or rec.get("material")):
        name = filament_label(rec.get("color"), rec.get("material"))
    hex_code = screen_hex(filament.get("color_hex")) or screen_hex(rec.get("hex"))
    who = f"#{sid} " if sid is not None else ""
    if brand and name and brand.casefold() not in name.casefold():
        who += f"{brand} "
    line = f"{label}: {who}{name or 'spool'}"
    if hex_code:
        line += f" (#{hex_code})"
    if not amounts_known:
        return line + " — amount unavailable (Spoolman did not answer)"
    grams = live.get("remaining_weight")
    if not isinstance(grams, (int, float)):
        return line + " — amount unknown"
    line += f" — {grams:.0f} g left"
    initial = live.get("initial_weight")
    if isinstance(initial, (int, float)) and initial > 0:
        line += f" ({grams / initial * 100:.0f}% of {initial:.0f} g)"
    else:
        line += " (percent unknown: the spool's starting weight is not recorded)"
    return line


def _names_filament(rec: dict[str, Any]) -> bool:
    return bool(rec.get("name") or rec.get("material") or rec.get("color"))


def loaded_text(*, client: Client = spoolman, state_path: Path | None = None,
                printer_id: str | None = None) -> str:
    """What is loaded on one printer, where, and how much is left (#744).

    The loaded-only companion to list_text: no shelf, so the answer is
    not buried in inventory nobody asked about. Locations come from the
    hardware record Print Handy already keeps — the slot map (shown over
    the same SLOTS range as list_text once a map exists, extra recorded
    slots included) plus the active spool when it sits outside the map
    (a single-spool printer's one position). A slot whose record names
    filament but binds no spool is reported as loaded-but-unrecorded,
    never silently dropped. Fail-soft per #387: a Spoolman that does not
    answer costs the amounts, never the answer — locations and spools
    are still reported from the record, with amounts named unavailable.
    """
    state = load_state(state_path, printer_id=printer_id)
    amounts_known = True
    by_id: dict[Any, dict[str, Any]] = {}
    try:
        spools = client("GET", "/api/v1/spool?allow_archived=false", None) or []
        by_id = {s.get("id"): s for s in spools if isinstance(s, dict)}
    except Exception:
        amounts_known = False
    slots = state.get("filament_slots") or {}
    locations: list[tuple[str, dict[str, Any]]] = []
    if slots:
        keys = {int(k) for k in slots if str(k).isdigit()}
        for slot in sorted(set(SLOTS) | keys):
            locations.append((f"Slot {slot}", dict(slots.get(str(slot)) or {})))
    active = state.get("spool") or {}
    active_id = active.get("spoolman_id")
    bound_ids = {rec.get("spoolman_id") for _label, rec in locations}
    if active_id is not None and active_id not in bound_ids:
        locations.append(("Spool", dict(active)))
    elif active_id is None and not slots and _names_filament(active):
        # A single-spool printer whose record names its filament but binds no Spoolman spool
        # (the Bambu A1 fixture): report it as loaded, never as an empty printer.
        locations.append(("Spool", dict(active)))
    lines = ["**Loaded filament**"]
    if not locations:
        lines.append("Nothing is recorded as loaded on this printer.")
    for label, rec in locations:
        sid = rec.get("spoolman_id")
        if sid is None:
            if _names_filament(rec):
                from .discord_print_summary import filament_label
                what = (str(rec.get("name") or "").strip()
                        or filament_label(rec.get("color"), rec.get("material")) or "filament")
                hint = (f"`/spool change {label.rsplit(' ', 1)[-1]} <spool#>` records which one"
                        if label.startswith("Slot") else "record it with `/spool`")
                lines.append(f"{label}: {what} is recorded here, but no spool is bound to it, "
                             f"so the amount left is unknown — {hint}")
            else:
                lines.append(f"{label}: empty")
            continue
        lines.append(_loaded_line(label, rec, by_id.get(sid) or {}, amounts_known))
    lines.append("Full inventory, shelf included: `/spool list`")
    return "\n".join(lines)


def filament_types(*, client: Client = spoolman) -> list[dict[str, Any]]:
    """The filament types the Spoolman database already knows (#475).

    One entry per distinct (brand, material, colour): the guided new-spool
    flow offers these so a known type supplies its colour and hex from the
    database instead of asking the user to re-type them.
    """
    rows = client("GET", "/api/v1/filament", None) or []
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        brand = str((row.get("vendor") or {}).get("name") or "").strip()
        material = str(row.get("material") or "").strip()
        color = str(row.get("name") or "").strip()
        key = (brand.casefold(), material.casefold(), color.casefold())
        if key in seen:
            continue
        seen[key] = {"filament_id": int(row["id"]), "brand": brand or None,
                     "material": material, "color": color,
                     "hex": screen_hex(row.get("color_hex"))}
    return sorted(seen.values(),
                  key=lambda t: ((t["brand"] or "").casefold(), t["material"].casefold(),
                                 t["color"].casefold()))


def next_spool_id(*, client: Client = spoolman) -> int:
    """The spool number the next created spool will get: max id + 1, archived included."""
    rows = client("GET", "/api/v1/spool?allow_archived=true", None) or []
    ids = [int(row["id"]) for row in rows
           if isinstance(row, dict) and isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool)]
    return max(ids) + 1 if ids else 1


def shelf_spools(*, client: Client = spoolman, state_path: Path | None = None,
                 printer_id: str | None = None) -> list[dict[str, Any]]:
    """Live spools that are not loaded in any slot, sorted by number (#475)."""
    state = load_state(state_path, printer_id=printer_id)
    loaded = _loaded_spool_ids(state)
    rows = client("GET", "/api/v1/spool?allow_archived=false", None) or []
    out = []
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None or int(row["id"]) in loaded:
            continue
        filament = row.get("filament") or {}
        grams = row.get("remaining_weight")
        out.append({"spool_id": int(row["id"]),
                    "name": str(filament.get("name") or f"Spool #{row['id']}"),
                    "material": str(filament.get("material") or ""),
                    "remaining_grams": float(grams) if isinstance(grams, (int, float)) else None})
    return sorted(out, key=lambda s: s["spool_id"])


def parse_new(text: str) -> dict[str, Any]:
    parts = [p.strip() for p in str(text or "").split(",")]
    if len(parts) < 4:
        raise SpoolError("format: /spool new brand, material, color, grams[, hex]  e.g. Sunlu, PLA+, Matte White, 1000, FFFFFF")
    brand, material, color, grams = parts[:4]
    try:
        g = float(re.sub(r"[^0-9.]", "", grams))
    except ValueError:
        g = 0
    if not material or not color or g <= 0:
        raise SpoolError("material, color, and a positive full starting weight in grams are required")
    hx = screen_hex(parts[4]) if len(parts) > 4 else None
    if len(parts) > 4 and parts[4] and hx is None:
        raise SpoolError(f"{parts[4]!r} is not a hex color (e.g. 0C6283, #0C6283 or #FFF)")
    return {"brand": brand or None, "material": material, "color": color, "grams": round(g, 1), "hex": hx}


def spec_color(spec: dict[str, Any]) -> tuple[str | None, tuple[str, ...], str | None]:
    """The color code a new spool records: (hex, multi hexes, source).

    An explicit hex (typed, a picked swatch, a label read) wins; otherwise
    it is derived from the color name (the operator never
    types hex). A name that lists several colors ("rose red blue green")
    records its first color as the swatch plus every color as the
    multi-color list. Nothing derivable returns (None, (), None): the
    walkthrough asks with swatch buttons before it gets here, and a one-shot
    command leaves Spoolman's color empty rather than inventing one.
    """
    given = screen_hex(spec.get("hex"))
    if given:
        return given, (), "given"
    guess = filament_color.derive(spec.get("color"), spec.get("material"))
    if guess is None:
        return None, (), None
    return guess.hex, guess.multi_hexes, "name"


def create_spool(spec: dict[str, Any], *, client: Client = spoolman) -> dict[str, Any]:
    """Create a spool on the shelf, reusing a matching filament type when one exists.

    The new spool always gets a color code when one can be worked out
    (spec_color); a reused filament type that has none gets it filled in,
    never replaced.
    """
    # One spelling per material, color and brand, so no entry path mints a
    # near-duplicate filament type ("pla plus" next to "PLA+", "Black PLA"
    # next to "Black"). An unknown material is refused, not stored.
    try:
        spec = spool_canon.canonical_spec(spec)
    except spool_canon.SpecError as exc:
        raise SpoolError(str(exc)) from None
    color_hex, multi_hexes, color_source = spec_color(spec)
    if color_hex:
        spec = {**spec, "hex": color_hex}
    filaments = client("GET", "/api/v1/filament", None) or []
    match = spool_canon.find_filament(filaments, spec)
    reused = True
    if match is None:
        reused = False
        body = {"name": spec["color"], "material": spec["material"], "diameter": 1.75,
                "density": 1.24, "weight": spec["grams"]}
        # Label temperatures from a spool photo (#514) are recorded on the
        # filament as its Spoolman temperatures — data, not authority: the
        # temperature resolution already lets a filament's recorded temps
        # win over material defaults for every material, and the #512
        # variant adjustments never override recorded data. A reused
        # filament keeps the temps it already has; they are not rewritten.
        if spec.get("nozzle_temp") is not None:
            body["settings_extruder_temp"] = spec["nozzle_temp"]
        if spec.get("bed_temp") is not None:
            body["settings_bed_temp"] = spec["bed_temp"]
        if spec.get("hex"):
            body["color_hex"] = spec["hex"]
        if spec.get("brand"):
            vendors = client("GET", "/api/v1/vendor", None) or []
            vendor = next((v for v in vendors if str(v.get("name") or "").casefold() == spec["brand"].casefold()), None)
            if vendor is None:
                vendor = client("POST", "/api/v1/vendor", {"name": spec["brand"]})
            body["vendor_id"] = vendor["id"]
        match = client("POST", "/api/v1/filament", body)
    if reused and color_hex and not first_hex_of(match):
        # A filament type created before color codes were always recorded:
        # fill the gap, best effort. A valid hex already there is left alone.
        try:
            client("PATCH", f"/api/v1/filament/{match['id']}", {"color_hex": color_hex})
        except Exception:
            pass
    if not reused and len(multi_hexes) > 1:
        # A multi-color name also records every color, best effort: the
        # representative swatch above is what slicing and the screen use.
        try:
            client("PATCH", f"/api/v1/filament/{match['id']}",
                   {"multi_color_hexes": ",".join(multi_hexes), "multi_color_direction": "longitudinal"})
        except Exception:
            pass
    # A purchase intake (#801) records what the roll cost and where it was
    # bought: Spoolman's own price field, and the purchase URL appended to
    # the created-spool comment (the watcher reads that comment's head, so
    # a purchase spool is still recognized as Print Handy-made).
    comment = CREATED_SPOOL_COMMENT
    if spec.get("purchase_url"):
        comment = f"{CREATED_SPOOL_COMMENT}; purchased: {spec['purchase_url']}"
    body = {"filament_id": match["id"], "initial_weight": spec["grams"],
            "remaining_weight": spec["grams"],
            "location": SHELF, "lot_nr": spec.get("lot"), "comment": comment}
    if spec.get("price") is not None:
        body["price"] = spec["price"]
    created = client("POST", "/api/v1/spool", body)
    return {"spool_id": created["id"], "filament_id": match["id"], "reused_filament": reused,
            "hex": color_hex, "hex_source": color_source}


def first_hex_of(filament: dict[str, Any] | None) -> str | None:
    """A filament's single color code: color_hex, else its first multi color."""
    filament = filament or {}
    return (filament_color.normalize_hex(filament.get("color_hex"))
            or filament_color.first_hex(filament.get("multi_color_hexes")))
