"""Offline coverage for Moonraker listener safety and filament insertion prompts."""
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock
from print_handy.runtime import moonraker_event_listener as mel




class NoMeshLineTests(unittest.TestCase):
    def test_blank_profile_is_flagged(self):
        self.assertTrue(mel.no_mesh_line("info Printing with bed mesh: "))
        self.assertTrue(mel.no_mesh_line("// Printing with bed mesh:"))

    def test_named_profile_and_other_lines_are_not_flagged(self):
        self.assertFalse(mel.no_mesh_line("info Printing with bed mesh: auto"))
        self.assertFalse(mel.no_mesh_line("info Global Z-Offset load: ignored (working with screen)"))
        self.assertFalse(mel.no_mesh_line(None))

    def test_alert_is_plain_and_actionable(self):
        self.assertIn("NO bed mesh", mel.NO_MESH_ALERT)
        self.assertIn("full bed leveling", mel.NO_MESH_ALERT)


class FilamentInsertionMonitorTests(unittest.TestCase):
    def make_monitor(self, directory):
        root = Path(directory)
        return mel.FilamentInsertionMonitor(root / "edges.json", root / "pending.json")

    def test_baseline_stable_transition_persisted_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self.make_monitor(tmp)
            self.assertEqual(monitor.observe({1: False, 2: True}, idle=True), [])
            self.assertEqual(monitor.observe({1: False, 2: True}, idle=True), [])
            self.assertEqual(monitor.observe({1: True, 2: True}, idle=True), [])
            self.assertEqual(monitor.observe({1: True, 2: True}, idle=True), [1])
            self.assertEqual(monitor.observe({1: True, 2: True}, idle=True), [])
            self.assertEqual(json.loads((Path(tmp) / "edges.json").read_text())["1"], True)
            self.assertEqual(json.loads((Path(tmp) / "pending.json").read_text())["1"]["status"], "awaiting_user_dispatch")

    def test_reconnect_and_busy_states_rebaseline_without_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self.make_monitor(tmp)
            self.assertEqual(monitor.observe({1: False}, idle=True), [])
            self.assertEqual(monitor.observe({1: True}, idle=True), [])
            self.assertEqual(monitor.observe({1: True}, idle=False), [])
            self.assertEqual(monitor.observe({1: True}, idle=True), [])
            self.assertEqual(monitor.observe({1: True}, idle=True), [])

    def test_quick_pull_reinsert_prompts_without_a_stable_absent_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = self.make_monitor(tmp)
            self.assertEqual(monitor.observe({1: True}, idle=True), [])
            self.assertEqual(monitor.observe({1: False}, idle=True), [])
            self.assertEqual(monitor.observe({1: True}, idle=True), [1])
            self.assertEqual(monitor.observe({1: True}, idle=True), [])

    def test_prompt_has_explicit_fields_and_no_guess(self):
        prompt = mel.insertion_prompt(3)
        self.assertIn("slot 3", prompt)
        self.assertIn("/spool change 3", prompt)
        self.assertIn("same", prompt)
        self.assertIn("Nothing has been changed yet", prompt)

    def test_button_prompt_has_no_typed_spool_command(self):
        prompt = mel.insertion_confirmation_text(3)
        self.assertIn("IFS slot 3", prompt)
        self.assertIn("Same spool", prompt)
        self.assertNotIn("/spool", prompt)

    def test_manual_resume_prompt_orders_existing_or_new_and_protects_inventory(self):
        prompt = mel.manual_resume_tracking_message(
            "job.gcode", slot=1, compatible_spools=[{"id": 23, "remaining_weight": 412,
                                                     "name": "Matte White", "material": "PLA+"}]
        )
        self.assertIn("job, IFS slot 1", prompt)
        self.assertIn("A) existing Spoolman spool, or B) new spool", prompt)
        self.assertIn("Matte White · PLA+ · #23 · 412 g left", prompt)
        self.assertIn("will not select it automatically", prompt)
        self.assertIn("load it and resume manually at the printer", prompt)
        self.assertIn("clear label photo (preferred)", prompt)
        self.assertIn("optional brand", prompt)
        self.assertIn("color/finish", prompt)
        self.assertIn("full starting weight in grams", prompt)
        self.assertIn("distinct spool at that full starting weight", prompt)
        self.assertIn("mark the replaced exhausted spool depleted", prompt)
        self.assertIn("map this IFS slot", prompt)
        self.assertIn("Similar-color records remain untouched", prompt)
        self.assertIn("never start, resume, pause, heat, or move", prompt)

    def test_spool_match_treats_pla_and_pla_plus_as_compatible(self):
        self.assertTrue(mel.spool_matches_recovery("PLA", "PLA+", "Matte White", "Matte White"))
        self.assertFalse(mel.spool_matches_recovery("PLA", "PLA+", "White", "Matte White"))
        self.assertFalse(mel.spool_matches_recovery("PETG", "PLA+", "Matte White", "Matte White"))


class RunoutIntakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pause = Path(self.tmp.name) / "runout.json"
        self.hardware = Path(self.tmp.name) / "hardware.json"
        self.hardware.write_text(json.dumps({"active_slot": 1, "filament_slots": {
            "1": {"spoolman_id": 10, "material": "PLA", "color": "white"}}}))
        self.old_pause = mel.RUNOUT_PAUSE_STATE
        self.old_hardware = mel.HARDWARE_STATE
        mel.RUNOUT_PAUSE_STATE = self.pause
        mel.HARDWARE_STATE = self.hardware

    def tearDown(self):
        mel.RUNOUT_PAUSE_STATE = self.old_pause
        mel.HARDWARE_STATE = self.old_hardware
        self.tmp.cleanup()

    def test_only_a_runout_alert_opens_intake(self):
        self.assertEqual(mel.classify_pause("Filament runout on slot 1"), "runout")
        self.assertEqual(mel.classify_pause("paused", "layer 12"), "other")
        self.assertIsNone(mel.load_runout_pause())

    def test_existing_spool_binds_without_a_printer_command(self):
        mel.persist_runout_pause("job.gcode", 1, 10)
        calls = []

        def client(method, path, body):
            calls.append((method, path, body))
            if method == "GET":
                return {"id": 23, "archived": False, "filament": {"material": "PLA", "name": "Matte White"}}
            return {"id": 23}

        result = mel.apply_runout_choice(None, mel.load_runout_pause(), {"status": "existing", "spool_id": 23}, client=client)
        self.assertEqual(result["printer_commands"], [])
        self.assertIn(("PATCH", "/api/v1/spool/10", {"archived": True, "remaining_weight": 0, "location": "depleted"}), calls)
        self.assertEqual(result["spool_id"], 23)

    def test_new_spool_uses_the_stated_full_weight(self):
        calls = []

        def client(method, path, body):
            calls.append((method, path, body))
            return {"id": 4 if "filament" in path else 31}

        choice = mel.parse_runout_reply("Sunlu, PLA+, Matte White, 1000, LOT9")
        result = mel.apply_runout_choice(None, {"slot": 1, "prior_spool_id": 10}, choice, client=client, filaments=[])
        created = [body for method, path, body in calls if method == "POST" and path == "/api/v1/spool"][0]
        self.assertEqual(created["initial_weight"], 1000)
        self.assertEqual(created["remaining_weight"], 1000)
        self.assertEqual(result["printer_commands"], [])
        self.assertIn(("PATCH", "/api/v1/spool/10", {"archived": True, "remaining_weight": 0, "location": "depleted"}), calls)

    def test_text_reply_allows_grams_suffix_and_terminal_punctuation(self):
        choice = mel.parse_runout_reply("Sunlu, PLA+, Black, 1000 g. ")
        self.assertEqual(choice["status"], "new")
        self.assertEqual(choice["grams"], 1000)

    def test_failed_or_untracked_nag_never_posts_plain_text(self):
        tracked = {"slot": 3, "discord_message_id": "12345", "nags": 1}
        untracked = {"slot": 3, "nags": 1}
        with mock.patch("print_handy.runtime.print_alert_buttons.post_spool_question", return_value=None) as component, \
             mock.patch.object(mel, "post") as text_post, \
             mock.patch.object(mel, "log"):
            self.assertFalse(mel.post_spool_question(tracked, nag=True))
            self.assertFalse(mel.post_spool_question(untracked, nag=True))
        component.assert_called_once()
        self.assertEqual(component.call_args.args[-1], "12345")
        text_post.assert_not_called()

    def test_generic_pause_has_no_pending_record(self):
        self.assertIsNone(mel.load_runout_pause())
        self.assertEqual(mel.recovery_slot("job.gcode"), (1, 10))


if __name__ == "__main__":
    unittest.main()
