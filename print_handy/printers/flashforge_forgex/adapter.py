"""Flashforge Adventurer 5M / 5M Pro on Forge-X firmware, through Moonraker.

Modeled on the OpenCreator adapter's packaging (its own vendor, facts
and firmware check around a plain Klipper-line adapter), not on the
ZMod single-extruder adapter: Forge-X is the stock-generation Klipper
patched in place plus Moonraker, and none of the ZMod firmware's
objects, delivery logic or start parameters exist on it. The base is
KlipperAdapter itself, which is already the single-extruder shape
Forge-X needs (one extruder, multi-tool mappings refused).

Start contract (documented form, Forge-X slicer documentation for
the supported 1.4.2 beta, checked 2026-11-15 for issue #1506): the layer statistics
line the beta's native timelapse and pause-at-layer need, then
START_PRINT on a single line carrying EXTRUDER_TEMP and BED_TEMP —
missing either aborts the job, so both are always written, in the
documented order, with the slicer's own placeholders. What is absent
is deliberate:

- No MESH=: Forge-X's MESH takes a bed-mesh profile name whose meaning
  is screen-mode dependent (tranche 2 of #1506 owns that question);
  the ZMod form's MESH=auto does not carry over.
- No Z_OFFSET and no SKIP_ZOFFSET: both are deprecated on Forge-X and
  fatal — passing either cancels the print.
- No TIMELAPSE parameter by default. [printer] forgex_timelapse "off"
  adds TIMELAPSE=0 to the sliced file's START_PRINT line (finalize_output)
  to skip the printer's timelapse for those prints. The templates emit
  no TIMELAPSE_TAKE_FRAME in the layer-change G-code, because the beta's
  native timelapse handles frames itself.

Print end is END_PRINT, which the firmware defines on every screen
mode. Readiness is the Klipper print_stats and webhooks checks plus
the extruder and heater_bed objects failing closed, plus the
firmware-mismatch warning when a sample positively carries ZMod
surfaces (firmware_check.py). Spoolman is absent by default on
Forge-X (its shipped moonraker.conf has no [spoolman] section), so
nothing here assumes a printer-side Spoolman.
"""
from ..base import basic_status
from ..klipper.adapter import KlipperAdapter
from . import facts, firmware_check, start_contract

# The documented machine start form, exactly as the templates carry it.
DOCUMENTED_START_LINES = (
    "SET_PRINT_STATS_INFO TOTAL_LAYER=[total_layer_count]",
    "START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer] "
    "BED_TEMP=[bed_temperature_initial_layer_single]",
)


class ForgeXAdapter(KlipperAdapter):
    # Not a Bondtech INDX host (#1198): the INDX code inherited from
    # KlipperAdapter never runs here, as on the OpenCreator adapter.
    supported_accessories = frozenset()
    facts = facts.AD5M

    def fact(self, name):
        return self.facts[name].value

    def machine_start_gcode(self, template_start, mesh="auto"):
        # The documented form is fixed by the firmware's contract, not
        # template-owned: the adapter builds it (the shipped templates
        # carry the same text, and the protected-settings baseline pins
        # the pair together). The mesh argument is accepted for the
        # shared adapter interface and deliberately not written: no
        # MESH= reaches a Forge-X file in this tranche (see above).
        return "\n".join(DOCUMENTED_START_LINES)

    def machine_end_gcode(self, template_end):
        return "END_PRINT"

    def finalize_output(self, text):
        # Per-print timelapse skip (#1506 tranche 2b): with [printer]
        # forgex_timelapse "printer" (the default) the file is unchanged and
        # the printer's own timelapse switch decides; "off" adds TIMELAPSE=0
        # to the file's single START_PRINT line.
        if start_contract.timelapse_choice(self.config.get("printer")) == "off":
            return start_contract.with_timelapse_skip(text)
        return text

    def start_plan(self, bed_temp, nozzle_temp, *, mesh="auto"):
        lines = [DOCUMENTED_START_LINES[0],
                 f"START_PRINT EXTRUDER_TEMP={nozzle_temp:g} BED_TEMP={bed_temp:g}"]
        for line in lines:
            bad = start_contract.forbidden_params(line)
            if bad:  # pragma: no cover - the form above never carries them
                raise ValueError(f"Forge-X start would carry {', '.join(bad)}, which cancel the print")
        return lines

    def readiness_problems(self, state, status, selection=None, **kw):
        problems = super().readiness_problems(state, status, selection, **kw)
        status = status if isinstance(status, dict) else {}
        # Fail closed on the heaters, as on the OpenCreator adapter: a
        # sample that omits them must not pass as a ready printer.
        for name in ("extruder", "heater_bed"):
            if not isinstance(status.get(name), dict):
                problems.append(f"{name} is not reporting")
        mismatch = firmware_check.zmod_mismatch_problem(status)
        if mismatch:
            problems.append(mismatch)
        # The stock screen needs a different start form than the one these
        # profiles slice (start_contract.py, #1506 tranche 2): refuse it by name.
        stock = start_contract.readiness_screen_problem(status)
        if stock:
            problems.append(stock)
        return problems

    def moonraker_objects(self):
        # mod_params carries the screen mode (start_contract.py) and is the
        # Forge-X evidence the mismatch warning reads.
        return ["webhooks", "print_stats", "virtual_sdcard", "extruder", "heater_bed",
                "exclude_object", "mod_params"]

    def normalize_status(self, raw):
        return basic_status(raw)

    def spool_remain_g(self, status, tool):
        """Grams left on the spool, or None when the sample does not say.

        Read-only, the OpenCreator/U1 posture: looks for the remaining
        fields beside a spool id on the extruder object. Absence is not
        zero. This does not query the printer.
        """
        raw = status if isinstance(status, dict) else {}
        block = raw.get("extruder")
        if not isinstance(block, dict):
            return None
        for key in ("filament_remaining_g", "spool_remain_g"):
            value = block.get(key)
            if type(value) in (int, float):
                return float(value)
        return None
