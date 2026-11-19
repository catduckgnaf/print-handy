"""What the Web UI settings catalog says and offers.

Three owner-facing promises are pinned here:

* No user-visible catalog text carries an issue or pull request number.
* The camera check defaults to the local failure detector, and the camera
  group no longer leads with an "AI" switch: the vision endpoint switch
  sits with the endpoint it gates, labeled by what it does.
* The slicing group offers the part gaps, prime tower, ZMod purge,
  Klipper macro and Flash Studio settings the slicer reads, each shown
  only for the printers that use it, and each one round-trips through
  the real settings loader.
"""
import copy
import re
import tempfile
import unittest
from pathlib import Path

from print_handy import printers, settings
from print_handy.printers.bambulab.profiles import ProfileError
from print_handy.runtime import local_vision, visual_watch
from print_handy.webui import (CATALOG, WebUIConfigError, config_page_visible, field_by_id,
                               write_changes)
from print_handy.webui import pages
from print_handy.webui.catalog import (FLASH_STUDIO_VENDORS, GROUP_FILE_ONLY_NOTES,
                                       START_MACRO_VENDORS, choices_for)

# "#1234", "(#12)", "issue 1490", "PR #493", "pull request 77". Hex colors
# never appear in catalog text, so a bare "#" followed by digits is a
# reference.
ISSUE_REFERENCE = re.compile(r"(?<![&\w])#\d+|\b(?:issues?|PRs?|pull requests?)\s*#?\d+", re.I)

NEW_SLICING_FIELDS = {
    "layout.arrange_gap_mm": "number",
    "layout.arrange_edge_mm": "number",
    "layout.merge_spacing_mm": "number",
    "layout.merge_edge_margin_mm": "number",
    "layout.single_copy_edge_mm": "number",
    "layout.multi_copy_edge_mm": "number",
    "layout.max_merged_hours": "number",
    "prime_tower.size_mm": "number",
    "prime_tower.part_clearance_mm": "number",
    "prime_tower.edge_mm": "number",
    "prime_tower.merge_slack_mm": "number",
    "purge.mode": "picker",
    "purge.prime_tower": "picker",
    "printer.start_macro": "text",
    "printer.end_macro": "text",
    "slicer.flash_studio_path": "text",
}


def load_for(model_key, extra=""):
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "local.yaml"
        local.write_text(f"printer:\n  model: {model_key}\n{extra}")
        return settings.load(local=local, environ={})


class NoIssueNumbersTests(unittest.TestCase):
    def test_the_pattern_catches_every_reference_shape(self):
        for text in ("see (#1234)", "#913 early layers", "issue 1490", "PR #493",
                     "pull request 77", "Issue #12"):
            with self.subTest(text=text):
                self.assertRegex(text, ISSUE_REFERENCE)
        for text in ("plain words", "0.4 mm", "PLA+ #FFFFFF? no", "C5_PRINT_START"):
            with self.subTest(text=text):
                self.assertNotRegex(text.replace("#FFFFFF", ""), ISSUE_REFERENCE)

    def test_no_label_help_or_choice_names_an_issue_or_pull_request(self):
        for field in CATALOG:
            with self.subTest(field=field.id):
                self.assertNotRegex(field.label, ISSUE_REFERENCE)
                self.assertNotRegex(field.help, ISSUE_REFERENCE)
                for choice in field.choices:
                    self.assertNotRegex(str(choice), ISSUE_REFERENCE)

    def test_no_group_note_or_heading_names_an_issue_or_pull_request(self):
        for group, note in GROUP_FILE_ONLY_NOTES.items():
            with self.subTest(group=group):
                self.assertNotRegex(note, ISSUE_REFERENCE)
        for name, heading in pages.GROUPS:
            with self.subTest(group=name):
                self.assertNotRegex(heading, ISSUE_REFERENCE)


