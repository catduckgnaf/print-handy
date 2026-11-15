"""Flashforge Creator 5 / Creator 5 Pro on OpenCreator firmware, through Moonraker.

Modeled on the Snapmaker U1 adapter (a Klipper-line multi-tool adapter),
not on the ZMod multi-tool adapter: OpenCreator is Kalico plus upstream
Moonraker, its toolheads are tools under the identity contract (slicer
tool i always prints with toolhead i), and none of the ZMod firmware's
objects or start form exist on it.

Start contract (documented form, OpenCreator slicer documentation,
checked 2026-11-14 for issue #1490): the file's machine start G-code is
the minimal documented form (print stats, bed, chamber on the Pro, fan
lines, G90, M83). It carries no print-start macro line at all,
deliberately: the firmware dispatcher synthesizes its C5_PRINT_START
from the file when the file has no explicit line, and an explicit line
loses the firmware's per-tool filling. Print end is firmware-owned
(C5_PRINT_STOP), so the end G-code is the template's, which is empty.
Verification models the synthesis itself (start_contract.py), whose
contract is source-verified against the fork, not hardware verified.

Readiness and active tool ride on the creator5_toolchanger object,
whose status shape is source-verified against the fork's default
branch (#1490 Kalico research): exactly active_tool (int 0 to 3 or
None, from the physical dock and grab pins as sole authority),
sensor_error (string or None), busy, build_plate_detected,
dock_positions and tool_measurements. None with a sensor_error means
the sensors disagree; None without one means no tool is mounted.
Readiness fails closed: the object must be reporting, every toolhead's
extruder object must be reporting, and the printer's AFC lane map must
be the identity map, because the firmware remaps a file's tools through
that map inside C5_PRINT_START, invisibly to the file. Print Handy
never sends SET_MAP (the command swaps lanes rather than assigning).

Filament records in Moonraker's lane_data namespace are read through
lane_data.py (tranche 3): both publisher shapes the firmware can put
there are accepted and labeled by source, disagreements surface as
conflicts, and nothing converges from them yet. Spool writes to the
namespace are refused outright (lane_data_write_refusal below): which
publisher owns a live install's namespace is unverified until a real
install's namespace has been captured, so a Print Handy write could
fight the printer's own publisher.
"""
from ..base import MultiColorUnsupported, basic_status
from ..klipper.adapter import KlipperAdapter
from . import facts, firmware_check, lane_data, start_contract

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

# The base Creator 5 has no chamber heater, so its start form omits the
# M191 line entirely: Print Handy does not write a chamber command for
# a heater that does not exist (the firmware would only answer it with
# a no-chamber-heater message).
BASE_START_LINES = tuple(line for line in DOCUMENTED_START_LINES if not line.startswith("M191 "))


