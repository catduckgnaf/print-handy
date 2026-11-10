"""First-boot parity suite (plan section 3): the env and Web UI surfaces.

Catduck's testing requirement for the first-boot / walkthrough parity
program: automated tests proving parity across five configuration
surfaces — Docker variables (env), Web UI, Discord, Matrix, Telegram —
for the access/setup behaviors. This module covers the env and Web UI
drivers plus the shared access-state assertions; the three chat drivers
live in tests/bot/test_setup_chat_parity.py.

Parity is about facts and outcomes, not bytes (the grain of
tests/bot/test_transport_render_parity.py): the same canonical answers
driven through any surface must yield the same effective config on the
parity key set and the same access outcome from access.resolve().

Scenario names are the plan's: S1 first_boot_open_everywhere,
S2 claim_password_via_each_surface, S3 leave_open_via_each_surface,
S3b clear_claim_parity (Q1: chat may also clear a claim),
S4 env_token_precedence, S5 full_walkthrough_identical_config,
S7 cross_surface_completion. (S6 and S8 are chat-side; see the bot
module. S9 is asserted there and, for the Web UI store, here.)

Rulings pinned here: Q2 leave-open is KEPT as an explicit saved record;
Q4 env-token precedence stays exactly as today; Q6 the bare-metal
[webui] enabled default flips to true (PR-1).

Every lane this suite once probed for has landed (#1157): the scenarios
assert the shipped behavior directly — what resolve() returns, what the
defaults are — so a regression fails instead of skipping.
"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode

from print_handy import settings
from print_handy.webui import access, pages, server, walkthrough
from print_handy.webui import config as config_api

FORM = {"Content-Type": "application/x-www-form-urlencoded"}
HTML = {"Accept": "text/html"}

# The canonical scenario script, expressed once as data (plan section 3).
# The chat module imports the same constants so every surface is driven
# with the same answers.
CANONICAL = {
    "model": "flashforge.ad5x",
    "moonraker_url": "http://printer.local:7125",
    "spoolman_url": "http://spoolman.local:7912",
    "camera_snapshot_url": "http://printer.local:8080/snapshot",
    "chat_id": "100000000000000001",
    "user_id": "200000000000000002",
    "username": "operator",
    "password": "correct-horse-battery-staple",
    "token": "docker-token-value-0123456789",
}

FAKE_SNAPSHOT = {
    "version": "0.test", "host": "testhost", "ok": True,
    "printer": {"model": "flashforge.ad5x", "name": "Flashforge AD5X",
                "vendor": "flashforge", "connection": "moonraker"},
    "bot": {"platform": "discord", "mode": "standalone", "ok": True,
            "reason": "connected", "heartbeat": {"connected": True}},
    "listener": {"required": True, "ok": True, "reason": "alive"},
    "spoolman": {"bundled": True, "url": "http://127.0.0.1:8000", "ok": True,
                 "detail": "HTTP 200"},
    "services": {"spoolman_url": "http://127.0.0.1:8000"},
    "doctor": [{"label": "settings", "status": "ok", "detail": "fine"}],
}

def make_data(tmp, extra="", environ=None):
    """A settings load rooted at a temp home, LAN bind, Web UI on."""
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/home\n"
                     "webui:\n  enabled: true\n  host: 0.0.0.0\n  port: 8088\n" + extra)
    environ = {"PRINT_HANDY_CONFIG": str(local), **(environ or {})}
    return settings.load(local=local, environ=environ), local, environ


def gate_reversed(data):
    """True once PR-1 lands: an unclaimed LAN install resolves OPEN."""
    return access.resolve(data, "0.0.0.0", environ={}).mode == access.OPEN


def saved_record(data):
    path = access.access_path(data)
    if not path.exists():
        return None
    return json.loads(path.read_text())


class WebUIDriver:
    """The existing in-process handler harness (as in
    tests/test_webui_walkthrough.py): a real server on an ephemeral port."""

    def __init__(self, test, tmp, extra="", environ=None):
        self.data, self.local, self.environ = make_data(tmp, extra, environ)
        self.httpd = server.make_server(self.data, environ=self.environ,
                                        host="0.0.0.0", port=0,
                                        snapshot_func=lambda: FAKE_SNAPSHOT)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        # addCleanup runs last-in-first-out: registering server_close
        # first makes shutdown() run first, so the serving loop stops
        # before its socket closes (the old order warned on Windows).
        test.addCleanup(self.httpd.server_close)
        test.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return (response.status, {k.lower(): v for k, v in response.getheaders()},
                    response.read().decode())
        finally:
            conn.close()

    def csrf(self):
        _, _, body = self.request("GET", "/walkthrough", headers=HTML)
        marker = f'name="{pages.CSRF_FIELD}" value="'
        start = body.index(marker) + len(marker)
        return body[start:body.index('"', start)]

    def post_step(self, step_id, **fields):
        pairs = [("step", step_id), ("action", "continue"),
                 (pages.CSRF_FIELD, self.csrf())]
        pairs += [(k, v) for k, v in fields.items()]
        return self.request("POST", "/walkthrough",
                            body=urlencode(pairs), headers=FORM)

    def claim_password(self):
        return self.post_step("login", **{
            pages.SETUP_USER_FIELD: CANONICAL["username"],
            pages.SETUP_PASSWORD_FIELD: CANONICAL["password"],
            pages.SETUP_CONFIRM_FIELD: CANONICAL["password"]})

    def leave_open(self):
        return self.post_step("login", **{pages.SETUP_OPEN_FIELD: "yes"})


# --------------------------------------------------------------------- S1

class S1FirstBootOpenEverywhere(unittest.TestCase):
    def test_s1_env_surface_no_token_resolves_open_unclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, _, _ = make_data(tmp)
            resolved = access.resolve(data, "0.0.0.0", environ={})
            self.assertEqual(resolved.mode, access.OPEN)
            self.assertIsNone(saved_record(data))  # open because unclaimed

    def test_s1_baremetal_code_default_enables_the_webui(self):
        """Q6: no shipped config at all — the code default itself is on."""
        data = settings.load(local=False, environ={})
        self.assertTrue(data["webui"]["enabled"])

    def test_s1_docker_shipped_config_surface_is_on(self):
        """The env surface as shipped: config/docker.yaml through the real
        loader already serves the Web UI on 8088 (true today, stays true)."""
        root = settings.ROOT
        data = settings.load(local=root / "config" / "docker.yaml", environ={})
        self.assertTrue(data["webui"]["enabled"])
        self.assertEqual(data["webui"]["port"], 8088)

    def test_s1_webui_first_boot_serves_the_walkthrough_with_no_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = WebUIDriver(self, tmp)
            self.assertTrue(gate_reversed(driver.data))
            code, headers, _ = driver.request("GET", "/", headers=HTML)
            self.assertNotEqual(headers.get("location"), "/setup")
            code, _, body = driver.request("GET", "/walkthrough", headers=HTML)
            self.assertEqual(code, 200)
            self.assertIn("Printer", body)


# --------------------------------------------------------------------- S2

class S2ClaimPassword(unittest.TestCase):
    def test_s2_store_contract_the_writers_all_surfaces_share(self):
        """Every surface claims through access.save_password/save_open;
        the stored record and resolve() outcome are surface-independent."""
        with tempfile.TemporaryDirectory() as tmp:
            data, _, _ = make_data(tmp)
            access.save_password(data, CANONICAL["password"],
                                 CANONICAL["password"],
                                 username=CANONICAL["username"])
            record = saved_record(data)
            self.assertEqual(record["mode"], access.PASSWORD)
            self.assertEqual(record["username"], CANONICAL["username"])
            self.assertNotIn(CANONICAL["password"], json.dumps(record))
            resolved = access.resolve(data, "0.0.0.0", environ={})
            self.assertEqual(resolved.mode, access.PASSWORD)
            self.assertTrue(resolved.matches_login(
                CANONICAL["username"], CANONICAL["password"]))
            self.assertFalse(resolved.matches_login(
                CANONICAL["username"], "wrong-password-1"))

    def test_s2_claim_password_via_webui(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = WebUIDriver(self, tmp)
            self.assertTrue(gate_reversed(driver.data))
            code, _, _ = driver.claim_password()
            self.assertIn(code, (200, 303))
            resolved = access.resolve(driver.data, "0.0.0.0", environ={})
            self.assertEqual(resolved.mode, access.PASSWORD)
            self.assertTrue(resolved.matches_login(
                CANONICAL["username"], CANONICAL["password"]))
            self.assertFalse(resolved.matches_login(
                CANONICAL["username"], "wrong-password-1"))


# --------------------------------------------------------------------- S3

class S3LeaveOpen(unittest.TestCase):
    def test_s3_leave_open_is_an_explicit_saved_record(self):
        """Q2 ruled: leave-open is kept as a saved record, not the mere
        absence of a claim — the record is what dismisses the nudge and
        what every other surface reports."""
        with tempfile.TemporaryDirectory() as tmp:
            data, _, _ = make_data(tmp)
            access.save_open(data, True)
            self.assertEqual(saved_record(data), {"mode": access.OPEN})
            resolved = access.resolve(data, "0.0.0.0", environ={})
            self.assertEqual(resolved.mode, access.OPEN)

    def test_s3_leave_open_via_webui(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = WebUIDriver(self, tmp)
            self.assertTrue(gate_reversed(driver.data))
            code, _, _ = driver.leave_open()
            self.assertIn(code, (200, 303))
            self.assertEqual(saved_record(driver.data), {"mode": access.OPEN})
            resolved = access.resolve(driver.data, "0.0.0.0", environ={})
            self.assertEqual(resolved.mode, access.OPEN)


class S3bClearClaim(unittest.TestCase):
    def test_s3b_clearing_a_claim_returns_to_the_unclaimed_state(self):
        """Q1: clearing is a first-class act. After a clear the stored
        state is indistinguishable from never-claimed, and an env token
        that was set all along still applies (Q4)."""
        with tempfile.TemporaryDirectory() as tmp:
            data, _, _ = make_data(tmp)
            access.save_password(data, CANONICAL["password"],
                                 CANONICAL["password"],
                                 username=CANONICAL["username"])
            # The reset every surface's clear route ends at: the saved
            # record is gone (deleting the file is the documented reset).
            access.access_path(data).unlink()
            self.assertIsNone(saved_record(data))
            resolved = access.resolve(data, "0.0.0.0", environ={})
            self.assertNotEqual(resolved.mode, access.PASSWORD)
            with_token = access.resolve(
                data, "0.0.0.0",
                environ={"WEBUI_ACCESS_TOKEN": CANONICAL["token"]})
            self.assertEqual(with_token.mode, access.TOKEN)
            self.assertTrue(with_token.matches(CANONICAL["token"]))


# --------------------------------------------------------------------- S4

class S4EnvTokenPrecedence(unittest.TestCase):
    """Q4 ruled: today's precedence is the final behavior. These pass
    against current main and must keep passing through the program."""

    def test_s4_saved_open_never_overrides_the_env_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, _, _ = make_data(tmp)
            access.save_open(data, True)
            resolved = access.resolve(
                data, "0.0.0.0",
                environ={"WEBUI_ACCESS_TOKEN": CANONICAL["token"]})
            self.assertEqual(resolved.mode, access.TOKEN)

    def test_s4_saved_password_coexists_with_the_env_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, _, _ = make_data(tmp)
            access.save_password(data, CANONICAL["password"],
                                 CANONICAL["password"],
                                 username=CANONICAL["username"])
            resolved = access.resolve(
                data, "0.0.0.0",
                environ={"WEBUI_ACCESS_TOKEN": CANONICAL["token"]})
            self.assertEqual(resolved.mode, access.PASSWORD)
            self.assertTrue(resolved.matches(CANONICAL["token"]))
            self.assertTrue(resolved.matches_login(
                CANONICAL["username"], CANONICAL["password"]))

    def test_s4_token_set_after_an_open_choice_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, _, _ = make_data(tmp)
            access.save_open(data, True)
            before = access.resolve(data, "0.0.0.0", environ={})
            self.assertIn(before.mode, (access.OPEN, access.SETUP))
            after = access.resolve(
                data, "0.0.0.0",
                environ={"WEBUI_ACCESS_TOKEN": CANONICAL["token"]})
            self.assertEqual(after.mode, access.TOKEN)
            self.assertFalse(after.matches("not-the-token"))

    def test_s4_require_auth_false_ignores_the_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, _, _ = make_data(tmp, extra="  require_auth: false\n")
            resolved = access.resolve(
                data, "0.0.0.0",
                environ={"WEBUI_ACCESS_TOKEN": CANONICAL["token"]})
            self.assertEqual(resolved.mode, access.OPEN)


# --------------------------------------------------------------------- S5

# The parity key set (plan section 3, S5): dotted key -> canonical value
# for the Discord-flavored canonical run.
PARITY_KEYS = {
    "printer.model": CANONICAL["model"],
    "services.moonraker_url": CANONICAL["moonraker_url"],
    "services.spoolman_url": CANONICAL["spoolman_url"],
    "services.camera_snapshot_url": CANONICAL["camera_snapshot_url"],
    "bot.platform": "discord",
    "discord.chat_id": CANONICAL["chat_id"],
    "discord.allowed_user_ids": CANONICAL["user_id"],
}


def parity_view(data):
    """The effective config reduced to the parity key set, normalized
    (ids as strings, URLs without a trailing slash)."""
    view = {}
    for dotted in PARITY_KEYS:
        section, key = dotted.split(".", 1)
        value = (data.get(section) or {}).get(key)
        if isinstance(value, (list, tuple)):
            value = ",".join(str(part) for part in value)
        if value is not None:
            value = str(value).rstrip("/")
        view[dotted] = value
    return view


class S5IdenticalConfig(unittest.TestCase):
    def test_s5_env_surface_reference_state(self):
        """The env surface plays the canonical scenario as a hand-written
        config + env: this is the reference state every other surface's
        result is compared against."""
        with tempfile.TemporaryDirectory() as tmp:
            extra = ("printer:\n  model: flashforge.ad5x\n"
                     "services:\n"
                     f"  moonraker_url: {CANONICAL['moonraker_url']}\n"
                     f"  spoolman_url: {CANONICAL['spoolman_url']}\n"
                     f"  camera_snapshot_url: {CANONICAL['camera_snapshot_url']}\n"
                     "bot:\n  platform: discord\n"
                     "discord:\n"
                     f"  chat_id: '{CANONICAL['chat_id']}'\n"
                     f"  allowed_user_ids: '{CANONICAL['user_id']}'\n")
            data, _, _ = make_data(tmp, extra=extra)
            self.assertEqual(parity_view(data),
                             {k: v.rstrip("/") for k, v in PARITY_KEYS.items()})

    def test_s5_step_sets_match_across_webui_and_chat_definitions(self):
        """Same steps reachable (plan claim 1): the canonical topics —
        printer, chat platform, camera, spoolman, login/access, review —
        must exist in both flow definitions."""
        from print_handy.bot import guided
        webui_steps = {step_id for step_id, _ in walkthrough.STEPS}
        self.assertEqual(webui_steps,
                         {"printer", "chat", "camera", "login",
                          "providers", "review"})
        webui_fields = {f for fields in walkthrough.STEP_FIELDS.values()
                        for f in fields}
        self.assertIn("services.spoolman_url", webui_fields)
        chat_keys = {step.key for step in guided.steps()}
        # The canonical topics as the landed step keys name them:
        # platform choice, camera, and the Web UI login step.
        missing = {"platform", "camera_snapshot_url", "webui_login"} - chat_keys
        self.assertEqual(missing, set())

    def test_s5_full_walkthrough_via_webui_yields_the_reference_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = WebUIDriver(self, tmp)
            webui_fields = {f for fields in walkthrough.STEP_FIELDS.values()
                            for f in fields}
            self.assertIn("services.spoolman_url", webui_fields)
            self.assertTrue(gate_reversed(driver.data))
            # Drive every field step with the canonical answers, using
            # the same form shape the existing walkthrough tests use.
            by_id = {item.id: item for item in
                     config_api.read_fields(environ=driver.environ)}
            for step_id, _label in walkthrough.STEPS:
                fields = {}
                for field_id in walkthrough.STEP_FIELDS[step_id]:
                    item = by_id[field_id]
                    if item.overridden:
                        continue
                    value = item.value
                    fields[field_id] = "" if value is None else str(value)
                overrides = {
                    "printer.model": CANONICAL["model"],
                    "services.moonraker_url": CANONICAL["moonraker_url"],
                    "services.spoolman_url": CANONICAL["spoolman_url"],
                    "services.camera_snapshot_url":
                        CANONICAL["camera_snapshot_url"],
                    "bot.platform": "discord",
                    "discord.chat_id": CANONICAL["chat_id"],
                    "discord.allowed_user_ids": CANONICAL["user_id"],
                }
                fields.update({k: v for k, v in overrides.items()
                               if k in fields})
                code, _, _ = driver.post_step(step_id, **fields)
                self.assertIn(code, (200, 303), f"step {step_id}")
            reloaded = settings.load(local=driver.local,
                                     environ=driver.environ)
            self.assertEqual(parity_view(reloaded),
                             {k: v.rstrip("/") for k, v in PARITY_KEYS.items()})


# --------------------------------------------------------------------- S7

class S7CrossSurfaceCompletion(unittest.TestCase):
    def test_s7_completion_record_is_shared(self):
        """Finishing setup anywhere ends the offers everywhere (PR-4):
        the shared record lives in print_handy.setup_state."""
        from print_handy import setup_state  # noqa: F401
        with tempfile.TemporaryDirectory() as tmp:
            data, _, _ = make_data(tmp)
            # Web UI completion is visible to the shared record, and the
            # walkthrough no longer offers itself afterwards.
            self.assertTrue(walkthrough.pending(data))
            setup_state.record_completion(data, surface="webui")
            self.assertFalse(walkthrough.pending(data))


if __name__ == "__main__":
    unittest.main()
