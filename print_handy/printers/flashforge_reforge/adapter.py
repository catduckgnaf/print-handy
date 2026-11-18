"""Flashforge Creator 5 / Creator 5 Pro on Reforge firmware, through Moonraker.

Reforge and OpenCreator are different firmwares on the same hardware,
but both put AFC in standalone toolchanger mode over their own tool
changer, one lane per toolhead, so the printer's AFC map decides which
toolhead prints each slicer tool on both. This adapter therefore extends
the OpenCreator adapter and keeps everything that reads AFC (the tool
map, ACE refusal, per-head filament presence, backup lanes, mount
errors, the Pro's chamber heater check, the spool-write refusal) with
Reforge's AFC naming (``afc.AfcLayout``: lanes and ``AFC_extruder``
sections ``e0`` to ``e3``, Reforge ``ff-afc.cfg``). What is Reforge's own
is implemented here:

- Print start (start_contract.py): ``[ff_print]`` reads bed, nozzle,
  first tool and first-layer height from the file and runs
  ``FF_BEFORE_PRINT_START``, then ``START_PRINT``, before the first file
  line. Print Handy's start form gives it all four values in plain lines
  and carries no macro line. Print end is firmware-owned
  (``FF_AFTER_PRINT_END`` runs ``END_PRINT``), so the end G-code is empty.
- The tool changer is ``ff_toolchange`` (Reforge
  ``klippy/extras/ff_toolchange.py`` ``get_status``): ``current_tool`` (-1
  when every tool is docked), ``state_ok`` and ``state_reason`` from the
  dock and grab sensors, ``docked_tools``, ``calibrated_tools`` and
  ``station_z``. Its ``toolchanger`` view reports ``status`` "changing"
  during a change.
- No power-loss recovery (Reforge's own ``docs/is-this-for-you.md`` lists
  it as lost), and no OpenCreator per-print overrides.

AFC is required. Reforge's tagged releases up to v20260827f-melitopol
(2026-09-24) carry no AFC; AFC came with the afc-migration merge on
master (2026-11-04). On a build without AFC the tool map and per-head
filament presence cannot be read, so readiness refuses (docs/REFORGE.md).
"""
import re

from ..flashforge_opencreator import afc, firmware_check
from ..flashforge_opencreator.adapter import CHAMBER_OBJECT, OpenCreatorAdapter
from . import facts, start_contract

# Reforge's AFC naming: [AFC_extruder e0..e3], each its own standalone
# lane "AFC_lane e<n>" whose extruder field is "e<n>" (ff-afc.cfg, and
# AFC-Klipper-Add-On 8ba0f7bf AFC_extruder.py, the commit Reforge pins).
# Reforge ships enable_multiple_mapping on, so the flag alone is not
# refused: a map is judged by what it says (one lane, one toolhead per tool).
REFORGE_AFC = afc.AfcLayout(re.compile(r"e(\d+)"), "AFC_extruder e{}",
                            refuse_multiple_mapping=False)

# The start form Print Handy writes. Every value ff_print derives is on a
# plain line of its own: the first-layer height (the same ;HEIGHT: line
# Reforge's own gcode/creator5-feature-test.gcode carries), bed, nozzle,
# and the initial tool, which on Reforge is AFC's T<n> and is a no-op when
# START_PRINT has already picked that head up. The fan lines turn off the
# fans Reforge's feature test names (P2 chamber loop and cool fans, the
# part fan, P3 the chamber fan). No macro line, no G28.
DOCUMENTED_START_LINES = (
    "SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]",
    ";HEIGHT:[initial_layer_print_height]",
    "M140 S[bed_temperature_initial_layer_single]",
    "M104 S[nozzle_temperature_initial_layer]",
    "M191 S[overall_chamber_temperature]",
    "M106 P2 S0 ; chamber loop and cool fans off",
    "M106 S0 ; part cooling fan off",
    "M106 P3 S0 ; chamber fan off",
    "G90",
    "M83",
    "T[initial_extruder]",
)

# The base Creator 5 has no chamber heater: no chamber line is written.
BASE_START_LINES = tuple(line for line in DOCUMENTED_START_LINES if not line.startswith("M191 "))


