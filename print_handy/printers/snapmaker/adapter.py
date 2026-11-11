"""Snapmaker U1: four toolheads on a Klipper-based firmware, reached through Moonraker.

Toolheads are tools, not IFS slots. Slicer tool i always prints with toolhead
i. A mapping that asks for anything else is refused. Spool remain is read only
when a status sample already carries a number; the U1's object names are not
confirmed on hardware.
"""
from ..base import MultiColorUnsupported
from ..klipper.adapter import KlipperAdapter
from . import facts, guard


class SnapmakerAdapter(KlipperAdapter):
    # Not a Bondtech INDX host (#1198): the INDX code inherited from KlipperAdapter never runs here.
    supported_accessories = frozenset()
    facts = facts.U1

    def fact(self, name):
        return self.facts[name].value

    @property
    def tool_count(self):
        return int(self.config["printer"].get("tool_count", 1))

    @property
    def slot_count(self):
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

    def gcode_problems(self, text, state=None):
        return guard.gcode_problems(text, self, state=state)

    def moonraker_objects(self):
        return ["webhooks", "print_stats", "virtual_sdcard",
                *[self._extruder_name(tool) for tool in range(self.tool_count)],
                "heater_bed", "exclude_object"]

    def spool_remain_g(self, status, tool):
        """Grams left on one toolhead, or None when the sample does not say.

        Looks for filament_spool_id's sibling remaining fields. Absence is not
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
