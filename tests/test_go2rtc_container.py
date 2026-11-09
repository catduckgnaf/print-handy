"""Contract tests for #246 stage 1: go2rtc container plumbing.

Everything offline: the pinned prebuilt script is parsed (and only its
refusal paths are executed), the supervisor/healthcheck gates run against
fake children, and the deployment files are checked as rendered artefacts.
No Docker build, camera, GPU or network access is needed.
"""
import contextlib
import io
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from print_handy import go2rtc, settings  # noqa: E402

DOCKERFILE = (ROOT / "Dockerfile").read_text()
COMPOSE_TEXT = (ROOT / "docker-compose.yml").read_text()
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
UNRAID_TEXT = (ROOT / "docker/unraid/print-handy.xml").read_text()
PREBUILT = ROOT / "docker" / "go2rtc-prebuilt.sh"

AMD64_SHA = "20b46c6910734cb05ecefb234066f36868ff19b42ba1418a7a32e13a3e035712"
ARM64_SHA = "af416f22b21a24ab83b8d78642190b4932ae695b4e386853f11d1b0319781763"


class Go2rtcPrebuiltTests(unittest.TestCase):
    """go2rtc is a pinned official prebuilt, verified before installation."""

    def test_pin_table_parses_to_the_official_assets_and_digests(self):
        helper = PREBUILT.read_text()
        self.assertIn("version=1.9.15", helper)
        self.assertIn(f"sha256_amd64={AMD64_SHA}", helper)
        self.assertIn(f"sha256_arm64={ARM64_SHA}", helper)
        self.assertIn("asset_amd64=go2rtc_linux_amd64", helper)
        self.assertIn("asset_arm64=go2rtc_linux_arm64", helper)
        self.assertIn("https://github.com/AlexxIT/go2rtc/releases/download/v${version}/${asset}", helper)
        self.assertIn("set -eu", helper)
        # The digest is verified before the binary is installed anywhere.
        self.assertLess(helper.index("sha256sum --check --strict"),
                        helper.index("install -m 0755"))

    def test_refuses_other_architectures_before_any_download(self):
        for bad in ("arm", "arm/v7", "386", "ppc64le", ""):
            with self.subTest(arch=bad):
                result = subprocess.run(["sh", str(PREBUILT), bad],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 2)
                self.assertIn("amd64 and arm64 only", result.stderr)


class RuntimeImageGo2rtcTests(unittest.TestCase):
    """The published image bundles go2rtc + VAAPI; a WITH_GO2RTC=false build excludes both."""

    def test_go2rtc_stage_and_runtime_copy(self):
        self.assertIn("AS go2rtc-prebuilt", DOCKERFILE)
        self.assertIn("COPY --from=go2rtc-prebuilt /out/ /", DOCKERFILE)
        self.assertIn("sh /usr/local/bin/go2rtc-prebuilt \"$TARGETARCH\"", DOCKERFILE)

    def test_vaapi_stack_is_inside_the_with_go2rtc_conditional(self):
        runtime = DOCKERFILE.split("FROM debian:13-slim AS runtime", 1)[1]
        for package in ("intel-media-va-driver", "mesa-va-drivers", "libva-drm2", "vainfo"):
            with self.subTest(package=package):
                self.assertIn(package, runtime)
        vaapi_block = runtime.split('if [ "$WITH_GO2RTC" = true ]', 1)[1]
        self.assertIn("intel-media-va-driver", vaapi_block)
        # No CUDA toolkit or vendor driver stack is baked in.
        self.assertNotIn("cuda-toolkit", runtime)
        self.assertNotIn("nvidia-", runtime)

    def test_workflows_build_the_published_image_with_go2rtc(self):
        """#353: one published image; every publisher leg builds it with go2rtc."""
        for name in ("docker-publish.yml", "nightly.yml"):
            with self.subTest(workflow=name):
                workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
                jobs = workflow["jobs"]
                build = next(j for j in jobs.values() if "matrix" in j.get("strategy", {}))
                entries = build["strategy"]["matrix"]["include"]
                self.assertEqual({entry["arch"] for entry in entries}, {"amd64", "arm64"})
                self.assertEqual(build["env"]["WITH_GO2RTC"], "true")
                self.assertEqual(build["env"]["WITH_GO2RTC"], build["env"]["WITH_ORCA"])
        validation = (ROOT / ".github/workflows/docker-image-validation.yml").read_text()
        self.assertIn('WITH_GO2RTC: "true"', validation)
        self.assertIn('--build-arg "WITH_GO2RTC=$WITH_GO2RTC"', validation)


