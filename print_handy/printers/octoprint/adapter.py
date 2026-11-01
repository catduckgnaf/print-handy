"""Single extruder Marlin through OctoPrint, never through Moonraker."""
import os

from ..klipper.adapter import KlipperAdapter
from .client import OctoPrintClient, OctoPrintError


class OctoPrintAdapter(KlipperAdapter):
    transport = 'octoprint'

    def client(self):
        return OctoPrintClient(self.config['services'].get('octoprint_url', ''), os.environ.get('OCTOPRINT_API_KEY', ''))

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

    def set_targets(self, hotend_c=None, bed_c=None):
        """Set the nozzle (tool0) / bed target temperatures through OctoPrint.

        OctoPrint is the one non-Moonraker family whose API sets heater
        targets; the Web UI control API (#708) reaches temperatures only
        through this method (and Moonraker's heater script), so every
        other family keeps a named refusal instead of a guessed command.
        """
        return self.client().set_targets(hotend_c=hotend_c, bed_c=bed_c)

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
        return {'state': state, 'filename': ((j.get('job') or {}).get('file') or {}).get('path', ''),
                'progress': completion / 100 if isinstance(completion, (int, float)) else None,
                'hotend_c': (temp.get('tool0') or {}).get('actual'), 'bed_c': (temp.get('bed') or {}).get('actual')}