class CameraCheckDefaultTests(unittest.TestCase):
    def test_the_local_failure_detector_is_the_default_camera_check(self):
        data = settings.load(local=False, environ={})
        self.assertIs(data["vision"]["local_detector_enabled"], True)
        self.assertIs(data["vision"]["ai_enabled"], False)
        self.assertEqual(data["vision"]["watch_source"], "auto")
        # With those defaults the watchdog judges frames with the detector
        # and never with an endpoint.
        self.assertTrue(local_vision.watch_uses_detector(data))
        self.assertFalse(visual_watch.vision_ai_enabled(data))

    def test_the_shipped_home_config_turns_the_detector_on(self):
        home = settings._read(settings.HOME)
        self.assertIs(home["vision"]["local_detector_enabled"], True)
        self.assertIs(home["vision"]["ai_enabled"], False)

    def test_an_old_config_that_turns_the_detector_off_still_loads_and_wins(self):
        data = load_for("klipper.generic", "vision:\n  local_detector_enabled: false\n")
        self.assertIs(data["vision"]["local_detector_enabled"], False)
        self.assertFalse(local_vision.watch_uses_detector(data))

    def test_the_camera_group_does_not_lead_with_an_ai_switch(self):
        camera = [field for field in CATALOG if field.group == "camera"]
        for field in camera:
            with self.subTest(field=field.id):
                self.assertNotRegex(field.label, r"\bAI\b")
        vision_fields = [field.id for field in camera if field.section == "vision"]
        self.assertEqual(vision_fields[0], "vision.local_detector_enabled")
        detector = field_by_id("vision.local_detector_enabled")
        self.assertIn("On by default", detector.help)

    def test_the_vision_endpoint_switch_sits_with_its_endpoint_and_says_what_it_does(self):
        switch = field_by_id("vision.ai_enabled")
        self.assertEqual(switch.group, "ai")
        self.assertEqual(switch.kind, "toggle")
        self.assertNotIn("AI camera checks", switch.label)
        self.assertIn("vision endpoint", switch.label)
        self.assertIn("local failure detector", switch.help)
        for field in CATALOG:
            with self.subTest(field=field.id):
                self.assertNotIn("AI camera checks", field.help)

    def test_the_off_reason_names_the_endpoint_not_ai(self):
        self.assertNotIn("AI camera", visual_watch.VISION_AI_OFF)
        self.assertIn("vision.ai_enabled", visual_watch.VISION_AI_OFF)

    def test_camera_watch_has_a_judge_out_of_the_box(self):
        # The watch is on by default, so its default judge must be on too.
        data = settings.load(local=False, environ={})
        self.assertIs(data["visual_watch"]["enabled"], True)
        self.assertTrue(local_vision.watch_uses_detector(data))

    def test_auto_light_help_does_not_contradict_the_plate_reference(self):
        help_text = field_by_id("vision.auto_light_off_minutes").help
        self.assertIn("empty-plate reference", help_text)
        self.assertNotIn("like the camera watchdog", help_text)

    def test_bambu_camera_source_is_offered_only_with_a_bambu_printer(self):
        field = field_by_id("services.camera_source")
        self.assertEqual(choices_for(field), tuple(settings.CAMERA_SOURCES))  # no data: all
        self.assertNotIn("bambu", choices_for(field, data=load_for("klipper.generic")))
        self.assertIn("bambu", choices_for(field, data=load_for("bambulab.a1")))
        roster = load_for("klipper.generic")
        roster["printers"] = [{"id": "a", "model": "klipper.generic"},
                              {"id": "b", "model": "bambulab.p1s"}]
        self.assertIn("bambu", choices_for(field, data=roster))

    def test_the_one_option_bot_mode_is_not_a_field(self):
        self.assertEqual(settings.BOT_MODES, ("standalone",))
        self.assertIsNone(field_by_id("bot.mode"))


