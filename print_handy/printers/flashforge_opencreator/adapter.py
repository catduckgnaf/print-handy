"""Flashforge Creator 5 / Creator 5 Pro on OpenCreator firmware, through Moonraker.

Modeled on the Snapmaker U1 adapter (a Klipper-line multi-tool adapter),
not on the ZMod multi-tool adapter: OpenCreator is Kalico plus upstream
Moonraker, its toolheads are tools under the identity contract (slicer
tool i always prints with toolhead i), and none of the ZMod firmware's
objects or start form exist on it.

Start contract (documented form, OpenCreator slicer documentation,
checked 2026-11-14 for issue #1490): the file's machine start G-code is
the minimal documented form (print stats, bed, chamber, fan lines, G90,
M83). It carries no print-start macro line at all, deliberately: the
firmware dispatcher synthesizes its C5_PRINT_START from the file when
the file has no explicit line, and an explicit line loses the firmware's
per-tool filling. Print end is firmware-owned (C5_PRINT_STOP), so the
end G-code is the template's, which is empty.

Active tool: reported as unknown. The toolchanger and AFC status object
shapes that would carry it are documented and config verified only, not
hardware verified (#1490), so no field name is invented here; the
read-only posture matches the U1's. Readiness fails closed: every
toolhead's extruder object must be reporting, on top of the Klipper
print_stats and webhooks checks.
"""
from ..base import MultiColorUnsupported, basic_status
from ..klipper.adapter import KlipperAdapter
from . import facts, firmware_check

# The documented machine start form, exactly as the templates carry it.
# The chamber line keeps the slicer's own placeholder: no chamber value
# is resolved at this layer, and the firmware reads the file's M191 line
# when it synthesizes its print start. No nozzle heating line: the
# firmware derives tool temperatures from the file itself.
DOCUMENTED_START_LINES = (
    "SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]",
    "M140 S[bed_temperature_initial_layer_single]",
    "M191 S[overall_chamber_temperature]",
    "M106",
    "M106 P2 S0 ; center fresh air input fan off",
    "M106 S0 ; model fan off",
    "M106 P3 S0 ; filter unit fan off",
    "G90",
    "M83",
)


class OpenCreatorAdapter(KlipperAdapter):
    # Not a Bondtech INDX host (#1198): the INDX code inherited from
    # KlipperAdapter never runs here, as on the Snapmaker adapter.
    supported_accessories = frozenset()
    facts = facts.CREATOR5

    def fact(self, name):
        return self.facts[name].value

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
                    f"this job uses tool T{t}.")
            if h != t + 1:
                raise MultiColorUnsupported(
                    f"{self.label} tool T{t} always prints with toolhead {t + 1}; "
                    f"a mapping to position {h} cannot be represented.")
            out[str(t)] = h
        return out or {"0": 1}

    def machine_start_gcode(self, template_start, mesh="auto"):
        # The documented form is fixed by the firmware's contract, not
        # template-owned: the adapter builds it (the shipped templates
        # carry the same text, and the protected-settings baseline pins
        # the pair together). No macro line, no mesh argument and no
        # nozzle heating line is ever added, so the firmware dispatcher
        # synthesizes its print start from the file as documented.
        return "\n".join(DOCUMENTED_START_LINES)

    def machine_end_gcode(self, template_end):
        # Print end is firmware-owned; the end G-code is empty, whatever
        # a template carries, so no end macro line reaches the file.
        return ""

    def start_plan(self, bed_temp, nozzle_temp, *, mesh="auto"):
        # The documented form resolved for one job: the bed line takes
        # the job's bed temperature. nozzle_temp is not written: the
        # firmware derives each used tool's temperature from the file's
        # own heating lines and config block.
        return [f"M140 S{bed_temp:g}" if line.startswith("M140 ") else line
                for line in DOCUMENTED_START_LINES]

    def readiness_problems(self, state, status, selection=None, **kw):
        problems = super().readiness_problems(state, status, selection, **kw)
        status = status if isinstance(status, dict) else {}
        # Checked unconditionally, as on the ZMod multi-tool adapter: a
        # sample that omits every extruder must fail closed, never pass
        # as a ready multi-tool printer.
        for tool in range(self.tool_count):
            name = self._extruder_name(tool)
            if not isinstance(status.get(name), dict):
                problems.append(f"toolhead {tool + 1} ({name}) is not reporting")
        mismatch = firmware_check.zmod_mismatch_problem(status)
        if mismatch:
            problems.append(mismatch)
        return problems

    def moonraker_objects(self):
        return ["webhooks", "print_stats", "virtual_sdcard",
                *[self._extruder_name(tool) for tool in range(self.tool_count)],
                "heater_bed", "exclude_object"]

    def normalize_status(self, raw):
        out = basic_status(raw)
        raw = raw if isinstance(raw, dict) else {}
        out["toolheads"] = [
            {"tool": tool, "name": self._extruder_name(tool),
             "temperature": (raw.get(self._extruder_name(tool)) or {}).get("temperature")}
            for tool in range(self.tool_count)]
        # No verified object provides the active tool on this firmware
        # yet (#1490); unknown is reported as None, never guessed from
        # another firmware's object.
        out["active_tool"] = None
        return out

    def spool_remain_g(self, status, tool):
        """Grams left on one toolhead, or None when the sample does not say.

        Read-only, the U1 posture: looks for the remaining fields beside
        a spool id on the tool's own extruder object. Absence is not
        zero. This does not query the printer.
        """
        raw = status if isinstance(status, dict) else {}
        block = raw.get(self._extruder_name(int(tool)))
        if not isinstance(block, dict):
            return None
        for key in ("filament_remaining_g", "spool_remain_g"):
            value = block.get(key)
            if type(value) in (int, float):
                return float(value)
        return None
