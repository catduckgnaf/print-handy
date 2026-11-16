"""Flashforge Creator 5 / Creator 5 Pro on OpenCreator firmware, through Moonraker.

Modeled on the Snapmaker U1 adapter (a Klipper-line multi-tool adapter),
not on the ZMod multi-tool adapter: OpenCreator is Kalico plus upstream
Moonraker, and the printer's AFC map decides which toolhead each slicer
tool uses (position i is the mapped head for tool i-1). None of the ZMod firmware's
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
the sensors disagree; None without one means every tool is docked,
which is the normal idle state and not a problem (print start picks up
the tool it needs). busy refuses: the toolchanger is mid-operation.
build_plate_detected is not a readiness input: it is the result of the
last plate check (None until one runs, and a stale False stays until
the next check), and print start runs its own check after homing.
Readiness fails closed: the object must be reporting, every toolhead's
extruder object must be reporting, AFC must be reporting without an
error state or a mount sensor error, and the printer's AFC tool map
must be readable. The firmware sends each file tool to the lane mapped
to it inside C5_PRINT_START, so that map, not the tool number, decides
which toolhead prints a tool. Print Handy follows it (#1490, decided
2026-11-15): a remapped printer is accepted, and a map that does not
give every tool exactly one lane and one toolhead is refused. The map
is read from the lanes AFC lists, under AFC_lane or AFC_stepper,
because lane names are the printer's own: a live capture with an ACE
unit lists lane1 to lane4 and has no AFC_lane extruderN object at all.
An attached ACE unit is refused by name. Print Handy never sends
SET_MAP (the command swaps lanes rather than assigning).

Filament records in Moonraker's lane_data namespace are read through
lane_data.py (tranche 3): both publisher shapes the firmware can put
there are accepted and labeled by source, disagreements surface as
conflicts, and nothing converges from them yet. Spool writes to the
namespace are refused outright (lane_data_write_refusal below): which
publisher owns a live install's namespace is unverified until a real
install's namespace has been captured, so a Print Handy write could
fight the printer's own publisher.
"""
from ...core import heater_ceiling
from ..base import MultiColorUnsupported, basic_status
from ..klipper.adapter import KlipperAdapter
from . import afc, facts, firmware_check, lane_data, start_contract

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


