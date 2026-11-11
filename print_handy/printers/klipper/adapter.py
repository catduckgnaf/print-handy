"""Stock Klipper + Moonraker: one extruder, no vendor macros, no vendor API.

Deliberately imports nothing from the ZMod/AD5X code (printers.flashforge.zmod, spool_sync).
Multi-color jobs are refused rather than approximated.

A configured Bondtech INDX accessory (#1198, printers/accessories.py) changes
this adapter's effective shape to shared_tool: N passive tools on the one
shared extruder and heater. Without the accessory every behavior below is
exactly the stock single-extruder behavior.
"""
import re

from ..base import MultiColorUnsupported, PrinterAdapter, basic_status

_PLAIN_TOOL_CHANGE = re.compile(r"^\s*T(\d+)\b", re.M)


class KlipperAdapter(PrinterAdapter):

    @property
    def _indx_entry(self):
        """The configured INDX accessory entry, or None when not fitted."""
        if not self.shared_tool:
            return None
        from ..accessories import indx_entry
        return indx_entry(self.config)

    @property
    def tool_count(self):
        """Tools on this printer: the INDX accessory's count, else 1."""
        entry = self._indx_entry
        if entry is not None:
            from ..accessories import indx_tool_count
            return indx_tool_count(entry) or 1
        return 1

    @property
    def slot_count(self):
        # With an INDX accessory the filament positions are its tools; without
        # one this stays the stock single position.
        return self.tool_count if self.shared_tool else 1

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
        if self.shared_tool:
            # The identity tool contract: slicer tool T(n) always prints with
            # tool n, recorded as position n + 1, exactly like a multi_tool
            # toolhead map but for the accessory's passive tools.
            out = {}
            for tool, position in (mapping or {}).items():
                try:
                    t, p = int(tool), int(position)
                except (TypeError, ValueError):
                    raise MultiColorUnsupported(
                        f"{self.label} tool mapping {tool!r} -> {position!r} is not a tool number") from None
                if not 0 <= t < self.tool_count:
                    raise MultiColorUnsupported(
                        f"{self.label} has {self.tool_count} INDX tools (T0-T{self.tool_count - 1}); "
                        f"this job uses tool T{t}. Slice it for this printer or use one with more tools.")
                if p != t + 1:
                    raise MultiColorUnsupported(
                        f"{self.label} tool T{t} always prints with INDX tool {t + 1}; "
                        f"a mapping to position {p} cannot be represented.")
                out[str(t)] = p
            return out or {"0": 1}
        tools = {str(k) for k in (mapping or {"0": 1})}
        if tools != {"0"}:
            raise MultiColorUnsupported(
                f"{self.label} has a single extruder; this job uses tools {sorted(tools)}. "
                "Slice it single-color or use a multi-slot printer.")
        return {"0": 1}

    def _indx_variables(self, raw):
        """The variables an INDX refusal or active-tool read needs, merged
        from the Moonraker status shapes that carry them: save_variables for
        live state, and the TOOL_POSITIONS macro record (live object or
        configfile settings) for docks_calibrated."""
        raw = raw if isinstance(raw, dict) else {}
        variables = {}
        save = raw.get("save_variables")
        if isinstance(save, dict) and isinstance(save.get("variables"), dict):
            variables.update(save["variables"])
        for holder in (raw.get("gcode_macro TOOL_POSITIONS"),
                       raw.get("gcode_macro tool_positions")):
            if isinstance(holder, dict):
                variables.update(holder)
        configfile = raw.get("configfile")
        settings = configfile.get("settings") if isinstance(configfile, dict) else None
        if isinstance(settings, dict):
            for key, value in settings.items():
                if isinstance(key, str) and key.lower() == "gcode_macro tool_positions" \
                        and isinstance(value, dict):
                    if "docks_calibrated" in value:
                        variables["docks_calibrated"] = value["docks_calibrated"]
        return variables

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
        if self.shared_tool:
            # status here is a raw Moonraker status dict on some paths and a
            # normalized one on others; the variables live on the raw shape,
            # and a normalized dict carries the refusals under its own key,
            # so both are read.
            from ..accessories import indx_refusals
            problems.extend(indx_refusals(self._indx_variables(status)))
            for refusal in status.get("indx_refusals") or []:
                if refusal not in problems:
                    problems.append(refusal)
        return problems

    def gcode_problems(self, text, state=None):
        if not self.shared_tool:
            return []
        # A multi-tool INDX job must change tools through the INDX macro,
        # which picks the tool up before heating it to its own filament's
        # temperature. Plain T<n> output sliced the filament-switcher way
        # carries neither the pickup nor the temperature.
        tools_used = {int(match.group(1)) for match in _PLAIN_TOOL_CHANGE.finditer(text or "")}
        if len(tools_used) > 1 and "CHANGE_TOOL" not in (text or ""):
            return [f"{self.label} has a Bondtech INDX toolchanger; this G-code changes tools "
                    f"with plain T commands ({', '.join(f'T{t}' for t in sorted(tools_used))}) and no "
                    "CHANGE_TOOL, so no tool would be picked up or heated. Slice it with the INDX "
                    "tool change G-code (CHANGE_TOOL TOOL={next_extruder} "
                    "TEMP={new_filament_temp})"]
        return []

    def moonraker_objects(self):
        objects = ["webhooks", "print_stats", "virtual_sdcard", "extruder", "heater_bed",
                   "exclude_object"]
        if self.shared_tool:
            # save_variables carries the active tool and the seat_suspect
            # refusal; configfile carries the INDX detection record and
            # docks_calibrated. There is no per-tool temperature object:
            # the one extruder temperature is the active tool's.
            objects += ["save_variables", "configfile"]
        return objects

    def normalize_status(self, raw):
        out = basic_status(raw)
        if self.shared_tool:
            from ..accessories import active_tool, indx_refusals
            variables = self._indx_variables(raw)
            out["active_tool"] = active_tool(variables)
            out["indx_refusals"] = indx_refusals(variables)
        return out
