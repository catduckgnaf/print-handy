"""Filament slot selection and the printer's filament telemetry.

Moved verbatim from ``print_handy.core.model_job`` (#1323). The old names are still
importable from there for one release.
"""
import time
import urllib.request

from .. import printers
from ..core import filament_color
from .common import FILAMENT_SLOTS, request_json


FILAMENT_SENSOR_OBJECTS = ("filament_switch_sensor head_switch_sensor", "zmod_color", "zmod_ifs", "print_stats", "save_variables")


def position_limit(adapter=None):
    """Highest colour position a selection may name on the printer.

    An IFS printer bounds selections by [printer] filament_slots; a multi_tool
    printer's positions are its toolheads (the adapter's slot_count) even
    though filament_slots stays 1 there, so the bound is whichever the printer
    actually has. Every other printer keeps exactly the bound it has today.
    """
    limit = FILAMENT_SLOTS
    try:
        limit = max(limit, int((adapter or printers.current()).slot_count or 1))
    except Exception:
        pass
    return limit


def validate_filament_selection(selection, spool_optional=False, limit=None):
    """Validate a job-bound, user-confirmed filament selection.

    `spool_optional` admits a selection with no Spoolman ID; only a single-extruder printer's unbound spool
    gets it (resolve_filament_selection, #263). `limit` is the printer's colour-position bound
    (position_limit); the default keeps the configured IFS bound.
    """
    if limit is None:
        limit = FILAMENT_SLOTS
    if not isinstance(selection, dict):
        raise ValueError("explicit filament selection is required (use 'filament-select' command)")
    slot = selection.get("slot")
    if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= limit:
        raise ValueError(f"filament slot must be an integer from 1 through {limit}")
    for key in ("color", "material"):
        if not isinstance(selection.get(key), str) or not selection[key].strip():
            raise ValueError(f"filament {key} is required")
    spool_id = selection.get("spoolman_id")
    if not (spool_id is None and spool_optional) and (
            not isinstance(spool_id, int) or isinstance(spool_id, bool) or spool_id <= 0):
        raise ValueError("a positive Spoolman spool ID is required")
    return {"slot": slot, "color": selection["color"].strip(),
            "material": selection["material"].strip(), "spoolman_id": spool_id}


def configured_slots(state):
    """The slot records intake offers, keyed by slot number.

    A recorded `filament_slots` map is used as is. A single-extruder printer with no slot map has one slot,
    its configured spool (#263): slot entries are only written from Spoolman, so an offline printer never
    gets one. Printers whose slots are bound in the record (AD5X IFS, stock Flashforge) still need their
    recorded map. A multi_slot printer whose feeder reports its own contents (Bambu AMS, #671) is not
    record-bound either: its record carries the one configured spool, offered as slot 1 the #263 way,
    and the printer's reported trays are matched to a job's filament slots at print start instead.
    """
    mappings = (state or {}).get("filament_slots") or {}
    current = printers.current()
    if mappings or (current.slot_count != 1 and current.slots_bound_by_record):
        return mappings
    spool = (state or {}).get("spool") or {}
    material = spool.get("material")
    if not isinstance(material, str) or not material.strip():
        return {}
    # The spool's recorded label is its color name; "loaded" names it without inventing a color.
    color = next((v.strip() for v in (spool.get("color"), spool.get("name")) if isinstance(v, str) and v.strip()),
                 "loaded")
    return {"1": {"color": color, "material": material.strip(), "spoolman_id": spool.get("spoolman_id")}}