# The Pro's chamber heater (shipped printer.pro.cfg, findings comment on #1490).
CHAMBER_OBJECT = "heater_generic chamber_heater"


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
    # AFC sets Moonraker's active spool at each tool change, so with a
    # Moonraker Spoolman link the printer counts filament itself (#1536).
    firmware_assigns_spools = True
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

    def finalize_output(self, text):
        # Per-print overrides (#1490 tranche 5): with every switch left to
        # the printer (the default) the file is unchanged and the firmware
        # synthesizes its own start. Any override makes Print Handy write
        # the full explicit C5_PRINT_START line the dispatcher would have
        # synthesized, plus the overrides, because an explicit line opts the
        # file out of synthesis and the firmware fills in nothing for it.
        overrides = start_contract.override_params(self.config.get("printer"))
        return start_contract.with_overrides(text, overrides)

    def start_plan(self, bed_temp, nozzle_temp, *, mesh="auto"):
        # The documented form resolved for one job: the bed line takes
        # the job's bed temperature. nozzle_temp is not written: the
        # firmware derives each used tool's temperature from the file's
        # own heating lines and config block.
        return [f"M140 S{bed_temp:g}" if line.startswith("M140 ") else line
                for line in self.start_lines()]

    def start_synthesis_problems(self, text, bed, hotend):
        """Verification problems under the dispatcher's synthesis (start_contract)."""
        # The chamber ceiling binds on the Pro only: the base model has
        # no chamber heater, and the firmware answers a chamber target
        # there with a notice.
        max_chamber = heater_ceiling.ceiling(self, "chamber") if self.has_chamber else None
        return start_contract.start_problems(text, bed, hotend, max_chamber=max_chamber)

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
        if changer.get("busy"):
            problems.append(
                "the toolchanger is busy (creator5_toolchanger reports an operation "
                "in progress); wait for it to finish")
        # None is every tool docked: the normal idle state, and print
        # start picks up the tool it needs.
        active = changer.get("active_tool")
        if active is not None and (type(active) is not int or not 0 <= active < self.tool_count):
            problems.append(f"creator5_toolchanger reports an unknown active tool ({active!r})")
        return problems

    def _afc_state_problems(self, status):
        afc = status.get("AFC")
        if not isinstance(afc, dict):
            return ["AFC is not reporting (its error state and lane map cannot be checked)"]
        problems = []
        if afc.get("error_state"):
            problems.append("AFC reports an error state; clear it at the printer before printing")
        for tool in range(self.tool_count):
            name = self._afc_extruder_name(tool)
            block = status.get(name)
            if not isinstance(block, dict):
                problems.append(f"{name} is not reporting (its mount sensors cannot be checked)")
            elif block.get("mount_sensor_error"):
                problems.append(f"toolhead {tool + 1} reports a mount sensor error: "
                                f"{block.get('mount_sensor_error')}")
        return problems

    def tool_head_map(self, status):
        """File tool -> toolhead index from the printer's own AFC map, or None when unresolved."""
        resolved, _ = afc.resolve_tool_map(status, self.tool_count)
        if resolved is None:
            return None
        return {tool: row["head"] for tool, row in resolved.items()}

    def _afc_map_problems(self, status):
        """The printer's tool map must be readable; whatever it says is followed.

        Inside C5_PRINT_START the firmware sends each file tool to the
        lane mapped to it, so the map decides which toolhead a tool
        prints with. It is read from the lanes AFC itself lists (#1490,
        live capture): each lane's map is the T commands it answers
        and its extruder is the toolhead it feeds. current_map is not
        used: it is empty until the first tool change. A remapped
        printer is accepted. A map that does not give every tool
        exactly one lane and one toolhead fails closed, as does an
        attached ACE unit, which feeds lanes Print Handy cannot yet
        account for.
        """
        problems = []
        units = afc.ace_units(status)
        if units:
            problems.append(
                f"an ACE unit is attached ({', '.join(units)}); printing through an ACE unit "
                "on OpenCreator is not supported by Print Handy yet")
        _, map_problems = afc.resolve_tool_map(status, self.tool_count)
        return problems + map_problems

    def _recovery_problems(self, status):
        """A held power-loss recovery refuses a new start (tranche 3).

        While ``recovery_available`` is true the printer holds the bed at
        the interrupted print's temperature and offers Resume every minute;
        a new start on top of it would fight that state. The object is part
        of the shipped config, so a sample without it fails closed.
        """
        row = status.get("power_loss_recovery")
        if not isinstance(row, dict):
            return ["power_loss_recovery is not reporting (a held interrupted print "
                    "cannot be ruled out)"]
        if row.get("recovery_available") is True:
            name = row.get("filename") if isinstance(row.get("filename"), str) else ""
            return [f"the printer is holding an interrupted print for power-loss recovery"
                    f"{f' ({name})' if name.strip() else ''}: resume or discard it at the "
                    "printer first"]
        if row.get("recovery_available") is not False:
            return ["power_loss_recovery reports no readable recovery state"]
        return []

    def _chamber_problems(self, status):
        """On the Pro, the chamber heater must be reporting (tranche 3).

        The Pro's start line can carry a chamber target, which the printer
        sends to ``heater_generic chamber_heater``. A Pro sample without
        that heater fails closed. The base model has no chamber heater and
        is never asked for one.
        """
        if not self.has_chamber:
            return []
        if not isinstance(status.get(CHAMBER_OBJECT), dict):
            return [f"{CHAMBER_OBJECT} is not reporting on this Creator 5 Pro "
                    "(the chamber target cannot be checked)"]
        return []

    def _used_tool_filament_problems(self, status, selection):
        """Every tool the job uses must have filament at its toolhead (tranche 3).

        The firmware's own synthesized start checks filament in the first
        tool only (findings comment on #1490), so a used second tool with
        no filament passes print start and stops later on runout. Print
        Handy checks each used tool's toolhead, following the printer's
        AFC map, from ``AFC_extruder extruderN.tool_start_status``. The
        used tools come from the job's recorded tool mapping; with none,
        every tool is checked. Unknown is a refusal, never a pass.
        """
        mapping = selection.get("tool_mapping") if isinstance(selection, dict) else None
        tools = sorted(int(t) for t in mapping if str(t).isdigit()) \
            if isinstance(mapping, dict) and mapping else list(range(self.tool_count))
        heads = self.tool_head_map(status) or {}
        problems = []
        for tool in tools:
            head = heads.get(tool, tool)
            if not (isinstance(head, int) and 0 <= head < self.tool_count):
                continue  # an unresolved map is already its own refusal
            block = status.get(self._afc_extruder_name(head))
            present = block.get("tool_start_status") if isinstance(block, dict) else None
            if present is True:
                continue
            where = f"T{tool}" + (f" (toolhead {head + 1})" if head != tool else "")
            if present is False:
                problems.append(f"{where} has no filament at the toolhead; load it at the printer")
            else:
                problems.append(f"{where} filament presence is not reporting "
                                f"({self._afc_extruder_name(head)} tool_start_status)")
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
        problems += self._afc_state_problems(status)
        problems += self._afc_map_problems(status)
        problems += self._recovery_problems(status)
        problems += self._chamber_problems(status)
        problems += self._used_tool_filament_problems(status, selection)
        mismatch = firmware_check.zmod_mismatch_problem(status)
        if mismatch:
            problems.append(mismatch)
        return problems

    def backup_lane_summary(self, status, selection=None):
        """Advisory text only; never a readiness refusal or mapping write."""
        from .backup import summarize_backup_lanes
        mapping = selection.get("tool_mapping") if isinstance(selection, dict) else None
        tools = list(mapping) if isinstance(mapping, dict) and mapping else range(self.tool_count)
        result = summarize_backup_lanes(status, tools)
        lines = ["OpenCreator backup lanes (display only; not hardware verified):"]
        for row in result["rows"]:
            lines.append(f"T{row['tool']}: {row['lane'] or 'unknown lane'} runout_lane="
                         f"{row['runout_lane'] or 'not configured'}")
        lines.extend(f"Warning: {warning}" for warning in result["warnings"])
        return "\n".join(lines)

    def moonraker_objects(self):
        return ["webhooks", "print_stats", "virtual_sdcard",
                *[self._extruder_name(tool) for tool in range(self.tool_count)],
                "heater_bed", "exclude_object", "creator5_toolchanger", "AFC",
                *[self._afc_extruder_name(tool) for tool in range(self.tool_count)],
                "power_loss_recovery", *([CHAMBER_OBJECT] if self.has_chamber else [])]

    def head_filament_sensors(self):
        """{extruder object name: its toolhead filament sensor objects}, for the resume guard.

        The fork names each tool's sensor ``extruderN_tool_start`` and the
        start macro falls back to ``extruder_tool_start`` for tool 0
        (findings comment on #1490). The shared resume guard finds no
        sensor named ``head`` on this printer, so it asks the adapter.
        """
        out = {}
        for tool in range(self.tool_count):
            names = [f"filament_switch_sensor extruder{tool}_tool_start"]
            if tool == 0:
                names.append("filament_switch_sensor extruder_tool_start")
            out[self._extruder_name(tool)] = names
        return out

    def moonraker_followup_objects(self, status):
        """Lane objects to query once AFC has named its lanes.

        Lane names are the printer's own (extruder0 to extruder3 on a
        standalone install, lane1 to lane4 behind an ACE unit), so they
        are read from AFC.lanes and never assumed.
        """
        return afc.lane_object_names(status)

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
        if isinstance(changer, dict) and changer.get("sensor_error"):
            # The sensors disagree: whatever the field holds is not a reading.
            active = None
        out["active_tool"] = active if type(active) is int and 0 <= active < self.tool_count else None
        from .backup import summarize_backup_lanes
        out["backup_lanes"] = summarize_backup_lanes(raw, range(self.tool_count))
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
