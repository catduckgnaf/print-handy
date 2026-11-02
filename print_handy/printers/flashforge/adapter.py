"""Flashforge on ZMod: the AD5X adapter wraps the ZMod/IFS code paths
(printers.flashforge.zmod); the single-extruder adapter below shares only the
ZMod start code and Moonraker transport, with every IFS behaviour off; the
multi-tool adapter models the Creator toolheads as tools, not IFS slots."""
import re

from ..base import MultiColorUnsupported, PrinterAdapter, basic_status


class ZmodAdapter(PrinterAdapter):
    has_native_screen = True
    # The IFS slots are bound to spools in the hardware record's slot map:
    # intake, slicing and print-now validate selections against that map.
    slots_bound_by_record = True

    @property
    def slot_count(self):
        return int(self.config["printer"]["filament_slots"])

    @property
    def native(self):
        from . import zmod
        return zmod

    def machine_start_gcode(self, template_start, mesh="auto"):
        # Exactly what orca_adapter built inline before the adapter existed.
        start = re.sub(r"\bMESH=\S+", "MESH=" + mesh, template_start)
        return ("M190 S[bed_temperature_initial_layer_single]\n"
                "M104 S[nozzle_temperature_initial_layer] T0\n" + start)

    def machine_end_gcode(self, template_end):
        return template_end

    def start_plan(self, bed_temp, nozzle_temp, *, mesh="auto"):
        return [f"M190 S{bed_temp:g}", f"M104 S{nozzle_temp:g} T0",
                f"START_PRINT BED_TEMP={bed_temp:g} EXTRUDER_TEMP={nozzle_temp:g} MESH={mesh}"]

    def moonraker_start_commands(self, remote_filename):
        return [{"method": "POST", "path": "/printer/print/start", "body": {"filename": remote_filename}}]

    def normalize_tool_mapping(self, mapping, state):
        return self.native.normalize_mapping(mapping, state)

    def readiness_problems(self, state, status, selection=None, **kw):
        from ...core import model_job
        return list(model_job.check_filament_readiness(state, status, selection, **kw))

    def screen_command(self, slot, material, hex_code):
        return self.native.screen.screen_command(slot, material, hex_code)

    def stock_api_url(self, host):
        return f"http://{host}:{self.config['services']['stock_api_port']}/detail"

    def moonraker_objects(self):
        return ["webhooks", "print_stats", "virtual_sdcard", "extruder", "heater_bed", "zmod_color", "zmod_ifs",
                "save_variables"]

    def normalize_status(self, raw):
        out = basic_status(raw)
        color = (raw or {}).get("zmod_color")
        slots = color.get("slots") if isinstance(color, dict) else None
        out["slots"] = slots if isinstance(slots, list) else []
        return out


class ZmodSingleExtruderAdapter(ZmodAdapter):
    """A ZMod printer with one extruder and no IFS (AD5M, AD5M Pro).

    Inherits the ZMod start/end G-code, start plan and Moonraker start command
    from ZmodAdapter. Everything IFS is off: one slot, multi-colour refused,
    no native-mode machinery (no zmod_color/zmod_ifs telemetry, no screen
    colour sync, no stock /detail API), and readiness is the plain
    print_stats/webhooks check the stock Klipper adapter uses. The PRINT_HANDY
    spool-match macro stays AD5X-only; nothing here claims it.
    """

    has_native_screen = False

    @property
    def slot_count(self):
        return 1

    @property
    def native(self):
        raise MultiColorUnsupported(f"{self.label} has no ZMod/native support")

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

    def screen_command(self, slot, material, hex_code):
        return None

    def stock_api_url(self, host):
        return None

    def moonraker_objects(self):
        return ["webhooks", "print_stats", "virtual_sdcard", "extruder", "heater_bed"]

    def normalize_status(self, raw):
        return basic_status(raw)


