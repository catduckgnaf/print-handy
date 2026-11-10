"""Issue #1031: the static live demo must stay simulated, honest and complete.

Offline contract tests for demo/: the scenario data drives every
journey to a terminal state, every button points at a real step,
the honesty banner is present, and nothing in the demo can reach a
real printer or a credential.

The LiveDemoProductionContract class is the drift guard: it compares
the scenario data against the production bot's own command table,
unmatched reply and help heading, and pins the production wording of
the confirmation and fit-refusal flows, so a wording change in the
real bot fails here instead of letting the demo drift silently.
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
        for required in ("happy", "too_big", "spools", "fallback", "help"):
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

    def test_help_journey_shows_the_command_list(self):
        data = load_scenarios()
        steps = data["journeys"]["help"]["steps"]
        self.assertTrue(any(s.get("show_commands") for s in steps))

    def test_fallback_journey_does_not_show_the_command_list(self):
        # #467: the command list is the answer to /print-help (the help
        # journey above), never the reply to unmatched free text.
        data = load_scenarios()
        steps = data["journeys"]["fallback"]["steps"]
        self.assertFalse(any(s.get("show_commands") for s in steps))


class LiveDemoProductionContract(unittest.TestCase):
    """Scenario wording pinned against the production bot (#1031 drift guard)."""

    def test_demo_commands_match_the_production_help_list(self):
        # Production help_text() lists every command except print-help
        # itself, in table order, with the table's usage and description.
        from print_handy.bot import commands

        expected = [
            {"name": c.name, "usage": c.usage, "description": c.description}
            for c in commands.COMMANDS
            if c.name != "print-help"
        ]
        self.assertEqual(load_scenarios()["commands"], expected)

    def test_unmatched_reply_matches_production(self):
        from print_handy.bot.runtime import unmatched_text

        steps = load_scenarios()["journeys"]["fallback"]["steps"]
        answer = next(s for s in steps if s["id"] == "fallback-answer")
        self.assertEqual(answer["text"], unmatched_text())

    def test_help_heading_matches_production_help_text(self):
        from print_handy.bot import commands

        steps = load_scenarios()["journeys"]["help"]["steps"]
        answer = next(s for s in steps if s.get("show_commands"))
        self.assertEqual(answer["text"], commands.help_text().splitlines()[0])

    def test_help_intent_routes_to_the_help_journey(self):
        data = load_scenarios()
        intent = next(i for i in data["intents"] if i["id"] == "intent-help")
        self.assertEqual(intent["journey"], "help")

    def test_confirm_buttons_match_production_labels(self):
        # prepared_intake's ready question: print_label is
        # "Slice and print now (bed is clear)" and cancel_label is
        # "Cancel print request"; its question opens "Ready to slice".
        steps = load_scenarios()["journeys"]["happy"]["steps"]
        confirm = next(s for s in steps if s.get("kind") == "confirm")
        self.assertTrue(confirm["text"].startswith("Ready to slice"))
        self.assertEqual(
            [b["label"] for b in confirm["buttons"]],
            ["Slice and print now (bed is clear)", "Cancel print request"],
        )

    def test_too_big_refusal_matches_production_fit_wording(self):
        # prepared_intake.fit_question opens "<part> (<extent>) does not
        # fit the bed full size as the author laid it out." and offers a
        # "Shrink option: ... fits at <pct> max." line; fit_choice_rows
        # labels the shrink button "Shrink to <pct>" and appends "Cancel".
        # The must-shrink corpus case pins the part at 260 x 260 x 230 mm
        # and the fitting scale at 83.8%.
        steps = load_scenarios()["journeys"]["too_big"]["steps"]
        refusal = next(s for s in steps if s["id"] == "big-refusal")
        self.assertTrue(
            refusal["text"].startswith(
                "Big plate (260 × 260 × 230 mm) does not fit the bed "
                "full size as the author laid it out."
            )
        )
        self.assertIn("Shrink option: as authored it fits at 83.8% max.", refusal["text"])
        self.assertEqual(
            [b["label"] for b in refusal["buttons"]],
            ["Shrink to 83.8%", "Cancel"],
        )


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