class OpenCreatorAdapter(KlipperAdapter):
    # Not a Bondtech INDX host (#1198): the INDX code inherited from
    # KlipperAdapter never runs here, as on the Snapmaker adapter.
    supported_accessories = frozenset()
    # Print end is firmware-owned (the dispatcher runs C5_PRINT_STOP at
    # end of file): verification must not demand an end macro line.
    firmware_owned_end = True
    # Spool-write posture (#1490 tranche 3): spool_sync.write_lane_side
    # returns this named refusal for these models instead of writing a
    # lane_data record. The namespace is published by the printer
    # itself, and which of its two publishers owns it on a live install
    # is unverified, so no key shape (T<n> or c5_tool<n>) is written.
    lane_data_write_refusal = (
        "not written: OpenCreator publishes the lane_data namespace itself "
        "and Print Handy has not verified which publisher owns it, so spool "
        "assignments are not written there (#1490)")
    facts = facts.CREATOR5

    @property
    def _model_facts(self):
        if self.model is not None and self.model.key.endswith("creator5_pro"):
            return facts.CREATOR5_PRO
        return facts.CREATOR5

    def fact(self, name):
        return self._model_facts[name].value

    @property
    def has_chamber(self):
        return self.model is not None and self.model.key.endswith("creator5_pro")

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

    @staticmethod
    def _afc_extruder_name(tool):
        return f"AFC_extruder extruder{tool}"

    def start_lines(self):
        return DOCUMENTED_START_LINES if self.has_chamber else BASE_START_LINES

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
        return "\n".join(self.start_lines())

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
                for line in self.start_lines()]

    def start_synthesis_problems(self, text, bed, hotend):
        """Verification problems under the dispatcher's synthesis (start_contract)."""
        return start_contract.start_problems(text, bed, hotend)

    def _toolchanger_problems(self, status):
        changer = status.get("creator5_toolchanger")
        if not isinstance(changer, dict):
            return ["creator5_toolchanger is not reporting (the active tool is unknown)"]
        problems = []
        sensor_error = changer.get("sensor_error")
        if sensor_error:
            problems.append(
                f"creator5_toolchanger reports a tool sensor error: {sensor_error} "
                "(the dock and grab sensors disagree about which tool is mounted)")
            return problems
        active = changer.get("active_tool")
        if active is None:
            problems.append(
                "no tool is mounted (creator5_toolchanger reports no active tool); "
                "mount a toolhead before printing")
        elif type(active) is not int or not 0 <= active < self.tool_count:
            problems.append(f"creator5_toolchanger reports an unknown active tool ({active!r})")
        return problems

    def _afc_map_problems(self, status):
        """The identity lane-map check: the printer-side remap is refused, never used.

        The map is read from each AFC_extruder object's map / current_map
        status (source-verified, #1490). A missing object or a missing
        map fails closed, on the same unconditional pattern as the
        extruder objects: a sample that omits them must never pass as a
        ready printer whose map was never checked.
        """
        problems = []
        offenders = []
        for tool in range(self.tool_count):
            name = self._afc_extruder_name(tool)
            block = status.get(name)
            if not isinstance(block, dict):
                problems.append(f"{name} is not reporting (its lane map cannot be checked)")
                continue
            raw = block.get("current_map", block.get("map"))
            if isinstance(raw, str):
                mapped = [raw] if raw else []
            elif isinstance(raw, (list, tuple, set)):
                mapped = [str(v) for v in raw]
            else:
                mapped = []
            if not mapped:
                problems.append(f"{name} does not report its lane map")
            elif set(mapped) != {f"T{tool}"}:
                offenders.append(f"extruder{tool} is mapped to {', '.join(mapped)}")
        if offenders:
            problems.append(
                "the printer's AFC lane map is not the identity map this printer's "
                "contract requires (" + "; ".join(offenders) + "); the printer would "
                "remap the file's tools inside C5_PRINT_START, invisibly to Print Handy. "
                "Restore the identity map at the printer (extruder0 to T0, extruder1 "
                "to T1, extruder2 to T2, extruder3 to T3) before printing")
        return problems

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
        problems += self._toolchanger_problems(status)
        problems += self._afc_map_problems(status)
        mismatch = firmware_check.zmod_mismatch_problem(status)
        if mismatch:
            problems.append(mismatch)
        return problems

    def moonraker_objects(self):
        return ["webhooks", "print_stats", "virtual_sdcard",
                *[self._extruder_name(tool) for tool in range(self.tool_count)],
                "heater_bed", "exclude_object", "creator5_toolchanger", "AFC",
                *[self._afc_extruder_name(tool) for tool in range(self.tool_count)]]

    def normalize_status(self, raw):
        out = basic_status(raw)
        raw = raw if isinstance(raw, dict) else {}
        out["toolheads"] = [
            {"tool": tool, "name": self._extruder_name(tool),
             "temperature": (raw.get(self._extruder_name(tool)) or {}).get("temperature")}
            for tool in range(self.tool_count)]
        # The active tool is the toolchanger object's, from the dock
        # and grab pins (source-verified contract, #1490): an integer
        # 0 to 3, or None when no tool is mounted or the sensors
        # disagree. Anything else in the field is not trusted.
        changer = raw.get("creator5_toolchanger")
        active = changer.get("active_tool") if isinstance(changer, dict) else None
        out["active_tool"] = active if type(active) is int and 0 <= active < self.tool_count else None
        return out

    def tool_lane_data(self, value):
        """Per-tool filament records from a raw ``lane_data`` namespace value.

        The reading half of tranche 3 (lane_data.py): both publisher
        shapes are accepted, every record is labeled with the shape it
        came from, and a tool whose publishers disagree carries a
        conflict instead of a claim. Nothing converges from these
        records yet; the live namespace capture is still the arbiter.
        """
        return lane_data.parse_tool_lane_data(value, self.tool_count)

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
