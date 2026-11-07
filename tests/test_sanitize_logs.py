"""#478: the export sanitizer — categories, pseudonyms, verification, report."""
import os
import unittest
from unittest import mock

from print_handy import sanitize_logs

SECRET = "hunter2secretvalue"
SEEDED = (
    f"connect http://192.168.1.50:7125/printer/info?api_key={SECRET} failed for user 123456789012345678\n"
    "retry http://ad5x.local:7125/websocket from 192.168.1.50, host printer.lan down\n"
    "user 123456789012345678 in channel 100000000000000001, mail ops@example.com\n"
    "state at /home/someone/.hermes/profiles/3d/state/debug.log, mac AA:BB:CC:DD:EE:FF\n"
    "serial: 01S09A1B2C3D4E5F, device ok token=abc123def456\n"
)


class SanitizerTests(unittest.TestCase):
    def sanitize(self, text=SEEDED, **kwargs):
        sanitizer = sanitize_logs.Sanitizer(**kwargs)
        return sanitizer, sanitizer.sanitize(text)

    def test_seeded_values_do_not_survive(self):
        _s, out = self.sanitize()
        with mock.patch.dict(os.environ, {"SOME_TOKEN": SECRET}):
            _s2, out2 = self.sanitize()
        for text in (out, out2):
            for bad in (SECRET, "192.168.1.50", "ad5x.local", "printer.lan",
                        "123456789012345678", "100000000000000001",
                        "/home/someone", "ops@example.com", "AA:BB:CC:DD:EE:FF",
                        "01S09A1B2C3D4E5F", "abc123def456"):
                self.assertNotIn(bad, text)
        self.assertEqual(sanitize_logs.find_residuals(out), {})

    def test_url_shape_survives_while_host_and_query_go(self):
        _s, out = self.sanitize()
        self.assertIn("http://ip-1:7125/printer/info?<redacted>", out)
        self.assertIn("http://host-1:7125/websocket", out)

    def test_pseudonyms_are_consistent_within_one_export(self):
        _s, out = self.sanitize()
        # The same user id appears twice and keeps one pseudonym.
        self.assertEqual(out.count("user-1"), 2)
        self.assertIn("channel-1", out)
        # The same IP in a URL and bare text keeps one pseudonym.
        self.assertGreaterEqual(out.count("ip-1"), 2)

    def test_known_ids_get_pseudonyms_even_without_a_pattern(self):
        _s, out = self.sanitize("operator 424242 pinged room 987654",
                                known_user_ids=["424242"], known_channel_ids=["987654"])
        self.assertIn("operator user-1 pinged room channel-1", out)
        self.assertNotIn("424242", out)
        self.assertNotIn("987654", out)

    def test_report_counts_match_what_was_removed(self):
        sanitizer, _out = self.sanitize(
            "a 10.0.0.1 b 10.0.0.2 c me@example.com d /tmp/x/y.log e 111111111111111111")
        self.assertEqual(sanitizer.counts["ip_addresses"], 2)
        self.assertEqual(sanitizer.counts["emails"], 1)
        self.assertEqual(sanitizer.counts["paths"], 1)
        self.assertEqual(sanitizer.counts["user_ids"], 1)
        report = sanitizer.report()
        self.assertIn("2 IP addresses", report)
        self.assertIn("1 email addresses", report)
        self.assertIn("Verification pass", report)
        self.assertNotIn("10.0.0.1", report)
        self.assertNotIn("me@example.com", report)

    def test_verification_pass_catches_what_the_first_pass_missed(self):
        # The contextual match in pass 1 registers the short user ID and
        # pseudonymizes that occurrence; the bare second occurrence is
        # below every generic pattern, so only the verification pass —
        # which re-runs with the registered pseudonym table — removes it.
        sanitizer, out = self.sanitize("user id 424242 did a thing; see ref 424242 for it")
        self.assertNotIn("424242", out)
        self.assertEqual(out.count("user-1"), 2)
        self.assertGreaterEqual(sanitizer.verification_counts["user_ids"], 1)
        self.assertIn("Verification pass removed", sanitizer.report())
        self.assertEqual(sanitize_logs.find_residuals(out), {})

    def test_proxied_url_inner_address_does_not_survive(self):
        _sanitizer, out = self.sanitize(
            "fetch http://proxy.local/fetch/http://192.168.1.9:7125/info?key=zzz now")
        self.assertNotIn("192.168.1.9", out)
        self.assertNotIn("proxy.local", out)
        self.assertEqual(sanitize_logs.find_residuals(out), {})

    def test_public_hosts_stay_readable_but_queries_go(self):
        _s, out = self.sanitize("see https://discord.com/channels/1?token=abc123456789")
        self.assertIn("https://discord.com/channels/1?<redacted>", out)

    def test_clean_text_is_unchanged_and_reports_nothing(self):
        sanitizer, out = self.sanitize("all quiet on the printer front")
        self.assertEqual(out, "all quiet on the printer front")
        self.assertEqual(sanitizer.report(), "Sanitization report: nothing needed removing.")

    def test_telegram_and_matrix_ids_become_pseudonyms(self):
        _s, out = self.sanitize("chat -1001234567890123 room !abc123:matrix.org user @op:matrix.org")
        self.assertNotIn("-1001234567890123", out)
        self.assertNotIn("!abc123:matrix.org", out)
        self.assertNotIn("@op:matrix.org", out)
        self.assertIn("channel-1", out)
        self.assertIn("user-1", out)