class TimelapseCaptureSettingsTests(unittest.TestCase):
    """New [timelapse] settings default to today's behaviour."""

    def test_defaults_are_off_and_unchanged_configs_load(self):
        data = settings.load(environ={})
        self.assertIs(data["timelapse"]["capture_enabled"], False)
        self.assertEqual(data["timelapse"]["capture_source_url"], "")

    def test_env_override_enables_capture(self):
        data = settings.load(environ={"PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED": "true"})
        self.assertIs(data["timelapse"]["capture_enabled"], True)

    def test_env_wins_over_the_file_value_in_both_directions(self):
        # Precedence contract (#540): the load order ends with the
        # environment, so PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED beats
        # timelapse.capture_enabled in the config file whichever way the
        # two disagree — the Unraid template variable can switch capture
        # off (or on) without editing a mounted file.
        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "local.yaml"
            local.write_text("timelapse:\n  capture_enabled: true\n")
            data = settings.load(local=local, environ={})
            self.assertIs(data["timelapse"]["capture_enabled"], True)
            data = settings.load(local=local,
                                 environ={"PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED": "false"})
            self.assertIs(data["timelapse"]["capture_enabled"], False)
            local.write_text("timelapse:\n  capture_enabled: false\n")
            data = settings.load(local=local,
                                 environ={"PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED": "true"})
            self.assertIs(data["timelapse"]["capture_enabled"], True)


def make_data(**timelapse):
    base = {"capture_enabled": True, "capture_source_url": ""}
    base.update(timelapse)
    return {"timelapse": base,
            "services": {"camera_snapshot_url": "http://printer.example/webcam/?action=snapshot"},
            "paths": {"cache": "/data/cache"}}


