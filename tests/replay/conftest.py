"""Replay harness: real Print Handy code against a fake Moonraker that serves recorded AD5X responses.

The fixtures in tests/fixtures/printers/ad5x_zmod/ were recorded from a real AD5X on ZMod (see the
README there). FakeMoonraker serves them over HTTP from a thread, so `_mr`, `_printer_phase`,
`_wait_printing`, the ZMod native module and the adapter all run unmodified over real sockets. Only
the upload subprocess is replaced (nothing is moved to a printer); nothing posts to Discord.

Time is scaled. A scenario is written in printer seconds (SET_ZCOLOR held 35 s while the printer
heats); the harness runs it at `scale` (default 1/20) and shortens `_mr`'s default timeout by the
same factor, so the hold still exceeds the client's timeout and a real socket timeout still happens.
"""
from __future__ import annotations

import copy
import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from print_handy import settings
from print_handy.core import model_job, printer_state
from print_handy.runtime import _config

from .helpers import FIXTURES, MOONRAKER_TIMEOUT_S, SCALE, hardware_record, recorded, reported_name


class FakeMoonraker:
    """Moonraker (and the Spoolman API) served from the recorded responses, with scripted behaviour."""

    def __init__(self, scale=SCALE):
        self.scale = scale
        self.lock = threading.RLock()
        self.status = recorded("objects_query.idle.json")["result"]["status"]
        self.status["bed_mesh"] = recorded("bed_mesh_profiles.json")["result"]["status"]["bed_mesh"]
        self.status["configfile"] = recorded("configfile_settings.json")["result"]["status"]["configfile"]
        self.history = recorded("history_list.json")["result"]["jobs"]
        self.active_spool = recorded("spool_id.json")["result"]["spool_id"]
        self.gcode_store = []  # {"message": str, "time": float} console entries, as Moonraker serves them
        self.requests = []  # (method, path, parsed query, body)
        self.unhandled = []
        self.uploads = []  # remote names the (mocked) upload subprocess received
        self.upload_fails = False
        self.record_path = None  # the hardware record file, once the ad5x fixture writes it
        self.latency = {}  # path -> printer seconds to hold the response
        self.script_hook = None  # callable(fake, script) -> printer seconds to hold the response
        self.late_write_failed = threading.Event()  # a held response found its client gone
        self._due = []  # (monotonic deadline, callable)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.fake = self
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # Time -----------------------------------------------------------------------------------
    def after(self, printer_seconds, action):
        """Run `action` once `printer_seconds` of scaled printer time have passed (on the next request)."""
        with self.lock:
            self._due.append((time.monotonic() + printer_seconds * self.scale, action))

    def _advance(self):
        with self.lock:
            now = time.monotonic()
            due = [item for item in self._due if item[0] <= now]
            self._due = [item for item in self._due if item[0] > now]
            for _deadline, action in sorted(due, key=lambda item: item[0]):
                action()

    # Printer state --------------------------------------------------------------------------
    def heat(self, nozzle=220.0, bed=60.0):
        with self.lock:
            self.status["extruder"]["target"] = nozzle
            self.status["heater_bed"]["target"] = bed
            self.status["idle_timeout"]["state"] = "Printing"

    def start_printing(self, remote):
        """print_stats reports the uploaded file, as the recorded printing state does."""
        with self.lock:
            printing = recorded("objects_query.printing.json")["result"]["status"]
            name = reported_name(remote)
            for key in ("print_stats", "virtual_sdcard", "idle_timeout", "extruder", "heater_bed"):
                self.status[key] = copy.deepcopy(printing[key])
            self.status["print_stats"]["filename"] = name
            self.status["virtual_sdcard"]["file_path"] = "/usr/data/gcodes/" + name
            job = copy.deepcopy(self.history[0])
            job.update(job_id=f"{int(job['job_id']) + 1:06d}", filename=name, status="in_progress",
                       start_time=time.time(), end_time=None)
            self.history.insert(0, job)

    def finish_job(self):
        """A finished job: the printer is idle and reports no file; history keeps the job."""
        with self.lock:
            idle = recorded("objects_query.idle.json")["result"]["status"]
            for key in ("print_stats", "virtual_sdcard", "idle_timeout", "extruder", "heater_bed"):
                self.status[key] = copy.deepcopy(idle[key])
            if self.history and self.history[0]["status"] == "in_progress":
                self.history[0].update(status="completed", end_time=time.time())

    def add_finished_job(self, remote, started=None):
        """A completed run of `remote` in history, as the printer reports it, with the printer idle."""
        with self.lock:
            job = copy.deepcopy(self.history[1])
            began = started if started is not None else time.time() + 1
            job.update(job_id=f"{int(self.history[0]['job_id']) + 1:06d}", filename=reported_name(remote),
                       status="completed", start_time=began, end_time=began + 600)
            self.history.insert(0, job)

    def set_slot(self, slot, **fields):
        with self.lock:
            for row in self.status["zmod_color"]["slots"]:
                if str(row["ID"]) == str(slot):
                    row.update(fields)

    # Observation ----------------------------------------------------------------------------
    @property
    def scripts(self):
        return [body["script"] for method, path, _q, body in self.requests
                if method == "POST" and path == "/printer/gcode/script" and body]

    def posts(self, path):
        return [body for method, p, _q, body in self.requests if method == "POST" and p == path]

    def zcolor(self):
        return [s for s in self.scripts if s.startswith("SET_ZCOLOR")]

    # Routes ---------------------------------------------------------------------------------
    def get(self, path, query):
        if path == "/printer/objects/query":
            result = {}
            for name, fields in query:
                if name not in self.status:
                    continue
                obj = self.status[name]
                wanted = [f for f in fields.split(",") if f] if fields else None
                result[name] = {k: v for k, v in obj.items() if k in wanted} if wanted else obj
            return {"result": {"eventtime": time.monotonic(), "status": result}}
        if path == "/server/history/list":
            limit = int(dict(query).get("limit") or 50)
            return {"result": {"count": len(self.history), "jobs": self.history[:limit]}}
        if path == "/server/files/list":
            if dict(query).get("root") == "config":
                return recorded("config_files_list.json")
            return recorded("files_list.json")
        if path == "/printer/info":
            return recorded("printer_info.json")
        if path == "/server/info":
            return recorded("server_info.json")
        if path == "/server/spoolman/spool_id":
            return {"result": {"spool_id": self.active_spool}}
        if path == "/server/gcode_store":
            return {"result": {"gcode_store": list(self.gcode_store)}}
        match = re.fullmatch(r"/api/v1/spool/(\d+)", path)
        if match and (FIXTURES / f"spoolman_spool_{match.group(1)}.json").exists():
            return recorded(f"spoolman_spool_{match.group(1)}.json")
        return None

    def post(self, path, body):
        if path == "/printer/gcode/script":
            return {"result": "ok"}
        if path == "/server/spoolman/spool_id":
            self.active_spool = (body or {}).get("spool_id")
            return {"result": {"spool_id": self.active_spool}}
        if path in ("/server/database/item", "/printer/print/start"):
            return {"result": "ok"}
        return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _serve(self, method):
        fake = self.server.fake
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = None
        with fake.lock:
            fake.requests.append((method, parts.path, query, body))
            fake._advance()
            payload = fake.get(parts.path, query) if method == "GET" else fake.post(parts.path, body)
            hold = fake.latency.get(parts.path, 0)
            if method == "POST" and parts.path == "/printer/gcode/script" and fake.script_hook:
                hold = fake.script_hook(fake, body["script"]) or hold
        if hold:
            time.sleep(hold * fake.scale)
        if payload is None:
            fake.unhandled.append((method, self.path))
            data, status = json.dumps({"error": {"code": 404, "message": "not recorded"}}).encode(), 404
        else:
            data, status = json.dumps(payload).encode(), 200
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
        except OSError:
            fake.late_write_failed.set()  # the client had already given up on this response
        self.close_connection = True

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")