if __name__ == "__main__":
    unittest.main()


class MachineNameAndPathRootTests(unittest.TestCase):
    """#1020 C3: operator machine names and the app-path rule."""

    def sanitize(self, text, **kwargs):
        sanitizer = sanitize_logs.Sanitizer(**kwargs)
        return sanitizer, sanitizer.sanitize(text)

    def test_machine_names_become_consistent_pseudonyms(self):
        _s, out = self.sanitize(
            "connect to Deepthought failed; deepthought is the NAS, DEEPTHOUGHT hosts it",
            machine_names=["Deepthought"])
        self.assertNotIn("eepthought", out)
        self.assertEqual(3, out.count("machine-1"))

    def test_machine_names_are_counted_and_reported(self):
        sanitizer, _out = self.sanitize("nas-box down, nas-box unreachable",
                                        machine_names=["nas-box"])
        self.assertEqual(2, sanitizer.counts["machine_names"])
        self.assertIn("operator machine names", sanitizer.report())

    def test_machine_name_inside_a_kept_path_is_still_pseudonymized(self):
        _s, out = self.sanitize("log at /data/deepthought-state/bot.log",
                                machine_names=["deepthought"],
                                keep_path_roots=[("/data", "app-data")])
        self.assertEqual("log at app-data/machine-1-state/bot.log", out)

    def test_paths_inside_keep_roots_are_relabeled_outside_paths_go(self):
        _s, out = self.sanitize(
            "state /data/state/print-handy.log, secret /home/op/private.txt, "
            "install /app/print_handy/logs.py",
            keep_path_roots=[("/data", "app-data"), ("/app", "app-install")])
        self.assertIn("app-data/state/print-handy.log", out)
        self.assertIn("app-install/print_handy/logs.py", out)
        self.assertNotIn("/data/state/print-handy.log", out)
        self.assertNotIn("/home/op/private.txt", out)
        self.assertIn("<local path>", out)
        # The relabeled form leaves no residual for a fresh sanitizer.
        self.assertEqual(sanitize_logs.find_residuals(out), {})

    def test_no_keep_roots_keeps_the_strict_behavior(self):
        _s, out = self.sanitize("state /data/state/print-handy.log")
        self.assertNotIn("/data/state/print-handy.log", out)
