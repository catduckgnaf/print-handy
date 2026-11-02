"""#286 stage 4: the Web UI child runs only when [webui] is enabled with a port.

The decision comes from the resolved configuration (print_handy.webui_gate,
mirroring the listener gate #256), never from an environment guess. Offline:
fake settings only, plus the docker/healthcheck.py Web UI row loaded from
source the way tests/test_listener_gate.py loads it.
"""
import contextlib
import importlib.util
import io
import os
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from print_handy import settings, webui_gate

ROOT = Path(__file__).resolve().parents[1]


def data(enabled, port, host="127.0.0.1"):
    return {"webui": {"enabled": enabled, "host": host, "port": port,
                      "access_token_env": "WEBUI_ACCESS_TOKEN"}}


class WebuiGateDecisionTests(unittest.TestCase):
    def test_enabled_with_a_port_starts(self):
        self.assertEqual(webui_gate.decide(data(True, 8080)), 0)
        self.assertEqual(webui_gate.webui_config(data(True, 8080)),
                         (True, "127.0.0.1", 8080, "WEBUI_ACCESS_TOKEN", True))

    def test_disabled_never_starts(self):
        self.assertEqual(webui_gate.decide(data(False, 8080)), 3)
        self.assertEqual(webui_gate.decide(data(False, 0)), 3)

    def test_enabled_without_a_port_does_not_start(self):
        self.assertEqual(webui_gate.decide(data(True, 0)), 3)

    def test_non_loopback_without_a_token_starts_in_setup(self):
        # #440 used to block this bind; since #708 it starts and serves only
        # the first-run setup page (print_handy.webui.access), so the gate
        # starts it and start_blocked never blocks.
        lan = data(True, 8088, host="0.0.0.0")
        self.assertEqual(webui_gate.decide(lan, environ={}), 0)
        self.assertIsNone(webui_gate.start_blocked(
            "0.0.0.0", 8088, "WEBUI_ACCESS_TOKEN", environ={}))
        env = {"WEBUI_ACCESS_TOKEN": "a-long-random-token"}
        self.assertEqual(webui_gate.decide(lan, environ=env), 0)
        self.assertEqual(webui_gate.decide(data(True, 8088), environ={}), 0)

    def test_require_auth_false_starts_a_lan_bind_without_a_token(self):
        open_lan = data(True, 8088, host="0.0.0.0")
        open_lan["webui"]["require_auth"] = False
        self.assertEqual(webui_gate.decide(open_lan, environ={}), 0)
        self.assertIsNone(webui_gate.start_blocked(
            "0.0.0.0", 8088, "WEBUI_ACCESS_TOKEN", environ={}, require_auth=False))

    def test_cli_warns_loudly_when_auth_is_off(self):
        out = io.StringIO()
        with mock.patch.object(webui_gate, "webui_config",
                               return_value=(True, "0.0.0.0", 8088, "WEBUI_ACCESS_TOKEN", False)), \
                contextlib.redirect_stdout(out):
            self.assertEqual(webui_gate.main(), 0)
        self.assertIn("authentication OFF", out.getvalue())


class WebuiGateCliTests(unittest.TestCase):
    def run_gate(self, config):
        with mock.patch.object(webui_gate, "webui_config", return_value=config):
            return webui_gate.main()

    def test_enabled_with_a_port_is_zero(self):
        self.assertEqual(self.run_gate((True, "127.0.0.1", 8080, "WEBUI_ACCESS_TOKEN", True)), 0)

    def test_disabled_and_portless_are_three(self):
        for config in ((False, "127.0.0.1", 8080, "WEBUI_ACCESS_TOKEN", True),
                       (True, "127.0.0.1", 0, "WEBUI_ACCESS_TOKEN", True)):
            with self.subTest(config=config):
                self.assertEqual(self.run_gate(config), 3)

    def test_unresolved_configuration_is_a_distinct_failure(self):
        with mock.patch.object(webui_gate, "webui_config",
                               side_effect=settings.SettingsError("bad webui")):
            self.assertEqual(webui_gate.main(), 2)

    def test_setup_pending_start_is_zero_and_says_so(self):
        # #708: a LAN bind with no token and no saved choice starts in
        # first-run setup; the log line tells the operator where to finish it.
        out = io.StringIO()
        with mock.patch.object(webui_gate, "setup_pending", return_value=True),                 contextlib.redirect_stdout(out):
            code = self.run_gate((True, "0.0.0.0", 8088, "WEBUI_ACCESS_TOKEN", True))
        self.assertEqual(code, 0)
        self.assertIn("first-run setup is waiting", out.getvalue())


