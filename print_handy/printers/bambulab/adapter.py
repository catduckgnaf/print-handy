"""Bambu Lab printers: Orca slicing and G-code validation, plus the opt-in LAN path.

VENDOR (bambulab/__init__.py) is the authority for connection ("bambulab_lan"), start code, slicers and
delivery. The LAN path (client.py) exists only when the operator configures [bambu] lan_host and
lan_serial and supplies the LAN access code through an environment variable; without that configuration
the adapter behaves exactly as the original offline adapter did. On top of the read-only telemetry it
carries the whole LAN delivery path: the FTPS upload transport (client.py) behind `upload()`, print
start (the `project_file` command) and pause/resume/stop control over MQTT, and the chamber camera
(camera.py: port-6000 stills for A1/P1, the go2rtc fan-out for X1 RTSPS stills — #294/#295).
Each printer subclasses this adapter with its facts (see a1.py). Deliberately imports nothing from the
ZMod/AD5X code and has no Moonraker or cloud client.
"""
import json
import os
from pathlib import Path

from ..base import MultiColorUnsupported, PrinterAdapter
from . import ams, guard, hms, profiles
from .client import BambuLanClient, BambuLanError, signature_required


class LiveUnsupported(RuntimeError):
    """A Moonraker-shaped live call reached a Bambu Lab printer; Bambu uses its LAN path instead."""


# Bambu `gcode_state` values -> Print Handy's normalized state vocabulary (the Klipper/OctoPrint shape).
_GCODE_STATES = {
    "IDLE": "standby", "INIT": "standby", "OFFLINE": "unknown", "UNKNOWN": "unknown",
    "RUNNING": "printing", "PREPARE": "printing", "SLICING": "printing",
    "PAUSE": "paused", "FINISH": "complete", "FAILED": "error",
}
_BUSY_STATES = {"RUNNING", "PREPARE", "SLICING", "PAUSE"}


