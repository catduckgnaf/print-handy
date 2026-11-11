"""Qidi printers: stock Klipper + Moonraker transport, pinned Orca templates.

The Qidi line runs ordinary Klipper behind Moonraker, so the transport methods are the
Klipper adapter's unchanged. What differs from klipper.generic is identity: the generic
profile's default bed (220 x 220 x 250 mm) matches no Qidi model, so each model carries its
own sourced facts and generated, identity-pinned Orca templates, and the start code is the
template's own sequence — Qidi's profiles open with the printer's PRINT_START macro and
continue inline, so the Klipper adapter's macro start is overridden with the template's
sequence verbatim. Each printer subclasses this adapter with its facts (see q1pro.py).
"""
import json

from ..klipper.adapter import KlipperAdapter
from . import guard, profiles


class QidiAdapter(KlipperAdapter):
    # Not a Bondtech INDX host (#1198): the INDX code inherited from KlipperAdapter never runs here.
    supported_accessories = frozenset()
    facts: dict = {}  # the printer's sourced facts (facts.py), set by each printer's subclass
    verified_slicers = ("orca",)

    def fact(self, name):
        return self.facts[name].value

    @property
    def motion_envelope_mm(self):
        return self.fact("motion_envelope_mm")

    def pinned_templates_folder(self):
        """Where this model's profiles are generated: <state>/orca-profiles/<templates>, never the repository."""
        from ... import settings
        return settings.generated_templates_dir(self.model, self.config)

    def pinned_templates(self):
        """The verified generated templates (profiles.require); an [orca] templates override is never the reference."""
        return profiles.require(self)

    def machine_start_gcode(self, template_start, mesh="auto"):
        return template_start  # the template's PRINT_START-led sequence is the start code

    def machine_end_gcode(self, template_end):
        return template_end

    def start_plan(self, bed_temp, nozzle_temp, *, mesh="auto"):
        # The vendor start sequence from the verified generated templates, as sliced.
        try:
            machine = json.loads((self.pinned_templates() / "machine.json").read_text())
        except Exception:
            return []
        return str(machine.get("machine_start_gcode") or "").splitlines()

    def gcode_problems(self, text, state=None):
        return guard.gcode_problems(text, self, state=state)
