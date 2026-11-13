#!/usr/bin/env python3
"""Four-slot spool lifecycle, read-only reconciliation, and accounting receipts.

Owner: coding (Phase 2 MVP offline implementation).
Enforces:
1. Four-slot IFS boundary: only slots 1, 2, 3, 4 are authoritative.
2. Replacement workflow: records slot, material, color, quantity (unknown accepted),
   and verifies mapped Spoolman spool is non-archived. Never invents weights.
3. Spoolman history preservation: never alters or deletes historical spools or consumption.
4. Read-only reconciliation: audits native IFS (/detail, zmod_color), hardware state,
   Moonraker active spool pointer, and Spoolman. Reports drift clearly without auto-fixing.
5. Job receipts: creates per-job accounting receipts from slicer metadata and Moonraker
   history without claiming exact remaining weight when unknown.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .. import settings
from . import filament_color, spoolman_client

LOCAL_TZ = ZoneInfo(settings.current()["discord"]["timezone"])

VALID_FILAMENT_SLOTS = tuple(range(1, settings.current()["printer"]["filament_slots"] + 1))


def now_iso() -> str:
    """Return ISO-8601 timestamp in local New York time."""
    return datetime.now(LOCAL_TZ).isoformat()


def validate_ifs_slot(slot: int | str) -> int:
    """Validate that slot is within physical IFS boundary (1-4)."""
    try:
        slot_int = int(slot)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid IFS slot {slot!r}; must be integer 1, 2, 3, or 4") from exc
    if slot_int not in VALID_FILAMENT_SLOTS:
        raise ValueError(f"IFS slot {slot_int} out of bounds; physical IFS has slots {', '.join(map(str, VALID_FILAMENT_SLOTS))} only")
    return slot_int


def normalize_hex_color(hex_code: str | None) -> str | None:
    """Validate a hex code the user typed (any filament_color.parse_hex form) and return RRGGBB.

    Raises only for an explicit value that is not a hex color; None stays None.
    """
    if hex_code is None:
        return None
    cleaned = filament_color.normalize_hex(hex_code)
    if cleaned is None:
        raise ValueError(f"Invalid hex color {hex_code!r}; must be a hex code such as 0C6283, #0C6283 or #FFF")
    return cleaned


def validate_quantity(
    quantity_kind: str = "unknown",
    remaining_grams: float | None = None,
) -> tuple[float | None, str, str]:
    """Validate spool quantity.
    
    Accepts quantity as unknown without inventing weight.
    Returns (remaining_grams, quantity_status, weight_confidence).
    """
    kind = str(quantity_kind).strip().lower()
    if kind == "unknown":
        if remaining_grams is not None:
            raise ValueError("unknown quantity must not specify numeric grams")
        return None, "unknown", "unknown"
    if kind == "grams":
        if remaining_grams is None:
            raise ValueError("grams quantity requires numeric remaining_grams")
        try:
            val = float(remaining_grams)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid remaining_grams {remaining_grams!r}; must be finite positive float") from exc
        if not math.isfinite(val) or val <= 0:
            raise ValueError(f"remaining_grams must be finite and positive, got {remaining_grams}")
        return round(val, 2), "user_estimate", "user_estimate"
    if kind == "full":
        if remaining_grams is not None:
            try:
                val = float(remaining_grams)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid full spool grams {remaining_grams!r}") from exc
            if not math.isfinite(val) or val <= 0:
                raise ValueError(f"full spool grams must be finite and positive, got {remaining_grams}")
            return round(val, 2), "full_assumed", "assumed"
        # Omitted grams for full spool retains weight as unknown; do not silently invent 1000g
        return None, "unknown", "unknown"
    raise ValueError(f"Unsupported quantity_kind {quantity_kind!r}; must be 'unknown', 'grams', or 'full'")


def fetch_spoolman_spool(
    spoolman_id: int,
    *,
    client: Callable[[str], dict[str, Any]] | dict[int, dict[str, Any]] | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Fetch spool from Spoolman using injected client, dict lookup, or HTTP."""
    if not isinstance(spoolman_id, int) or isinstance(spoolman_id, bool) or spoolman_id <= 0:
        raise ValueError(f"Invalid spoolman_id {spoolman_id!r}; must be positive integer")
    if client is not None:
        if callable(client):
            return client(f"/api/v1/spool/{spoolman_id}")
        if isinstance(client, dict):
            if spoolman_id in client:
                return client[spoolman_id]
            raise RuntimeError(f"Spoolman spool #{spoolman_id} not found in client data")
    if base_url:
        spool = spoolman_client.get_spool(spoolman_id, base_url=base_url)
        if spool is None:
            raise RuntimeError(f"Spoolman spool #{spoolman_id} unavailable: Spoolman did not answer")
        return spool
    raise RuntimeError(f"Cannot resolve Spoolman spool #{spoolman_id}: no client or URL provided")