def load_docker_healthcheck():
    spec = importlib.util.spec_from_file_location(
        "pb_docker_healthcheck_webui", ROOT / "docker" / "healthcheck.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DockerHealthcheckWebuiRowTests(unittest.TestCase):
    """docker/healthcheck.py mirrors the gate: skipped when disabled."""

    def run_main(self, module, urlopen):
        env = {"DISCORD_BOT_TOKEN": "", "PRINT_HANDY_BUNDLED_SPOOLMAN": "true"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(module, "listener_required", return_value=False), \
                mock.patch.object(module.urllib.request, "urlopen", urlopen), \
                contextlib.redirect_stdout(out):
            code = module.main()
        return code, out.getvalue()

    @staticmethod
    def ok_response(status=200):
        response = mock.MagicMock()
        response.__enter__.return_value.status = status
        return response

    def test_disabled_webui_is_an_ok_skip(self):
        module = load_docker_healthcheck()
        with mock.patch.object(module, "webui_config",
                               return_value=(False, "127.0.0.1", 0, "WEBUI_ACCESS_TOKEN", True)):
            code, out = self.run_main(module, mock.Mock(return_value=self.ok_response()))
        self.assertEqual(code, 0)
        self.assertIn("ok  webui: disabled", out)

    def test_enabled_but_portless_is_an_ok_skip(self):
        module = load_docker_healthcheck()
        with mock.patch.object(module, "webui_config",
                               return_value=(True, "127.0.0.1", 0, "WEBUI_ACCESS_TOKEN", True)):
            code, out = self.run_main(module, mock.Mock(return_value=self.ok_response()))
        self.assertEqual(code, 0)
        self.assertIn("ok  webui: enabled but no port chosen; not started", out)

    def urlopen_for(self, healthz):
        """Fake urlopen: Spoolman always answers 200; /healthz follows `healthz`.

        `healthz` is a status int, or an exception instance to raise. Returns
        (mock, list of probed /healthz URLs).
        """
        probed = []

        def _open(request, timeout=0):
            url = request.full_url if hasattr(request, "full_url") else str(request)
            if url.endswith("/healthz"):
                probed.append(url)
                if isinstance(healthz, Exception):
                    raise healthz
                if healthz != 200:
                    raise urllib.error.HTTPError(url, healthz, "unavailable", {}, None)
            return self.ok_response(200)

        return mock.Mock(side_effect=_open), probed

    def test_lan_webui_without_a_token_is_probed(self):
        # #708: no longer blocked (#440); it serves /healthz in setup mode,
        # so the row probes it like any running Web UI.
        module = load_docker_healthcheck()
        urlopen, probed = self.urlopen_for(200)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WEBUI_ACCESS_TOKEN", None)
            with mock.patch.object(module, "webui_config",
                                   return_value=(True, "0.0.0.0", 8088, "WEBUI_ACCESS_TOKEN", True)):
                code, out = self.run_main(module, urlopen)
        self.assertEqual(code, 0)
        self.assertIn("ok  webui: serving on 0.0.0.0:8088 (HTTP 200)", out)
        self.assertEqual(probed, ["http://127.0.0.1:8088/healthz"])

    def test_serving_webui_is_ok_on_200_and_503(self):
        # 503 from /healthz is the bot heartbeat's verdict, which the bot
        # row owns; for the Web UI row both prove the server is serving.
        for status in (200, 503):
            with self.subTest(status=status):
                module = load_docker_healthcheck()
                urlopen, probed = self.urlopen_for(status)
                with mock.patch.dict(os.environ, {"WEBUI_ACCESS_TOKEN": "tok"}), \
                        mock.patch.object(module, "webui_config",
                                          return_value=(True, "0.0.0.0", 8080, "WEBUI_ACCESS_TOKEN", True)):
                    code, out = self.run_main(module, urlopen)
                self.assertEqual(code, 0)
                self.assertIn(f"ok  webui: serving on 0.0.0.0:8080 (HTTP {status})", out)
                # A wildcard bind is probed through loopback, never the wildcard itself.
                self.assertEqual(probed, ["http://127.0.0.1:8080/healthz"])

    def test_unreachable_webui_fails(self):
        module = load_docker_healthcheck()
        urlopen, _probed = self.urlopen_for(urllib.error.URLError("refused"))
        with mock.patch.object(module, "webui_config",
                               return_value=(True, "127.0.0.1", 8080, "WEBUI_ACCESS_TOKEN", True)):
            code, out = self.run_main(module, urlopen)
        self.assertEqual(code, 1)
        self.assertIn("FAIL  webui:", out)

    def test_unresolved_webui_configuration_fails(self):
        module = load_docker_healthcheck()
        with mock.patch.object(module, "webui_config",
                               side_effect=settings.SettingsError("bad webui")):
            code, out = self.run_main(module, mock.Mock(return_value=self.ok_response()))
        self.assertEqual(code, 1)
        self.assertIn("FAIL  webui: configuration unresolved", out)


if __name__ == "__main__":
    unittest.main()
