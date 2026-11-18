"""Elegoo Centauri printers: Orca slicing and G-code validation, plus opt-in SDCP LAN.

VENDOR (elegoo_sdcp/__init__.py) is the authority for connection ("elegoo_sdcp"), start code,
slicers and delivery. The LAN path (client.py) exists only when the operator configures
[elegoo_sdcp] host; SDCP has no authentication, so there is no secret to supply. Without the
host configured the adapter behaves as an offline adapter. Mirrors the Prusa adapter's shape
deliberately: status, verified upload, gated print start, pause/resume/stop — nothing
Moonraker-shaped. See docs/ELEGOO.md.
"""
import json

from ..base import MultiColorUnsupported, PrinterAdapter
from . import guard, profiles
from .client import SdcpClient, SdcpError


class LiveUnsupported(RuntimeError):
    """A Moonraker-shaped live call reached a Centauri printer; Centauri uses SDCP instead."""


# SDCP machine status codes -> Print Handy's normalized state vocabulary (client.py documents
# the codes; 1 homing and 9 heating are pre-print motion, 10 is file transfer).
_STATUS_STATES = {0: "standby", 1: "printing", 2: "printing", 5: "paused", 6: "error",
                  7: "standby", 8: "complete", 9: "printing", 10: "printing"}
_BUSY_CODES = {1, 2, 5, 9, 10}


