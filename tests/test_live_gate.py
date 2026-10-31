"""#311 stage 4: the live-view gate, the shared session record, presence.

The Activity server runs only when [live_stream] is enabled AND
configured (print_handy.live_gate, mirroring the Web UI gate #286 and
the go2rtc gate #246): the decision comes from the resolved settings
plus the presence — never the values — of the deployment environment
(the Discord application ID and the two secrets). Offline throughout:
fake settings, fake environment, a fake session file, and the docker
healthcheck loaded from source.
"""
import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy import live_activity, live_gate, live_stream, settings

ROOT = Path(__file__).resolve().parents[1]

FULL_ENV = {"DISCORD_CLIENT_ID": "123456789012345678",
            "DISCORD_CLIENT_SECRET": "client-secret-value",
            "LIVE_STREAM_SESSION_SECRET": "session-secret-value"}


def data(**live):
    section = {"enabled": True, "voice_channel_id": "424242424242424242",
               "auto_start_on_print": False, "max_height": 720, "fps": 15,
               "max_bitrate_kbps": 2500, "encoder": "auto",
               "empty_timeout_s": 300, "activity_origin": "https://pb.example",
               "port": 8091}
    section.update(live)
    return {"live_stream": section,
            "discord": {"approval_user_id": "42", "allowed_user_ids": "43, 44"}}


class LiveGateDecisionTests(unittest.TestCase):
    def test_enabled_and_configured_is_ready(self):
        config = live_gate.live_config(data(), FULL_ENV)
        self.assertTrue(config.ready)
        self.assertEqual(config.missing, ())
        self.assertEqual(live_gate.decide(data(), FULL_ENV), 0)

    def test_disabled_is_never_ready_and_names_nothing_missing(self):
        config = live_gate.live_config(data(enabled=False), {})
        self.assertFalse(config.enabled)
        self.assertEqual(config.missing, ())
        self.assertEqual(live_gate.decide(data(enabled=False), {}), 3)

    def test_each_missing_piece_is_named(self):
        cases = [
            (data(voice_channel_id=""), FULL_ENV, "voice_channel_id"),
            (data(activity_origin=""), FULL_ENV, "activity_origin"),
            (data(port=0), FULL_ENV, "port is 0"),
            (data(), {"DISCORD_CLIENT_SECRET": "x", "LIVE_STREAM_SESSION_SECRET": "y"},
             "DISCORD_CLIENT_ID"),
            (data(), {"DISCORD_CLIENT_ID": "1", "LIVE_STREAM_SESSION_SECRET": "y"},
             "DISCORD_CLIENT_SECRET"),
            (data(), {"DISCORD_CLIENT_ID": "1", "DISCORD_CLIENT_SECRET": "x"},
             "LIVE_STREAM_SESSION_SECRET"),
        ]
        for config_data, env, needle in cases:
            with self.subTest(needle=needle):
                config = live_gate.live_config(config_data, env)
                self.assertFalse(config.ready)
                self.assertEqual(live_gate.decide(config_data, env), 3)
                self.assertIn(needle, "; ".join(config.missing))

    def test_secret_values_never_appear_in_the_missing_report(self):
        config = live_gate.live_config(data(port=0), FULL_ENV)
        self.assertNotIn("client-secret-value", "; ".join(config.missing))
        self.assertNotIn("session-secret-value", "; ".join(config.missing))


class LiveGateCliTests(unittest.TestCase):
    def run_gate(self, config):
        with mock.patch.object(live_gate, "live_config", return_value=config):
            return live_gate.main()

    def test_ready_is_zero(self):
        config = live_gate.LiveConfig(enabled=True, port=8091,
                                      activity_origin="https://pb.example",
                                      client_id="123", missing=())
        self.assertEqual(self.run_gate(config), 0)

    def test_disabled_and_incomplete_are_three(self):
        for config in (live_gate.LiveConfig(enabled=False, port=0, activity_origin="",
                                            client_id="", missing=()),
                       live_gate.LiveConfig(enabled=True, port=0, activity_origin="",
                                            client_id="", missing=("port is 0",))):
            with self.subTest(config=config):
                self.assertEqual(self.run_gate(config), 3)

    def test_unresolved_configuration_is_a_distinct_failure(self):
        with mock.patch.object(live_gate, "live_config",
                               side_effect=settings.SettingsError("bad live")):
            self.assertEqual(live_gate.main(), 2)


class AllowlistTests(unittest.TestCase):
    def test_allowlist_is_the_bot_trust_list(self):
        self.assertEqual(live_activity.allowlist_from_settings(data()),
                         ("42", "43", "44"))

    def test_allowlist_without_an_approval_user(self):
        config = data()
        config["discord"] = {"approval_user_id": "", "allowed_user_ids": "43"}
        self.assertEqual(live_activity.allowlist_from_settings(config), ("43",))