def resolve_filament_selection(state, selection):
    """Resolve metadata from the requested slot, never the globally active spool.

    Configured slot labels are consistency checks, not proof of physical loading.
    An unmapped slot requires an explicit spool ID rather than an inferred one. A single-extruder
    printer's slot whose spool has no Spoolman binding needs none (#263); Spoolman is advisory (#235).
    """
    limit = position_limit()
    if not isinstance(selection, dict):
        return validate_filament_selection(selection, limit=limit)
    selection = dict(selection)
    slot = selection.get("slot")
    if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= limit:
        raise ValueError(f"filament slot must be an integer from 1 through {limit}")
    mappings = configured_slots(state)
    configured = mappings.get(str(slot), {}) if isinstance(mappings, dict) else {}
    if not isinstance(configured, dict):
        raise ValueError(f"slot {slot} configured mapping is invalid")
    mapped_id = configured.get("spoolman_id")
    # "Unbound" = the slot's spool is the printer's one configured spool (the
    # configured_slots synthesis), not a per-slot record binding: a printer
    # whose slots are not bound in the record (single-extruder models, and the
    # Bambu AMS models since #671) needs no Spoolman ID for it.
    unbound = bool(configured) and mapped_id is None and (
        printers.current().slot_count == 1 or not printers.current().slots_bound_by_record)
    if selection.get("spoolman_id") is None:
        if mapped_id is None and not unbound:
            raise ValueError(f"slot {slot} requires an explicit Spoolman spool ID; no configured mapping")
        selection["spoolman_id"] = mapped_id
    for key in ("color", "material"):
        if selection.get(key) is None and configured.get(key) is not None:
            selection[key] = configured[key]
    selection = validate_filament_selection(selection, spool_optional=unbound, limit=limit)
    if mapped_id is not None and selection["spoolman_id"] != mapped_id:
        raise ValueError(f"slot {slot} spool mismatch: requested #{selection['spoolman_id']}, configured #{mapped_id}")
    def same_selection(key, configured_value, chosen):
        if key == "color" and filament_color.parse_hex(configured_value) and filament_color.parse_hex(chosen):
            # Both are hex: #FFF, FFFFFF and FAFAFA are one color, 067749 and 0C6283 are not.
            return filament_color.compare_colors(configured_value, chosen) == "same"
        return " ".join(configured_value.strip().casefold().split()) == " ".join(chosen.strip().casefold().split())
    for key in ("color", "material"):
        value = configured.get(key)
        if value is not None and (not isinstance(value, str) or not same_selection(key, value, selection[key])):
            raise ValueError(f"slot {slot} {key} does not match the configured selection")
    return selection


