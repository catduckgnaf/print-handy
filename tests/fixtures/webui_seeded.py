"""Reusable offline synthetic seed for rendered Web UI browser checks."""
from __future__ import annotations

import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from print_handy.runtime import print_records, spool_sync
from print_handy.webui import walkthrough
from tests.test_webui_spools import FakeSpoolman


class SeededWebUI:
    """One-call context manager exposing isolated settings and seeded stores."""

    def __init__(self, root=None):
        self._temporary = tempfile.TemporaryDirectory(prefix="ph-webui-seed-") if root is None else None
        self.root = Path(root or self._temporary.name)
        self.root.mkdir(parents=True, exist_ok=True)
        self.fake_spoolman = FakeSpoolman()
        self._patches = []
        self._spoolman = None
        try:
            self._seed()
            self._start_spoolman()
        except BaseException:
            self.close()
            raise

    def _start_spoolman(self):
        fake = self.fake_spoolman

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/api/v1/health':
                    result = {'status': 'healthy'}
                elif self.path == '/api/v1/info':
                    result = {'version': '0.23.0'}
                else:
                    try:
                        result = fake('GET', self.path)
                    except AssertionError:
                        self.send_error(404)
                        return
                body = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        service = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        self._spoolman = service, thread
        self.spoolman_url = f'http://127.0.0.1:{service.server_port}'
        patcher = mock.patch.object(spool_sync, 'SPOOLMAN', self.spoolman_url)
        patcher.start()
        self._patches.append(patcher)

    def _seed(self):
        self.prints = self.root / "vault" / "Prints"
        self.plans = self.root / "vault" / "Plans"
        self.archive = self.root / "archive"
        self.cache = self.root / "cache"
        self.designs = self.root / "designs"
        self.state = self.root / "state"
        for directory in (self.prints, self.plans, self.archive, self.cache, self.designs, self.state):
            directory.mkdir(parents=True, exist_ok=True)
        for target, value, path in ((print_records, "PRINTS_DIR", self.prints),
                                    (print_records, "PLANS_DIR", self.plans)):
            patcher = mock.patch.object(target, value, path)
            patcher.start()
            self._patches.append(patcher)
        states = ("complete", "error", "cancelled", "printing", "stale")
        for number in range(1, 41):
            state = states[(number - 1) % len(states)]
            data = print_records.build_record_data(
                f"synthetic-{number:02d}.gcode",
                stats={"state": state, "print_duration": 120.0 + number,
                       "filament_used": None if number % 7 == 0 else 800.125 + number},
                context={"print_number": number,
                         "title": f"Synthetic long title for populated Web UI record {number} with realistic descriptive text",
                         "started_at": f"2026-06-{(number - 1) % 28 + 1:02d}T12:00:00Z",
                         "printer": "Synthetic AD5X"})
            print_records.write_completed_record(data)
        self.fake_spoolman.spools.clear()
        self.fake_spoolman.spools.update({
            20: self.fake_spoolman._spool(20, 31, location="IFS Slot 1", remaining=865.722, initial=1000, ph_slot="flashforge-ad5x:1"),
            30: self.fake_spoolman._spool(30, 37, location="IFS Slot 2", remaining=520, initial=1000, ph_slot="flashforge-ad5x:2"),
            31: self.fake_spoolman._spool(31, 39, location="IFS Slot 3", remaining=635, initial=1000, ph_slot="flashforge-ad5x:3"),
            32: self.fake_spoolman._spool(32, 41, location="IFS Slot 4", remaining=740, initial=1000, ph_slot="flashforge-ad5x:4"),
            21: self.fake_spoolman._spool(21, 37, location="Shelf", remaining=412.0384, initial=1000),
            22: self.fake_spoolman._spool(22, 39, location="Shelf", remaining=12.3456, initial=1000),
            23: self.fake_spoolman._spool(23, 41, location="Shelf", remaining=999.999, initial=1000),
            24: self.fake_spoolman._spool(24, 31, location="Shelf", remaining=250, initial=1000),
            25: self.fake_spoolman._spool(25, 37, location="Shelf", remaining=700, initial=1000),
            26: self.fake_spoolman._spool(26, 39, location="Shelf", remaining=400, initial=1000, archived=True),
        })
        self.fake_spoolman.spools[20]["extra"][spool_sync.PH_NICKNAME_FIELD] = json.dumps("Weekend pink")
        self._seed_archive()
        self._seed_designs()

    def _seed_archive(self):
        job = self.archive / "seeded-multiplate"
        job.mkdir(parents=True, exist_ok=True)
        plates = []
        for number in range(1, 4):
            name = f"plate-{number}.gcode"
            (job / name).write_text("; synthetic plate\n", encoding="utf-8")
            plates.append({"gcode": name, "name": f"Synthetic plate {number}"})
        payload = {"title": "Long synthetic archive job title for multi-plate populated page", "gcode": plates[0]["gcode"], "derived_plates": plates[1:]}
        (job / "JOB.json").write_text(json.dumps(payload), encoding="utf-8")

    def _seed_designs(self):
        for number in range(1, 4):
            folder = self.designs / f"seeded-design-{number}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "session.json").write_text(json.dumps({"title": f"Synthetic design session {number}", "status": "ready", "preview": f"preview-{number}.svg"}), encoding="utf-8")
            (folder / f"preview-{number}.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160"><rect width="240" height="160" fill="#456"/></svg>', encoding="utf-8")


    def configure(self, settings_data):
        paths = settings_data.setdefault("paths", {})
        paths.update(records_vault=self.root / "vault", archive=self.archive,
                     cache=self.cache, state_home=self.state)
        settings_data.setdefault('services', {})['spoolman_url'] = self.spoolman_url
        self.settings = settings_data
        walkthrough.save_state(settings_data, {"step": 0, "done": True})
        return settings_data

    def close(self):
        if self._spoolman is not None:
            service, thread = self._spoolman
            service.shutdown()
            service.server_close()
            thread.join()
            self._spoolman = None
        for patcher in reversed(self._patches):
            patcher.stop()
        self._patches.clear()
        if self._temporary:
            self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