class ReforgeAdapter(OpenCreatorAdapter):
    firmware_name = "Reforge"
    afc_layout = REFORGE_AFC
    lane_data_write_refusal = (
        "not written: Reforge's AFC publishes the lane_data namespace itself "
        "and Print Handy has not verified how it uses it, so spool "
        "assignments are not written there")
    facts = facts.CREATOR5

    @property
    def _model_facts(self):
        if self.model is not None and self.model.key.endswith("creator5_pro"):
            return facts.CREATOR5_PRO
        return facts.CREATOR5

    def start_lines(self):
        return DOCUMENTED_START_LINES if self.has_chamber else BASE_START_LINES

    def finalize_output(self, text):
        # No per-print overrides on Reforge: the file is left as sliced.
        return text

    def start_plan(self, bed_temp, nozzle_temp, *, mesh="auto"):
        out = []
        for line in self.start_lines():
            if line.startswith("M140 "):
                line = f"M140 S{bed_temp:g}"
            elif line.startswith("M104 "):
                line = f"M104 S{nozzle_temp:g}"
            out.append(line)
        return out

    def start_synthesis_problems(self, text, bed, hotend):
        """Verification problems under ff_print's derivation (start_contract)."""
        from ...core import heater_ceiling
        max_chamber = heater_ceiling.ceiling(self, "chamber") if self.has_chamber else None
        return start_contract.start_problems(text, bed, hotend, max_chamber=max_chamber)

    def _active_tool(self, status):
        """The mounted tool from ff_toolchange, or None when none is mounted or it is unknown."""
        changer = status.get("ff_toolchange") if isinstance(status, dict) else None
        if not isinstance(changer, dict) or changer.get("state_ok") is not True:
            return None
        current = changer.get("current_tool")
        if type(current) is int and 0 <= current < self.tool_count:
            return current
        return None

    def _toolchanger_problems(self, status):
        changer = status.get("ff_toolchange")
        if not isinstance(changer, dict) or not changer:
            return ["ff_toolchange is not reporting (which tool is mounted is unknown)"]
        if changer.get("state_ok") is not True:
            reason = changer.get("state_reason") or "no reason given"
            return [f"ff_toolchange reports the tool changer state is not ok: {reason} "
                    "(the dock and grab sensors do not agree on which tool is mounted)"]
        problems = []
        view = status.get("toolchanger")
        if isinstance(view, dict) and view.get("status") == "changing":
            problems.append("the tool changer is in the middle of a tool change; wait for it to finish")
        current = changer.get("current_tool")
        if type(current) is not int or not -1 <= current < self.tool_count:
            problems.append(f"ff_toolchange reports an unknown mounted tool ({current!r})")
        return problems

    def _recovery_problems(self, status):
        # Reforge has no power-loss recovery to hold a print.
        return []

    def _used_heads(self, status, selection):
        mapping = selection.get("tool_mapping") if isinstance(selection, dict) else None
        tools = sorted(int(t) for t in mapping if str(t).isdigit()) \
            if isinstance(mapping, dict) and mapping else list(range(self.tool_count))
        heads = self.tool_head_map(status) or {}
        return [(tool, heads.get(tool, tool)) for tool in tools]

    def _used_tool_filament_problems(self, status, selection):
        """Filament at each used head (shared), plus Reforge's own pre-motion gate.

        Reforge's _FF_PREFLIGHT refuses a job before anything moves when a
        used head is neither docked nor mounted, or is not calibrated, or
        the calibration station has no Z (ff-print-macros.cfg). Print
        Handy checks the same facts up front, for every head the job uses
        through the printer's map, so the refusal names the cause before
        the file is sent. Unknown is a refusal.
        """
        problems = super()._used_tool_filament_problems(status, selection)
        changer = status.get("ff_toolchange")
        if not isinstance(changer, dict) or changer.get("state_ok") is not True:
            return problems  # already its own refusal
        docked, calibrated = changer.get("docked_tools"), changer.get("calibrated_tools")
        current = changer.get("current_tool")
        if not isinstance(docked, list) or not isinstance(calibrated, list):
            return problems + ["ff_toolchange does not report which tools are docked and "
                               "calibrated (Reforge's print-start gate cannot be checked)"]
        if changer.get("station_z") is None:
            problems.append("the tool calibration station has no Z (ff_toolchange station_z); "
                            "Reforge refuses to print until it is calibrated")
        for tool, head in self._used_heads(status, selection):
            if not (isinstance(head, int) and 0 <= head < self.tool_count):
                continue
            where = f"T{tool}" + (f" (toolhead {head + 1})" if head != tool else "")
            if head not in docked and head != current:
                problems.append(f"{where} is neither in its dock nor mounted; install the tool")
            if head not in calibrated:
                problems.append(f"{where} is not calibrated; Reforge refuses to print with it "
                                "until its tool offset is calibrated")
        return problems

    def _firmware_mismatch_problems(self, status):
        # An unknown object comes back from Moonraker as an empty mapping:
        # absent, not present.
        present = {name: block for name, block in status.items()
                   if not (isinstance(block, dict) and not block)}
        mismatch = firmware_check.not_reforge_problem(present)
        return [mismatch] if mismatch else []

    def readiness_problems(self, state, status, selection=None, **kw):
        problems = super().readiness_problems(state, status, selection, **kw)
        problems += start_contract.prepare_problems(status if isinstance(status, dict) else {})
        return problems

    def moonraker_objects(self):
        return ["webhooks", "print_stats", "virtual_sdcard",
                *[self._extruder_name(tool) for tool in range(self.tool_count)],
                "heater_bed", "exclude_object", "ff_toolchange", "toolchanger", "ff_print",
                "gcode_macro FF_BEFORE_PRINT_START", "AFC",
                *[self._afc_extruder_name(tool) for tool in range(self.tool_count)],
                # Asked for so a printer on another Creator firmware is named.
                "zmod_color", "creator5_toolchanger",
                *([CHAMBER_OBJECT] if self.has_chamber else [])]

    def head_filament_sensors(self):
        """AFC's toolhead switch per tool (``filament_switch_sensor e<n>_tool_start``).

        AFC registers it only when its sensors are shown in the web UI; when
        it is absent the resume guard reads presence as unknown.
        """
        return {self._extruder_name(tool): [f"filament_switch_sensor e{tool}_tool_start"]
                for tool in range(self.tool_count)}

    def normalize_status(self, raw):
        out = super().normalize_status(raw)
        out["active_tool"] = self._active_tool(raw if isinstance(raw, dict) else {})
        return out
