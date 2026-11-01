"""Pre and post slice safety gates. Pure text checks, no slicer."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from print_handy.core import slice_guard as sg

GOOD_GCODE = "; estimated printing time = 1h 2m\nG28\nT0\nG1 X10 Y10 E1\nG1 X200 Y200 E2\n"


class PreSliceTests(unittest.TestCase):
    def test_model_inside_the_bed_passes(self):
        r = sg.pre_slice(bounds=(5, 5, 215, 215), tool_slots={0: 1})
        self.assertEqual(r.status, "ok")

    def test_model_past_the_bed_edge_is_blocked(self):
        r = sg.pre_slice(bounds=(5, 5, 221, 100), tool_slots={0: 1})
        self.assertEqual(r.status, "blocked")
        self.assertIn("exceed the 220x220 mm bed", r.blocked[0])

    def test_missing_bounds_only_warns(self):
        r = sg.pre_slice(bounds=None, tool_slots={0: 1})
        self.assertEqual(r.status, "warnings")

    def test_changed_start_gcode_is_blocked(self):
        before = {"machine_start_gcode": "START_PRINT", "layer_height": 0.2}
        after = {"machine_start_gcode": "START_PRINT\nM104 S300", "layer_height": 0.2}
        r = sg.pre_slice(bounds=(0, 0, 10, 10), before_profile=before, profile=after, tool_slots={0: 1})
        self.assertIn("machine_start_gcode", r.blocked[0])

    def test_ordinary_setting_change_is_allowed(self):
        r = sg.pre_slice(bounds=(0, 0, 10, 10), before_profile={"layer_height": 0.2},
                         profile={"layer_height": 0.28}, tool_slots={0: 1})
        self.assertTrue(r.ok)

    def test_more_filaments_than_slots_is_blocked(self):
        r = sg.pre_slice(bounds=(0, 0, 10, 10), filaments=5, tool_slots={i: 1 for i in range(5)})
        self.assertTrue(any("only 4 IFS slots" in b for b in r.blocked))

    def test_multi_filament_needs_a_slot_map(self):
        r = sg.pre_slice(bounds=(0, 0, 10, 10), filaments=2)
        self.assertTrue(any("no explicit T-index" in b for b in r.blocked))

    def test_slot_outside_the_ifs_is_blocked(self):
        r = sg.pre_slice(bounds=(0, 0, 10, 10), filaments=1, tool_slots={"T0": 5})
        self.assertTrue(any("valid slots are 1 through 4" in b for b in r.blocked))

    def test_missing_required_profile_key_is_blocked(self):
        r = sg.pre_slice(bounds=(0, 0, 10, 10), profile={"nozzle_diameter": ""},
                         required_keys=("nozzle_diameter",), tool_slots={0: 1})
        self.assertIn("nozzle_diameter", r.blocked[0])


class PostSliceTests(unittest.TestCase):
    def test_clean_gcode_passes(self):
        self.assertEqual(sg.post_slice(GOOD_GCODE, tool_slots={0: 1}).status, "ok")

    def test_empty_gcode_is_blocked(self):
        self.assertEqual(sg.post_slice("", tool_slots={0: 1}).status, "blocked")

    def test_move_off_the_bed_is_blocked(self):
        r = sg.post_slice(GOOD_GCODE + "G0 X230 Y10\n", tool_slots={0: 1})
        self.assertIn("X230 Y10", r.blocked[0])

    def test_unmapped_tool_is_blocked(self):
        r = sg.post_slice(GOOD_GCODE + "T2\n", tool_slots={0: 1})
        self.assertTrue(any("T2" in b for b in r.blocked))

    def test_tool_beyond_the_ifs_is_blocked(self):
        r = sg.post_slice(GOOD_GCODE + "T4\n", tool_slots={0: 1, 4: 1})
        self.assertTrue(any("only 4 IFS slots" in b for b in r.blocked))

    def test_conflict_report_is_blocked(self):
        r = sg.post_slice(GOOD_GCODE + "; G-code conflicts detected\n", tool_slots={0: 1})
        self.assertTrue(any("conflict" in b for b in r.blocked))

    def test_real_slicer_time_headers_are_recognized(self):
        for header in ("; estimated printing time (normal mode) = 1h 2m 3s",
                       "; model printing time: 1h 2m; total estimated time: 1h 5m",
                       "; estimated printing time = 12m", ";TIME:3720"):
            with self.subTest(header=header):
                self.assertEqual(sg.post_slice(header + "\nG1 X10 Y10\n").warnings, [])

    def test_missing_time_header_warns(self):
        r = sg.post_slice("G1 X10 Y10\n")
        self.assertEqual(r.status, "warnings")

    def test_failed_orca_result_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = Path(tmp) / "result.json"
            result.write_text(json.dumps({"return_code": -101, "error_string": "slicing result conflict"}))
            r = sg.post_slice(GOOD_GCODE, tool_slots={0: 1}, result_json=result)
        self.assertTrue(any("-101" in b for b in r.blocked))

    def test_reads_gcode_from_a_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.gcode"
            path.write_text(GOOD_GCODE)
            self.assertTrue(sg.post_slice(path, tool_slots={0: 1}).ok)

    def test_settings_are_read_only_from_lines_that_start_with_a_comment(self):
        # #734: the per-line setting checks skip non-comment lines; their patterns were always anchored at ';'.
        for line, blocked in ((";  ENABLE_ARC_FITTING = 1", True), ("; enable_arc_fitting = 1", True),
                              (" ; enable_arc_fitting = 1", False), ("G1 X1 ; enable_arc_fitting = 1", False)):
            with self.subTest(line=line):
                r = sg.post_slice(GOOD_GCODE + line + "\n", tool_slots={0: 1})
                self.assertEqual(any("enable_arc_fitting" in b for b in r.blocked), blocked)


class GeneratorIdentityTests(unittest.TestCase):
    """#434: identity is read from what each slicer actually writes, in the real 2.8.2.61 shapes."""

    # The first lines of a real Bambu Studio 2.8.2.61 A1 slice, verbatim.
    BAMBU_HEADER = ("; HEADER_BLOCK_START\n"
                    "; BambuStudio 02.08.02.61\n"
                    "; model printing time: 16m 18s; total estimated time: 22m 34s\n"
                    "; total layer number: 100\n"
                    "; HEADER_BLOCK_END\n")

    def test_orca_generated_by_line(self):
        text = "; HEADER_BLOCK_START\n; generated by OrcaSlicer 2.5.0-alpha on 2026-10-27 at 17:34:51\n"
        self.assertTrue(sg.generator_identity(text).startswith("OrcaSlicer 2.5.0-alpha"))

    def test_flash_studio_generated_by_line(self):
        self.assertEqual(sg.generator_identity("; generated by Flash Studio 1.7.19\n"), "Flash Studio 1.7.19")

    def test_bambu_studio_header_block(self):
        self.assertEqual(sg.generator_identity(self.BAMBU_HEADER), "BambuStudio 02.08.02.61")

    def test_invented_bambu_generated_by_line_is_no_identity(self):
        # The form the mocked suites once fabricated; no released Bambu Studio writes it.
        self.assertIsNone(sg.generator_identity("; generated by BambuStudio 02.08.02.61\nG1 X1\n"))

    def test_bambu_version_line_without_the_header_block_is_no_identity(self):
        self.assertIsNone(sg.generator_identity("; BambuStudio 02.08.02.61\nG1 X1\n"))

    def test_unknown_and_missing_identities(self):
        self.assertIsNone(sg.generator_identity("; generated by PrusaSlicer 2.8.0\n"))
        self.assertIsNone(sg.generator_identity("G1 X1 Y1\n"))
        self.assertIsNone(sg.generator_identity(""))