class Go2rtcGateAndConfigTests(unittest.TestCase):
    def test_gate_warns_and_continues_on_an_unknown_setting(self):
        # #1058, the incident shape: the load itself carried a setting this
        # version does not know. The gate must warn, naming the key, and
        # still decide — never exit 2 as "configuration unresolved".
        settings.load(environ={"PRINT_HANDY__LISTENER__TICKS": "5"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = go2rtc.gate(make_data(capture_enabled=False))
        self.assertEqual(code, 3)
        self.assertIn("go2rtc gate: WARNING", err.getvalue())
        self.assertIn("PRINT_HANDY__LISTENER__TICKS", err.getvalue())

    def test_gate_exit_codes(self):
        self.assertEqual(go2rtc.gate(make_data(capture_enabled=False)), 3)
        self.assertEqual(go2rtc.gate(make_data()), 0)
        no_source = make_data()
        no_source["services"]["camera_snapshot_url"] = ""
        self.assertEqual(go2rtc.gate(no_source), 3)

    def test_source_derives_stream_from_moonraker_snapshot(self):
        source = go2rtc.resolve_source(make_data())
        self.assertEqual(source.kind, "mjpeg")
        self.assertEqual(source.stream_url,
                         "http://printer.example/webcam/?action=stream")

    def test_source_override_wins_and_rtsp_stays_rtsp(self):
        source = go2rtc.resolve_source(make_data(capture_source_url="rtsp://cam.example/live"))
        self.assertEqual(source.kind, "rtsp")
        self.assertEqual(go2rtc.source_url(source), "rtsp://cam.example/live")

    def test_gate_never_prints_ffmpeg_source_credentials(self):
        # P1-71: the gate prints the resolved source; an ffmpeg: source wraps
        # a nested rtsp URL whose password must not reach the log line.
        data = make_data(
            capture_source_url="ffmpeg:rtsp://user:hunter2@cam.example/live#video=h264")
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(go2rtc.gate(data), 0)
        self.assertNotIn("hunter2", out.getvalue())
        self.assertIn("cam.example", out.getvalue())

    def test_snapshot_only_source_still_renders(self):
        data = make_data()
        data["services"]["camera_snapshot_url"] = "http://printer.example/snap.jpg"
        source = go2rtc.resolve_source(data)
        self.assertEqual(source.kind, "snapshot")
        self.assertIn("http://printer.example/snap.jpg", go2rtc.render_config(source))

    def test_rendered_config_binds_loopback_only(self):
        config = go2rtc.render_config(go2rtc.resolve_source(make_data()))
        self.assertIn('listen: "127.0.0.1:1984"', config)
        self.assertIn('listen: "127.0.0.1:8554"', config)
        self.assertNotIn("0.0.0.0", config)

    def test_credentials_never_printed_but_config_file_is_0600(self):
        secret_url = "http://user:hunter2@cam.example/webcam/?action=snapshot&api_key=sekrit"
        data = make_data()
        data["services"]["camera_snapshot_url"] = secret_url
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(go2rtc.gate(data), 0)
        self.assertNotIn("hunter2", out.getvalue())
        self.assertNotIn("sekrit", out.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            path = go2rtc.write_config(Path(directory) / "go2rtc.yaml", data)
            # go2rtc needs the raw URL; the file itself is the secret.
            self.assertIn("hunter2", path.read_text())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_check_probes_the_api(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 1984), Handler)
        except OSError:
            self.skipTest("port 1984 is in use on this host")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            ok, reason = go2rtc.check()
            self.assertTrue(ok, reason)
        finally:
            server.shutdown()
        ok, _ = go2rtc.check(timeout=1)
        self.assertFalse(ok)


def make_live_data(**live):
    section = {"enabled": True}
    section.update(live)
    data = make_data(capture_enabled=False)
    data["live_stream"] = section
    return data


class Go2rtcLiveGateTests(unittest.TestCase):
    """#311 stage 4: live viewing alone starts the same one go2rtc child."""

    def test_live_viewing_alone_opens_the_gate(self):
        self.assertEqual(go2rtc.gate(make_live_data()), 0)
        self.assertEqual(go2rtc.needed_by(make_live_data()), ("live viewing",))

    def test_both_features_share_the_one_gate(self):
        data = make_live_data()
        data["timelapse"]["capture_enabled"] = True
        self.assertEqual(go2rtc.needed_by(data), ("timelapse capture", "live viewing"))
        self.assertEqual(go2rtc.gate(data), 0)

    def test_live_viewing_without_a_source_degrades_off(self):
        data = make_live_data()
        data["services"]["camera_snapshot_url"] = ""
        self.assertEqual(go2rtc.gate(data), 3)

    def test_neither_feature_needs_go2rtc(self):
        data = make_data(capture_enabled=False)
        data["live_stream"] = {"enabled": False}
        self.assertEqual(go2rtc.gate(data), 3)

    def test_the_stream_name_is_the_one_the_activity_page_requests(self):
        from print_handy import live_activity, live_stream
        config = go2rtc.render_config(go2rtc.resolve_source(make_live_data()))
        self.assertIn(f"  {live_stream.GO2RTC_STREAM_NAME}: ", config)
        self.assertEqual(live_activity.DEFAULT_STREAM_NAME,
                         live_stream.GO2RTC_STREAM_NAME)


class SupervisorGo2rtcGateTests(unittest.TestCase):
    """supervise.sh starts go2rtc only when the gate says so (fake children)."""

    def environment(self, directory, gate_code):
        fake_python = Path(directory, "python3")
        fake_python.write_text(
            "#!/bin/bash\n"
            "case \"$*\" in\n"
            "  *print_handy.listener_gate*--roster*) printf 'printer1\\n'; exit 0 ;;\n"
            "  *print_handy.listener_gate*) exit 3 ;;\n"
            "  *print_handy.bot_gate*) exit 3 ;;\n"
            "  *print_handy.webui_gate*) exit 3 ;;\n"
            "  *print_handy.live_gate*) exit 3 ;;\n"
            "  *print_handy.go2rtc*--write-config*) printf '%s\\n' \"$GO2RTC_TEST_CONFIG\" ;;\n"
            f"  *print_handy.go2rtc*) exit {gate_code} ;;\n"
            "  *) echo 'unexpected python3 call: $*' >&2; exit 9 ;;\n"
            "esac\n")
        fake_python.chmod(0o755)
        fake_go2rtc = Path(directory, "go2rtc")
        fake_go2rtc.write_text(
            "#!/bin/bash\n"
            "printf '%s\\n' \"$*\" > \"$GO2RTC_TEST_MARKER\"\n"
            "exec sleep 60\n")
        fake_go2rtc.chmod(0o755)
        return {**os.environ, "PATH": directory + ":" + os.environ["PATH"],
                "PRINT_HANDY_BUNDLED_SPOOLMAN": "false",
                "PRINT_HANDY__SERVICES__SPOOLMAN_URL": "http://external.invalid:8000",
                "DISCORD_BOT_TOKEN": "",
                "GO2RTC_BIN": str(fake_go2rtc),
                "GO2RTC_TEST_CONFIG": str(Path(directory, "go2rtc.yaml")),
                "GO2RTC_TEST_MARKER": str(Path(directory, "go2rtc-args"))}

    def run_supervisor(self, gate_code):
        directory = tempfile.mkdtemp()
        env = self.environment(directory, gate_code)
        process = subprocess.Popen(["bash", str(ROOT / "docker/supervise.sh")],
                                   env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        return directory, process

    def test_gate_off_idles_without_go2rtc(self):
        directory, process = self.run_supervisor(3)
        try:
            time.sleep(1.5)
            self.assertIsNone(process.poll())
            self.assertFalse(Path(directory, "go2rtc-args").exists())
            process.send_signal(signal.SIGTERM)
            out, _ = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0)
            self.assertIn("go2rtc not started", out)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_gate_on_starts_go2rtc_with_the_generated_config(self):
        directory, process = self.run_supervisor(0)
        marker = Path(directory, "go2rtc-args")
        try:
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            self.assertEqual(marker.read_text().strip(),
                             f"-config {Path(directory, 'go2rtc.yaml')}")
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_gate_unresolved_stops_the_container(self):
        directory, process = self.run_supervisor(2)
        out, err = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 2)
        self.assertIn("go2rtc gate failed", err)
        self.assertFalse(Path(directory, "go2rtc-args").exists())


class DeploymentContractTests(unittest.TestCase):
    """Loopback-only, passthrough opt-in, and healthcheck coverage."""

    def test_no_go2rtc_port_is_published_anywhere(self):
        # The one published port is the Web UI's (published by default for
        # first boot); neither go2rtc port (1984 API, 8554 RTSP) may appear.
        self.assertEqual(COMPOSE["services"]["combined"].get("ports"),
                         ["8088:${PRINT_HANDY__WEBUI__PORT:-8088}"])
        for text in (COMPOSE_TEXT, UNRAID_TEXT):
            self.assertNotIn("1984", text)
            self.assertNotIn("8554", text)

    def test_gpu_passthrough_is_commented_opt_in(self):
        service = COMPOSE["services"]["combined"]
        self.assertNotIn("devices", service)
        self.assertNotIn("group_add", service)
        self.assertIn("#   - /dev/dri:/dev/dri", COMPOSE_TEXT)
        self.assertIn("# devices:", COMPOSE_TEXT)

    def test_unraid_device_is_an_optional_entry_and_capture_is_config_only(self):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(UNRAID_TEXT)
        self.assertNotIn("--device", root.findtext("ExtraParams") or "")
        self.assertIn("/dev/dri", UNRAID_TEXT)
        # The slimmed template's GPU surface is the optional /dev/dri
        # device entry (Advanced View); it is a template entry, never an
        # ExtraParams flag.
        devices = {c.get("Target"): c for c in root.findall("Config")
                   if c.get("Type") == "Device"}
        self.assertEqual(devices["/dev/dri"].get("Required"), "false")
        self.assertEqual(devices["/dev/dri"].get("Display"), "advanced")
        # Capture itself is no longer a template variable at all: the
        # opt-in lives in the config file / Compose, off by default
        # (test_compose_carries_capture_with_off_default below).
        variables = {c.get("Target"): c for c in root.findall("Config")
                     if c.get("Type") == "Variable"}
        self.assertNotIn("PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED", variables)
        self.assertNotIn("PRINT_HANDY__TIMELAPSE__CAPTURE_SOURCE_URL", variables)

    def test_compose_carries_capture_with_off_default(self):
        environment = COMPOSE["services"]["combined"]["environment"]
        self.assertEqual(environment["PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED"],
                         "${PRINT_HANDY__TIMELAPSE__CAPTURE_ENABLED:-false}")

    def test_healthcheck_covers_go2rtc_only_through_the_gate(self):
        text = (ROOT / "docker/healthcheck.py").read_text()
        self.assertIn("go2rtc.gate()", text)
        self.assertIn("go2rtc.check()", text)
        self.assertIn("go2rtc.binary_present()", text)

    def test_supervise_skips_go2rtc_without_the_binary(self):
        text = (ROOT / "docker/supervise.sh").read_text()
        self.assertIn('go2rtc_bin="${GO2RTC_BIN:-/usr/local/bin/go2rtc}"', text)
        self.assertIn('if [ -x "$go2rtc_bin" ]', text)
        self.assertIn("python3 -m print_handy.go2rtc", text)


class FanoutFrameUrlTests(unittest.TestCase):
    """#311 one-upstream rule: stills consumers use the fan-out only
    when go2rtc should actually be running."""

    def fanout(self, data, binary=True):
        from unittest import mock
        if binary:
            fake = tempfile.NamedTemporaryFile(delete=False)
            fake.close()
            os.chmod(fake.name, 0o755)
            path = fake.name
        else:
            path = "/nonexistent/go2rtc"
        with mock.patch.dict(os.environ, {"GO2RTC_BIN": path}):
            return go2rtc.fanout_frame_url(data)

    def test_capture_on_with_binary_and_source(self):
        self.assertEqual(self.fanout(make_data()), go2rtc.FRAME_URL)
        self.assertEqual(go2rtc.FRAME_URL,
                         "http://127.0.0.1:1984/api/frame.jpeg?src=camera")

    def test_live_viewing_on_with_binary_and_source(self):
        self.assertEqual(self.fanout(make_live_data()), go2rtc.FRAME_URL)

    def test_neither_feature_on_means_no_fanout(self):
        self.assertIsNone(self.fanout(make_data(capture_enabled=False)))

    def test_no_binary_means_no_fanout(self):
        self.assertIsNone(self.fanout(make_data(), binary=False))

    def test_no_source_means_no_fanout(self):
        data = make_data()
        data["services"]["camera_snapshot_url"] = ""
        self.assertIsNone(self.fanout(data))

    def test_unresolvable_source_reads_as_no_fanout(self):
        data = make_data()
        data["services"]["camera_snapshot_url"] = "::::"
        self.assertIsNone(self.fanout(data))

    def test_frame_url_is_the_one_timelapse_capture_uses(self):
        from print_handy.hermes import timelapse_capture
        self.assertEqual(timelapse_capture.GO2RTC_FRAME_URL, go2rtc.FRAME_URL)


def mode_data(mode="", capture_enabled=False, moonraker=True):
    """A settings dict for the mode gates, with the mode left to derive."""
    timelapse = {"capture_enabled": capture_enabled, "capture_source_url": ""}
    if mode:
        timelapse["mode"] = mode
    services = {"camera_snapshot_url": "http://printer.example/webcam/?action=snapshot"}
    if moonraker:
        services["moonraker_url"] = "http://printer.example:7125"
    return {"timelapse": timelapse, "services": services, "paths": {"cache": "/data/cache"}}


class TimelapseModeGateTests(unittest.TestCase):
    """2.2.2 wiring: [timelapse] mode gates capture and the rendered path.

    Before the fix the mode was a dead catalog option: capture read only
    capture_enabled and nothing read the mode at all.
    """

    def test_mode_off_vetoes_capture_and_the_rendered_path(self):
        data = mode_data(mode="off", capture_enabled=True)
        self.assertFalse(go2rtc.capture_active(data))
        self.assertFalse(go2rtc.rendered_active(data))
        self.assertEqual(go2rtc.needed_by(data), ())

    def test_mode_container_opens_capture_and_closes_the_rendered_path(self):
        data = mode_data(mode="container", capture_enabled=False)
        self.assertTrue(go2rtc.capture_active(data))
        self.assertFalse(go2rtc.rendered_active(data))
        self.assertEqual(go2rtc.needed_by(data), ("timelapse capture",))

    def test_mode_rendered_leaves_the_capture_switch_in_charge(self):
        self.assertTrue(go2rtc.capture_active(mode_data(mode="rendered", capture_enabled=True)))
        self.assertFalse(go2rtc.capture_active(mode_data(mode="rendered", capture_enabled=False)))
        self.assertTrue(go2rtc.rendered_active(mode_data(mode="rendered")))

    def test_derived_modes_match_the_settings_derivation(self):
        # The same three derivations settings._validate_timelapse applies:
        # Moonraker URL -> rendered; capture on without one -> container;
        # neither -> off (and capture stays off there).
        self.assertEqual(go2rtc.timelapse_mode(mode_data()), "rendered")
        self.assertEqual(go2rtc.timelapse_mode(mode_data(capture_enabled=True, moonraker=False)),
                         "container")
        self.assertEqual(go2rtc.timelapse_mode(mode_data(moonraker=False)), "off")
        self.assertTrue(go2rtc.capture_active(mode_data(capture_enabled=True, moonraker=False)))


def pin_data(pin, **timelapse):
    data = make_data(**timelapse)
    data["services"]["camera_source"] = pin
    return data


class CameraSourcePinTests(unittest.TestCase):
    """2.2.2 wiring: [services] camera_source narrows the fan-out source.

    Before the fix nothing read the pin: "none" did not turn camera
    images off and an explicit source changed nothing.
    """

    def test_pin_none_resolves_no_source_and_the_gate_stays_closed(self):
        data = pin_data("none")
        self.assertIsNone(go2rtc.resolve_source(data))
        self.assertEqual(go2rtc.gate(data), 3)

    def test_pin_moonraker_ignores_a_stream_override(self):
        source = go2rtc.resolve_source(
            pin_data("moonraker", capture_source_url="rtsp://cam.example/live"))
        self.assertEqual(source.kind, "mjpeg")
        self.assertEqual(source.stream_url, "http://printer.example/webcam/?action=stream")

    def test_pin_go2rtc_accepts_a_stream_but_refuses_a_stills_only_source(self):
        source = go2rtc.resolve_source(
            pin_data("go2rtc", capture_source_url="rtsp://cam.example/live"))
        self.assertEqual(source.kind, "rtsp")
        stills = pin_data("go2rtc")
        stills["services"]["camera_snapshot_url"] = "http://printer.example/snap.jpg"
        self.assertIsNone(go2rtc.resolve_source(stills))

    def test_pin_bambu_resolves_a_shared_source_only_from_the_stream_override(self):
        self.assertIsNone(go2rtc.resolve_source(pin_data("bambu")))
        source = go2rtc.resolve_source(
            pin_data("bambu", capture_source_url="rtsps://192.0.2.5:322/stream_0"))
        self.assertEqual(source.kind, "rtsp")

    def test_pin_auto_keeps_the_shipped_behavior(self):
        source = go2rtc.resolve_source(pin_data("auto", capture_source_url="rtsp://cam.example/live"))
        self.assertEqual(source.kind, "rtsp")
        source = go2rtc.resolve_source(pin_data(""))
        self.assertEqual(source.kind, "mjpeg")


def live_data(**live):
    data = make_data(capture_enabled=False)
    section = {"enabled": True, "max_height": 720, "fps": 15,
               "max_bitrate_kbps": 2500, "encoder": "auto"}
    section.update(live)
    data["live_stream"] = section
    data["paths"]["ffmpeg"] = "/nonexistent/ffmpeg"
    return data


class LiveTranscodeConfigTests(unittest.TestCase):
    """2.2.2 wiring: the [live_stream] feed values shape the generated config.

    Before the fix the four values were dead options: build_ffmpeg_args
    had no production caller and the shipped feed ignored them. Now a
    value away from its default makes write_config publish a transcoded
    stream, built by that same builder, over the raw stream's restream.
    """

    CPU_PROBE = None  # built in setUp: an ffmpeg with only libx264

    def setUp(self):
        from print_handy.live_stream import EncoderProbe
        self.cpu_probe = EncoderProbe(encoders=frozenset({"libx264"}))

    def test_no_plan_at_the_default_feed_values(self):
        self.assertIsNone(go2rtc.transcode_plan(live_data(), probe=self.cpu_probe))
        self.assertIsNone(go2rtc.transcode_plan(live_data(enabled=False, fps=30),
                                                probe=self.cpu_probe))

    def test_plan_carries_the_section_values_and_the_probed_candidate(self):
        plan = go2rtc.transcode_plan(
            live_data(max_height=1080, fps=30, max_bitrate_kbps=6000, encoder="cpu"),
            probe=self.cpu_probe)
        self.assertEqual(plan["candidate"].encoder, "libx264")
        self.assertEqual((plan["max_height"], plan["fps"], plan["bitrate_kbps"]),
                         (1080, 30, 6000))

    def test_plan_carries_the_configured_ffmpeg_and_the_config_uses_it(self):
        # P2-262: paths.ffmpeg is the executable transcode_plan probes;
        # the generated exec command must launch that same binary, not
        # a bare "ffmpeg" that may resolve elsewhere — or nowhere.
        data = live_data(fps=30)
        plan = go2rtc.transcode_plan(data, probe=self.cpu_probe)
        self.assertEqual(plan["ffmpeg"], "/nonexistent/ffmpeg")
        config = go2rtc.render_config(go2rtc.resolve_source(data), transcode=plan)
        self.assertIn("exec:/nonexistent/ffmpeg", config)
        self.assertNotIn("exec:ffmpeg ", config)

    def test_rendered_config_publishes_the_transcoded_stream(self):
        data = live_data(max_height=1080, fps=30, max_bitrate_kbps=6000, encoder="cpu")
        config = go2rtc.render_config(go2rtc.resolve_source(data),
                                      transcode=go2rtc.transcode_plan(data, probe=self.cpu_probe))
        # The raw stream keeps its name and URL (capture reads it)...
        self.assertIn('camera: "http://printer.example/webcam/?action=stream"', config)
        # ...and the live stream is an ffmpeg exec over the loopback
        # restream, with all four values in the argv and go2rtc's
        # {output} placeholder as the publish target.
        self.assertIn("camera-live:", config)
        # P2-262: the exec launches paths.ffmpeg ("/nonexistent/ffmpeg"
        # in this fixture) — the binary the plan probed.
        self.assertIn("exec:/nonexistent/ffmpeg", config)
        self.assertIn("-i rtsp://127.0.0.1:8554/camera", config)
        self.assertIn("scale=-2:1080", config)
        self.assertIn("-r 30", config)
        self.assertIn("-b:v 6000k", config)
        self.assertIn("libx264", config)
        self.assertIn("-f rtsp {output}", config)

    def test_write_config_end_to_end(self):
        # paths.ffmpeg names no real binary: the probe reports nothing
        # and the plan still exists on the guaranteed CPU candidate.
        import tempfile
        data = live_data(fps=24)
        with tempfile.TemporaryDirectory() as tmp:
            path = go2rtc.write_config(Path(tmp) / "go2rtc.yaml", data)
            text = path.read_text()
        self.assertIn("camera-live:", text)
        self.assertIn("-r 24", text)
        data = live_data()
        with tempfile.TemporaryDirectory() as tmp:
            path = go2rtc.write_config(Path(tmp) / "go2rtc.yaml", data)
            self.assertNotIn("camera-live", path.read_text())


if __name__ == "__main__":
    unittest.main()