class SlicingFieldTests(unittest.TestCase):
    def test_new_slicing_fields_exist_with_their_kind_and_group(self):
        for field_id, kind in NEW_SLICING_FIELDS.items():
            with self.subTest(field=field_id):
                field = field_by_id(field_id)
                self.assertIsNotNone(field)
                self.assertEqual(field.kind, kind)
                self.assertEqual(field.group, "slicing")
                self.assertTrue(field.help)
                if kind == "number":
                    self.assertIsNotNone(field.minimum)
                    self.assertGreater(field.minimum, 0)  # the loader wants a positive number
                    self.assertIsNotNone(field.maximum)

    def test_every_model_default_fits_the_number_bounds(self):
        for key, model in printers.MODELS.items():
            defaults = settings._merge(model.defaults(), settings._read(settings.HOME))
            for field_id, kind in NEW_SLICING_FIELDS.items():
                if kind != "number":
                    continue
                field = field_by_id(field_id)
                value = defaults.get(field.section, {}).get(field.key)
                if value is None:
                    continue
                with self.subTest(model=key, field=field_id):
                    self.assertGreaterEqual(value, field.minimum)
                    self.assertLessEqual(value, field.maximum)
        for preset in settings.SPACING_PRESETS.values():
            for (section, key), value in preset.items():
                field = field_by_id(f"{section}.{key}")
                with self.subTest(preset_key=f"{section}.{key}"):
                    self.assertGreaterEqual(value, field.minimum)
                    self.assertLessEqual(value, field.maximum)

    def test_purge_pickers_carry_the_settings_choice_sets(self):
        self.assertEqual(choices_for(field_by_id("purge.mode")), tuple(settings.PURGE_MODES))
        self.assertEqual(choices_for(field_by_id("purge.prime_tower")),
                         tuple(settings.PURGE_TOWER_CHOICES))

    def test_start_macro_vendors_are_the_ones_whose_files_call_the_macro(self):
        # The visibility rule is the consumer's own: a vendor is listed
        # exactly when changing [printer] start_macro changes the start
        # G-code its adapter writes into a sliced file.
        calls_macro = set()
        for key, model in printers.MODELS.items():
            cfg = load_for(key)
            cfg["printer"]["start_macro"] = "ZZ_TEST_START"
            cfg["printer"]["end_macro"] = "ZZ_TEST_END"
            adapter = printers.get(key, copy.deepcopy(cfg))
            try:
                text = adapter.machine_start_gcode("", "auto") + adapter.machine_end_gcode("")
            except ProfileError:
                # A Bambu Lab model writes its start G-code from profiles
                # generated on the operator's machine and never calls the macro.
                self.assertEqual(model.vendor.key, "bambulab")
                continue
            if "ZZ_TEST_START" in text and "ZZ_TEST_END" in text:
                calls_macro.add(model.vendor.key)
        self.assertEqual(calls_macro, set(START_MACRO_VENDORS))

    def test_fields_show_only_for_the_printers_that_use_them(self):
        klipper = load_for("klipper.generic")
        ad5x = load_for("flashforge.ad5x")
        ad5m = load_for("flashforge.ad5m")
        creator = load_for("flashforge_opencreator.creator5")
        self.assertTrue(config_page_visible("printer.start_macro", klipper))
        self.assertTrue(config_page_visible("printer.end_macro", klipper))
        self.assertFalse(config_page_visible("printer.start_macro", ad5x))
        self.assertFalse(config_page_visible("printer.end_macro", creator))
        # The prime tower belongs to printers with more than one filament.
        self.assertFalse(config_page_visible("prime_tower.size_mm", klipper))
        self.assertTrue(config_page_visible("prime_tower.size_mm", ad5x))
        self.assertTrue(config_page_visible("prime_tower.edge_mm", creator))
        # ZMod's purge plan runs only with the native ZMod screen.
        self.assertTrue(config_page_visible("purge.mode", ad5x))
        self.assertTrue(config_page_visible("purge.prime_tower", ad5x))
        self.assertFalse(config_page_visible("purge.mode", klipper))
        self.assertFalse(config_page_visible("purge.mode", ad5m))
        moved = copy.deepcopy(klipper)
        moved["purge"]["mode"] = "prime_tower"
        self.assertTrue(config_page_visible("purge.mode", moved))  # can be put back
        # Flash Studio is a Flashforge-only route.
        self.assertTrue(config_page_visible("slicer.flash_studio_path", ad5m))
        self.assertFalse(config_page_visible("slicer.flash_studio_path", klipper))
        self.assertFalse(config_page_visible("slicer.flash_studio_path", creator))
        self.assertTrue(all(vendor.startswith("flashforge") for vendor in FLASH_STUDIO_VENDORS))
        # The gaps every printer's layout uses are always shown.
        for field_id in ("layout.arrange_gap_mm", "layout.max_merged_hours"):
            with self.subTest(field=field_id):
                self.assertTrue(config_page_visible(field_id, klipper))
                self.assertTrue(config_page_visible(field_id, ad5x))

    def test_a_roster_zmod_printer_shows_the_purge_fields(self):
        klipper = load_for("klipper.generic")
        roster = copy.deepcopy(klipper)
        roster["printers"] = [{"id": "main", "model": "klipper.generic"},
                              {"id": "ad5x", "model": "flashforge.ad5x"}]
        self.assertFalse(config_page_visible("purge.mode", klipper))
        self.assertTrue(config_page_visible("purge.mode", roster))


class SlicingWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.local = Path(self.tmp.name) / "local.yaml"

    def test_gap_and_tower_fields_round_trip_and_pin_over_the_preset(self):
        self.local.write_text("printer:\n  model: flashforge.ad5x\nlayout:\n  spacing: medium\n")
        changed = write_changes({"layout.arrange_gap_mm": 7.5, "layout.max_merged_hours": 6,
                                 "prime_tower.size_mm": 40, "layout.single_copy_edge_mm": 2},
                                config_path=self.local, environ={})
        self.assertEqual(set(changed), {("layout", "arrange_gap_mm"),
                                        ("layout", "max_merged_hours"),
                                        ("prime_tower", "size_mm"),
                                        ("layout", "single_copy_edge_mm")})
        data = settings.load(local=self.local, environ={})
        self.assertEqual(data["layout"]["arrange_gap_mm"], 7.5)  # pinned, not the preset's 5.0
        self.assertEqual(data["layout"]["arrange_edge_mm"],
                         settings.SPACING_PRESETS["medium"][("layout", "arrange_edge_mm")])
        self.assertEqual(data["layout"]["max_merged_hours"], 6)
        self.assertEqual(data["prime_tower"]["size_mm"], 40)
        self.assertEqual(data["layout"]["single_copy_edge_mm"], 2)

    def test_purge_macro_and_flash_studio_fields_round_trip(self):
        self.local.write_text("printer:\n  model: klipper.generic\n")
        write_changes({"printer.start_macro": "START_PRINT", "printer.end_macro": "END_PRINT"},
                      config_path=self.local, environ={})
        data = settings.load(local=self.local, environ={})
        self.assertEqual((data["printer"]["start_macro"], data["printer"]["end_macro"]),
                         ("START_PRINT", "END_PRINT"))
        self.local.write_text("printer:\n  model: flashforge.ad5x\n")
        write_changes({"purge.mode": "printer_chute", "purge.prime_tower": "off",
                       "slicer.flash_studio_path": "/opt/flash-studio/FlashStudio"},
                      config_path=self.local, environ={})
        data = settings.load(local=self.local, environ={})
        self.assertEqual(data["purge"]["mode"], "printer_chute")
        self.assertEqual(data["purge"]["prime_tower"], "off")
        self.assertEqual(data["slicer"]["flash_studio_path"], "/opt/flash-studio/FlashStudio")

    def test_bad_values_are_refused_and_the_file_is_unchanged(self):
        self.local.write_text("printer:\n  model: klipper.generic\n")
        before = self.local.read_bytes()
        for changes in ({"printer.start_macro": "PRINT START"},
                        {"purge.mode": "bucket"},
                        {"layout.arrange_gap_mm": 0}):
            with self.subTest(changes=changes):
                with self.assertRaises(WebUIConfigError):
                    write_changes(changes, config_path=self.local, environ={})
                self.assertEqual(self.local.read_bytes(), before)

    def test_the_slicing_group_names_its_file_only_settings(self):
        note = GROUP_FILE_ONLY_NOTES["slicing"]
        for name in ("[material_temps]", "[material_tuning]", "[nozzle_temp_offsets]",
                     "[cold_pull]", "flush_multiplier", "[zmod] plugins"):
            with self.subTest(name=name):
                self.assertIn(name, note)
        self.assertIsNone(field_by_id("purge.flush_multiplier"))
        self.assertIsNone(field_by_id("zmod.plugins"))


if __name__ == "__main__":
    unittest.main()