def telemetry_state(value):
    """Classify raw telemetry without coercing strings, numbers, or null to booleans."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def read_filament_telemetry(state):
    """Read and label filament telemetry without triggering any printer action."""
    objects = urllib.parse.quote("&".join(FILAMENT_SENSOR_OBJECTS), safe="&")
    try:
        response = request_json(f"{state['moonraker_url']}/printer/objects/query?{objects}")
        status = response.get("result", {}).get("status", {}) if isinstance(response, dict) else {}
    except Exception as exc:
        status = {}
        query_error = f"{type(exc).__name__}: {exc}"
    else:
        query_error = None
    if not isinstance(status, dict):
        status = {}
        query_error = query_error or "invalid Moonraker status payload"
    sensor = status.get("filament_switch_sensor head_switch_sensor")
    color = status.get("zmod_color")
    slots = color.get("slots") if isinstance(color, dict) else None
    raw_zmod_slots = [
        {"ID": item.get("ID"), "hasFilament": item.get("hasFilament")}
        for item in slots if isinstance(item, dict)
    ] if isinstance(slots, list) else "unknown"
    ifs = status.get("zmod_ifs")
    ports = ifs.get("Ports") if isinstance(ifs, dict) else None
    variables_obj = status.get("save_variables")
    variables = variables_obj.get("variables") if isinstance(variables_obj, dict) else None
    mode = variables.get("display_off") if isinstance(variables, dict) else None
    if type(mode) is not int or mode not in (0, 1):
        mode = None
    mode_name = "native" if mode == 0 else "screenless" if mode == 1 else "unknown"
    stock_detail_error = None
    stock_detail_used = False
    if mode == 0:
        try:
            native_slots = read_stock_filament_slots(state)
            # Native authenticated /detail is authoritative for occupancy.
            apply_stock_presence(status, native_slots)
            slots = color.get("slots") if isinstance(color, dict) else None
            stock_detail_used = True
        except Exception as exc:
            # Preserve raw ZMod telemetry, but never present its known-stale
            # native-mode occupancy flags as evidence of empty or loaded slots.
            stock_detail_error = f"{type(exc).__name__}: {exc}"
            if isinstance(slots, list):
                for item in slots:
                    if isinstance(item, dict):
                        item["hasFilament"] = "unknown"
                        item["presence_source"] = "conflicted: stock /detail unavailable; raw zmod_color preserved separately"
    return {
        "queried_at_epoch": time.time(),
        "source": "Moonraker printer/objects/query",
        "printer_mode": mode_name,
        "display_off": mode if mode in (0, 1) else "unknown",
        "stock_detail_authoritative": stock_detail_used,
        "stock_detail_error": stock_detail_error,
        "raw_zmod_color_presence": raw_zmod_slots,
        "freshness_note": "Query time is recorded; the endpoint does not provide per-field sample timestamps, so underlying sensor freshness is unknown.",
        "query_error": query_error,
        "configured_palette": [
            {"ID": item.get("ID"), "Material": item.get("Material"), "Color": item.get("Color")}
            for item in slots if isinstance(item, dict)
        ] if isinstance(slots, list) else "unknown",
        "feeder_slots": [
            {"ID": item.get("ID"), "hasFilament": telemetry_state(item.get("hasFilament"))}
            for item in slots if isinstance(item, dict)
        ] if isinstance(slots, list) else "unknown",
        "feeder_ports_zero_based": [telemetry_state(port) for port in ports]
        if isinstance(ports, list) else "unknown",
        "feeder_slot_mapping": "zmod_color slot IDs are 1-based; zmod_ifs Ports list indices are 0-based",
        "ifs_enabled": telemetry_state(ifs.get("ifs")) if isinstance(ifs, dict) else "unknown",
        "silk_state": ifs.get("Silk") if isinstance(ifs, dict) else "unknown",
        "toolhead": {
            "source_object": "filament_switch_sensor head_switch_sensor",
            "filament_detected": telemetry_state(sensor.get("filament_detected"))
            if isinstance(sensor, dict) else "unknown",
            "enabled": telemetry_state(sensor.get("enabled")) if isinstance(sensor, dict) else "unknown",
        },
    }


# Stock firmware /detail readers live with the ZMod code; these delegate through the printer adapter.
def read_stock_detail(state, *, post=None):
    """Read native status using profile credentials without logging them."""
    return printers.current().native.read_stock_detail(state, post=post)


def read_stock_filament_slots_from_detail(detail):
    """Extract native slot presence from one already-fetched authenticated detail sample."""
    return printers.current().native.read_stock_filament_slots_from_detail(detail)


def read_stock_filament_slots(state, *, post=None):
    """Native presence, not the screen-mode hardcoded zmod_color flags."""
    return read_stock_filament_slots_from_detail(read_stock_detail(state, post=post))


def apply_stock_presence(status, stock):
    """Replace zmod_color hasFilament with stock presence; unknown stays unknown."""
    return printers.current().native.apply_stock_presence(status, stock)


def _validate_slot_selection(state, selection):
    """The selected IFS slot, not the globally active spool, owns a single-color job."""
    slot = state.get('filament_slots', {}).get(str(selection['slot']), {})
    mapping_matches = (slot.get('spoolman_id') and slot.get('spoolman_id') == selection['spoolman_id']
                       and str(slot.get('material', '')).casefold() == selection['material'].casefold())
    if mapping_matches and not slot.get('physical_color_verified'):
        # Only the color confirmation is missing: say so plainly and offer
        # the one-button fix instead of a retry that fails the same way.
        from ..core.slice_refusal import unconfirmed_slot_color
        raise unconfirmed_slot_color(
            selection['slot'], slot,
            f"selected IFS slot {selection['slot']} is unverified or its material/spool mapping differs")
    if (not slot.get('physical_color_verified') or not slot.get('spoolman_id')
            or slot.get('spoolman_id') != selection['spoolman_id']
            or str(slot.get('material', '')).casefold() != selection['material'].casefold()):
        # A different spool or material is never waved through: plain reason, no one-tap fix.
        from ..core.slice_refusal import slot_mismatch
        raise slot_mismatch(selection['slot'], slot, selection,
                            f"selected IFS slot {selection['slot']} is unverified or its material/spool mapping differs")
    return slot
