"""Elegoo SDCP v3.0 LAN client for the Centauri line: discovery, status, upload, start, control.

Original implementation written against Elegoo's published SDCP documentation. The protocol
has **no authentication at all** and runs in plaintext on the LAN — a fact the docs state
plainly; Print Handy adds nothing on top and the printer must sit on a trusted network.

Shape of the protocol:
- Discovery: a UDP broadcast of ``M99999`` to port 3000; the printer answers with JSON
  carrying its ``MainboardID``.
- Control: a WebSocket at ``ws://<host>:3030/websocket``. Requests go to topic
  ``sdcp/request/<MainboardID>`` as ``{"Cmd": n, "Data": {...}}`` inside the SDCP envelope;
  answers arrive on ``sdcp/response/<MainboardID>`` matched by RequestID. Commands used here:
  0 status, 128 start print, 129 pause, 130 stop, 131 resume, 258 list files, 260 file info,
  403 control device (the chamber light, #516).
- Upload: chunked HTTP ``POST http://<host>:3030/uploadFile/upload`` (1 MiB chunks, MD5 of
  the whole file in the form fields), because the WebSocket channel carries no file data.

Transports are injectable (``ws_factory``, ``opener``, ``udp``) so tests run fully offline.
Mutations are never auto-retried; a start must be confirmed by a status poll, and an
unconfirmed start is reported uncertain by the caller, never resubmitted.
"""
import hashlib
import json
import re
import socket
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DISCOVERY_PORT = 3000
SDCP_PORT = 3030
CHUNK = 1024 * 1024

CMD_STATUS = 0
CMD_START = 128
CMD_PAUSE = 129
CMD_STOP = 130
CMD_RESUME = 131
CMD_LIST_FILES = 258
CMD_FILE_INFO = 260
CMD_CONTROL_DEVICE = 403

_HOST_OK = re.compile(r"^[A-Za-z0-9.-]+$")
_REMOTE_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*\.(gcode|bgcode)$", re.IGNORECASE)


class SdcpError(RuntimeError):
    """An SDCP failure in terms an operator can act on (never carries payload secrets — there are none)."""


def _default_ws_factory(url):
    from websockets.sync.client import connect  # the one declared dependency beyond stdlib

    return connect(url, open_timeout=10)


def _default_udp(host, timeout):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        sock.sendto(b"M99999", (host, DISCOVERY_PORT))
        # The sender address is part of the answer: discovery must be able to
        # tell the configured printer's reply from any other printer's (P2-91).
        return sock.recvfrom(65535)
    finally:
        sock.close()


