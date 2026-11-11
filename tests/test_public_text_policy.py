"""Offline coverage for the public-text hygiene check (issues and PRs)."""
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_public_text.py"


def load():
    spec = importlib.util.spec_from_file_location("public_text", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicTextTests(unittest.TestCase):
    def setUp(self):
        self.pt = load()

    def cats(self, text, terms=()):
        return {category for _n, category in self.pt.scan(text, list(terms))}

    def test_flags_private_places(self):
        self.assertIn("home directory path", self.cats("see /home/someone/work/log.txt"))
        self.assertIn("NAS or Unraid path", self.cats("state in /mnt/user/appdata/x"))
        self.assertIn("root home path", self.cats("cat /root/.ssh/config"))
        self.assertIn("private IP address", self.cats("printer at 192.168.45.50"))
        self.assertIn("private IP address", self.cats("host 10.0.0.7 and 172.20.1.1"))
        self.assertIn("local hostname", self.cats("reach nas.home.arpa or box.lan"))
        self.assertIn("MAC address", self.cats("mac 88:a9:a7:9e:15:91"))
        self.assertIn("email address", self.cats("mail me at a.b@example.org"))
        self.assertIn("SSH or copy target", self.cats("ran ssh root@box and it worked"))
        self.assertIn("chat or account ID", self.cats("channel 1546517941535776829"))
        self.assertIn("token", self.cats("ghp_" + "a" * 30))

    def test_allows_public_and_documentation_text(self):
        for text in ("see scripts/check_public_text.py and /data/cache/timelapse",
                     "documentation range 203.0.113.9 and 192.0.2.1",
                     "public resolver 8.8.8.8", "listens on 127.0.0.1:8088",
                     "commit 4f98d63 and run 41452866482",
                     "version 1.7.5-2 on /usr/data/gcodes"):
            self.assertEqual(set(), self.cats(text), text)

    def test_operator_names_and_waiver(self):
        terms = self.pt.denylist("Uranium, my-nas")
        self.assertEqual({"operator machine name"}, self.cats("docker host Uranium", terms))
        self.assertEqual({"operator machine name"}, self.cats("on my-nas today", terms))
        self.assertEqual(set(), self.cats("uranium-ore mining", [t for t in terms if False]))
        self.assertEqual(set(), self.cats("example 192.168.1.5 public-text-ok", terms))
        self.assertEqual(set(), self.cats("Uraniumish", terms))

    def test_output_never_echoes_values(self):
        secret = "192.168.45.50"
        errors = self.pt.check({"title": "t", "body": f"x\nprinter {secret}\n"}, [])
        self.assertEqual(["body line 2: private IP address"], errors)
        self.assertNotIn(secret, " ".join(errors))

    def test_extra_texts_are_checked(self):
        errors = self.pt.check({"title": "", "body": "", "extra": ["fine", "path /home/x/y"]}, [])
        self.assertEqual(["text 2 line 1: home directory path"], errors)

    def test_no_reply_trailers_pass_and_real_addresses_still_fail(self):
        for line in ("Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
                     "Co-authored-by: catduckgnaf <catduckgnaf@users.noreply.github.com>",
                     "Author: CatDuck <129601174+catduckgnaf@users.noreply.github.com>"):
            self.assertEqual(set(), self.cats(line), line)
        for line in ("Co-Authored-By: Someone <someone@example.org>",
                     "noreply@anthropic.com and reach me at me@example.org",
                     "noreply@anthropic.com.evil.example",
                     "x@users.noreply.github.com.example.org"):
            self.assertIn("email address", self.cats(line), line)


if __name__ == "__main__":
    unittest.main()
