"""Single extruder Marlin through OctoPrint, never through Moonraker."""
import json
import os

from ..klipper.adapter import KlipperAdapter
from . import guard
from .client import OctoPrintClient, OctoPrintError


class OctoPrintAdapter(KlipperAdapter):
    # Not a Bondtech INDX host (#1198): the INDX code inherited from KlipperAdapter never runs here.
    supported_accessories = frozenset()
    transport = 'octoprint'

    def fact(self, name):
        """A model fact for the family guard and the heater ceiling, from the model's own declarations.

        The OctoPrint models carry no separate facts table: bed and height are the
        model settings, the expected printer_model is the one the model's pinned
        Orca template declares, and the motion envelope is the bed itself (the
        Marlin start sequence never parks off the bed) with the same derived
        travel headroom the Prusa facts record. Temperature limits are declared
        by no OctoPrint model today, so max_hotend_c / max_bed_c come only from
        the model's own settings when they carry them and raise KeyError
        otherwise — a caller reading a limit through fact() treats that as "no
        limit declared", exactly as core.heater_ceiling does.
        """
        printer = self.config.get("printer") or {}
        if name == "bed_mm":
            return tuple(float(v) for v in printer["bed_mm"])
        if name == "max_print_height_mm":
            return float(printer["max_print_height_mm"])
        if name == "motion_envelope_mm":
            bed = self.fact("bed_mm")
            return (0.0, 0.0, bed[0], bed[1])
        if name == "travel_z_headroom_mm":
            return 1.0
        if name == "bed_exclude_area":
            return ()
        if name == "printer_model":
            return self._template_printer_model()
        if name in ("max_hotend_c", "max_bed_c") and name in printer:
            return float(printer[name])
        raise KeyError(name)

    def _template_printer_model(self):
        """The printer_model the model's pinned Orca template declares, read once per adapter."""
        cached = getattr(self, "_printer_model_cache", None)
        if cached is not None:
            return cached
        try:
            machine = json.loads((self.templates_dir() / "machine.json").read_text())
            model = machine["printer_model"]
        except Exception as exc:
            raise KeyError("printer_model") from exc
        self._printer_model_cache = str(model)
        return self._printer_model_cache

    def gcode_problems(self, text, state=None):
        # The family guard (#1469), not the Klipper INDX check this class
        # inherits: no OctoPrint model is an INDX host, so the inherited
        # early return left every OctoPrint/Marlin model unchecked.
        return guard.gcode_problems(text, self, state=state)

    def client(self):
        return OctoPrintClient(self.config['services'].get('octoprint_url', ''), os.environ.get('OCTOPRINT_API_KEY', ''))

    def send_gcode(self, lines):
        """Send raw G-code lines through the client's printer-command path.

        The Web UI send-G-code route (ha-print-handy #1) calls this the
        same way controls call set_target_temperature on other families:
        one entry per non-empty line, in order. Blank lines are dropped
        here as well so no caller can post an empty command.
        """
        commands = [str(line).strip() for line in (lines or []) if str(line).strip()]
        if not commands:
            raise OctoPrintError('no G-code commands to send; nothing sent')
        return self.client().send_commands(commands)

    def control(self, action):
        """Pause, resume or stop the current print through OctoPrint's job API.

        The shared control vocabulary is pause/resume/stop (the shape the
        other families' adapters expose); OctoPrint's own job command for
        stopping is "cancel", so the translation lives here, in the family's
        adapter, and callers never see it.
        """
        actions = {'pause': 'pause', 'resume': 'resume', 'stop': 'cancel'}
        if action not in actions:
            raise OctoPrintError(f'{action} is unsupported by the OctoPrint integration')
        return self.client().control(actions[action])

    def machine_start_gcode(self, template_start, mesh='auto'):
        return template_start

    def machine_end_gcode(self, template_end):
        return template_end  # Slicer owns the entire end sequence.

    def start_plan(self, bed_temp, nozzle_temp, *, mesh='auto'):
        return [f'M190 S{bed_temp:g}', f'M109 S{nozzle_temp:g}']

    def moonraker_start_commands(self, remote_filename):
        raise OctoPrintError('Moonraker start is unsupported for OctoPrint')

    def moonraker_objects(self):
        raise OctoPrintError('Moonraker objects are unsupported for OctoPrint')

    def readiness_problems(self, state, status, selection=None, **kw):
        try:
            OctoPrintClient.require_ready(status)
        except OctoPrintError as exc:
            return [str(exc)]
        return []

    def normalize_status(self, raw):
        p = raw.get('printer') or {}
        j = raw.get('job') or {}
        flags = (p.get('state') or {}).get('flags') or {}
        state = ('paused' if flags.get('paused') else 'printing' if flags.get('printing') else
                 'standby' if flags.get('ready') and flags.get('operational') else 'unknown')
        temp = p.get('temperature') or {}
        completion = (j.get('progress') or {}).get('completion')
        left = _number((j.get('progress') or {}).get('printTimeLeft'))  # seconds; null when unknown
        # Plain OctoPrint reports no layer numbers, so no layer keys are emitted here.
        return {'state': state, 'filename': ((j.get('job') or {}).get('file') or {}).get('path', ''),
                'progress': completion / 100 if isinstance(completion, (int, float)) else None,
                'hotend_c': (temp.get('tool0') or {}).get('actual'), 'bed_c': (temp.get('bed') or {}).get('actual'),
                'hotend_target_c': _number((temp.get('tool0') or {}).get('target')),
                'bed_target_c': _number((temp.get('bed') or {}).get('target')),
                'remaining_min': left // 60 if left is not None else None}


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
