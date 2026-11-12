"""Thin print-in-place models (FlexyRing): Arachne flex profile, brim-off allowed, failure logs kept."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy.core import orca_adapter
from print_handy.core import slice_guard as sg

GCODE = "; estimated printing time = 1h 2m\n; brim_type = {brim}\nG28\nT0\nG1 X10 Y10 E1\nG1 X200 Y200 E2\n"


class FlexProfileTests(unittest.TestCase):
    def test_flex_profile_follows_the_guide(self):
        p = orca_adapter.FLEX_PROFILE
        self.assertEqual(p['wall_generator'], 'arachne')
        self.assertEqual(p['wall_loops'], '2')
        self.assertEqual(p['sparse_infill_density'], '15%')
        self.assertEqual(p['initial_layer_print_height'], '0.2')
        self.assertEqual(p['initial_layer_speed'], '20')
        for key, off in (('brim_type', 'no_brim'), ('enable_support', '0'), ('ironing_type', 'no ironing'),
                         ('fuzzy_skin', 'none'), ('raft_layers', '0')):
            self.assertEqual(p[key], off, key)

    def test_designer_wall_generator_is_preserved(self):
        self.assertIn('wall_generator', orca_adapter.PROCESS_KEYS)


class BrimGuardTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        Path(self.dir.name, 'process.json').write_text(json.dumps({'brim_type': 'outer_only'}))

    def tearDown(self):
        self.dir.cleanup()

    def _check(self, brim):
        return sg.post_slice(GCODE.format(brim=brim), tool_slots={0: 1}, templates=self.dir.name)

    def test_brim_turned_off_is_not_foreign(self):
        self.assertFalse(any('brim_type' in b for b in self._check('no_brim').blocked))

    def test_template_brim_passes(self):
        self.assertFalse(any('brim_type' in b for b in self._check('outer_only').blocked))

    def test_a_different_brim_is_still_blocked(self):
        self.assertTrue(any('brim_type' in b for b in self._check('mouse_ear').blocked))


class FailureLogTests(unittest.TestCase):
    def test_logs_survive_the_slice_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, 'sliced', 'abc123'); folder.mkdir(parents=True)
            (folder / 'orca.log').write_text('boom')
            (folder / 'result.json').write_text('{"return_code": -100}')
            state = Path(tmp, 'state')
            from print_handy.runtime import _config
            with mock.patch.object(_config, 'state_home', return_value=Path(tmp)):
                kept = orca_adapter._keep_failure_logs(folder)
            self.assertEqual(kept, state / 'slice-failures' / 'abc123')
            self.assertEqual((kept / 'orca.log').read_text(), 'boom')
            self.assertTrue((kept / 'result.json').is_file())

    def test_a_logging_error_never_masks_the_slice_error(self):
        from print_handy.runtime import _config
        with mock.patch.object(_config, 'state_home', side_effect=OSError('ro')):
            self.assertEqual(orca_adapter._keep_failure_logs('/nope/x'), Path('/nope/x'))


class RetryTriggerTests(unittest.TestCase):
    def _result(self, tmp, payload):
        Path(tmp, 'result.json').write_text(json.dumps(payload))
        return Path(tmp)

    def test_only_orca_minus_100_triggers_the_flex_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._result(tmp, {'return_code': -100, 'error_string': 'Failed slicing the model.'})
            self.assertTrue(orca_adapter._orca_slicing_rejected(folder))

    def test_a_conflict_or_crash_does_not_trigger_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self._result(tmp, {'return_code': -101, 'error_string': 'G-code conflicts detected'})
            self.assertFalse(orca_adapter._orca_slicing_rejected(folder))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(orca_adapter._orca_slicing_rejected(Path(tmp)))


if __name__ == '__main__':
    unittest.main()
