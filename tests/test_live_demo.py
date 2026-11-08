"""Issue #1031: the static live demo must stay simulated, honest and complete.

Offline contract tests for demo/: the scenario data drives every
journey to a terminal state, every button points at a real step,
the honesty banner is present, and nothing in the demo can reach a
real printer or a credential.
"""
import json
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"

BANNER = "Demo — simulated printer, nothing is printed, sample data only."
FORBIDDEN_CONNECTION_WORDS = (
    "moonraker", "octoprint", "websocket", "wss:", "klipper",
    "discord.com", "api_key", "bot_token", "access_token",
)
TOKEN_LIKE = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}")


def load_scenarios():
    return json.loads((DEMO / "scenarios.json").read_text())


class LiveDemoScenarios(unittest.TestCase):
    def test_scenario_file_is_well_formed(self):
        data = load_scenarios()
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["honesty_banner"], BANNER)
        for required in ("happy", "too_big", "spools", "fallback"):
            self.assertIn(required, data["journeys"])
        self.assertTrue(data["samples"])
        self.assertTrue(data["commands"])

    def test_every_journey_reaches_a_terminal_state(self):
        data = load_scenarios()
        for journey_id, journey in data["journeys"].items():
            with self.subTest(journey=journey_id):
                self.assertTrue(journey["steps"])
                terminals = [s for s in journey["steps"] if s.get("kind") == "terminal"]
                self.assertTrue(terminals, f"{journey_id} has no terminal step")
                for step in terminals:
                    self.assertIn(step.get("terminal_state"), ("completed", "cancelled"))

    def test_every_button_and_intent_reference_exists(self):
        data = load_scenarios()
        for journey_id, journey in data["journeys"].items():
            step_ids = {s["id"] for s in journey["steps"]}
            for step in journey["steps"]:
                for button in step.get("buttons") or []:
                    with self.subTest(journey=journey_id, button=button.get("id")):
                        self.assertIn(button["next"], step_ids)
                if step.get("next"):
                    self.assertIn(step["next"], step_ids)
        for intent in data["intents"]:
            self.assertIn(intent["journey"], data["journeys"])
        for sample in data["samples"]:
            self.assertIn(sample["journey"], data["journeys"])

    def test_happy_path_mentions_the_starter_at_the_terminal_post(self):
        data = load_scenarios()
        terminals = [s for s in data["journeys"]["happy"]["steps"] if s.get("kind") == "terminal"]
        self.assertTrue(any(s.get("mention_starter") for s in terminals))
        confirm = [s for s in data["journeys"]["happy"]["steps"] if s.get("kind") == "confirm"]
        self.assertTrue(confirm, "happy path must keep the confirm-before-print card")

    def test_fallback_journey_shows_the_command_list(self):
        data = load_scenarios()
        steps = data["journeys"]["fallback"]["steps"]
        self.assertTrue(any(s.get("show_commands") for s in steps))


class LiveDemoPage(unittest.TestCase):
    def test_page_contains_the_honesty_banner_and_reset(self):
        html = (DEMO / "index.html").read_text()
        self.assertIn(BANNER, html)
        self.assertIn('id="reset"', html)
        self.assertIn("print-handy-demo", html)

    def test_no_external_urls_or_token_like_strings(self):
        for path in sorted(DEMO.iterdir()):
            if path.is_file():
                text = path.read_text()
                with self.subTest(file=path.name):
                    self.assertNotIn("http://", text)
                    self.assertNotIn("https://", text)
                    self.assertIsNone(TOKEN_LIKE.search(text))

    def test_no_reference_to_a_real_printer_connection(self):
        for path in sorted(DEMO.iterdir()):
            if path.is_file():
                lowered = path.read_text().lower()
                for word in FORBIDDEN_CONNECTION_WORDS:
                    with self.subTest(file=path.name, word=word):
                        self.assertNotIn(word, lowered)

    def test_demo_files_exist_with_no_build_step(self):
        for name in ("index.html", "styles.css", "app.js", "scenarios.json"):
            self.assertTrue((DEMO / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