class ElegooSdcpAdapter(PrinterAdapter):
    transport = "elegoo_sdcp"
    facts: dict = {}  # the printer's sourced facts (facts.py), set by the printer's subclass
    verified_slicers = ("orca",)

    def fact(self, name):
        return self.facts[name].value

    @property
    def slot_count(self):
        return 1

    @property
    def motion_envelope_mm(self):
        return self.fact("motion_envelope_mm")

    def _unsupported(self, what):
        return LiveUnsupported(f"{self.label}: {what} is not supported over Moonraker; Centauri printers "
                               "use SDCP (status, upload and print start are separate calls). "
                               "Nothing was sent.")

    # -- LAN configuration ---------------------------------------------------
    def _sdcp(self):
        return (self.config.get("elegoo_sdcp") or {})

    def sdcp_configured(self) -> bool:
        """True when [elegoo_sdcp] host is set (SDCP needs nothing else — it has no auth)."""
        return bool(str(self._sdcp().get("host") or "").strip())

    def client(self, **overrides):
        """An SDCP client for the configured printer. Raises SdcpError when unconfigured."""
        if not self.sdcp_configured():
            raise SdcpError(f"{self.label}: SDCP is not configured; set [elegoo_sdcp] host to the "
                            "printer's LAN address")
        return SdcpClient(str(self._sdcp()["host"]).strip(), **overrides)

    def snapshot(self):
        """One SDCP status answer (Cmd 0), the shape normalize_status consumes."""
        return self.client().status()

    def upload(self, local_path, remote_name=None):
        """Upload a sliced file (chunked HTTP, verified by the client). Starts nothing by itself."""
        return self.client().upload(local_path, remote_name)

    def start_print(self, remote_name):
        """Start an uploaded, verified file. Gates live in the print path."""
        return self.client().start_print(remote_name)

    def control(self, action):
        """Pause, resume or stop the current print over SDCP."""
        return self.client().control(action)

    def set_light(self, on):
        """Switch the chamber light over SDCP (#516).

        The light is the one accessory the auto light-off feature may
        touch; this is a plain transport call — the automation policy
        and its gates live in runtime (light_control / family_control).
        """
        return self.client().set_light(on)

    # -- slicer ---------------------------------------------------------------
    def pinned_templates_folder(self):
        """Where this model's profiles are generated: <state>/orca-profiles/<templates>, never the repository."""
        from ... import settings
        return settings.generated_templates_dir(self.model, self.config)

    def pinned_templates(self):
        """The verified generated templates (profiles.require); an [orca] templates override is never the reference."""
        return profiles.require(self)

    def machine_start_gcode(self, template_start, mesh="auto"):
        return template_start  # the slicer owns the entire start sequence

    def machine_end_gcode(self, template_end):
        return template_end  # the slicer owns the entire end sequence

    def start_plan(self, bed_temp, nozzle_temp, *, mesh="auto"):
        # The vendor start sequence from the verified generated templates, as sliced.
        try:
            machine = json.loads((self.pinned_templates() / "machine.json").read_text())
        except Exception:
            return []
        return str(machine.get("machine_start_gcode") or "").splitlines()

    def moonraker_start_commands(self, remote_filename):
        raise self._unsupported("starting a print")

    def moonraker_objects(self):
        raise self._unsupported("Moonraker telemetry (this printer reports over SDCP)")

    def gcode_problems(self, text, state=None):
        return guard.gcode_problems(text, self, state=state)

    # -- color / tools -----------------------------------------------------------
    def normalize_tool_mapping(self, mapping, state):
        tools = {str(k) for k in (mapping or {"0": 1})}
        if tools != {"0"}:
            raise MultiColorUnsupported(
                f"{self.label}: multi-tool jobs are not supported; this job uses tools "
                f"{sorted(tools)}. Slice it single-tool (tool 0).")
        return {"0": 1}

    # -- readiness / status ---------------------------------------------------------
    def readiness_problems(self, state, status, selection=None, **kw):
        """Hardware-record checks, plus live checks when `status` is an SDCP status answer.

        With no status this is the offline behaviour: the recorded bed/height/nozzle must fit
        the model. With a status answer, a busy printer is not ready and an error state is
        reported.
        """
        problems = []
        state = state or {}
        bed, height = self.fact("bed_mm"), self.fact("max_print_height_mm")
        record_bed = state.get("bed_mm", bed)
        if any(float(v) > limit for v, limit in zip(record_bed, bed)):
            problems.append(f"recorded bed {record_bed[0]:g}x{record_bed[1]:g} mm exceeds the {self.label} "
                            f"{bed[0]:g}x{bed[1]:g} mm printable area")
        if float(state.get("max_print_height_mm", height)) > height:
            problems.append(f"recorded height {float(state['max_print_height_mm']):g} mm exceeds the "
                            f"{self.label} {height:g} mm limit")
        nozzle = state.get("nozzle") or {}
        if nozzle.get("diameter_mm") not in self.supported_nozzle_sizes:
            problems.append(f"recorded nozzle {nozzle.get('diameter_mm')!r} mm has no {self.label} template "
                            f"(supported: {', '.join(f'{s:g}' for s in self.supported_nozzle_sizes)})")
        code = _status_code(status)
        if code is not None:
            if code in _BUSY_CODES:
                problems.append(f"printer is busy (SDCP status {code})")
            elif _STATUS_STATES.get(code) == "error":
                problems.append(f"printer reports an error state (SDCP status {code})")
        return problems

    def normalize_status(self, raw):
        """An SDCP status answer (client.status()) -> the common status shape."""
        if not isinstance(raw, dict):
            return {"state": "unknown", "filename": "", "progress": None, "hotend_c": None, "bed_c": None}
        status = raw.get("Status") if isinstance(raw.get("Status"), dict) else {}
        info = raw.get("PrintInfo") if isinstance(raw.get("PrintInfo"), dict) else {}
        code = _status_code(raw)
        total = info.get("TotalLayer") or 0
        current = info.get("CurrentLayer") or 0
        progress = (current / total) if total else None
        filename = str(info.get("Filename") or info.get("FileName") or "").rsplit("/", 1)[-1]
        remaining = info.get("RemainTime", info.get("RemainingTime"))
        return {
            "state": _STATUS_STATES.get(code, "unknown") if code is not None else "unknown",
            "filename": filename,
            "progress": progress,
            "hotend_c": _number(status.get("TempOfNozzle", info.get("TempOfNozzle"))),
            "bed_c": _number(status.get("TempOfHotbed", info.get("TempOfHotbed"))),
            "hotend_target_c": _number(status.get("TargetTempOfNozzle")),
            "bed_target_c": _number(status.get("TargetTempOfHotbed")),
            "layer": current or None,
            "total_layers": total or None,
            "remaining_min": (remaining // 60 if isinstance(remaining, (int, float))
                              and not isinstance(remaining, bool) else None),
            "status_code": code,
        }


def _status_code(raw):
    """The first CurrentStatus code from an SDCP status answer, or None when raw is not one."""
    if not isinstance(raw, dict):
        return None
    status = raw.get("Status") if isinstance(raw.get("Status"), dict) else None
    if status is None:
        return None
    codes = status.get("CurrentStatus")
    if isinstance(codes, list) and codes and isinstance(codes[0], int):
        return codes[0]
    return codes if isinstance(codes, int) else None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