class ZmodMultiToolAdapter(ZmodSingleExtruderAdapter):
    """A ZMod printer with independent toolheads (Creator 5, Creator 5 Pro).

    Four toolheads, each with its own extruder and filament, selected by the
    sliced file's bare T0-T3 lines — a tool changer, not a feeder. The tools
    are modelled as tools: [printer] tool_count names them and filament_slots
    stays 1, so no IFS behaviour in core (feeder slot questions, spool-switch
    claims, the AD5X PRINT_HANDY macro) switches on. Slicer tool i always
    prints with toolhead i (Klipper objects extruder..extruder3); a mapping
    that asks for anything else — a tool remapped to another head, a fifth
    tool — cannot be represented and is refused, never dropped.

    Start and end G-code are the sibling ZMod form (START_PRINT ... MESH=,
    inherited): the C5Pro build's START_PRINT macro takes BED_TEMP,
    EXTRUDER_TEMP and MESH like the AD5X/AD5M builds (issue #301 evidence
    addendum). Readiness and status are per toolhead: every extruder object
    plus zmod_color.active_tool_id, never zmod_ifs. Spool identity is one
    Spoolman record per toolhead ("Toolhead N" locations, the naming ZMod
    AFC Lite's lanes use), not per IFS slot. No native-screen sync and no
    stock /detail API, as for the single-extruder adapter.
    """

    @property
    def tool_count(self):
        return int(self.config["printer"].get("tool_count", 1))

    @property
    def slot_count(self):
        # Filament positions on this machine are its toolheads.
        return self.tool_count

    @staticmethod
    def _extruder_name(tool):
        return "extruder" if tool == 0 else f"extruder{tool}"

    def normalize_tool_mapping(self, mapping, state):
        out = {}
        for tool, head in (mapping or {}).items():
            try:
                t, h = int(tool), int(head)
            except (TypeError, ValueError):
                raise MultiColorUnsupported(
                    f"{self.label} tool mapping {tool!r} -> {head!r} is not a tool number") from None
            if not 0 <= t < self.tool_count:
                raise MultiColorUnsupported(
                    f"{self.label} has {self.tool_count} toolheads (T0-T{self.tool_count - 1}); "
                    f"this job uses tool T{t}. Slice it for this printer or use one with more tools.")
            if h != t + 1:
                raise MultiColorUnsupported(
                    f"{self.label} tool T{t} always prints with toolhead {t + 1}; "
                    f"a mapping to position {h} cannot be represented.")
            out[str(t)] = h
        return out or {"0": 1}

    def readiness_problems(self, state, status, selection=None, **kw):
        problems = super().readiness_problems(state, status, selection, **kw)
        status = status if isinstance(status, dict) else {}
        # The required objects are checked unconditionally (P2-105): a sample
        # that omits every extruder — idle print_stats, ready webhooks — must
        # fail closed, never pass as a ready multi-tool printer just because
        # the toolhead checks were skipped.
        for tool in range(self.tool_count):
            name = self._extruder_name(tool)
            if not isinstance(status.get(name), dict):
                problems.append(f"toolhead {tool + 1} ({name}) is not reporting")
        color = status.get("zmod_color")
        if not isinstance(color, dict):
            problems.append("zmod_color is not reporting (the active tool is unknown)")
        elif color.get("active_tool_id") is None:
            problems.append("zmod_color does not report an active tool")
        else:
            active = color["active_tool_id"]
            if type(active) is not int or not 0 <= active < self.tool_count:
                problems.append(f"zmod_color reports an unknown active tool ({active!r})")
        return problems

    def moonraker_objects(self):
        return ["webhooks", "print_stats", "virtual_sdcard",
                *[self._extruder_name(tool) for tool in range(self.tool_count)],
                "heater_bed", "zmod_color"]

    def normalize_status(self, raw):
        out = basic_status(raw)
        raw = raw if isinstance(raw, dict) else {}
        out["toolheads"] = [
            {"tool": tool, "name": self._extruder_name(tool),
             "temperature": (raw.get(self._extruder_name(tool)) or {}).get("temperature")}
            for tool in range(self.tool_count)]
        color = raw.get("zmod_color")
        out["active_tool"] = color.get("active_tool_id") if isinstance(color, dict) else None
        return out