@pytest.fixture
def moonraker(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    fake = FakeMoonraker()
    yield fake
    fake.close()


@pytest.fixture
def client_errors(monkeypatch):
    """Exceptions the real urlopen raised, recorded as they pass through (nothing is mocked)."""
    seen = []
    real = urllib.request.urlopen

    def spy(req, *args, **kwargs):
        try:
            return real(req, *args, **kwargs)
        except BaseException as exc:
            seen.append((getattr(req, "full_url", req), exc))
            raise
    monkeypatch.setattr(urllib.request, "urlopen", spy)
    return seen


@pytest.fixture
def ad5x(moonraker, tmp_path, monkeypatch):
    """The recorded AD5X behind the fake Moonraker, with Print Handy configured to talk to it."""
    record = tmp_path / "printer_hardware.json"
    record.write_text(json.dumps(hardware_record(moonraker.url)))
    monkeypatch.setattr(printer_state, "STATE", str(record))
    moonraker.record_path = record
    data = copy.deepcopy(settings.current())
    # Whatever an earlier test left installed, nothing here may write the host's profile or records.
    data["paths"].update(state_home=tmp_path / "profile", archive=tmp_path / "archive",
                         records_vault=tmp_path / "vault", cache=tmp_path / "cache")
    data["services"].update(moonraker_url=moonraker.url, spoolman_url=moonraker.url)
    monkeypatch.setattr(settings, "_override", data)
    monkeypatch.setattr(_config, "_override", data)
    assert str(_config.records_vault()).startswith("/tmp"), "replay tests must not write the host's records vault"
    monkeypatch.setattr(model_job, "START_CLAIMS", tmp_path / "claims.json")
    # A real timeout, scaled: _mr's 30 s becomes 1.5 s, and the fake holds a 35 s POST for 1.75 s.
    monkeypatch.setattr(model_job._mr, "__defaults__", (None, MOONRAKER_TIMEOUT_S * moonraker.scale))
    real_run = subprocess.run

    def upload_only(cmd, *args, **kwargs):
        if len(cmd) >= 4 and str(cmd[0]) == str(model_job.MOONRAKERCTL) and cmd[1] == "upload":
            if moonraker.upload_fails:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="curl: (7) printer unreachable")
            moonraker.uploads.append(cmd[3])
            body = json.dumps({"result": {"item": {"root": "gcodes", "path": cmd[3]}}})
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(model_job.subprocess, "run", upload_only)
    return moonraker