class SdcpClient:
    def __init__(self, host, mainboard_id=None, *, ws_factory=None, opener=None, udp=None,
                 request_timeout=15.0):
        host = str(host or "").strip()
        if not _HOST_OK.fullmatch(host) or "://" in host:
            raise SdcpError("the SDCP host must be a plain printer IP address or hostname")
        self.host = host
        self.mainboard_id = str(mainboard_id or "").strip()
        self.ws_factory = ws_factory or _default_ws_factory
        self.opener = opener or (lambda req, timeout=None: urllib.request.urlopen(req, timeout=timeout))
        self.udp = udp or _default_udp
        self.request_timeout = float(request_timeout)
        self._ws = None

    # -- discovery --------------------------------------------------------
    def _is_configured_sender(self, addr):
        """True when a discovery answer came from the configured host itself.

        The broadcast reaches every SDCP printer on the LAN; an answer from a
        different printer must never supply the MainboardID used for requests
        sent to ``self.host`` (P2-91). Fails closed: an unidentifiable or
        unresolvable sender is not the configured printer.
        """
        try:
            sender = str(addr[0])
        except (TypeError, IndexError, KeyError):
            return False
        if sender == self.host:
            return True
        try:
            return socket.gethostbyname(self.host) == sender
        except OSError:
            return False

    def discover(self):
        """Broadcast M99999 and return the configured printer's MainboardID (and remember it).

        Answers from other printers on the LAN are ignored: only a reply sent
        by ``self.host`` may set the MainboardID (P2-91). Injected ``udp``
        doubles that return bare payload bytes carry no sender to check and
        are taken at face value; the real transport always reports one.
        """
        deadline = time.monotonic() + 5.0
        heard_other = False
        for _attempt in range(8):  # bounded: re-broadcasting forever helps no one
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = self.udp("255.255.255.255", remaining)
            except OSError as exc:
                raise SdcpError(f"SDCP discovery got no answer from {self.host}: {exc}") from exc
            raw, addr = result if isinstance(result, tuple) else (result, None)
            if addr is not None and not self._is_configured_sender(addr):
                heard_other = True
                continue  # some other printer answered first; keep listening for ours
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
                mainboard_id = str(payload["Data"]["MainboardID"]).strip()
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise SdcpError("the SDCP discovery answer was not the expected JSON") from exc
            if not mainboard_id:
                raise SdcpError("the SDCP discovery answer carried no MainboardID")
            self.mainboard_id = mainboard_id
            return mainboard_id
        if heard_other:
            raise SdcpError(f"SDCP discovery heard answers only from other printers, "
                            f"none from {self.host}")
        raise SdcpError(f"SDCP discovery got no answer from {self.host}")

    def _mid(self):
        return self.mainboard_id or self.discover()

    # -- WebSocket request/response --------------------------------------
    def _connect(self):
        if self._ws is None:
            try:
                self._ws = self.ws_factory(f"ws://{self.host}:{SDCP_PORT}/websocket")
            except Exception as exc:  # connection refused, handshake failure, ...
                raise SdcpError(f"could not open the SDCP WebSocket on {self.host}:3030: {exc}") from exc
        return self._ws

    def close(self):
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def request(self, cmd, data=None, timeout=None):
        mid = self._mid()
        ws = self._connect()
        request_id = uuid.uuid4().hex
        envelope = {"Id": uuid.uuid4().hex,
                    "Data": {"Cmd": int(cmd), "Data": data or {}, "RequestID": request_id,
                             "MainboardID": mid, "TimeStamp": int(time.time() * 1000), "From": 0},
                    "Topic": f"sdcp/request/{mid}"}
        try:
            ws.send(json.dumps(envelope))
        except Exception as exc:
            self.close()
            raise SdcpError(f"sending SDCP command {cmd} to {self.host} failed: {exc}") from exc
        deadline = time.monotonic() + (timeout or self.request_timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                # Each receive is bounded by what is left of the deadline: a
                # printer that holds the socket open but stops answering must
                # not block the request past request_timeout (P2-89).
                raw = ws.recv(timeout=remaining)
            except TimeoutError as exc:
                self.close()
                raise SdcpError(f"the printer did not answer SDCP command {cmd} in time") from exc
            except Exception as exc:
                self.close()
                raise SdcpError(f"the SDCP connection to {self.host} dropped during command {cmd}: {exc}") from exc
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue  # notices and non-JSON frames are not answers; keep waiting
            body = message.get("Data") if isinstance(message, dict) else None
            if not isinstance(body, dict) or body.get("RequestID") != request_id:
                continue
            if int(body.get("Cmd", -1)) != int(cmd):
                continue
            inner = body.get("Data")
            ack = body.get("Ack")
            if ack is None and isinstance(inner, dict):
                ack = inner.get("Ack")  # firmware builds differ on which level carries Ack
            if ack not in (0, None):
                raise SdcpError(f"the printer refused SDCP command {cmd} (Ack {ack})")
            return inner or {}
        raise SdcpError(f"the printer did not answer SDCP command {cmd} in time")

    # -- commands ----------------------------------------------------------
    def status(self):
        return self.request(CMD_STATUS, {})

    def list_files(self):
        return self.request(CMD_LIST_FILES, {"Url": "/local/"})

    def file_info(self, remote_name):
        return self.request(CMD_FILE_INFO, {"FileName": f"/local/{remote_name}"})

    def upload(self, local_path, remote_name=None):
        path = Path(local_path)
        remote = remote_name or path.name
        if not _REMOTE_OK.fullmatch(remote) or ".." in remote:
            raise SdcpError(f"invalid SDCP remote file name: {remote!r}")
        if path.suffix.lower() not in (".gcode", ".bgcode"):
            raise SdcpError("the SDCP upload accepts .gcode or .bgcode files only")
        data = path.read_bytes()
        if not data:
            raise SdcpError("refusing to upload an empty file")
        md5 = hashlib.md5(data).hexdigest()
        upload_id = uuid.uuid4().hex
        url = f"http://{self.host}:{SDCP_PORT}/uploadFile/upload"
        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + CHUNK]
            boundary = "----printhandysdcp" + uuid.uuid4().hex
            fields = {"Uuid": upload_id, "Offset": str(offset), "TotalSize": str(len(data)),
                      "Check": "1", "S-File-MD5": md5}
            body = b""
            for key, value in fields.items():
                body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                         f"{value}\r\n").encode()
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"File\"; "
                     f"filename=\"{remote}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
            body += chunk + f"\r\n--{boundary}--\r\n".encode()
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}"})
            try:
                with self.opener(req, timeout=60) as response:
                    status = int(getattr(response, "status", 200))
                    response.read()
            except urllib.error.URLError as exc:
                raise SdcpError(f"the SDCP upload to {self.host} failed at byte {offset}: {exc}") from exc
            if status >= 400:
                raise SdcpError(f"the SDCP upload to {self.host} was refused (HTTP {status})")
            offset += len(chunk)
        # Verify the printer holds the file at the uploaded size before anything may start it.
        try:
            info = self.file_info(remote)
        except SdcpError as exc:
            raise SdcpError(f"the uploaded file {remote} could not be verified on the printer: {exc}") from exc
        size = info.get("FileSize", info.get("Size")) if isinstance(info, dict) else None
        if size is not None and int(size) != len(data):
            raise SdcpError(f"the uploaded file {remote} could not be verified on the printer "
                            f"(printer reports {size} bytes, uploaded {len(data)})")
        return remote

    def start_print(self, remote_name):
        if not _REMOTE_OK.fullmatch(remote_name):
            raise SdcpError(f"invalid SDCP remote file name: {remote_name!r}")
        # The field set the printer's own WebUI sends. Calibration_switch stays 0: Print
        # Handy never starts a calibration the operator did not ask for (the Bambu rule).
        self.request(CMD_START, {"Filename": f"/local/{remote_name}", "StartLayer": 0,
                                 "Calibration_switch": 0, "PrintPlatformType": 0, "Tlp_Switch": 0})
        return remote_name

    def control(self, action, **_ignored):
        commands = {"pause": CMD_PAUSE, "resume": CMD_RESUME, "stop": CMD_STOP}
        if action not in commands:
            raise SdcpError(f"unsupported SDCP control action: {action!r}")
        self.request(commands[action], {})
        return True

    def set_light(self, on):
        """Switch the chamber light (Cmd 403, control device) — #516.

        Cmd 403 is SDCP's partial device-control update: the same
        command carries fan speeds and print speed, and only the field
        being changed is sent, so this sends LightStatus alone and
        leaves the fans and any RGB accent state exactly as they are.
        ``SecondLight`` is the printer's LED chamber light in the
        status answer's LightStatus block (1 on, 0 off). Note Cmd 386
        is not a light command: the published command tables name it
        the video-stream enable — it belongs to the camera audit in
        camera.py, not here.
        """
        self.request(CMD_CONTROL_DEVICE,
                     {"LightStatus": {"SecondLight": 1 if on else 0}})
        return True