class FailureClassTests(unittest.TestCase):
    def test_known_failures(self):
        self.assertEqual(sg.classify_orca_failure("", -101), "gcode_conflict")
        self.assertEqual(sg.classify_orca_failure("bad param values"), "invalid_3mf_profile_value")
        self.assertEqual(sg.classify_orca_failure("", -50), "orca_cli_failure")
        self.assertEqual(sg.classify_orca_failure("weird"), "unknown_orca_failure")


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.baseline = self.dir / "baseline.json"

    def tearDown(self):
        self.tmp.cleanup()

    def approve(self):
        with contextlib.redirect_stdout(io.StringIO()):  # approve_baseline prints the diff
            sg.approve_baseline(self.gcode(), path=self.baseline)

    def gcode(self, start="START_PRINT MESH=MESH_DATA", end="END_PRINT"):
        path = self.dir / "out.gcode"
        path.write_text(f"; machine_start_gcode = {start}\n; machine_end_gcode = {end}\nG28\n")
        return path

    def test_missing_baseline_warns_and_allows(self):
        r = sg.enforce_approved_baseline(self.gcode(), path=self.baseline)
        self.assertEqual(r.status, "warnings")

    def test_approved_settings_pass(self):
        self.approve()
        self.assertTrue(sg.enforce_approved_baseline(self.gcode(), path=self.baseline).ok)

    def test_per_job_mesh_name_does_not_trip_the_guard(self):
        self.approve()
        r = sg.enforce_approved_baseline(self.gcode(start="START_PRINT MESH=auto"), path=self.baseline)
        self.assertTrue(r.ok)

    def test_drifted_end_gcode_is_blocked(self):
        self.approve()
        r = sg.enforce_approved_baseline(self.gcode(end="END_PRINT M84"), path=self.baseline)
        self.assertIn("machine_end_gcode", r.blocked[0])
