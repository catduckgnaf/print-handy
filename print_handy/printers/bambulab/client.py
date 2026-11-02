"""Bambu Lab LAN transport: MQTT over TLS on port 8883; FTPS upload on port 990.

Protocol facts are from docs/BAMBU-CONNECTIVITY.md (#187), verified against the maintained
integrations at pinned commits: subscribe to ``device/<serial>/report``, publish to
``device/<serial>/request``, user ``bblp`` with the printer's LAN access code as password.
A1/P1 printers report deltas, so a snapshot is only complete after a ``pushall`` request
answers with a full ``push_status`` (``msg == 0``); ``get_version`` identifies the machine.

Print commands (stage d) are the ``project_file`` start and pause/resume/stop payloads in
this module. They are dumb transport only: every gate that must hold before one is sent
(LAN configured, printer idle, firmware signature bit clear, bed-clear approval consumed,
submission lease held) lives in the print path (``core/model_job._print_bambu``), never
here. The FTPS side uploads one ``.gcode.3mf`` package to the printer's storage root and
verifies it by reading the remote size back — uploading never starts a print by itself.

TLS: the peer certificate chain is verified against Bambu Lab's own CA bundle
(``bambu_ca.pem`` beside this file: the public BBL CA certificates, carried via the
MIT-licensed ha-bambulab repository at commit 0e027ff135a6d9265cb756d3e246747954c76722;
root subject ``CN = BBL CA2 RSA``, SHA-256 fingerprint
E9:8F:19:57:8B:3F:12:4A:CE:6B:8A:24:7F:FE:DA:52:DC:99:C8:9F:D4:E7:D2:0C:82:82:99:77:B7:F3:35:02).
Hostname checking is off because the certificate CN is the printer serial, not its IP
address — the same posture as pybambu. There is deliberately no CERT_NONE mode.
TLS is capped at 1.2: some Bambu firmware never answers a TLS 1.3 ClientHello.

Both transports are injectable (``mqtt_factory`` / ``ftp_factory``) so tests run fully
offline with fakes, following the OctoPrint client's injectable-opener pattern. Error
messages never include the access code or a payload body.
"""
from __future__ import annotations

import copy
import ftplib
import json
import secrets
import ssl
import time
from pathlib import Path

MQTT_PORT = 8883
FTPS_PORT = 990
MQTT_USER = "bblp"
CA_FILE = Path(__file__).with_name("bambu_ca.pem")

GET_VERSION = {"info": {"sequence_id": "0", "command": "get_version"}}
PUSHALL = {"pushing": {"sequence_id": "0", "command": "pushall"}}
START_PUSH = {"pushing": {"sequence_id": "0", "command": "start"}}

# Print controls, same shape in pybambu's commands.py at the pinned commit.
PRINT_CONTROLS = ("pause", "resume", "stop")


def chamber_light_payload(on):
    """The `system/ledctrl` command switching the chamber light (#516).

    The chamber light is a fixed protocol target — led_node
    "chamber_light" — switched with led_mode on/off; the timing fields
    are the steady-state values pybambu and the Home Assistant
    integration send (500 ms on/off times, no looping), as recorded in
    docs/BAMBU-CONNECTIVITY.md. Whether a given printer has the light
    is reported by the printer itself in its `lights_report` telemetry;
    this builder just builds the command.
    """
    return {"system": {"sequence_id": "0", "command": "ledctrl",
                       "led_node": "chamber_light",
                       "led_mode": "on" if on else "off",
                       "led_on_time": 500, "led_off_time": 500,
                       "loop_times": 0, "interval_time": 0}}


# The `project_file` sequence id every maintained client uses for a print start
# (#308, from the #298 Stage 0 audit of maziggy/bambuddy at 98ecaf5: protocol facts
# only, no code copied — Bambuddy is AGPL-3.0, this repo is MIT).
PROJECT_FILE_SEQUENCE_ID = "20000"