def validate_spoolman_mapping(
    spoolman_id: int,
    expected_material: str,
    *,
    spool_data: dict[str, Any] | None = None,
    client: Any = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Verify that the target Spoolman spool exists, is NOT archived, and material matches."""
    if spool_data is None:
        spool_data = fetch_spoolman_spool(spoolman_id, client=client, base_url=base_url)
    if spool_data.get("archived") is True:
        raise RuntimeError(f"Spoolman spool #{spoolman_id} is archived; cannot map to an active IFS slot")
    filament = spool_data.get("filament") or {}
    spool_material = str(filament.get("material") or spool_data.get("material") or "").strip()
    from .printer_state import same_material
    if spool_material and not same_material(spool_material, expected_material):
        raise RuntimeError(
            f"Spoolman spool #{spoolman_id} material '{spool_material}' does not match requested '{expected_material}'"
        )
    return spool_data


def replace_slot_spool(
    state: dict[str, Any],
    slot: int | str,
    *,
    material: str,
    color: str,
    spoolman_id: int,
    hex_code: str | None = None,
    spool_name: str | None = None,
    quantity_kind: str = "unknown",
    remaining_grams: float | None = None,
    verified_color: bool = True,
    verified_loading: bool = True,
    notes: str | None = None,
    spoolman_client: Any = None,
    spoolman_spool_data: dict[str, Any] | None = None,
    persist: bool = False,
) -> dict[str, Any]:
    """Execute the four-slot spool replacement workflow.
    
    Identifies target physical IFS slot (1-4), records material and color,
    accepts quantity as unknown, and binds to a non-archived Spoolman spool.
    Preserves all historical Spoolman data and modifies only the target slot.
    """
    slot_num = validate_ifs_slot(slot)
    material_clean = str(material).strip()
    if not material_clean:
        raise ValueError("material is required and cannot be empty")
    color_clean = str(color).strip()
    if not color_clean:
        raise ValueError("color is required and cannot be empty")
    hex_clean = normalize_hex_color(hex_code)
    qty_grams, qty_status, confidence = validate_quantity(quantity_kind, remaining_grams)

    spool_url = state.get("spoolman_url")
    spool_record = validate_spoolman_mapping(
        spoolman_id,
        material_clean,
        spool_data=spoolman_spool_data,
        client=spoolman_client,
        base_url=spool_url if spoolman_client is None else None,
    )

    resolved_name = spool_name
    if not resolved_name:
        filament = spool_record.get("filament") or {}
        resolved_name = filament.get("name") or spool_record.get("name") or f"{color_clean.title()} {material_clean}"

    filament_slots = state.setdefault("filament_slots", {})
    slot_key = str(slot_num)
    old_slot = copy.deepcopy(filament_slots.get(slot_key))

    # The same fallback order as every binder: the typed hex, Spoolman's color_hex,
    # its multi_color_hexes, then the name, then a loudly reported neutral default.
    filament = spool_record.get("filament") or {}
    hex_final, hex_source = filament_color.resolve(
        {"hex": hex_clean, "name": filament.get("name")}, spoolman_hex=filament.get("color_hex"),
        spoolman_multi=filament.get("multi_color_hexes"), name=color_clean, material=material_clean,
        warn_label=f"Slot {slot_num}")
    if (hex_source in filament_color.DERIVED_SOURCES and old_slot and old_slot.get("spoolman_id") == spoolman_id
            and filament_color.normalize_hex(old_slot.get("hex"))
            and old_slot.get("hex_source") not in filament_color.DERIVED_SOURCES):
        # Re-binding the same spool: keep the code it already had over a guess.
        hex_final, hex_source = filament_color.normalize_hex(old_slot["hex"]), old_slot.get("hex_source") or "slot"

    updated_slot = {
        "slot": slot_num,
        "material": material_clean,
        "color": color_clean.lower(),
        "hex": hex_final,
        "hex_source": hex_source,
        "spoolman_id": spoolman_id,
        "name": resolved_name,
        "remaining_grams": qty_grams,
        "quantity_status": qty_status,
        "weight_confidence": confidence,
        "physical_color_verified": bool(verified_color),
        "physical_loading_verified": bool(verified_loading),
        "updated_at": now_iso(),
        "verification_note": notes or f"Physical spool replacement mapped to IFS slot {slot_num}",
    }

    # Only the target slot is updated; other slots are untouched.
    filament_slots[slot_key] = updated_slot

    if persist:
        from . import printer_state
        printer_state.save(state)

    return {
        "status": "ok",
        "slot": slot_num,
        "previous_slot": old_slot,
        "slot_data": updated_slot,
        "spoolman_id": spoolman_id,
        "spoolman_name": resolved_name,
        "quantity_status": qty_status,
        "remaining_grams": qty_grams,
        "historical_mutation": False,
        "four_slot_boundary_enforced": True,
    }


def reconcile_four_slot_state(
    state: dict[str, Any],
    *,
    detail_data: dict[str, Any] | None = None,
    zmod_color_data: dict[str, Any] | None = None,
    moonraker_spool_id: int | None = None,
    spoolman_spools: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read-only reconciliation across native IFS state, hardware config, Moonraker, and Spoolman.
    
    Reports drift clearly and never auto-fixes it.
    """
    drifts: list[dict[str, Any]] = []
    slot_reports: dict[str, Any] = {}
    filament_slots = state.get("filament_slots") or {}

    # Audit physical four-slot boundary: detect legacy or out-of-range slots in config
    for raw_slot_key, raw_cfg in filament_slots.items():
        try:
            slot_int = int(raw_slot_key)
            if slot_int in VALID_FILAMENT_SLOTS:
                continue
        except (ValueError, TypeError):
            pass
        sid = raw_cfg.get("spoolman_id") if isinstance(raw_cfg, dict) else None
        drift = {
            "category": "out_of_range_slot_definition",
            "slot": str(raw_slot_key),
            "severity": "warning",
            "description": f"Hardware config contains non-physical or legacy slot entry '{raw_slot_key}' (spool #{sid}) outside valid IFS slots (1-4)",
            "sources": {"slot_entry": str(raw_slot_key), "config": raw_cfg},
        }
        drifts.append(drift)

    detail_slots: dict[int, bool | None] = {}
    if detail_data is not None:
        try:
            from .model_job import read_stock_filament_slots_from_detail
            detail_slots = read_stock_filament_slots_from_detail(detail_data)
        except Exception as exc:
            drifts.append({
                "category": "native_detail_unreadable",
                "slot": "global",
                "severity": "warning",
                "description": f"Native /detail parsing failed: {exc}",
                "sources": {"detail": str(exc)},
            })

    zmod_slots: dict[int, dict[str, Any]] = {}
    if zmod_color_data is not None:
        raw_zslots = zmod_color_data.get("slots") or []
        for item in raw_zslots:
            try:
                sid = int(item.get("ID"))
                zmod_slots[sid] = item
            except (TypeError, ValueError):
                continue

    # Reconcile each of the 4 authoritative physical slots
    for slot_id in VALID_FILAMENT_SLOTS:
        s_key = str(slot_id)
        cfg = filament_slots.get(s_key) or {}
        has_cfg = bool(cfg)

        detail_presence = detail_slots.get(slot_id)
        zmod_slot = zmod_slots.get(slot_id)
        zmod_presence = zmod_slot.get("hasFilament") if zmod_slot else None
        zmod_material = zmod_slot.get("Material") if zmod_slot else None
        zmod_color = zmod_slot.get("Color") if zmod_slot else None

        sid = cfg.get("spoolman_id")
        spool_record = spoolman_spools.get(sid) if (spoolman_spools and sid) else None

        slot_drifts = []

        # 1. Occupancy drift
        if detail_presence is not None:
            if detail_presence is False and cfg.get("physical_loading_verified") is True:
                drift = {
                    "category": "occupancy_drift",
                    "slot": slot_id,
                    "severity": "warning",
                    "description": f"Native /detail reports slot {slot_id} empty (hasFilament=false) but hardware config reports physical loading verified",
                    "sources": {"native_detail": False, "hardware_config": True},
                }
                drifts.append(drift)
                slot_drifts.append(drift)
            elif detail_presence is True and not has_cfg:
                drift = {
                    "category": "occupancy_drift",
                    "slot": slot_id,
                    "severity": "warning",
                    "description": f"Native /detail reports slot {slot_id} loaded but hardware config has no slot definition",
                    "sources": {"native_detail": True, "hardware_config": None},
                }
                drifts.append(drift)
                slot_drifts.append(drift)

        if zmod_presence is not None and detail_presence is not None and zmod_presence != detail_presence:
            drift = {
                "category": "occupancy_conflict",
                "slot": slot_id,
                "severity": "info",
                "description": f"Native /detail presence ({detail_presence}) conflicts with zmod_color presence ({zmod_presence}) for slot {slot_id}",
                "sources": {"native_detail": detail_presence, "zmod_color": zmod_presence},
            }
            drifts.append(drift)
            slot_drifts.append(drift)

        # 2. Material drift
        cfg_mat = cfg.get("material")
        if zmod_material and cfg_mat and zmod_material.casefold() != cfg_mat.casefold():
            drift = {
                "category": "material_drift",
                "slot": slot_id,
                "severity": "warning",
                "description": f"Slot {slot_id} material in zmod_color ('{zmod_material}') differs from hardware config ('{cfg_mat}')",
                "sources": {"zmod_color": zmod_material, "hardware_config": cfg_mat},
            }
            drifts.append(drift)
            slot_drifts.append(drift)

        if spool_record and cfg_mat:
            spool_mat = (spool_record.get("filament") or {}).get("material") or spool_record.get("material")
            from .printer_state import same_material
            if spool_mat and not same_material(spool_mat, cfg_mat):
                drift = {
                    "category": "material_drift",
                    "slot": slot_id,
                    "severity": "error",
                    "description": f"Slot {slot_id} Spoolman spool #{sid} material ('{spool_mat}') differs from hardware config ('{cfg_mat}')",
                    "sources": {"spoolman": spool_mat, "hardware_config": cfg_mat},
                }
                drifts.append(drift)
                slot_drifts.append(drift)

        # 3. Color drift
        cfg_color = cfg.get("color")
        if zmod_color and cfg_color and zmod_color.casefold() != cfg_color.casefold():
            drift = {
                "category": "color_drift",
                "slot": slot_id,
                "severity": "info",
                "description": f"Slot {slot_id} color in zmod_color ('{zmod_color}') differs from hardware config ('{cfg_color}')",
                "sources": {"zmod_color": zmod_color, "hardware_config": cfg_color},
            }
            drifts.append(drift)
            slot_drifts.append(drift)

        # 4. Spoolman status drift
        if sid and spoolman_spools is not None:
            if spool_record is None:
                drift = {
                    "category": "spoolman_missing",
                    "slot": slot_id,
                    "severity": "error",
                    "description": f"Slot {slot_id} maps to Spoolman spool #{sid} which does not exist in Spoolman inventory",
                    "sources": {"spoolman_id": sid},
                }
                drifts.append(drift)
                slot_drifts.append(drift)
            elif spool_record.get("archived") is True:
                drift = {
                    "category": "spoolman_archived_drift",
                    "slot": slot_id,
                    "severity": "error",
                    "description": f"Slot {slot_id} maps to Spoolman spool #{sid} which is archived",
                    "sources": {"spoolman_id": sid, "archived": True},
                }
                drifts.append(drift)
                slot_drifts.append(drift)
            else:
                loc = spool_record.get("location")
                if loc and f"slot {slot_id}" not in loc.lower() and f"slot{slot_id}" not in loc.lower() and f"ifs {slot_id}" not in loc.lower():
                    drift = {
                        "category": "location_drift",
                        "slot": slot_id,
                        "severity": "info",
                        "description": f"Slot {slot_id} Spoolman spool #{sid} location is '{loc}', expected slot {slot_id}",
                        "sources": {"spoolman_location": loc, "expected_slot": slot_id},
                    }
                    drifts.append(drift)
                    slot_drifts.append(drift)

        slot_reports[s_key] = {
            "slot": slot_id,
            "configured": has_cfg,
            "spoolman_id": sid,
            "material": cfg_mat,
            "color": cfg_color,
            "native_detail_presence": detail_presence,
            "zmod_presence": zmod_presence,
            "drifts": slot_drifts,
        }

    # 5. Moonraker active spool pointer reconciliation
    active_drifts = []
    hw_active = (state.get("spool") or {}).get("spoolman_id")
    if moonraker_spool_id is not None:
        if hw_active is not None and moonraker_spool_id != hw_active:
            drift = {
                "category": "active_spool_pointer_drift",
                "slot": "active_spool",
                "severity": "warning",
                "description": f"Moonraker active spool pointer (#{moonraker_spool_id}) differs from hardware state active spool (#{hw_active})",
                "sources": {"moonraker": moonraker_spool_id, "hardware_state": hw_active},
            }
            drifts.append(drift)
            active_drifts.append(drift)

        # Check if active pointer corresponds to any configured physical slot (1-4 only)
        mapped_sids = {}
        for slot_id in VALID_FILAMENT_SLOTS:
            s_key = str(slot_id)
            cfg = filament_slots.get(s_key) or filament_slots.get(slot_id)
            if isinstance(cfg, dict) and cfg.get("spoolman_id"):
                mapped_sids[cfg["spoolman_id"]] = slot_id

        if moonraker_spool_id not in mapped_sids:
            drift = {
                "category": "active_spool_unmapped_slot",
                "slot": "active_spool",
                "severity": "info",
                "description": f"Moonraker active spool #{moonraker_spool_id} is not mapped to any currently loaded physical IFS slot ({list(mapped_sids.keys())})",
                "sources": {"moonraker_spool_id": moonraker_spool_id, "loaded_spools": list(mapped_sids.keys())},
            }
            drifts.append(drift)
            active_drifts.append(drift)

    return {
        "timestamp": now_iso(),
        "drift_detected": len(drifts) > 0,
        "drift_count": len(drifts),
        "drifts": drifts,
        "slot_reports": slot_reports,
        "active_spool": {
            "moonraker_pointer": moonraker_spool_id,
            "hardware_state": hw_active,
            "drifts": active_drifts,
        },
        "mutation_performed": False,
        "read_only": True,
    }


def parse_slicer_metadata(gcode_text: str) -> dict[str, Any]:
    """Extract slicer metadata comments from OrcaSlicer / BambuStudio G-code."""
    meta: dict[str, Any] = {
        "total_filament_used_g": None,
        "total_filament_used_mm": None,
        "total_filament_used_cm3": None,
        "filament_used_g_list": [],
        "estimated_time_str": None,
        "tool_mapping": None,
        "tool_spool_ids": {},
    }

    # Estimated printing time
    m_time = re.search(r"^;\s*estimated printing time \(normal mode\)\s*=\s*(.+)$", gcode_text, re.M)
    if m_time:
        meta["estimated_time_str"] = m_time.group(1).strip()

    # Total filament used [g]
    m_g = re.search(r"^;\s*total filament used \[g\]\s*=\s*([\d.]+)", gcode_text, re.M)
    if m_g:
        meta["total_filament_used_g"] = float(m_g.group(1))

    # Total filament used [mm]
    m_mm = re.search(r"^;\s*total filament used \[mm\]\s*=\s*([\d.]+)", gcode_text, re.M)
    if not m_mm:
        m_mm = re.search(r"^;\s*filament used \[mm\]\s*=\s*([\d.]+)", gcode_text, re.M)
    if m_mm:
        meta["total_filament_used_mm"] = float(m_mm.group(1))

    # Total filament used [cm3]
    m_cm3 = re.search(r"^;\s*filament used \[cm3\]\s*=\s*([\d.]+)", gcode_text, re.M)
    if m_cm3:
        meta["total_filament_used_cm3"] = float(m_cm3.group(1))

    # Bambu Studio writes its usage totals ONLY in the header block, with
    # different spellings and a colon separator (verified against a real
    # Bambu Studio 2.8.2.61 A1 slice; #444). OrcaSlicer/Flash files never
    # contain these lines, so these fallbacks cannot change their metadata.
    if meta["total_filament_used_g"] is None:
        m_bambu_g = re.search(r"^;\s*total filament weight \[g\]\s*:\s*([\d.]+)", gcode_text, re.M)
        if m_bambu_g:
            meta["total_filament_used_g"] = float(m_bambu_g.group(1))
    if meta["total_filament_used_mm"] is None:
        m_bambu_mm = re.search(r"^;\s*total filament length \[mm\]\s*:\s*([\d.]+)", gcode_text, re.M)
        if m_bambu_mm:
            meta["total_filament_used_mm"] = float(m_bambu_mm.group(1))
    if meta["total_filament_used_cm3"] is None:
        m_bambu_cm3 = re.search(r"^;\s*total filament volume \[cm\^3\]\s*:\s*([\d.]+)", gcode_text, re.M)
        if m_bambu_cm3:
            # The header labels this value [cm^3] but the number is mm3: in
            # the real slice it equals filament length x cross-section
            # (1263.89 mm -> 3040.0) and weight / density (3.77 g / 1.24 =
            # 3.04 cm3), so convert to the cm3 this field holds.
            meta["total_filament_used_cm3"] = float(m_bambu_cm3.group(1)) / 1000.0
    if meta["estimated_time_str"] is None and re.search(r"^; BambuStudio ", gcode_text, re.M):
        # Both BBL slicers write the header totals line with the same
        # spelling, so only Bambu-identified files take this fallback and
        # OrcaSlicer metadata stays byte-identical.
        m_bambu_time = re.search(r"^;.*?total estimated time\s*:\s*([^;\n]+)", gcode_text, re.M)
        if m_bambu_time:
            meta["estimated_time_str"] = m_bambu_time.group(1).strip()
    # Bambu Studio writes no per-filament used lines anywhere in the file
    # (full-file check of real one- and two-filament 2.8.2.61 slices), so
    # filament_used_g_list keeps whatever the Orca spellings above found.

    # Per-filament used [g] list
    m_g_list = re.search(r"^;\s*filament used \[g\]\s*=\s*([0-9.,\s]+)", gcode_text, re.M)
    if m_g_list:
        try:
            meta["filament_used_g_list"] = [float(x.strip()) for x in m_g_list.group(1).split(",") if x.strip()]
        except ValueError:
            pass

    # Tool mapping comment if present. The hermes_tool_slots anchor is a frozen
    # format: see docs/PROJECT-REFERENCE.md, Frozen Hermes wire names.
    m_map = re.search(r"^;\s*hermes_tool_slots\s*=\s*(.+)$", gcode_text, re.M)
    if m_map:
        try:
            parsed_map = json.loads(m_map.group(1).strip())
            meta["tool_mapping"] = {str(tool): value.get('physical_slot') if isinstance(value, dict) else value
                                    for tool, value in parsed_map.items()}
            meta["tool_spool_ids"] = {str(tool): value['spoolman_id'] for tool, value in parsed_map.items()
                                      if isinstance(value, dict) and value.get('spoolman_id') is not None}
        except (ValueError, TypeError, AttributeError):
            pass

    return meta


def generate_job_accounting_receipt(
    *,
    job_manifest: dict[str, Any] | None = None,
    gcode_text: str | None = None,
    gcode_path: str | Path | None = None,
    state: dict[str, Any] | None = None,
    spoolman_spools: dict[int, dict[str, Any]] | None = None,
    moonraker_history_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a per-job accounting receipt without claiming exact remaining spool weight.
    
    Unknown values remain unknown. Preserves Spoolman history without mutation.
    """
    manifest = job_manifest or {}
    st = state or {}
    filament_slots = st.get("filament_slots") or {}

    raw_gcode = gcode_text
    if raw_gcode is None and gcode_path is not None:
        p = Path(gcode_path)
        if p.exists():
            raw_gcode = p.read_text(encoding="utf-8", errors="replace")

    slicer_meta = parse_slicer_metadata(raw_gcode) if raw_gcode else {}

    # Determine tool to slot mapping
    tool_map = slicer_meta.get("tool_mapping")
    if not tool_map and manifest.get("tool_to_physical_slot"):
        tool_map = manifest["tool_to_physical_slot"]

    selected_slot = None
    if not tool_map:
        sel = manifest.get("filament_selection") or {}
        if isinstance(sel, dict) and sel.get("slot") is not None:
            selected_slot = validate_ifs_slot(sel["slot"])
            tool_map = {"0": selected_slot}
        else:
            # Fallback to active spool slot ONLY if mapped to a valid physical IFS slot (1-4)
            active_sid = (st.get("spool") or {}).get("spoolman_id")
            if active_sid is not None:
                for s_str, slot_data in filament_slots.items():
                    try:
                        s_int = validate_ifs_slot(s_str)
                    except (ValueError, TypeError):
                        continue
                    if isinstance(slot_data, dict) and slot_data.get("spoolman_id") == active_sid:
                        selected_slot = s_int
                        tool_map = {"0": selected_slot}
                        break

    # Determine total filament usage from slicer or history
    total_est_g = slicer_meta.get("total_filament_used_g")
    total_est_mm = slicer_meta.get("total_filament_used_mm")
    per_tool_g = slicer_meta.get("filament_used_g_list") or []

    # Moonraker actual history if available
    hist = moonraker_history_job or {}
    hist_filament_mm = hist.get("filament_used")
    actual_total_g = None
    if hist_filament_mm is not None:
        try:
            mm = float(hist_filament_mm)
            density = float((st.get("spool") or {}).get("density", 1.24))
            diam = float((st.get("spool") or {}).get("diameter_mm", 1.75))
            vol_mm3 = math.pi * ((diam / 2.0) ** 2) * mm
            actual_total_g = round(vol_mm3 * (density / 1000.0), 2)
        except (ValueError, TypeError):
            pass

    # Build per-slot accounting entries
    slot_entries: list[dict[str, Any]] = []
    if tool_map:
        tools_sorted = sorted(tool_map.keys(), key=lambda x: int(x) if str(x).isdigit() else str(x))

        for idx, tool in enumerate(tools_sorted):
            slot_num = validate_ifs_slot(tool_map[tool])
            s_key = str(slot_num)
            slot_cfg = filament_slots.get(s_key) or {}

            sid = slot_cfg.get("spoolman_id")
            spool_rec = spoolman_spools.get(sid) if (spoolman_spools and sid) else None

            # Estimated grams used by this tool/slot
            tool_est_g = None
            if len(per_tool_g) > idx:
                tool_est_g = per_tool_g[idx]
            elif len(tool_map) == 1 and total_est_g is not None:
                tool_est_g = total_est_g

            # Initial weight from Spoolman or hardware config
            initial_weight_g = None
            weight_conf = "unknown"
            if spool_rec:
                rem = spool_rec.get("remaining_weight")
                if rem is not None and math.isfinite(float(rem)) and float(rem) > 0:
                    initial_weight_g = round(float(rem), 2)
                    weight_conf = "spoolman_recorded"
            elif slot_cfg.get("remaining_grams") is not None:
                initial_weight_g = float(slot_cfg["remaining_grams"])
                weight_conf = slot_cfg.get("weight_confidence", "user_estimate")

            # Projected remaining weight
            est_remaining_g = None
            if initial_weight_g is not None and tool_est_g is not None:
                est_remaining_g = round(initial_weight_g - tool_est_g, 2)

            entry = {
                "logical_tool": tool,
                "ifs_slot": slot_num,
                "mapping_status": "mapped",
                "spoolman_id": sid,
                "spool_name": slot_cfg.get("name") or (spool_rec.get("name") if spool_rec else None),
                "material": slot_cfg.get("material") or (spool_rec.get("material") if spool_rec else None),
                "color": slot_cfg.get("color"),
                "hex": slot_cfg.get("hex"),
                "estimated_used_g": tool_est_g,
                "estimated_used_mm": total_est_mm if len(tool_map) == 1 else None,
                "actual_used_g": actual_total_g if len(tool_map) == 1 else None,
                "starting_spool_weight_g": initial_weight_g,
                "estimated_remaining_weight_g": est_remaining_g,
                "weight_confidence": weight_conf,
                "quantity_status": "known_estimate" if initial_weight_g is not None else "unknown",
            }
            slot_entries.append(entry)
    else:
        # Tool to physical slot mapping is unknown: represent clearly without fabricating slot 1
        entry = {
            "logical_tool": None,
            "ifs_slot": None,
            "mapping_status": "unknown",
            "spoolman_id": None,
            "spool_name": None,
            "material": None,
            "color": None,
            "hex": None,
            "estimated_used_g": total_est_g,
            "estimated_used_mm": total_est_mm,
            "actual_used_g": actual_total_g,
            "starting_spool_weight_g": None,
            "estimated_remaining_weight_g": None,
            "weight_confidence": "unknown",
            "quantity_status": "unknown",
        }
        slot_entries.append(entry)

    job_title = manifest.get("title") or (Path(gcode_path).stem if gcode_path else "job")
    receipt = {
        "receipt_id": f"receipt-{uuid.uuid4().hex[:12]}",
        "created_at": now_iso(),
        "job_title": job_title,
        "gcode_file": str(gcode_path) if gcode_path else None,
        "estimated_duration": slicer_meta.get("estimated_time_str"),
        "total_estimated_filament_g": total_est_g,
        "total_actual_filament_g": actual_total_g,
        "tool_mapping_status": "known" if tool_map else "unknown",
        "slots": slot_entries,
        "four_slot_boundary_enforced": True,
        "spoolman_mutation": False,
        "historical_mutation": False,
        "accounting_receipt_note": (
            "Per-job filament accounting receipt. Exact remaining spool weights are not claimed "
            "when unknown starting inventory was provided; unknown starting weight remains unknown."
        ),
    }
    return receipt


def fetch_native_detail(state: dict[str, Any], *, timeout: float = 3.0) -> dict[str, Any] | None:
    """Fetch native /detail if available (read-only telemetry)."""
    try:
        from .model_job import read_stock_detail
        return read_stock_detail(state)
    except Exception:
        return None


def fetch_moonraker_zmod_color(
    state: dict[str, Any],
    *,
    timeout: float = 3.0,
    client: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Query Moonraker for zmod_color status (read-only)."""
    mr_url = state.get("moonraker_url")
    if not mr_url:
        return None
    try:
        if client:
            data = client(f"{mr_url.rstrip('/')}/printer/objects/query?zmod_color")
        else:
            import urllib.request
            url = f"{mr_url.rstrip('/')}/printer/objects/query?zmod_color"
            req = urllib.request.Request(url, headers={"User-Agent": "Hermes-SpoolLifecycle/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        return (data.get("result") or {}).get("status", {}).get("zmod_color")
    except Exception:
        return None


def fetch_moonraker_active_spool(
    state: dict[str, Any],
    *,
    timeout: float = 3.0,
    client: Callable[[str], dict[str, Any]] | None = None,
) -> int | None:
    """Query Moonraker for active spool ID pointer (read-only)."""
    mr_url = state.get("moonraker_url")
    if not mr_url:
        return None
    try:
        if client:
            data = client(f"{mr_url.rstrip('/')}/server/spoolman/spool_id")
        else:
            import urllib.request
            url = f"{mr_url.rstrip('/')}/server/spoolman/spool_id"
            req = urllib.request.Request(url, headers={"User-Agent": "Hermes-SpoolLifecycle/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        return (data.get("result") or {}).get("spool_id")
    except Exception:
        return None


def fetch_all_spoolman_spools(
    state: dict[str, Any],
    *,
    timeout: float = 3.0,
    client: Callable[[str], Any] | None = None,
) -> dict[int, dict[str, Any]] | None:
    """Fetch all spools from Spoolman and index by integer spool ID (read-only)."""
    sm_url = state.get("spoolman_url")
    if not sm_url:
        return None
    try:
        if client:
            data = client(f"{sm_url.rstrip('/')}/api/v1/spool")
        else:
            data = spoolman_client.list_spools(base_url=sm_url, timeout=timeout)
        if isinstance(data, list):
            spools = {}
            for item in data:
                if isinstance(item, dict) and "id" in item:
                    try:
                        spools[int(item["id"])] = item
                    except (ValueError, TypeError):
                        pass
            return spools
        if isinstance(data, dict):
            return {int(k): v for k, v in data.items() if str(k).isdigit() and isinstance(v, dict)}
    except Exception:
        pass
    return None


def collect_reconciliation_sources(
    state: dict[str, Any],
    *,
    timeout: float = 3.0,
    moonraker_client: Callable[[str], dict[str, Any]] | None = None,
    spoolman_client: Callable[[str], Any] | None = None,
    detail_reader: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Collect read-only telemetry across native /detail, zmod_color, Moonraker, and Spoolman."""
    detail_data = None
    if detail_reader:
        try:
            detail_data = detail_reader(state)
        except Exception:
            pass
    else:
        detail_data = fetch_native_detail(state, timeout=timeout)

    zmod_color_data = fetch_moonraker_zmod_color(state, timeout=timeout, client=moonraker_client)
    mr_spool_id = fetch_moonraker_active_spool(state, timeout=timeout, client=moonraker_client)
    spoolman_spools = fetch_all_spoolman_spools(state, timeout=timeout, client=spoolman_client)

    return {
        "detail_data": detail_data,
        "zmod_color_data": zmod_color_data,
        "moonraker_spool_id": mr_spool_id,
        "spoolman_spools": spoolman_spools,
    }


def main():
    ap = argparse.ArgumentParser(description="Four-slot spool lifecycle, reconciliation, and receipt management.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # replace
    p_rep = sub.add_parser("replace", help="Record physical spool replacement in an IFS slot")
    p_rep.add_argument("--slot", type=int, required=True, choices=list(VALID_FILAMENT_SLOTS), help=f"Target IFS slot (1-{VALID_FILAMENT_SLOTS[-1]})")
    p_rep.add_argument("--material", required=True, help="Filament material (e.g. PLA, PETG)")
    p_rep.add_argument("--color", required=True, help="Filament color name")
    p_rep.add_argument("--spoolman-id", type=int, required=True, help="Non-archived Spoolman spool ID")
    p_rep.add_argument("--hex", dest="hex_code", help="Optional 6-character hex RGB")
    p_rep.add_argument("--name", dest="spool_name", help="Optional human-readable spool label")
    p_rep.add_argument("--quantity", choices=["unknown", "grams", "full"], default="unknown", help="Spool quantity kind")
    p_rep.add_argument("--grams", type=float, default=None, help="Numeric remaining grams (if quantity is grams)")
    p_rep.add_argument("--persist", action="store_true", help="Save updated state to printer_hardware.json")

    # reconcile
    p_rec = sub.add_parser("reconcile", help="Read-only reconciliation across native IFS, hardware config, Moonraker, and Spoolman")
    p_rec.add_argument("--state-file", help="Path to printer_hardware.json (default: live profile state)")
    p_rec.add_argument("--offline", action="store_true", help="Skip live queries; reconcile only provided/local state")
    p_rec.add_argument("--detail-file", help="Path to JSON file with native /detail response")
    p_rec.add_argument("--zmod-file", help="Path to JSON file with Moonraker zmod_color object query")
    p_rec.add_argument("--spoolman-file", help="Path to JSON file with Spoolman spools list or dict")
    p_rec.add_argument("--moonraker-spool-id", type=int, help="Override Moonraker active spool ID")
    p_rec.add_argument("--timeout", type=float, default=3.0, help="HTTP timeout in seconds (default: 3.0)")

    # receipt
    p_rcpt = sub.add_parser("receipt", help="Generate per-job accounting receipt from G-code and job metadata")
    p_rcpt.add_argument("--gcode", help="Path to G-code file")
    p_rcpt.add_argument("--job-json", help="Path to JOB.json file")

    args = ap.parse_args()

    from . import printer_state

    if args.cmd == "replace":
        st = printer_state.load()
        result = replace_slot_spool(
            st,
            args.slot,
            material=args.material,
            color=args.color,
            spoolman_id=args.spoolman_id,
            hex_code=args.hex_code,
            spool_name=args.spool_name,
            quantity_kind=args.quantity,
            remaining_grams=args.grams,
            persist=args.persist,
        )
        print(json.dumps(result, indent=2))
        return

    if args.cmd == "reconcile":
        if args.state_file:
            st = filament_color.adopt_renamed_slots_key(json.loads(Path(args.state_file).read_text()))
        else:
            st = printer_state.load()

        detail_data = json.loads(Path(args.detail_file).read_text()) if args.detail_file else None
        zmod_color_data = None
        if args.zmod_file:
            raw_zmod = json.loads(Path(args.zmod_file).read_text())
            zmod_color_data = (raw_zmod.get("result") or {}).get("status", {}).get("zmod_color") if "result" in raw_zmod else raw_zmod
        spoolman_spools = None
        if args.spoolman_file:
            raw_sm = json.loads(Path(args.spoolman_file).read_text())
            if isinstance(raw_sm, list):
                spoolman_spools = {int(s["id"]): s for s in raw_sm if isinstance(s, dict) and "id" in s}
            elif isinstance(raw_sm, dict):
                spoolman_spools = {int(k): v for k, v in raw_sm.items() if str(k).isdigit() and isinstance(v, dict)}
        mr_spool_id = args.moonraker_spool_id

        if not args.offline:
            live = collect_reconciliation_sources(st, timeout=args.timeout)
            if detail_data is None:
                detail_data = live.get("detail_data")
            if zmod_color_data is None:
                zmod_color_data = live.get("zmod_color_data")
            if mr_spool_id is None:
                mr_spool_id = live.get("moonraker_spool_id")
            if spoolman_spools is None:
                spoolman_spools = live.get("spoolman_spools")

        report = reconcile_four_slot_state(
            st,
            detail_data=detail_data,
            zmod_color_data=zmod_color_data,
            moonraker_spool_id=mr_spool_id,
            spoolman_spools=spoolman_spools,
        )
        print(json.dumps(report, indent=2))
        return

    if args.cmd == "receipt":
        st = printer_state.load()
        job_data = json.loads(Path(args.job_json).read_text()) if args.job_json else None
        receipt = generate_job_accounting_receipt(
            job_manifest=job_data,
            gcode_path=args.gcode,
            state=st,
        )
        print(json.dumps(receipt, indent=2))
        return


if __name__ == "__main__":
    main()
