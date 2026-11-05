"""Failed X-WebUI-Token guesses back off like failed logins, and weak
passwords are refused at setup. Offline: temp settings, ephemeral loopback port.
"""
import tempfile
import unittest
from pathlib import Path

from print_handy import settings
from print_handy.webui import access, server
from tests import test_webui_login as base
TOKEN, WRONG = base.TOKEN, base.WRONG
MAX = access.LoginThrottle().max_failures


class HeaderThrottleTests(unittest.TestCase):
    start = base.LoginTests.start
    request = base.LoginTests.request

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.httpd, self.port, _local, _env = self.start(
            self.tmp, environ={"WEBUI_ACCESS_TOKEN": TOKEN})

    def get(self, token=None):
        headers = {server.TOKEN_HEADER: token} if token is not None else {}
        return self.request(self.port, "GET", "/api/status", headers=headers)

    def test_wrong_tokens_lock_the_address_out_even_for_the_right_token(self):
        for _ in range(MAX - 1):
            self.assertEqual(self.get(WRONG)[0], 401)
        self.assertEqual(self.get(WRONG)[0], 429)  # the guess that trips it
        code, headers, body = self.get(TOKEN)
        self.assertEqual(code, 429)
        self.assertTrue(int(headers["retry-after"]) > 0)
        self.assertIn("too many", body)

    def test_a_missing_header_is_not_a_guess(self):
        for _ in range(MAX + 2):
            self.assertEqual(self.get()[0], 401)
        self.assertEqual(self.get(TOKEN)[0], 200)

    def test_success_clears_the_counter(self):
        for _ in range(MAX - 1):
            self.get(WRONG)
        self.assertEqual(self.get(TOKEN)[0], 200)
        for _ in range(MAX - 1):
            self.assertEqual(self.get(WRONG)[0], 401)
        self.assertEqual(self.get(TOKEN)[0], 200)


class PasswordGuidanceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        local = Path(tmp) / "local.yaml"
        local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n")
        self.data = settings.load(local=local, environ={})

    def refused(self, password, username=None):
        with self.assertRaises(access.AccessError) as ctx:
            access.save_password(self.data, password, password, username=username)
        self.assertFalse(access.access_path(self.data).exists())
        return str(ctx.exception)

    def test_common_passwords_are_refused_with_guidance(self):
        self.assertIn("12 or more", self.refused("Password123"))
        self.refused("12345678")

    def test_username_and_repeats_are_refused(self):
        self.assertIn("username", self.refused("catduckx", "catduckx"))
        self.refused("aaaaaaaaaa")

    def test_a_passphrase_is_accepted(self):
        access.save_password(self.data, "correct horse battery", "correct horse battery",
                             username="catduck")
        self.assertTrue(access.access_path(self.data).exists())


class ThrottleTableBoundTests(unittest.TestCase):
    """The shared throttle's tables stay capped no matter how many
    distinct addresses spray failures (Codex review on this PR): with a
    fixed clock, lockouts never expire, so only the entry cap can bound
    the lockout table."""

    def test_lockout_and_failure_tables_stay_bounded(self):
        throttle = access.LoginThrottle(max_entries=8, clock=lambda: 1000.0)
        for i in range(64):
            address = f"2001:db8::{i:x}"
            for _ in range(throttle.max_failures):
                throttle.record_failure(address)
            self.assertLessEqual(len(throttle._locked), 8)
            self.assertLessEqual(len(throttle._failures), 8)
        # The most recent attackers are the ones still locked out.
        self.assertEqual(throttle.retry_after("2001:db8::3f"), throttle.lockout_s + 1)


if __name__ == "__main__":
    unittest.main()
