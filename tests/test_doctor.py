"""The doctor reports every check and fails on any problem. Network and slicer are mocked."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy import doctor, settings


HARDWARE = {"spool": {"material": "PLA", "density": 1.24}, "temps_c": {"nozzle": 210, "bed": 60},
            "nozzle": {"diameter_mm": 0.4}}


class CheckLoggingTests(unittest.TestCase):
    """check_logging probes the configured log file itself, with a unique
    temporary name per probe (P2-162, P2-163)."""

    def test_a_directory_at_the_log_path_fails(self):
        # A directory is not writable-as-a-log even though its folder is;
        # probing only a sibling file reported this ok.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "pb.log"
            target.mkdir()
            with self.assertRaises(RuntimeError):
                doctor.check_logging({"logging": {"file": str(target)}})

    def test_an_existing_log_file_is_checked_without_truncating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "pb.log"
            target.write_text("precious line\n")
            result = doctor.check_logging({"logging": {"file": str(target)}})
            self.assertIn("file", result)
            self.assertEqual(target.read_text(), "precious line\n")

    def test_a_stale_probe_at_the_old_fixed_name_does_not_fail_the_check(self):
        # A leftover (or a concurrent run's probe) at the old fixed probe
        # name must not matter: each probe uses a unique temporary name.
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "pb.log"
            (Path(tmp) / ".doctor-write-test").mkdir()
            result = doctor.check_logging({"logging": {"file": str(target)}})
            self.assertIn("file", result)
            self.assertTrue((Path(tmp) / ".doctor-write-test").is_dir())

    def test_the_probe_leaves_nothing_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "pb.log"
            doctor.check_logging({"logging": {"file": str(target)}})
            self.assertEqual([p.name for p in Path(tmp).iterdir()], [])


class DoctorTests(unittest.TestCase):
    def run_doctor(self, local_toml="", hardware=None):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "local.toml"
            path.write_text('[bot]\nmode = "standalone"\n[discord]\nchat_id = "123"\n' + local_toml)
            data = settings.load(local=path, environ={"PRINT_HANDY__PATHS__STATE_HOME": str(self.profile)})
        state = settings.state_dir(data)
        state.mkdir(parents=True, exist_ok=True)
        if hardware is not False:
            (state / "printer_hardware.json").write_text(json.dumps(hardware or HARDWARE))
        out = io.StringIO()
        with mock.patch.dict("os.environ", {"DISCORD_BOT_TOKEN": "token-present"}), \
                mock.patch.object(doctor, "check_discord_login", return_value="logged in as test bot"), \
                mock.patch.object(doctor.settings, "load", return_value=data), \
                mock.patch.object(doctor.shutil, "which", side_effect=self.which), \
                mock.patch.object(doctor.subprocess, "run", return_value=mock.MagicMock(returncode=self.orca_code)):
            code = doctor.run(out)
        return code, out.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = Path(self.tmp.name) / "profile"
        self.orca_code = 0  # what the stubbed `orca --help` exits with
        self.which = lambda name: f"/usr/bin/{name}" if name == "flatpak" else None

    def test_home_setup_passes_and_skips_unset_services(self):
        code, text = self.run_doctor()
        self.assertEqual(code, 0, text)
        self.assertIn("ok    settings: Flashforge AD5X, bed 220x220 mm, 4 slot(s), 0.4 mm nozzle", text)
        self.assertIn("skip  moonraker", text)
        self.assertIn("ok    orca cli: flatpak run com.orcaslicer.OrcaSlicer", text)

    def test_stray_state_files_warn_without_failing(self):
        root = Path(self.tmp.name) / "hermes"
        self.profile = root / "profiles" / "main"
        (root / "state").mkdir(parents=True)
        (root / "state" / "print_alert_buttons.json").write_text("{}")
        with mock.patch.dict("os.environ", {"HOME": str(Path(self.tmp.name) / "home")}):
            code, text = self.run_doctor()
        self.assertEqual(code, 0, text)
        self.assertIn("WARN  stray state files: 1 outside", text)
        self.assertIn(str(root / "state" / "print_alert_buttons.json"), text)

    def test_no_stray_state_files_is_ok(self):
        with mock.patch.dict("os.environ", {"HOME": str(Path(self.tmp.name) / "home")}):
            code, text = self.run_doctor()
        self.assertIn("ok    stray state files: none", text)

    def test_bad_settings_fail_and_name_the_key(self):
        out = io.StringIO()
        with mock.patch.object(doctor.settings, "load", side_effect=settings.SettingsError("[printer] bed_mm bad")):
            self.assertEqual(doctor.run(out), 1)
        self.assertIn("FAIL  settings: [printer] bed_mm bad", out.getvalue())

    def test_template_drift_fails(self):
        code, text = self.run_doctor("[printer]\nbed_mm = [235, 235]\n")
        self.assertEqual(code, 1)
        self.assertIn("FAIL  orca templates", text)

    def test_unreachable_service_fails_and_other_checks_still_run(self):
        with mock.patch.object(doctor.urllib.request, "urlopen", side_effect=OSError("connection refused")):
            code, text = self.run_doctor('[services]\nspoolman_url = "http://spoolman.lan:7912"\n')
        self.assertEqual(code, 1)
        # #387: the Spoolman probe goes through spoolman_client.health; the
        # failure names the service and keeps the transport detail.
        self.assertIn("FAIL  spoolman: http://spoolman.lan:7912 Spoolman health check failed: "
                      "OSError: connection refused", text)
        self.assertIn("ok    core", text)

    def test_healthy_service_passes(self):
        response = mock.MagicMock(status=200)
        response.__enter__.return_value = response
        with mock.patch.object(doctor.urllib.request, "urlopen", return_value=response) as urlopen:
            code, text = self.run_doctor('[services]\nmoonraker_url = "http://ad5x.lan:7125/"\n')
        self.assertEqual(code, 0, text)
        # The first call is the service probe; the doctor's zmod version row
        # (#562) reads the update status afterwards on this ZMod install.
        self.assertEqual(urlopen.call_args_list[0][0][0], "http://ad5x.lan:7125/printer/info")

    def test_doctor_run_reports_the_zmod_version_row(self):
        """#562 Part 2, Gap 2: the row shows in a full run, so the
        diagnostics bundle (which embeds doctor output) carries it too."""
        with mock.patch.object(doctor.urllib.request, "urlopen",
                               side_effect=_routing_urlopen(STATUS_WITH_ZMOD)):
            code, text = self.run_doctor('[services]\nmoonraker_url = "http://ad5x.lan:7125"\n')
        self.assertEqual(code, 0, text)
        self.assertIn("ok    zmod version: 1.7.5", text)

    def test_doctor_shows_the_default_model_warning_row(self):
        """#678: the settings layer decides (see DefaultedModelWarningTests); doctor only shows it."""
        with mock.patch.object(settings, "defaulted_model_warning", return_value="set PRINT_HANDY__PRINTER__MODEL"):
            code, text = self.run_doctor()
        self.assertIn("WARN  default model: set PRINT_HANDY__PRINTER__MODEL", text)
        self.assertNotIn("default model", self.run_doctor()[1])

    def test_doctor_shows_the_unknown_settings_warning_row(self):
        """#1058: unknown settings are a warning row, never a failed check; doctor only shows it."""
        with mock.patch.object(settings, "unknown_settings_warning",
                               return_value="unknown setting(s) ignored: [listener] ticks"):
            code, text = self.run_doctor()
        self.assertIn("WARN  unknown settings: unknown setting(s) ignored: [listener] ticks", text)
        self.assertNotIn("unknown settings", self.run_doctor()[1])

    def test_missing_orca_binary_fails(self):
        code, text = self.run_doctor('[orca]\ncommand = ["definitely-not-orca-xyz"]\n')
        self.assertEqual(code, 1)
        self.assertIn("FAIL  orca cli: definitely-not-orca-xyz is not on PATH", text)

    def test_orca_that_will_not_start_fails(self):
        self.orca_code = 1
        code, text = self.run_doctor()
        self.assertEqual(code, 1)
        self.assertIn("FAIL  orca cli: flatpak run com.orcaslicer.OrcaSlicer --help exited 1", text)

    def test_orca_probe_runs_from_a_writable_scratch_dir(self):
        # OrcaSlicer writes 00000.log into its cwd and aborts (-6) when it cannot; docker exec's
        # default cwd (/app) is not writable by the container user, so the probe must not inherit it.
        seen = {}

        def fake_run(command, **kwargs):
            cwd = kwargs.get("cwd")
            seen["cwd"] = cwd
            seen["writable"] = bool(cwd) and os.path.isdir(cwd) and os.access(cwd, os.W_OK)
            return mock.MagicMock(returncode=0)

        with mock.patch.object(doctor.shutil, "which", return_value="/usr/local/bin/orca-slicer"),                 mock.patch.object(doctor.subprocess, "run", side_effect=fake_run):
            doctor.check_orca({"orca": {"command": ["/usr/local/bin/orca-slicer"]}})
        self.assertTrue(seen["writable"])
        self.assertNotEqual(os.path.abspath(seen["cwd"]), os.path.abspath(os.getcwd()))
        self.assertFalse(os.path.exists(seen["cwd"]))  # scratch dir is cleaned up

    def test_missing_hardware_record_fails_with_the_path(self):
        code, text = self.run_doctor(hardware=False)
        self.assertEqual(code, 1)
        self.assertIn("FAIL  hardware record:", text)
        self.assertIn("printer_hardware.json is missing", text)

    def test_hardware_record_nozzle_must_match_settings(self):
        code, text = self.run_doctor(hardware={**HARDWARE, "nozzle": {"diameter_mm": 0.6}})
        self.assertEqual(code, 1)
        self.assertIn("recorded nozzle 0.6 mm does not match settings 0.4 mm", text)

    def test_hardware_record_needs_its_sections(self):
        code, text = self.run_doctor(hardware={"nozzle": {"diameter_mm": 0.4}})
        self.assertIn("lacks spool, temps_c", text)

    def test_hardware_record_needs_the_fields_slicing_reads(self):
        code, text = self.run_doctor(hardware={**HARDWARE, "spool": {}, "temps_c": {"nozzle": "hot"}})
        self.assertEqual(code, 1)
        self.assertIn("lacks spool.material, spool.density, temps_c.nozzle, temps_c.bed", text)

    def test_bad_service_url_is_a_settings_error(self):
        with self.assertRaisesRegex(settings.SettingsError, "must be a URL"):
            self.run_doctor('[services]\nspoolman_url = "spoolman.lan"\n')