# Canonical AMS tray ids in a resolved mapping (ams_mapping.py produces these):
# a regular AMS / AMS Lite tray is ams_id * 4 + slot_id, an AMS-HT unit is its unit
# id (128-135, single tray), the external spool is 254 (left on a dual-nozzle
# printer) or 255 (right), and -1 is a filament slot with no tray.
EXTERNAL_TRAYS = (254, 255)
UNMAPPED_TRAY = -1


def _new_submission_id():
    """One fresh submission id for a print start: epoch milliseconds, clamped.

    The audit records that Bambuddy shares one fresh id across project/task/subtask
    and clamps it — oversized values make P1 firmware treat new jobs as
    continuations of an old one. Epoch ms is 13 digits today; the clamp keeps it
    there whatever the clock says.
    """
    return str(max(0, min(int(time.time() * 1000), 9_999_999_999_999)))


def _ams_wire_forms(mapping):
    """(flat `ams_mapping`, `ams_mapping2`, `use_ams`) for a canonical tray mapping.

    Wire shapes from the #298 audit (row 4): the flat list converts external
    spools to -1 (the firmware rejects raw 254/255 there), while `ams_mapping2`
    entries {ams_id, slot_id} carry the externals, with an unmapped slot as
    {255, 255}; an AMS-HT unit (128-135) passes through as its own ams_id with
    slot 0. `use_ams` is a JSON bool, reconciled from the mapping — any real
    tray makes it True, an all-external/unmapped mapping makes it False — and
    never an integer: on dual-nozzle firmware an int is read as a nozzle index.
    """
    flat, detailed = [], []
    for tray in mapping:
        if tray == UNMAPPED_TRAY:
            flat.append(-1)
            detailed.append({"ams_id": 255, "slot_id": 255})
        elif tray in EXTERNAL_TRAYS:
            flat.append(-1)
            detailed.append({"ams_id": tray, "slot_id": 0})
        elif 128 <= tray <= 135:  # AMS-HT unit: single tray, passes through
            flat.append(tray)
            detailed.append({"ams_id": tray, "slot_id": 0})
        else:
            flat.append(tray)
            detailed.append({"ams_id": tray // 4, "slot_id": tray % 4})
    use_ams = any(tray not in (UNMAPPED_TRAY, *EXTERNAL_TRAYS) for tray in mapping)
    return flat, detailed, use_ams


def project_file_payload(remote_name, plate="Metadata/plate_1.gcode", *, ams_mapping=None,
                         nozzle_mapping=None, submission_id=None):
    """The `project_file` print-start command for a package already on the printer.

    Field set triangulated from pybambu's PRINT_PROJECT_FILE_TEMPLATE and
    bambulabs_api's working LAN client (both cited in the #187 research): `param` is
    the plate G-code inside the package, `file`/`url` name the uploaded package.
    Calibration flags are all false — the behaviour the Home Assistant service call
    uses (its template defaults them true and the call overrides to false) — so the
    printer runs the file as sliced, without extra auto-calibration passes Print
    Handy did not ask for.

    AMS (#308, wire shapes from the #298 audit): with no mapping the payload is the
    legacy single-tray form (`ams_mapping: [0]`, `use_ams: false`) — the file was
    sliced for the recorded spool. A resolved mapping (canonical global tray ids,
    see ams_mapping.py) is sent in both wire forms with `use_ams` reconciled, and
    `md5` is sent empty (validation skipped deliberately, as in the audited
    client); no `cfg` field is ever sent. `nozzle_mapping` (H2C rack: physical
    nozzle ids) is included only when a caller supplies a resolved list — when
    rack resolution is unknown the field is omitted and the firmware auto-picks,
    which the audit records as preferable to a wrong id.
    """
    if ams_mapping is None:
        flat, detailed, use_ams = [0], [{"ams_id": 0, "slot_id": 0}], False
    else:
        flat, detailed, use_ams = _ams_wire_forms(list(ams_mapping))
    submission = str(submission_id) if submission_id is not None else _new_submission_id()
    body = {"sequence_id": PROJECT_FILE_SEQUENCE_ID, "command": "project_file", "param": plate,
            "file": remote_name, "url": f"ftp:///{remote_name}",
            "bed_type": "auto", "timelapse": False,
            "bed_leveling": False, "flow_cali": False, "vibration_cali": False,
            "layer_inspect": False, "use_ams": use_ams, "ams_mapping": flat,
            "ams_mapping2": detailed, "md5": "",
            "subtask_name": remote_name,
            "profile_id": "0", "project_id": submission, "subtask_id": submission,
            "task_id": submission}
    if nozzle_mapping is not None:
        body["nozzle_mapping"] = list(nozzle_mapping)
    return {"print": body}

# print.fun bit: the firmware requires signed MQTT commands (Developer Mode is off),
# so every write is blocked even though reads keep working. See docs/BAMBU-CONNECTIVITY.md.
MQTT_SIGNATURE_REQUIRED = 0x20000000


class BambuLanError(RuntimeError):
    """A LAN telemetry failure, with no credential or payload detail attached."""


def signature_required(print_payload) -> bool:
    """True when the telemetry payload's `fun` bitfield says writes need signed commands."""
    fun = (print_payload or {}).get("fun")
    if not isinstance(fun, str):
        return False
    try:
        return bool(int(fun, 16) & MQTT_SIGNATURE_REQUIRED)
    except ValueError:
        return False


def _id_keyed(entries) -> bool:
    """True when every entry is a dict carrying an `id` (AMS units, their trays)."""
    return all(isinstance(entry, dict) and "id" in entry for entry in entries)


def _merge_id_list(accumulated: list, update: list) -> list:
    """Merge two id-keyed lists entry by entry: entries the update names are
    merged in place, entries it does not name survive, new ids append."""
    out = copy.deepcopy(accumulated)
    positions = {entry["id"]: index for index, entry in enumerate(out)}
    for entry in update:
        if entry["id"] in positions:
            out[positions[entry["id"]]] = merge_print(out[positions[entry["id"]]], entry)
        else:
            positions[entry["id"]] = len(out)
            out.append(copy.deepcopy(entry))
    return out


def merge_print(accumulated: dict, update: dict) -> dict:
    """Merge one (possibly delta) `print` payload into the accumulated snapshot state.

    Id-keyed lists (the AMS unit list, a unit's tray list) merge by id
    (P2-107): a delta naming one tray must not discard its siblings or the
    fields it does not repeat. Every other list replaces wholesale.
    """
    out = copy.deepcopy(accumulated)
    for key, value in (update or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge_print(out[key], value)
        elif (isinstance(value, list) and value and isinstance(out.get(key), list)
              and _id_keyed(out[key]) and _id_keyed(value)):
            out[key] = _merge_id_list(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def lan_ssl_context() -> ssl.SSLContext:
    """The verified TLS context for a printer connection (see module docstring)."""
    context = ssl.create_default_context(cafile=str(CA_FILE))
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False  # the certificate CN is the serial, never the IP
    return context


def _paho_factory(host, serial, access_code, client_id=None):
    """Build the real Paho client. Imported lazily: paho-mqtt is only needed with LAN on.

    The default client ID is the persistent session's (`print-handy-<serial>`),
    the one the listener connects with. A caller that must not disturb that
    session — a snapshot read (P2-265) — passes its own distinct `client_id`:
    MQTT disconnects the existing client when a new connection reuses its ID.
    """
    client_id = client_id or f"print-handy-{serial.lower()}"
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise BambuLanError("paho-mqtt is not installed; Bambu Lab LAN telemetry needs it "
                            "(pip install paho-mqtt, or use the full requirements.txt)") from exc
    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv311,
                             client_id=client_id, clean_session=True)
    except (AttributeError, TypeError):  # paho < 2 has no CallbackAPIVersion
        client = mqtt.Client(protocol=mqtt.MQTTv311, client_id=client_id,
                             clean_session=True)
    client.username_pw_set(MQTT_USER, password=access_code)
    client.tls_set_context(lan_ssl_context())
    client.tls_insecure_set(True)  # hostname only; the chain is still verified (module docstring)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


class _ImplicitFTPS(ftplib.FTP_TLS):
    """Implicit FTPS (port 990) the way Bambu firmware requires it.

    Two firmware quirks, both taken from the maintained clients (pybambu's
    ``ImplicitFTP_TLS`` and bambulabs_api's identical class): the control socket is
    TLS-wrapped from the moment it exists (implicit mode), and the data connection must
    reuse the control connection's TLS session or the printer refuses it. The store
    variant skips the TLS ``unwrap()`` close handshake, which some firmware never
    answers (pybambu's ``storbinary_no_unwrap``).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host,
                                            session=self.sock.session)
        return conn, size

    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd, rest) as conn:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(buf)
            conn.close()  # no unwrap(): firmware may never answer it (see class docstring)
        return self.voidresp()


def _ftps_factory(host, access_code):
    """Build the real FTPS connection: verified Bambu CA context, logged in, data protected."""
    ftp = _ImplicitFTPS(context=lan_ssl_context())
    try:
        ftp.connect(host=host, port=FTPS_PORT, timeout=15)
        ftp.login(user=MQTT_USER, passwd=access_code)
        ftp.prot_p()
    except Exception as exc:
        raise BambuLanError(f"Bambu Lab FTPS at {host} could not be reached or rejected the login "
                            f"({type(exc).__name__}); check the printer is on, in LAN Mode, and the "
                            "LAN access code secret is current") from exc
    return ftp


def _remote_name(local_path, remote_name):
    """The storage-root file name for an upload: a bare, plain `.gcode.3mf` basename."""
    name = remote_name if remote_name is not None else Path(local_path).name
    if (not name or name != Path(name).name or not name.endswith(".gcode.3mf")
            or any(c.isspace() or ord(c) < 32 for c in name)):
        raise BambuLanError(f"invalid Bambu Lab remote file name {name!r}: uploads are a single "
                            ".gcode.3mf file name without folders or spaces")
    return name


class BambuLanClient:
    """One-shot LAN telemetry: connect, take a complete snapshot, disconnect."""

    def __init__(self, host, serial, access_code, *, mqtt_factory=None, ftp_factory=None, timeout_s=15.0):
        if not host or "://" in host or "/" in host or ":" in host or any(c.isspace() for c in host):
            raise BambuLanError("Bambu Lab LAN host must be a plain IP address or hostname, without scheme or port")
        if not serial or not str(serial).strip():
            raise BambuLanError("Bambu Lab LAN serial number is required (printer Settings; upper case is applied)")
        if not access_code:
            raise BambuLanError("Bambu Lab LAN access code is not set; put it in the environment variable named "
                                "by [bambu] lan_access_code_env (default BAMBU_LAN_PASSWORD), never in config files")
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 120:
            raise BambuLanError("Bambu Lab LAN timeout must be a number of seconds up to 120")
        self.host = host
        self.serial = str(serial).strip().upper()
        self._access_code = access_code
        self.timeout_s = float(timeout_s)
        self._mqtt_factory = mqtt_factory or _paho_factory
        self._ftp_factory = ftp_factory or _ftps_factory

    @property
    def report_topic(self):
        return f"device/{self.serial}/report"

    @property
    def request_topic(self):
        return f"device/{self.serial}/request"

    def _publish(self, client, payload):
        result = client.publish(self.request_topic, json.dumps(payload))
        rc = getattr(result, "rc", 0)
        if rc != 0:
            raise BambuLanError(f"Bambu Lab LAN request was not accepted by the MQTT client (rc={rc}); nothing was sent")

    def _snapshot_mqtt_client(self):
        """The MQTT client for one snapshot read, on a distinct client ID.

        A snapshot is a read, not the persistent session: connecting with the
        listener's client ID (`print-handy-<serial>`) would disconnect the
        listener (MQTT 3.1.1 §3.1.4), and two reads sharing an ID would
        disconnect each other, so every read gets a fresh ID (P2-265). Only
        the built-in Paho factory takes the `client_id` argument; an injected
        factory keeps the plain (host, serial, access_code) call — that seam
        is how tests substitute fakes, and no real connection happens there.
        """
        if self._mqtt_factory is _paho_factory:
            client_id = f"print-handy-{self.serial.lower()}-status-{secrets.token_hex(4)}"
            return _paho_factory(self.host, self.serial, self._access_code, client_id=client_id)
        return self._mqtt_factory(self.host, self.serial, self._access_code)

    def snapshot(self):
        """Return {"print": {...}, "info": {...} | None, "online": True} from a complete pushall.

        Raises BambuLanError when the printer cannot be reached, rejects the access code,
        or does not answer with a complete snapshot before the timeout. Error text carries
        no access code and no payload content.
        """
        client = self._snapshot_mqtt_client()
        state = {"print": {}, "info": None, "complete": False, "denied": False, "failed": None}

        def on_connect(mqtt_client, _userdata, _flags, reason_code, *_rest):
            rc = getattr(reason_code, "value", reason_code)
            if rc == 5:
                state["denied"] = True
                return
            if rc != 0:
                state["failed"] = f"MQTT connection refused (rc={rc})"
                return
            mqtt_client.subscribe(self.report_topic)
            self._publish(mqtt_client, GET_VERSION)
            self._publish(mqtt_client, PUSHALL)

        def on_message(_client, _userdata, message):
            try:
                payload = json.loads(message.payload)
            except (ValueError, TypeError, AttributeError):
                return
            if not isinstance(payload, dict):
                return
            if isinstance(payload.get("print"), dict):
                update = payload["print"]
                if update.get("command") == "push_status" and update.get("msg") == 0:
                    state["complete"] = True
                state["print"] = merge_print(state["print"], update)
            if isinstance(payload.get("info"), dict) and payload["info"].get("command") == "get_version":
                state["info"] = payload["info"]

        client.on_connect = on_connect
        client.on_message = on_message
        try:
            try:
                client.connect(self.host, MQTT_PORT, keepalive=30)
            except Exception as exc:
                raise BambuLanError(f"Bambu Lab printer at {self.host} could not be reached "
                                    f"({type(exc).__name__}); check the printer is on, in LAN Mode, and on this network") from exc
            deadline = time.monotonic() + self.timeout_s
            while time.monotonic() < deadline:
                if state["denied"]:
                    raise BambuLanError("the printer rejected the LAN access code (MQTT access denied); "
                                        "re-read the current code on the printer (Settings > WLAN) — it changes "
                                        "after a factory reset — and update the secret, not the config")
                if state["failed"]:
                    raise BambuLanError(f"Bambu Lab LAN connection failed: {state['failed']}")
                if state["complete"] and state["info"] is not None:
                    break
                client.loop(timeout=0.5)
            if state["denied"]:
                raise BambuLanError("the printer rejected the LAN access code (MQTT access denied)")
            if not state["complete"]:
                raise BambuLanError(f"no complete status snapshot from the Bambu Lab printer at {self.host} within "
                                    f"{self.timeout_s:g}s; it may be busy, asleep, or another client (Bambu Studio / "
                                    "Handy) may hold its single MQTT session")
            return {"print": state["print"], "info": state["info"], "online": True}
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    def upload(self, local_path, remote_name=None):
        """Upload one `.gcode.3mf` package to the printer's storage root via FTPS; return its remote name.

        The upload is verified by reading the remote size back: a size the printer cannot
        report, or one that differs from the local file, is a failure — the package on the
        printer is then incomplete or different and must not be started. Uploading never
        starts a print. Raises BambuLanError with no credential detail on any failure.
        """
        path = Path(local_path)
        if not path.is_file():
            raise BambuLanError(f"no package file at {path}; nothing was uploaded")
        name = _remote_name(path, remote_name)
        size = path.stat().st_size
        ftp = self._ftp_factory(self.host, self._access_code)
        try:
            with path.open("rb") as fp:
                ftp.storbinary(f"STOR {name}", fp)
            remote_size = ftp.size(name)
        except BambuLanError:
            raise
        except Exception as exc:
            raise BambuLanError(f"FTPS upload of {name} to the Bambu Lab printer at {self.host} failed "
                                f"({type(exc).__name__}); the file on the printer may be incomplete — "
                                "check its storage before retrying") from exc
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
        if remote_size != size:
            raise BambuLanError(f"FTPS upload of {name} could not be verified: the printer reports "
                                f"{remote_size!r} bytes, the local package is {size} bytes; do not start "
                                "this file — delete it on the printer and upload again")
        return name

    def _send(self, payload):
        """Publish one command payload and wait until the client accepts it. Transport only.

        A successful return means the payload was handed to the MQTT client without an
        error — not that the printer acted on it. Callers confirm effects from a fresh
        snapshot. Raises BambuLanError on refusal, access denial or timeout.
        """
        client = self._mqtt_factory(self.host, self.serial, self._access_code)
        state = {"sent": False, "denied": False, "failed": None}

        def on_connect(mqtt_client, _userdata, _flags, reason_code, *_rest):
            rc = getattr(reason_code, "value", reason_code)
            if rc == 5:
                state["denied"] = True
                return
            if rc != 0:
                state["failed"] = f"MQTT connection refused (rc={rc})"
                return
            try:
                self._publish(mqtt_client, payload)
                state["sent"] = True
            except BambuLanError as exc:
                state["failed"] = str(exc)

        client.on_connect = on_connect
        try:
            try:
                client.connect(self.host, MQTT_PORT, keepalive=30)
            except Exception as exc:
                raise BambuLanError(f"Bambu Lab printer at {self.host} could not be reached "
                                    f"({type(exc).__name__}); nothing was sent") from exc
            deadline = time.monotonic() + self.timeout_s
            while time.monotonic() < deadline:
                if state["denied"]:
                    raise BambuLanError("the printer rejected the LAN access code (MQTT access denied); nothing was sent")
                if state["failed"]:
                    raise BambuLanError(f"Bambu Lab LAN command was not sent: {state['failed']}")
                if state["sent"]:
                    return
                client.loop(timeout=0.5)
            raise BambuLanError(f"the Bambu Lab printer at {self.host} did not accept a command within "
                                f"{self.timeout_s:g}s; whether anything was sent is unknown — check the "
                                "printer before retrying")
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    def start_print(self, remote_name, plate="Metadata/plate_1.gcode", *, ams_mapping=None,
                    nozzle_mapping=None, submission_id=None):
        """Send `project_file` for a package already uploaded under `remote_name`.

        No gates live here by design (see the module docstring): the print path checks
        readiness, the firmware signature bit and the bed-clear approval first, resolves
        any AMS mapping (ams_mapping.py), and confirms the start from telemetry afterwards.
        """
        name = _remote_name(remote_name, remote_name)
        if not plate.startswith("Metadata/") or not plate.endswith(".gcode"):
            raise BambuLanError(f"invalid plate path inside the package: {plate!r}")
        self._send(project_file_payload(name, plate, ams_mapping=ams_mapping,
                                        nozzle_mapping=nozzle_mapping, submission_id=submission_id))

    def control(self, action):
        """Send a pause / resume / stop command for the current print."""
        if action not in PRINT_CONTROLS:
            raise BambuLanError(f"unsupported Bambu Lab print control {action!r}; use one of "
                                f"{', '.join(PRINT_CONTROLS)}")
        self._send({"print": {"sequence_id": "0", "command": action}})

    def set_chamber_light(self, on):
        """Switch the chamber light on or off (#516). Transport only.

        A successful return means the command was handed to the MQTT
        client — callers confirm the effect from a fresh snapshot's
        lights_report, the same posture as the print controls.
        """
        self._send(chamber_light_payload(on))
