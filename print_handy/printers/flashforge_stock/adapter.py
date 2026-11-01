"""Stock Flashforge adapter: LAN HTTP for status, upload, and start. One tool, four IFS bays.

Reads /detail, including IFS slot occupancy, uploads one .gcode with print disabled,
and starts that file only through print_gcode. It never sends a temperature, a fan
command, or a Klipper macro. Multicolor and Moonraker are refused: stock start is
one tool (T0) and this firmware does not speak Moonraker. Slot presence is read
from /detail and is not verified on a live printer.
"""
from __future__ import annotations

import os

from ..base import MultiColorUnsupported, PrinterAdapter
from .client import StockClient, StockLanError
from .guard import stock_gcode_problems


class StockFlashforgeAdapter(PrinterAdapter):
    """One tool on stock firmware, with IFS slot occupancy, over the Flashforge LAN HTTP API."""

    transport = "flashforge_stock"
    has_native_screen = False
    # The four IFS bays are bound to spools in the hardware record's slot map,
    # like the ZMod AD5X: a stock print requires a record-bound selection.
    slots_bound_by_record = True
    verified_slicers = ("orca", "flash_studio")

    @property
    def slot_count(self):
        return int(self.config["printer"]["filament_slots"])

    def client(self):
        """The LAN client. Missing host or credentials raise before any socket opens."""
        section = (self.config or {}).get("flashforge_stock") or {}
        host = str(section.get("host") or "").strip()
        if not host:
            raise StockLanError("stock LAN host is not configured ([flashforge_stock] host)")
        if not os.environ.get("AD5X_SERIAL", "").strip() or not os.environ.get("AD5X_CHECK_CODE", "").strip():
            raise StockLanError("stock LAN credentials missing (AD5X_SERIAL, AD5X_CHECK_CODE)")
        return StockClient(host)

    def control(self, action):
        """Pause, resume or stop the current job over stock LAN (jobCtl_cmd).

        Protocol facts from the same public docs the client cites
        (Parallel-7/flashforge-api-docs, HTTP REST API): /control carries
        cmd "jobCtl_cmd" — "Manages the current print job (pause, resume,
        cancel)" — with args {"jobID": "", "action": ...}; the resume
        action is spelled "continue" there. The shared control vocabulary
        (pause/resume/stop) is translated here so callers never see the
        firmware's spelling.
        """
        actions = {"pause": "pause", "resume": "continue", "stop": "cancel"}
        if action not in actions:
            raise StockLanError(f"unsupported stock LAN control action: {action!r}")
        return self.client().control("jobCtl_cmd", {"jobID": "", "action": actions[action]})

    def machine_start_gcode(self, template_start, mesh="auto"):
        return template_start  # the slicer owns the start sequence; Print Handy adds nothing

    def machine_end_gcode(self, template_end):
        return template_end

    def start_plan(self, bed_temp, nozzle_temp, *, mesh="auto"):
        return []  # stock firmware heats from the slicer G-code, not from a Print Handy preamble

    def moonraker_start_commands(self, remote_filename):
        raise StockLanError("Moonraker start is unsupported for stock Flashforge firmware")

    def moonraker_objects(self):
        raise StockLanError("Moonraker objects are unsupported for stock Flashforge firmware")

    def gcode_problems(self, text, state=None):
        return stock_gcode_problems(text)

    def normalize_tool_mapping(self, mapping, state):
        # One tool only. IFS bays are physical slots for that tool, not extra tools.
        if set(mapping) != {"0"}:
            raise MultiColorUnsupported(f"{self.label} starts one tool (T0); multicolor is not supported")
        slot = mapping["0"]
        if type(slot) is not int or not 1 <= slot <= self.slot_count:
            raise MultiColorUnsupported(f"{self.label} tool T0 must map to IFS slot 1 through {self.slot_count}")
        return {"0": slot}

    def readiness_problems(self, state, status, selection=None, **kw):
        reduced = self.normalize_status(status or {})
        if reduced["state"] not in ("standby", "complete", "cancelled"):
            return [f"printer state is {reduced['state']!r}, not idle"]
        return []

    def normalize_status(self, raw):
        detail = raw.get("detail") if isinstance(raw, dict) and isinstance(raw.get("detail"), dict) else raw
        detail = detail if isinstance(detail, dict) else {}
        reduced = StockClient.status_of(detail)
        # Targets use the /detail names the ZMod native idle gate already reads (the AD5X
        # nozzle is the "right" one). Layer and remaining-time fields are not known here.
        return {**reduced, "hotend_c": None, "bed_c": None,
                "hotend_target_c": _number(detail.get("rightTargetTemp")),
                "bed_target_c": _number(detail.get("platTargetTemp"))}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def stock_api_url(self, host):
        return None  # the ZMod /detail poller must not run against this adapter