class _FakeResponse:
    """A urlopen response: a status for the service probe, JSON for the reader."""

    def __init__(self, payload=None, status=200):
        self.status = status
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self.payload).encode() if self.payload is not None else b""


def _routing_urlopen(status_payload):
    """Route by path: the update-status read gets the payload, probes get a bare 200."""
    def fake(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if url.endswith("/machine/update/status"):
            return _FakeResponse(status_payload)
        return _FakeResponse()
    return fake


STATUS_WITH_ZMOD = {"result": {"version_info": {
    "klipper": {"version": "v0.12.0-289", "remote_version": "v0.12.0-289"},
    "zmod": {"version": "1.7.5", "remote_version": "1.7.5"}}}}


class ZmodVersionDoctorTests(unittest.TestCase):
    """#562 Part 2, Gap 2 (option b): the ZMod version rides in the doctor."""

    DATA = {"printer": {"model": "flashforge.ad5x"},
            "services": {"moonraker_url": "http://ad5x.lan:7125"}}

    def check(self, urlopen):
        with mock.patch.object(doctor.urllib.request, "urlopen", side_effect=urlopen):
            return doctor.check_zmod_version(self.DATA)

    def test_unreadable_update_manager_version_falls_back_to_the_os_info(self):
        """Live ZMod reports its git_repo components as "?"; the OS info names it."""
        status = {"result": {"version_info": {"Z-Mod": {"version": "?", "remote_version": "?"}}}}
        system_info = {"result": {"system_info": {"distribution": {
            "name": "zmod 1.7.5 -> 1.7.5-2", "version": "1.7.5"}}}}

        def fake(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else req
            if url.endswith("/machine/update/status"):
                return _FakeResponse(status)
            if url.endswith("/machine/system_info"):
                return _FakeResponse(system_info)
            return _FakeResponse()
        self.assertEqual(self.check(fake), "1.7.5-2 (from the printer's OS info)")

    def test_version_is_read_through_the_firmware_reader(self):
        detail = self.check(_routing_urlopen(STATUS_WITH_ZMOD))
        self.assertEqual(detail, "1.7.5")

    def test_absent_component_reports_unknown_not_silence(self):
        payload = {"result": {"version_info": {
            "klipper": {"version": "v0.12.0-289", "remote_version": "v0.12.0-289"}}}}
        self.assertEqual(self.check(_routing_urlopen(payload)),
                         "unknown (not reported by Moonraker)")

    def test_component_without_a_current_version_reports_unknown(self):
        payload = {"result": {"version_info": {"zmod": {"remote_version": "1.7.5"}}}}
        self.assertEqual(self.check(_routing_urlopen(payload)),
                         "unknown (not reported by Moonraker)")

    def test_unreachable_moonraker_is_named_unknown_and_never_raises(self):
        def refused(req, timeout=None):
            raise OSError("connection refused")
        detail = self.check(refused)
        self.assertTrue(detail.startswith("unknown (Moonraker's update status could not be read:"),
                        detail)
        self.assertIn("connection refused", detail)

    def test_malformed_status_is_named_unknown_and_never_raises(self):
        detail = self.check(_routing_urlopen({"result": {"status": {}}}))
        self.assertTrue(detail.startswith("unknown (Moonraker's update status could not be read:"),
                        detail)

    def test_non_zmod_family_skips_without_calling_out(self):
        data = {"printer": {"model": "klipper.generic"},
                "services": {"moonraker_url": "http://printer.lan:7125"}}
        urlopen = mock.Mock(side_effect=AssertionError("must not be called"))
        with mock.patch.object(doctor.urllib.request, "urlopen", urlopen):
            self.assertIsNone(doctor.check_zmod_version(data))

    def test_no_moonraker_url_skips(self):
        data = {"printer": {"model": "flashforge.ad5x"}, "services": {}}
        self.assertIsNone(doctor.check_zmod_version(data))