class BambuAdapter(PrinterAdapter):
    transport = "bambulab_lan"
    facts: dict = {}  # the printer's sourced facts (facts.py), set by each printer's subclass
    # Slicers this printer's guard accepts. Bambu Studio is the Bambu Lab default (owner decision); every line
    # of its output still passes the same guard, which refuses anything that is not plain G/M/T code.
    verified_slicers = ("bambu_studio", "orca")
    vendor_material_profiles = True  # filament_template() returns the checked vendor profile of the material

    def fact(self, name):
        return self.facts[name].value

    @property
    def slot_count(self):
        # The AMS is this printer's slot system (#671): one extruder fed by the
        # AMS unit's slots, counted per unit from the model's facts (every AMS
        # variant — AMS, AMS Lite, AMS 2 Pro — carries four trays per unit).
        # The dual-nozzle models stay at 1: two independent extruders are not a
        # slot system (their profiles record why), and their trays are resolved
        # per filament slot at print start (ams_mapping.py), not counted here.
        if self.multi_slot:
            return int(self.fact("ams")[1])
        return 1

    @property
    def motion_envelope_mm(self):
        return self.fact("motion_envelope_mm")

    def _unsupported(self, what):
        return LiveUnsupported(f"{self.label}: {what} is not supported over Moonraker; Bambu Lab printers "
                               "use the LAN path (status, upload and print start are separate calls). "
                               "Nothing was sent.")

    # -- LAN configuration -------------------------------------------------
    def _lan(self):
        return (self.config.get("bambu") or {})

    def lan_configured(self) -> bool:
        """True when [bambu] lan_host and lan_serial are both set (the access code is a secret, checked by client())."""
        lan = self._lan()
        return bool(str(lan.get("lan_host") or "").strip() and str(lan.get("lan_serial") or "").strip())

    def access_code(self) -> str:
        """The LAN access code from the environment variable [bambu] lan_access_code_env names. Never from config."""
        name = str(self._lan().get("lan_access_code_env") or "BAMBU_LAN_PASSWORD")
        return os.environ.get(name, "")

    def client(self, **overrides):
        """A LAN telemetry client for the configured printer. Raises BambuLanError when unconfigured."""
        if not self.lan_configured():
            raise BambuLanError(f"{self.label}: LAN telemetry is not configured; set [bambu] lan_host and "
                                "lan_serial and supply the LAN access code via the environment")
        return BambuLanClient(str(self._lan()["lan_host"]).strip(), str(self._lan()["lan_serial"]).strip(),
                              self.access_code(), **overrides)

    def upload(self, local_path, remote_name=None):
        """Upload a `.gcode.3mf` package to the printer's storage root (FTPS, size-verified).

        Stage (c): this is the transport the print-start stage builds on. By itself it
        starts nothing, and no delivery method calls it yet.
        """
        if not str(local_path).endswith(".gcode.3mf"):
            raise BambuLanError(f"{self.label}: only a packaged .gcode.3mf can be uploaded to a Bambu Lab "
                                "printer; slice and package the job first (the Bambu package delivery does this)")
        return self.client().upload(local_path, remote_name)

    def start_print(self, remote_name, plate="Metadata/plate_1.gcode", **kw):
        """Send the print-start command for an uploaded package. Gates live in the print path.

        Extra keyword arguments (ams_mapping, nozzle_mapping, submission_id) pass
        through to the LAN client unchanged (#308).
        """
        return self.client().start_print(remote_name, plate, **kw)

    def extruder_count(self) -> int:
        """How many extruders this model prints with (1 unless its facts say more, #308)."""
        return int(self.facts["extruders"].value) if "extruders" in self.facts else 1

    def control(self, action):
        """Pause, resume or stop the current print over LAN MQTT."""
        return self.client().control(action)

    def set_chamber_light(self, on):
        """Switch the chamber light over LAN MQTT (#516).

        The light is the one accessory the auto light-off feature may
        touch; this is a plain transport call — the automation policy
        and its gates live in runtime (light_control / family_control).
        """
        return self.client().set_chamber_light(on)

    # -- Camera (#294) ------------------------------------------------------
    def _camera_pin_gate(self):
        """Refuse camera work the [services] camera_source pin excludes (2.2.2).

        "none" turns camera images off everywhere the setting applies,
        including this printer's own camera; "moonraker" pins stills to
        the Moonraker webcam source, which this printer does not have.
        "auto", "bambu" and "go2rtc" leave the vendor paths below
        exactly as they were (the X1 stills already come through the
        go2rtc fan-out; the A1/P1 port-6000 path is this printer's own
        Bambu source).
        """
        from ... import go2rtc
        pin = go2rtc.camera_source(self.config)
        if pin == "none":
            raise BambuLanError(f"{self.label}: camera images are off "
                                "([services] camera_source is none); no snapshot was taken")
        if pin == "moonraker":
            raise BambuLanError(f"{self.label}: [services] camera_source is moonraker, the "
                                "Moonraker webcam source, which this printer does not have; "
                                "no snapshot was taken")

    def camera_kind(self):
        """The verified camera protocol for this model ("tcp6000" / "rtsps"), or None when unverified."""
        from . import camera
        return camera.camera_kind(self.model.key if self.model else None)

    def camera_snapshot(self, **overrides):
        """One bounded JPEG from the printer's own camera, via the LAN camera path (camera.py).

        This is the same internal snapshot seam the Moonraker path fills from
        `services.camera_snapshot_url`: callers get JPEG bytes or a plain
        BambuLanError, never a half answer. X1-family stills come through
        #246's go2rtc fan-out by decision (recorded on #294) — Print Handy
        grows no RTSP stack — and say exactly what to enable when the
        fan-out is not running for this printer; a model with no verified
        camera protocol says that instead of guessing.
        """
        from . import camera
        self._camera_pin_gate()
        kind = self.camera_kind()
        if kind is None:
            raise BambuLanError(f"{self.label}: Print Handy has not verified a camera protocol for this model; "
                                "no snapshot was taken")
        if not self.lan_configured():
            raise BambuLanError(f"{self.label}: the camera is not configured; set [bambu] lan_host and "
                                "lan_serial and supply the LAN access code via the environment")
        if kind == "rtsps":
            return camera.fanout_snapshot(str(self._lan()["lan_host"]).strip(), self.access_code(), **overrides)
        client = camera.BambuCameraClient(str(self._lan()["lan_host"]).strip(), self.access_code(), **overrides)
        return client.snapshot()

    def camera_frame_source(self):
        """(url, fetch) feeding the shared timelapse capture session (#295).

        The url names the frame source for the session manifest: the
        go2rtc stills URL for X1 models (their frames are go2rtc frames,
        fetched with camera.fetch_url_jpeg), a credential-free
        `bambu-tcp6000://` label for A1/P1 (fetch takes one port-6000
        snapshot per call and ignores the label). Raises BambuLanError
        naming the reason when this printer cannot feed frames — no
        verified protocol, LAN not configured, or the X1 fan-out not
        running for this printer — so the caller logs it once, plainly.
        """
        from . import camera
        self._camera_pin_gate()
        kind = self.camera_kind()
        if kind is None:
            raise BambuLanError(f"{self.label}: Print Handy has not verified a camera protocol for this model; "
                                "no timelapse frames can be captured")
        if not self.lan_configured():
            raise BambuLanError(f"{self.label}: the camera is not configured; set [bambu] lan_host and "
                                "lan_serial and supply the LAN access code via the environment")
        host = str(self._lan()["lan_host"]).strip()
        if kind == "rtsps":
            return camera.fanout_frame_url(host, self.access_code()), camera.fetch_url_jpeg
        return f"bambu-tcp6000://{host}", lambda _url: self.camera_snapshot()

    # slicer: the vendor start/end G-code is used as is; Print Handy adds and changes nothing. The only accepted
    # start and end are the ones in the verified generated profiles (profiles.py), so an [orca] templates
    # override with other vendor code is refused before Orca runs.
    def _vendor_gcode(self, key):
        return json.loads((self.pinned_templates() / "machine.json").read_text())[key]

    def machine_start_gcode(self, template_start, mesh="auto"):
        guard.require_bambu_code(template_start, f"{self.label} start G-code")
        if template_start != self._vendor_gcode("machine_start_gcode"):
            raise guard.ForeignStartCode(f"{self.label} start G-code is not the {self.fact('printer_model')} vendor "
                                         "sequence from the verified profiles; check [orca] templates")
        return template_start

    def machine_end_gcode(self, template_end):
        guard.require_bambu_code(template_end, f"{self.label} end G-code")
        if template_end != self._vendor_gcode("machine_end_gcode"):
            raise guard.ForeignStartCode(f"{self.label} end G-code is not the {self.fact('printer_model')} vendor "
                                         "sequence from the verified profiles; check [orca] templates")
        return template_end

    def start_plan(self, bed_temp, nozzle_temp, *, mesh="auto"):
        # Orca fills the vendor placeholders; these are the template lines the printer runs first.
        return self._vendor_gcode("machine_start_gcode").splitlines()

    def pinned_templates_folder(self):
        """Where this model's profiles are generated: <state>/orca-profiles/<templates>, never the repository."""
        from ... import settings
        return settings.generated_templates_dir(self.model, self.config)

    def pinned_templates(self):
        """The verified generated templates (profiles.require); an [orca] templates override is never the reference.

        Raises profiles.ProfileError when they are missing, edited or not a checked OrcaSlicer version.
        """
        return profiles.require(self)

    def filament_template(self, templates, material):
        """The checked vendor filament profile for material (profiles.filament_template), or ProfileError.

        In the verified generated folder the profile is re-verified. An [orca] templates override may carry its own
        filaments/<MATERIAL>.json or a filament.json of that material; the guard still holds the output to the
        verified profiles. A material with no profile is refused either way.
        """
        templates = Path(templates)
        if templates.resolve() == self.pinned_templates_folder().resolve():
            return profiles.filament_template(self, material, templates)
        key = profiles.material_key(material)
        own = templates / profiles.FILAMENTS / f"{key}.json"
        if own.is_file():
            return own
        default = templates / "filament.json"
        kind = profiles._first(json.loads(default.read_text()).get("filament_type"))
        if profiles.material_key(kind) != key:
            raise profiles.ProfileError(f"{self.label}: the [orca] templates folder {templates} has no {key} filament "
                                        f"profile (its filament.json is {kind}). Nothing was sliced")
        return default

    def filament_summary(self, state):
        """How the loaded filament will be named in summaries (#264): "Black PETG, your preset 'PETG fast'"
        with an operator preset registered for the material, "Black PETG (Generic PETG @BBL A1)" with the
        checked vendor profile, or None when the record names no material or no checked profile serves it.
        Display only; never raises.
        """
        try:
            spool = (state or {}).get("spool") or {}
            material = spool.get("material")
            if not material:
                return None
            colour = next((v.strip() for v in (spool.get("color"), spool.get("name"))
                           if isinstance(v, str) and v.strip()), "")
            base = f"{colour} {material}" if colour else str(material)
            key = profiles.material_key(material)
            try:
                entry = profiles.operator_presets(self).get(key)
            except profiles.ProfileError:
                entry = None
            if entry:
                return f"{base}, your preset '{entry['name']}'"
            names = profiles.filament_set(self)
            if key in names:
                return f"{base} ({names[key]})"
            if not names and key == "PLA":
                return f"{base} ({self.fact('orca_profiles')['filament']})"
            return None
        except Exception:
            return None

    def gcode_problems(self, text, state=None):
        return guard.gcode_problems(text, self, state=state)

    def moonraker_start_commands(self, remote_filename):
        raise self._unsupported("starting a print")

    def normalize_tool_mapping(self, mapping, state):
        tools = {str(k) for k in (mapping or {"0": 1})}
        if tools != {"0"}:
            raise MultiColorUnsupported(
                f"{self.label}: {self.fact('ams')[0]} multi-color is not supported; this job uses tools "
                f"{sorted(tools)}. Slice it single-color.")
        return {"0": 1}

    def readiness_problems(self, state, status, selection=None, **kw):
        """Hardware-record checks, plus live checks when `status` is a LAN snapshot (or its print payload).

        With no live status this is exactly the original offline behaviour. With a snapshot: a busy or
        errored printer is not ready, an active print error / HMS entry is reported, and the firmware
        signature bit (Developer Mode off) is reported because it blocks every future write.
        """
        problems = []
        state = state or {}
        bed, height = self.fact("bed_mm"), self.fact("max_print_height_mm")
        record_bed = state.get("bed_mm", bed)
        if any(float(v) > limit for v, limit in zip(record_bed, bed)):
            problems.append(f"recorded bed {record_bed[0]:g}x{record_bed[1]:g} mm exceeds the {self.label} "
                            f"{bed[0]:g}x{bed[1]:g} mm printable area")
        if float(state.get("max_print_height_mm", height)) > height:
            problems.append(f"recorded height {float(state['max_print_height_mm']):g} mm exceeds the {self.label} "
                            f"{height:g} mm limit")
        nozzle = state.get("nozzle") or {}
        if nozzle.get("diameter_mm") not in self.supported_nozzle_sizes:
            problems.append(f"recorded nozzle {nozzle.get('diameter_mm')!r} mm has no {self.label} template "
                            f"(supported: {', '.join(f'{s:g}' for s in self.supported_nozzle_sizes)})")
        from ...core.nozzle import NozzleError, normalize_nozzle_type
        try:
            kind = normalize_nozzle_type(nozzle.get("type") or "")
        except NozzleError:
            kind = nozzle.get("type")
        if kind not in self.supported_nozzle_types:
            problems.append(f"recorded nozzle type {nozzle.get('type')!r} is not an {self.label} option "
                            f"({', '.join(self.supported_nozzle_types)})")
        payload = _print_payload(status)
        if payload is not None:
            gcode_state = str(payload.get("gcode_state") or "").upper()
            if gcode_state in _BUSY_STATES:
                problems.append(f"printer is busy ({gcode_state.lower()})")
            if payload.get("print_error"):
                look = hms.lookup_error(payload["print_error"], self._hms_prefix())
                if look.listed and look.text:
                    problems.append(f"printer reports a print error: {look.text} (code {payload['print_error']})")
                elif not look.listed:
                    problems.append(f"printer reports a print error (code {payload['print_error']})")
                # Listed-but-empty is not a fault (#306): Bambu lists the code with no text.
            entries = [h for h in (payload.get("hms") or []) if isinstance(h, dict) and h.get("attr", 0)]
            faults = []
            for entry in entries:
                look = hms.decode_entry(entry, self._hms_prefix())
                if look is None or look.is_fault:
                    faults.append((entry, look))
            for entry, look in faults:
                if look is not None and look.text:
                    problems.append(f"printer reports an HMS error: {look.text} (HMS {look.key})")
            unknown = [(e, l) for e, l in faults if l is None or not l.text]
            if unknown:
                codes = ", ".join(str(l.key) if l is not None else
                                  f"attr={e.get('attr')} code={e.get('code')}" for e, l in unknown)
                problems.append(f"printer reports {len(unknown)} active HMS error(s) (unknown code(s): {codes})")
            if signature_required(payload):
                problems.append("printer firmware requires signed MQTT commands (Developer Mode is off): "
                                "telemetry works, but upload and print control would be blocked")
        return problems

    def _hms_prefix(self):
        """The Bambu Studio device prefix for HMS texts: the first three characters of the
        configured serial, when the generated HMS table carries that prefix; else None
        (common texts apply)."""
        serial = str(self._lan().get("lan_serial") or "").strip().upper()
        prefix = serial[:3]
        return prefix if prefix in hms.prefixes() else None

    def moonraker_objects(self):
        raise self._unsupported("Moonraker telemetry (this printer reports over Bambu LAN MQTT)")

    def normalize_status(self, raw):
        """A LAN snapshot (client.snapshot()) or bare print payload -> the common status shape.

        Anything else (including a Moonraker-shaped dict) normalizes to unknown, as before.
        Temperatures come from the classic fields; newer firmware's packed `device.*.info.temp`
        words are not decoded in this stage. The `ams` entry is the AMS state the printer
        itself reports, decoded read-only by ams.py (#307); it changes no gate and no payload.
        """
        payload = _print_payload(raw)
        if payload is None:
            return {"state": "unknown", "filename": "", "progress": None, "hotend_c": None, "bed_c": None,
                    "ams": None}
        percent = payload.get("mc_percent")
        remaining = payload.get("mc_remaining_time")
        gcode_state = str(payload.get("gcode_state") or "").upper()
        return {
            "state": _GCODE_STATES.get(gcode_state, "unknown"),
            "gcode_state": gcode_state,  # the raw Bambu value, kept for start-confirmation evidence (#271)
            "stage": payload.get("mc_print_stage"),
            "filename": payload.get("subtask_name") or payload.get("gcode_file") or "",
            "progress": percent / 100 if isinstance(percent, (int, float)) and not isinstance(percent, bool) else None,
            "hotend_c": _number(payload.get("nozzle_temper")),
            "bed_c": _number(payload.get("bed_temper")),
            "hotend_target_c": _number(payload.get("nozzle_target_temper")),
            "bed_target_c": _number(payload.get("bed_target_temper")),
            "remaining_min": remaining if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) else None,
            "layer": payload.get("layer_num"),
            "total_layer": payload.get("total_layer_num"),
            "writes_blocked": signature_required(payload),
            "hms_errors": self._decoded_hms(payload),
            "print_error_text": self._decoded_print_error(payload),
            # AMS state the printer itself reports (#307): decoded trays / external spool /
            # Filament Backup, read-only. None when the snapshot carried no AMS block.
            # The get_version reply in the same snapshot types the units (AMS vs AMS Lite).
            "ams": ams.decode_ams(payload, raw.get("info") if isinstance(raw, dict) else None),
        }

    def _decoded_hms(self, payload):
        """The payload's `hms[]` entries decoded (#306): each as {code, description, listed};
        description None for an unlisted code, "" for a listed-but-empty one."""
        out = []
        for entry in payload.get("hms") or []:
            if not isinstance(entry, dict) or not entry.get("attr", 0):
                continue
            look = hms.decode_entry(entry, self._hms_prefix())
            if look is None:
                out.append({"code": None, "description": None, "listed": False})
            else:
                out.append({"code": look.key, "description": look.text, "listed": look.listed})
        return out

    def _decoded_print_error(self, payload):
        """The decoded `print_error` description (#306), or None when the value is absent,
        unlisted, or listed-but-empty."""
        if not payload.get("print_error"):
            return None
        look = hms.lookup_error(payload["print_error"], self._hms_prefix())
        return look.text if look.listed and look.text else None


def _print_payload(raw):
    """The Bambu `print` payload inside a snapshot dict, or `raw` itself when it is one. None otherwise."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("print"), dict):
        return raw["print"]
    if "gcode_state" in raw or "mc_percent" in raw:
        return raw
    return None


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# Backwards-compatible name: the A1/P1S modules and older tests import the offline-era class name.
BambuOfflineAdapter = BambuAdapter
