"""Spoolman -> printer one-way spool sync and the /spool command logic.

Spoolman is the source of truth. The AD5X screen is only *told* what Spoolman
says, through the Z-Mod `CHANGE_ZCOLOR SLOT= TYPE= HEX=` macro. Nothing here
reads the screen back into Spoolman.

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
from ..core import filament_color, spoolman_client
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


def spoolman(method: str, path: str, body: Any = None) -> Any:
    """The /spool command layer's Spoolman transport: the one wrapper (#387).

    An unavailable Spoolman surfaces as SpoolError, this module's contract
    for a refused /spool action — the operator asked for a Spoolman
    operation, so the failure is reported, never swallowed.
    """
    try:
        return spoolman_client.request(method, path, body, base_url=SPOOLMAN, timeout=8)
    except spoolman_client.SpoolmanUnavailable as exc:
        raise SpoolError(str(exc)) from exc


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
        raise SpoolError(f"Spoolman has no spool #{spool_id}")
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
    client("PATCH", f"/api/v1/spool/{int(spool_id)}", {"location": f"IFS Slot {slot}"})
    if prior and int(prior) != int(spool_id) and prior_disposition == "shelf":
        prior_rec = client("GET", f"/api/v1/spool/{int(prior)}", None)
        if isinstance(prior_rec, dict) and not prior_rec.get("archived") and \
                str(prior_rec.get("location") or "") in ("", f"IFS Slot {slot}"):
            client("PATCH", f"/api/v1/spool/{int(prior)}", {"location": SHELF})
    if prior and int(prior) != int(spool_id) and prior_disposition == "archive":
        # The operator picked this fate in the guided change flow (#475):
        # the old spool leaves the live inventory, its record kept.
        client("PATCH", f"/api/v1/spool/{int(prior)}", {"archived": True})
    if prior and int(prior) != int(spool_id) and prior_disposition == "depleted":
        # Same shape as the runout pattern: archived, 0 g left, depleted.
        client("PATCH", f"/api/v1/spool/{int(prior)}",
               {"archived": True, "remaining_weight": 0, "location": "depleted"})
    if prior and int(prior) != int(spool_id) and prior_disposition == "delete":
        # The operator explicitly picked Delete for the replaced spool
        # (#512): its Spoolman record is removed, not archived. This is the
        # one destructive disposition, so no flow passes it by default.
        client("DELETE", f"/api/v1/spool/{int(prior)}", None)
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
    if push_screen_update:
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
    return {"slot": slot, "spool_id": int(spool_id), "prior_spool_id": prior, "record": record,
            "screen": screen, "usage": usage, "prior_disposition": prior_disposition}


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
    usage = "" if result["usage"] == "unchanged" else f" Filament usage {result['usage']}."
    return f"Slot {result['slot']} → spool #{result['spool_id']} ({r['name']}{grams}).{moved} {screen}{usage}"


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
            out.append(f"Slot {slot}: {pushed}")
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
    color_hex, multi_hexes, color_source = spec_color(spec)
    if color_hex:
        spec = {**spec, "hex": color_hex}
    filaments = client("GET", "/api/v1/filament", None) or []
    match = None
    reused = True
    for f in filaments:
        if (str(f.get("material") or "").casefold() == spec["material"].casefold()
                and str(f.get("name") or "").casefold() == spec["color"].casefold()
                and str((f.get("vendor") or {}).get("name") or "").casefold() == str(spec.get("brand") or "").casefold()):
            match = f
            break
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
    created = client("POST", "/api/v1/spool", {
        "filament_id": match["id"], "initial_weight": spec["grams"], "remaining_weight": spec["grams"],
        "location": SHELF, "lot_nr": spec.get("lot"), "comment": CREATED_SPOOL_COMMENT,
    })
    return {"spool_id": created["id"], "filament_id": match["id"], "reused_filament": reused,
            "hex": color_hex, "hex_source": color_source}


def first_hex_of(filament: dict[str, Any] | None) -> str | None:
    """A filament's single color code: color_hex, else its first multi color."""
    filament = filament or {}
    return (filament_color.normalize_hex(filament.get("color_hex"))
            or filament_color.first_hex(filament.get("multi_color_hexes")))