class SharedSessionRecordTests(unittest.TestCase):
    def test_round_trip_through_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live_session.json"
            session = live_stream.LiveStreamSession().start("42", now=1000.0)
            session.note_viewers(2, now=1001.0)
            live_stream.write_session_file(session, path)
            back = live_stream.read_session_file(path)
            self.assertTrue(back.is_live)
            self.assertEqual((back.started_by, back.started_at, back.viewers),
                             ("42", 1000.0, 2))
            self.assertIsNone(back.empty_since)

    def test_missing_and_corrupt_records_are_idle_never_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live_session.json"
            self.assertFalse(live_stream.read_session_file(path).is_live)
            path.write_text("{not json")
            self.assertFalse(live_stream.read_session_file(path).is_live)
            path.write_text('{"state": "live", "started_at": "soon"}')
            self.assertFalse(live_stream.read_session_file(path).is_live)


class PresenceTests(unittest.TestCase):
    """The Activity server's presence notes drive the empty timeout."""

    SECTION = {"enabled": True, "voice_channel_id": "424242424242424242",
               "empty_timeout_s": 300}

    def make_presence(self, tmp):
        path = Path(tmp) / "live_session.json"
        session = live_stream.LiveStreamSession().start("42", now=1000.0)
        live_stream.write_session_file(session, path)
        return path, live_activity.SessionPresence(path, self.SECTION)

    def test_a_viewer_clears_the_empty_clock_and_a_leave_restarts_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, presence = self.make_presence(tmp)
            presence.note_viewer("42", now=1100.0)
            back = live_stream.read_session_file(path)
            self.assertEqual(back.viewers, 1)
            self.assertIsNone(back.empty_since)
            presence.note_leave("42", now=1200.0)
            back = live_stream.read_session_file(path)
            self.assertEqual(back.viewers, 0)
            self.assertEqual(back.empty_since, 1200.0)

    def test_tick_ends_a_session_that_stayed_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, presence = self.make_presence(tmp)
            self.assertTrue(presence.tick(now=1000.0 + 299).is_live)
            self.assertFalse(presence.tick(now=1000.0 + 300).is_live)
            self.assertFalse(live_stream.read_session_file(path).is_live)

    def test_tick_keeps_a_watched_session_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, presence = self.make_presence(tmp)
            presence.note_viewer("42", now=1100.0)
            self.assertTrue(presence.tick(now=1000.0 + 10000).is_live)


def load_docker_healthcheck():
    spec = importlib.util.spec_from_file_location(
        "pb_docker_healthcheck_live", ROOT / "docker" / "healthcheck.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DockerHealthcheckLiveRowTests(unittest.TestCase):
    """docker/healthcheck.py mirrors the live gate through the same helper."""

    def run_main(self, module, urlopen):
        env = {"DISCORD_BOT_TOKEN": "", "PRINT_HANDY_BUNDLED_SPOOLMAN": "true"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(module, "listener_required", return_value=False), \
                mock.patch.object(module, "webui_config",
                                  return_value=(False, "127.0.0.1", 0, "WEBUI_ACCESS_TOKEN", True)), \
                mock.patch.object(module.urllib.request, "urlopen", urlopen), \
                contextlib.redirect_stdout(out):
            code = module.main()
        return code, out.getvalue()

    @staticmethod
    def ok_response(status=200):
        response = mock.MagicMock()
        response.__enter__.return_value.status = status
        return response

    def config(self, **kwargs):
        base = {"enabled": False, "port": 0, "activity_origin": "",
                "client_id": "", "missing": ()}
        base.update(kwargs)
        return live_gate.LiveConfig(**base)

    def test_disabled_live_view_is_an_ok_skip(self):
        module = load_docker_healthcheck()
        with mock.patch.object(module, "live_config", return_value=self.config()):
            code, out = self.run_main(module, mock.Mock(return_value=self.ok_response()))
        self.assertEqual(code, 0)
        self.assertIn("ok  live view: disabled", out)

    def test_incomplete_live_view_is_an_ok_report(self):
        module = load_docker_healthcheck()
        config = self.config(enabled=True, missing=("[live_stream] port is 0 (no port chosen)",))
        with mock.patch.object(module, "live_config", return_value=config):
            code, out = self.run_main(module, mock.Mock(return_value=self.ok_response()))
        self.assertEqual(code, 0)
        self.assertIn("ok  live view: enabled but not configured; not started", out)

    def test_serving_live_view_is_ok(self):
        module = load_docker_healthcheck()
        config = self.config(enabled=True, port=8091,
                             activity_origin="https://pb.example", client_id="123")
        with mock.patch.object(module, "live_config", return_value=config):
            code, out = self.run_main(module, mock.Mock(return_value=self.ok_response()))
        self.assertEqual(code, 0)
        self.assertIn("ok  live view: Activity server serving on 127.0.0.1:8091", out)

    def test_unreachable_live_view_fails(self):
        module = load_docker_healthcheck()
        config = self.config(enabled=True, port=8091,
                             activity_origin="https://pb.example", client_id="123")
        with mock.patch.object(module, "live_config", return_value=config):
            code, out = self.run_main(module, mock.Mock(side_effect=OSError("refused")))
        self.assertEqual(code, 1)
        self.assertIn("FAIL  live view:", out)

    def test_unresolved_live_configuration_fails(self):
        module = load_docker_healthcheck()
        with mock.patch.object(module, "live_config",
                               side_effect=settings.SettingsError("bad live")):
            code, out = self.run_main(module, mock.Mock(return_value=self.ok_response()))
        self.assertEqual(code, 1)
        self.assertIn("FAIL  live view: configuration unresolved", out)


if __name__ == "__main__":
    unittest.main()
