"""Stock Klipper + Moonraker: one extruder, no vendor macros, no vendor API.

Deliberately imports nothing from the ZMod/AD5X code (printers.flashforge.zmod, spool_sync).
Multi-color jobs are refused rather than approximated.
"""
from ..base import MultiColorUnsupported, PrinterAdapter, basic_status


class KlipperAdapter(PrinterAdapter):

    @property
    def slot_count(self):
        return 1

    @property
    def start_macro(self):
        return self.config["printer"].get("start_macro", "PRINT_START")

    @property
    def end_macro(self):
        return self.config["printer"].get("end_macro", "PRINT_END")

    def machine_start_gcode(self, template_start, mesh="auto"):
        return (f"{self.start_macro} BED=[bed_temperature_initial_layer_single] "
                "HOTEND=[nozzle_temperature_initial_layer]\nG90\nM83\nG92 E0")

    def machine_end_gcode(self, template_end):
        return self.end_macro

    def start_plan(self, bed_temp, nozzle_temp, *, mesh="auto"):
        return [f"{self.start_macro} BED={bed_temp:g} HOTEND={nozzle_temp:g}", "G90", "M83", "G92 E0"]

    def moonraker_start_commands(self, remote_filename):
        return [{"method": "POST", "path": "/printer/print/start", "body": {"filename": remote_filename}}]

    def normalize_tool_mapping(self, mapping, state):
        tools = {str(k) for k in (mapping or {"0": 1})}
        if tools != {"0"}:
            raise MultiColorUnsupported(
                f"{self.label} has a single extruder; this job uses tools {sorted(tools)}. "
                "Slice it single-color or use a multi-slot printer.")
        return {"0": 1}

    def readiness_problems(self, state, status, selection=None, **kw):
        status = status if isinstance(status, dict) else {}
        problems = []
        pstate = (status.get("print_stats") or {}).get("state")
        if pstate is None:
            problems.append("print_stats state is unavailable")
        elif pstate in ("printing", "paused"):
            problems.append(f"printer is busy ({pstate})")
        webhooks = (status.get("webhooks") or {}).get("state")
        if webhooks not in (None, "ready"):
            problems.append(f"Klipper is not ready ({webhooks})")
        return problems

    def moonraker_objects(self):
        return ["webhooks", "print_stats", "virtual_sdcard", "extruder", "heater_bed",
                "exclude_object"]

    def normalize_status(self, raw):
        return basic_status(raw)
