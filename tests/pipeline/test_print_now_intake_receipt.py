"""#970: an out-of-band `model_job print-now` start must reconcile the intake
receipt that owns the artifact.

The chat intake leaves a sliced job at SLICED_NOT_STARTED; starting that exact
artifact from the CLI used to leave the receipt there, so chat could offer to
start a job that was already printing and the receipt history was wrong.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy.core import model_job
from print_handy.hermes import print_next

IID = "a" * 64


def mock_upload_run(cmd, *args, **kwargs):
    if len(cmd) >= 4 and cmd[1] == "upload":
        remote = cmd[3]
        return mock.Mock(returncode=0, stdout=json.dumps(
            {"result": {"item": {"root": "gcodes", "path": remote}}}), stderr="")
    return mock.Mock(returncode=0, stdout="", stderr="")


class PrintNowIntakeReceiptTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(model_job, "live_snapshot",
                              return_value={"nozzle_c": None, "bed_c": None, "slots": "unknown"})
        p.start(); self.addCleanup(p.stop)
        for p in (mock.patch.object(model_job, "mesh_readiness", return_value={
                      "profile": "auto", "saved_at": None, "print_leveling": 0, "problem": None,
                      "stale": None, "z_offset_mm": -0.005, "offset_source": "saved gcode offset"}),
                  mock.patch.object(model_job, "screen_z_offset", return_value=0.025),
                  mock.patch.object(model_job, "_zmod_version", return_value=None),
                  mock.patch.object(model_job, "_fetch_remote_gcode_sha256",
                                    side_effect=lambda url, name: name.rsplit(".", 1)[0].split("_", 1)[1]
                                    if "_" in name else "")):
            p.start(); self.addCleanup(p.stop)

    def _job(self, d, sha_override=None):
        """An archived job plus its SLICED_NOT_STARTED intake receipt."""
        folder = Path(d) / "job"; (folder / "out").mkdir(parents=True)
        g = folder / "out" / "x.gcode"
        g.write_text("START_PRINT EXTRUDER_TEMP=220 BED_TEMP=60 Z_OFFSET=-0.2\n")
        jp = folder / "JOB.json"
        jp.write_text(json.dumps({"title": "X", "gcode": str(g), "intake_id": IID}))
        sha = model_job.file_sha256(g)
        receipts = Path(d) / "model-link-intakes"; receipts.mkdir()
        receipt = {"intake_id": IID, "url": "https://example.invalid/x", "status": "SLICED_NOT_STARTED",
                   "sliced": True, "uploaded": False, "started": False,
                   "job_json": str(jp), "gcodes": [str(g)],
                   "gcode_sha256": {str(g): sha_override or sha},
                   "chat_id": "1", "thread_id": None}
        receipt_path = receipts / f"{IID}.json"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        return jp, g, sha, receipt_path

    def _patches(self, d):
        return [mock.patch.object(model_job, "START_CLAIMS", Path(d) / "claims.json"),
                mock.patch.object(model_job, "INTAKE_RECEIPTS", Path(d) / "model-link-intakes"),
                mock.patch.object(print_next, "CLAIMS_DIR", Path(d) / "print-next-claims"),
                mock.patch.object(model_job.printer_state, "load", return_value={"moonraker_url": "http://m"}),
                mock.patch.object(model_job, "_printer_phase", return_value=("standby", "x.gcode", "Ready")),
                mock.patch.object(model_job, "verify_gcode"),
                mock.patch.object(model_job, "_verify_filament_readiness"),
                mock.patch.object(model_job, "_mr"),
                mock.patch.object(model_job, "_wait_printing", return_value="printing"),
                mock.patch.object(model_job, "record_start", return_value="/vault/x.md"),
                mock.patch.object(model_job, "_assign_print_number", return_value=83),
                mock.patch.object(model_job.subprocess, "run", side_effect=mock_upload_run)]

    def test_print_now_on_sliced_not_started_intake_updates_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            jp, g, sha, receipt_path = self._job(d)
            ps = self._patches(d)
            for p in ps: p.start()
            try:
                out = model_job.print_now(jp)
            finally:
                for p in ps: p.stop()
            self.assertTrue(out["started"])
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(receipt["status"], "STARTED")
            self.assertIs(receipt["started"], True)
            self.assertIs(receipt["uploaded"], True)
            self.assertEqual(receipt["started_via"], "print-now")
            self.assertEqual(receipt["print_number"], 83)
            self.assertEqual(receipt["print_id"], 83)

    def test_second_start_attempt_from_chat_is_refused_as_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            jp, g, sha, receipt_path = self._job(d)
            ps = self._patches(d)
            for p in ps: p.start()
            try:
                model_job.print_now(jp)
            finally:
                for p in ps: p.stop()
            # The chat start path (print_next.start) must now refuse the same
            # target as a duplicate, without spawning print-now again.
            log = Path(d) / "spawned.log"
            fake = Path(d) / "fake_print_now.sh"
            fake.write_text(f"#!/bin/sh\necho ran >> {log}\n")
            fake.chmod(0o755)
            target = {"plan_id": f"intake-{IID[:16]}", "part_id": "plate1", "copy": 1,
                      "quantity": 1, "gcode": str(g), "gcode_sha256": sha, "job_json": str(jp)}
            ok, detail = asyncio.run(print_next.start(
                target, model_job=fake, idle_check=lambda: None,
                claims_dir=Path(d) / "print-next-claims"))
            self.assertFalse(ok)
            self.assertIn("already started", detail)
            self.assertFalse(log.exists())

    def test_print_now_hash_mismatch_leaves_receipt_alone_and_logs(self):
        with tempfile.TemporaryDirectory() as d:
            jp, g, sha, receipt_path = self._job(d, sha_override="0" * 64)
            before = receipt_path.read_text()
            ps = self._patches(d)
            for p in ps: p.start()
            try:
                with self.assertLogs("print_handy.core.model_job", level="WARNING") as logs:
                    out = model_job.print_now(jp)
            finally:
                for p in ps: p.stop()
            self.assertTrue(out["started"])  # the print itself still started
            self.assertEqual(receipt_path.read_text(), before)  # receipt untouched
            self.assertTrue(any("hash mismatch" in line for line in logs.output), logs.output)
            self.assertFalse((Path(d) / "print-next-claims").exists())

    def test_print_now_without_intake_receipt_starts_normally(self):
        with tempfile.TemporaryDirectory() as d:
            jp, g, sha, receipt_path = self._job(d)
            receipt_path.unlink()
            ps = self._patches(d)
            for p in ps: p.start()
            try:
                out = model_job.print_now(jp)
            finally:
                for p in ps: p.stop()
            self.assertTrue(out["started"])
            self.assertFalse(receipt_path.exists())


if __name__ == "__main__":
    unittest.main()
