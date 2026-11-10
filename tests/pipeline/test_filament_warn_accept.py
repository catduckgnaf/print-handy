"""Filament warn-and-accept at the slice gates (owner ruling, catduck,
2026-11-10): "You should never block a print for that" / "I actually really
don't want anything filament to block a print, allow the user to accept the
warning."

A filament warning with no recorded acceptance stops the slice with
FilamentWarningPending (nothing proceeds silently); an explicit acceptance
bound to the exact warning texts, the source hash and the selected plates
lets the same job slice, and the accepted warnings ride on the job record.
Hard problems that are not filament data (decisions bound to another source)
refuse exactly as before. Since #1189 (owner decision, catduck, 2026-11-10)
an unmapped used filament, a slot past capacity and color choices never made
are no longer refusals: they print from a named fallback once accepted (see
tests/pipeline/test_filament_fallbacks_1189.py).
"""
import asyncio
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from print_handy.core import model_job as mj
from print_handy.core import source_colors as sc
from tests.core.test_plate_usage import write_source, two_plate_source, selection

STATE = {'bed_mm': [220, 220], 'max_print_height_mm': 250,
         'nozzle': {'diameter_mm': 0.4},
         'spool': {'name': 'PLA', 'material': 'PLA', 'density': 1.24},
         'temps_c': {'nozzle': 210, 'bed': 60},
         # Verified slots (#1189): a source with no color choices now slices
         # single-color from the first loaded slot, which the slot check
         # verifies like any recorded one-color choice.
         'filament_slots': {'1': {'color': 'white', 'material': 'PLA', 'spoolman_id': 10,
                                  'physical_color_verified': True},
                            '2': {'color': 'black', 'material': 'PLA', 'spoolman_id': 11,
                                  'physical_color_verified': True}}}


def _unknown_usage_manifest(work):
    path = write_source(work / "noext.3mf",
                        [("Base", 1, None), ("Knob", None, None)], [[0], [1]])
    return path, sc.inventory(path)


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _job_file(self, job):
        jp = self.work / "JOB.json"
        jp.write_text(json.dumps(job))
        return jp

    def test_gate_raises_pending_persists_the_record_and_never_proceeds(self):
        path, manifest = _unknown_usage_manifest(self.work)
        decisions = {'source_sha256': manifest['source_sha256'], 'mode': 'single_color',
                     'selection': selection(1)}
        jp = self._job_file({'downloaded_file': str(path)})
        job = json.loads(jp.read_text())
        with self.assertRaises(mj.FilamentWarningPending) as caught:
            mj._enforce_filament_readiness(job, manifest, decisions, [2],
                                           job_folder=self.work)
        pending = caught.exception
        self.assertTrue(any('plate 2' in w for w in pending.warnings))
        self.assertEqual(pending.source_sha256, manifest['source_sha256'])
        self.assertEqual(pending.selected_plates, ['2'])
        saved = json.loads(jp.read_text())
        self.assertEqual(saved['filament_warnings_pending']['warnings'], pending.warnings)
        self.assertNotIn('filament_warning_acknowledgement', saved)

    def test_recorded_acceptance_lets_the_gate_pass_and_is_bound(self):
        path, manifest = _unknown_usage_manifest(self.work)
        decisions = {'source_sha256': manifest['source_sha256'], 'mode': 'single_color',
                     'selection': selection(1)}
        jp = self._job_file({'downloaded_file': str(path)})
        job = json.loads(jp.read_text())
        with self.assertRaises(mj.FilamentWarningPending):
            mj._enforce_filament_readiness(job, manifest, decisions, [2],
                                           job_folder=self.work)
        mj.record_filament_warning_acknowledgement(jp)
        job = json.loads(jp.read_text())
        warnings = mj._enforce_filament_readiness(job, manifest, decisions, [2],
                                                  job_folder=self.work)
        self.assertTrue(any('plate 2' in w for w in warnings))
        ack = job['filament_warning_acknowledgement']
        self.assertTrue(ack['acknowledged'])
        self.assertEqual(ack['warnings'], warnings)
        self.assertEqual(ack['selected_plates'], ['2'])
        self.assertEqual(ack['source_sha256'], manifest['source_sha256'])
        self.assertEqual(job['filament_warnings'], warnings)
        self.assertNotIn('filament_warnings_pending', job)

    def test_acceptance_does_not_transfer_to_another_source(self):
        path, manifest = _unknown_usage_manifest(self.work)
        decisions = {'source_sha256': manifest['source_sha256'], 'mode': 'single_color',
                     'selection': selection(1)}
        jp = self._job_file({'downloaded_file': str(path)})
        job = json.loads(jp.read_text())
        with self.assertRaises(mj.FilamentWarningPending):
            mj._enforce_filament_readiness(job, manifest, decisions, [2],
                                           job_folder=self.work)
        mj.record_filament_warning_acknowledgement(jp)
        other_path = write_source(self.work / "other.3mf",
                                  [("Base", 1, None), ("Knob2", None, None)], [[0], [1]])
        other = sc.inventory(other_path)
        self.assertNotEqual(other['source_sha256'], manifest['source_sha256'])
        other_decisions = {'source_sha256': other['source_sha256'], 'mode': 'single_color',
                           'selection': selection(1)}
        job = json.loads(jp.read_text())
        with self.assertRaises(mj.FilamentWarningPending):
            mj._enforce_filament_readiness(job, other, other_decisions, [2],
                                           job_folder=self.work)

    def test_pending_survives_the_worker_error_boundary(self):
        # slice_job runs inside the heavy worker; the pending signal must
        # come back across that boundary as the same type or no surface
        # could present the warnings to the operator.
        from print_handy.core import worker_errors
        from print_handy.core.filament_warnings import FilamentWarningPending
        original = FilamentWarningPending(['plate 2: support usage is unknown'],
                                          source_sha256='abc', selected_plates=[2])
        decoded = worker_errors.decode(worker_errors.encode(original))
        self.assertIsInstance(decoded, FilamentWarningPending)
        self.assertEqual(decoded.warnings, original.warnings)
        self.assertEqual(decoded.source_sha256, 'abc')
        self.assertEqual(decoded.selected_plates, ['2'])

    def test_recording_an_acceptance_with_no_pending_warnings_is_refused(self):
        jp = self._job_file({'downloaded_file': 'unused'})
        with self.assertRaises(ValueError):
            mj.record_filament_warning_acknowledgement(jp)
        with self.assertRaises(ValueError):
            mj.record_filament_warning_acknowledgement({}, warnings=[])

    def test_unmapped_filament_is_a_fallback_warning_not_covered_by_another_acceptance(self):
        # Behavior change (#1189, owner decision 2026-11-10): #1190 pinned a
        # used filament with no slot mapping as a hard refusal. With the
        # printer record it now becomes a warning naming the fallback slot.
        # An acceptance for some other warning set still does not cover it.
        manifest = sc.inventory(two_plate_source(self.work / "two.3mf"))
        parts = {p["name"]: p for p in manifest["parts"]}
        decisions = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                     "mode": "per_part",
                     "choices": {"part:" + parts["Base"]["key"]: selection(1),
                                 "part:" + parts["Lid"]["key"]: selection(2)}}
        job = {}
        job['filament_warning_acknowledgement'] = {
            'acknowledged': True, 'warnings': ['some earlier warning'],
            'selected_plates': [2], 'source_sha256': manifest['source_sha256']}
        with self.assertRaises(mj.FilamentWarningPending) as caught:
            mj._enforce_filament_readiness(job, manifest, decisions, [2], slot_capacity=4,
                                           job_folder=self.work, state=STATE)
        self.assertEqual(caught.exception.warnings,
                         ["plate 2: source filament 3 is used but has no slot chosen (part Knob); "
                          "it prints from slot 1, the printer's first loaded slot"])
        # Without the printer record (no fallback can be named) it refuses as before.
        with self.assertRaisesRegex(RuntimeError, 'print readiness blocked') as hard:
            mj._enforce_filament_readiness(job, manifest, decisions, [2], job_folder=self.work)
        self.assertNotIsInstance(hard.exception, mj.FilamentWarningPending)

    def test_decisions_bound_to_another_source_still_refuse(self):
        # Decision integrity is not filament data: it stays a hard refusal
        # with the printer record and an acceptance present.
        manifest = sc.inventory(two_plate_source(self.work / "two.3mf"))
        decisions = {"source_sha256": "another source", "mode": "single_color",
                     "selection": selection(1)}
        job = {'filament_warning_acknowledgement': {
            'acknowledged': True, 'warnings': ['some earlier warning'],
            'selected_plates': [2], 'source_sha256': manifest['source_sha256']}}
        with self.assertRaisesRegex(RuntimeError, 'print readiness blocked') as caught:
            mj._enforce_filament_readiness(job, manifest, decisions, [2], slot_capacity=4,
                                           job_folder=self.work, state=STATE)
        self.assertNotIsInstance(caught.exception, mj.FilamentWarningPending)


class SliceJobWarnAcceptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _patches(self):
        return (mock.patch.object(mj, "ARCHIVE", self.work),
                mock.patch.object(mj.printer_state, "load", return_value=STATE),
                mock.patch.object(mj.printer_state, "check", return_value=(True, [], {})))

    def test_selected_plate_unknown_usage_waits_for_acceptance_then_slices(self):
        path, manifest = _unknown_usage_manifest(self.work)
        job_path = self.work / "JOB.json"
        job_path.write_text(json.dumps({
            'downloaded_file': str(path),
            'audit': {'plates': 2, 'size_x': '10', 'size_y': '10', 'size_z': '10'},
            'title': 'Unknown usage plate', 'selected_plates': [2],
            'color_decisions': {'source_sha256': manifest['source_sha256'],
                                'mode': 'single_color',
                                'selection': {'slot': 1, 'spoolman_id': 10,
                                              'color': 'white', 'material': 'PLA'}}}))
        p1, p2, p3 = self._patches()
        with p1, p2, p3, mock.patch.object(
                mj, "slice_multi_plate", return_value=Path("result.gcode")) as slicer:
            with self.assertRaises(mj.FilamentWarningPending):
                mj.slice_job(job_path)
            slicer.assert_not_called()
            saved = json.loads(job_path.read_text())
            self.assertIn('filament_warnings_pending', saved)
            mj.record_filament_warning_acknowledgement(job_path)
            result = mj.slice_job(job_path)
            self.assertEqual(result, Path("result.gcode"))
            slicer.assert_called_once()
            saved = json.loads(job_path.read_text())
            self.assertTrue(saved['filament_warning_acknowledgement']['acknowledged'])
            self.assertTrue(any('plate 2' in w
                                for w in saved['filament_warnings']))
            # The acceptance authorized that one slicing run: the run marks
            # it used, and a reslice consumes it at entry and presents the
            # warnings again rather than silently reusing the acceptance.
            self.assertTrue(
                saved['filament_warning_acknowledgement']['used_by_slice'])
            with self.assertRaises(mj.FilamentWarningPending):
                mj.slice_job(job_path)
            self.assertEqual(slicer.call_count, 1)
            saved = json.loads(job_path.read_text())
            self.assertNotIn('filament_warning_acknowledgement', saved)
            self.assertIn('filament_warnings_pending', saved)

    def test_damaged_usage_claim_waits_for_acceptance_then_slices(self):
        source = self.work / "multi.3mf"
        slice_info = ('<config>'
                      '<plate><metadata key="index" value="1"/>'
                      '<filament id="bad" used_g="1" used_m="1"/></plate>'
                      '<plate><metadata key="index" value="2"/>'
                      '<filament id="1" used_g="2" used_m="1"/></plate></config>')
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("Metadata/plate_1.png", b"png")
            archive.writestr("Metadata/plate_2.png", b"png")
            archive.writestr("Metadata/slice_info.config", slice_info)
        job_path = self.work / "JOB.json"
        job_path.write_text(json.dumps({
            'downloaded_file': str(source),
            'audit': {'plates': 2, 'size_x': '100', 'size_y': '100', 'size_z': '10'},
            'title': 'Damaged claim'}))
        p1, p2, p3 = self._patches()
        with p1, p2, p3, mock.patch.object(
                mj, "slice_multi_plate", return_value=Path("result.gcode")) as slicer:
            with self.assertRaises(mj.FilamentWarningPending) as caught:
                mj.slice_job(job_path)
            self.assertTrue(any('plate 1' in w for w in caught.exception.warnings))
            # Since #1189 the same round also names the one-color fallback:
            # this job has no color choices, which slice_multi_plate refused.
            self.assertTrue(any('color choices were never made' in w
                                for w in caught.exception.warnings))
            slicer.assert_not_called()
            mj.record_filament_warning_acknowledgement(job_path)
            result = mj.slice_job(job_path)
            self.assertEqual(result, Path("result.gcode"))
            slicer.assert_called_once()

    def test_cli_reports_pending_then_the_accept_flag_slices(self):
        # The CLI cannot ask mid-run: a pending round is printed as a
        # packet and the job stops; the explicit resume flag records the
        # bound acceptance from the job's pending record and slices.
        job_path = self.work / "JOB.json"
        job_path.write_text(json.dumps({
            'downloaded_file': 'x.3mf',
            'filament_warnings_pending': {
                'warnings': ['plate 2: support usage is unknown'],
                'source_sha256': 'abc', 'selected_plates': ['2'],
                'at': '2026-11-10T00:00:00-0500'}}))
        pending = mj.FilamentWarningPending(['plate 2: support usage is unknown'],
                                            source_sha256='abc', selected_plates=[2])
        from contextlib import ExitStack
        common = [mock.patch.object(mj.printer_state, "load", return_value={}),
                  mock.patch.object(mj, "intake_unit_confirmation", return_value=True),
                  mock.patch.object(mj, "intake_color_choices", return_value=True)]
        with ExitStack() as stack:
            for patcher in common:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(
                sys, "argv", ['model_job.py', 'slice', str(job_path), '--approved']))
            stack.enter_context(mock.patch.object(mj, "slice_job", side_effect=pending))
            out = stack.enter_context(mock.patch('sys.stdout', new_callable=io.StringIO))
            mj.main()
        self.assertIn('FILAMENT_WARNINGS_PENDING', out.getvalue())
        self.assertIn('plate 2: support usage is unknown', out.getvalue())
        self.assertIn('--accept-filament-warnings', out.getvalue())
        saved = json.loads(job_path.read_text())
        self.assertNotIn('filament_warning_acknowledgement', saved)
        with ExitStack() as stack:
            for patcher in common:
                stack.enter_context(patcher)
            stack.enter_context(mock.patch.object(
                sys, "argv", ['model_job.py', 'slice', str(job_path), '--approved',
                              '--accept-filament-warnings']))
            slicer = stack.enter_context(
                mock.patch.object(mj, "slice_job", return_value='fake.gcode'))
            stack.enter_context(mock.patch('sys.stdout', new_callable=io.StringIO))
            mj.main()
        slicer.assert_called_once()
        saved = json.loads(job_path.read_text())
        ack = saved['filament_warning_acknowledgement']
        self.assertEqual(ack['warnings'], ['plate 2: support usage is unknown'])
        self.assertNotIn('filament_warnings_pending', saved)

    def test_choices_never_made_is_a_warning_round(self):
        # Behavior change (#1189, owner decision 2026-11-10): #1190 pinned
        # missing color decisions as a refusal at the choices gate, never a
        # warning round. They are now the #1190 round itself: the job stops
        # with the fallback named, and nothing slices until it is accepted.
        source = two_plate_source(self.work / "two.3mf")
        job_path = self.work / "JOB.json"
        job_path.write_text(json.dumps({
            'downloaded_file': str(source),
            'audit': {'plates': 2, 'size_x': '10', 'size_y': '10', 'size_z': '10'},
            'title': 'Needs choices'}))
        p1, p2, p3 = self._patches()
        with p1, p2, p3, mock.patch.object(mj, "slice_multi_plate") as slicer:
            with self.assertRaises(mj.FilamentWarningPending) as caught:
                mj.slice_job(job_path)
            slicer.assert_not_called()
            self.assertEqual(caught.exception.warnings,
                             ['color choices were never made for this print; every part prints '
                              "single-color from slot 1, the printer's first loaded slot"])
            saved = json.loads(job_path.read_text())
            self.assertEqual(saved['filament_warnings_pending']['warnings'],
                             caught.exception.warnings)
            self.assertNotIn('color_decisions', saved)



_SLOT1 = {'slot': 1, 'spoolman_id': 10, 'color': 'white', 'material': 'PLA'}
_SLOT2 = {'slot': 2, 'spoolman_id': 11, 'color': 'black', 'material': 'PLA'}


class WholeSourceMultiPlateTests(unittest.TestCase):
    """#1155: nothing in production writes job['selected_plates'], so a
    multi-plate source is sliced whole. Its usage findings over all plates
    still warn (warn-and-accept, #1189); the per-plate mapping rows never
    refuse a whole-source job."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _patches(self):
        return (mock.patch.object(mj, "ARCHIVE", self.work),
                mock.patch.object(mj.printer_state, "load", return_value=STATE),
                mock.patch.object(mj.printer_state, "check", return_value=(True, [], {})))

    def _job(self, path, decisions, **extra):
        job_path = self.work / "JOB.json"
        job_path.write_text(json.dumps({
            'downloaded_file': str(path),
            'audit': {'plates': 2, 'size_x': '10', 'size_y': '10', 'size_z': '10'},
            'title': 'Whole source', 'color_decisions': decisions, **extra}))
        return job_path

    def test_unknown_usage_on_any_plate_warns_then_slices_after_acceptance(self):
        path, manifest = _unknown_usage_manifest(self.work)
        job_path = self._job(path, {'source_sha256': manifest['source_sha256'],
                                    'mode': 'single_color', 'selection': _SLOT1})
        p1, p2, p3 = self._patches()
        with p1, p2, p3, mock.patch.object(
                mj, "slice_multi_plate", return_value=Path("result.gcode")) as slicer:
            with self.assertRaises(mj.FilamentWarningPending) as caught:
                mj.slice_job(job_path)
            slicer.assert_not_called()
            self.assertTrue(any('plate 2' in w and 'objects filament usage is unknown' in w
                                for w in caught.exception.warnings), caught.exception.warnings)
            self.assertEqual(caught.exception.selected_plates, [])
            mj.record_filament_warning_acknowledgement(job_path)
            self.assertEqual(mj.slice_job(job_path), Path("result.gcode"))
            slicer.assert_called_once()

    def test_known_usage_slices_with_no_warning_round(self):
        source = two_plate_source(self.work / "two.3mf")
        manifest = sc.inventory(source)
        job_path = self._job(source, {'source_sha256': manifest['source_sha256'],
                                      'mode': 'single_color', 'selection': _SLOT1})
        p1, p2, p3 = self._patches()
        with p1, p2, p3, mock.patch.object(
                mj, "slice_multi_plate", return_value=Path("result.gcode")) as slicer:
            self.assertEqual(mj.slice_job(job_path), Path("result.gcode"))
            slicer.assert_called_once()
        self.assertNotIn('filament_warnings_pending', json.loads(job_path.read_text()))

    def test_whole_source_job_never_hard_refuses_on_mapping_rows(self):
        # Filament 3 prints on plate 1 only through a height range; its
        # color question is the Knob part on plate 2, so plate 1 holds no
        # part of filament 3. With a plate selection that is a hard "no
        # validated slot mapping" refusal; a whole-source job, whose
        # questions were never scoped per plate, must slice as it does now.
        ranges = ('<objects><object id="1"><range min_z="0" max_z="5">'
                  '<option opt_key="extruder">3</option></range></object></objects>')
        source = write_source(self.work / "ranges.3mf",
                              [("Base", 1, None), ("Knob", 3, None)], [[0], [1]],
                              members={"Metadata/layer_config_ranges.xml": ranges})
        manifest = sc.inventory(source)
        self.assertIn(3, manifest['plate_usage']['1']['source_filaments'])
        parts = {p['name']: p for p in manifest['parts']}
        decisions = {'source_sha256': manifest['source_sha256'], 'schema': 'part_slots_v1',
                     'mode': 'per_part',
                     'choices': {'part:' + parts['Base']['key']: _SLOT1,
                                 'part:' + parts['Knob']['key']: _SLOT2}}
        self.assertEqual({row['key'] for row in sc.canonical_prompts(manifest)},
                         set(decisions['choices']))
        self.assertTrue(any('no validated slot mapping' in p for p in
                            sc.plate_readiness_problems(manifest, decisions, ['1'])))
        job_path = self._job(source, decisions, color_schema='part_slots_v1')
        p1, p2, p3 = self._patches()
        with p1, p2, p3, mock.patch.object(
                mj, "slice_multi_plate", return_value=Path("result.gcode")) as slicer:
            self.assertEqual(mj.slice_job(job_path), Path("result.gcode"))
            slicer.assert_called_once()
        self.assertNotIn('filament_warnings_pending', json.loads(job_path.read_text()))

class DiscordWarningRoundTests(unittest.TestCase):
    """The Discord color flow presents filament warnings once; only an
    explicit acceptance saves the choices, and cancelling saves nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _job(self):
        from .test_discord_color_intake import Adapter  # noqa: F401  (re-exported below)
        slice_info = """<config>
          <plate><metadata key="index" value="1"/><filament id="1" used_g="10" used_m="3"/><filament id="3" used_g="4" used_m="1"/></plate>
        </config>"""
        path = write_source(self.work / "disagree.3mf",
                            [("Base", 1, None), ("Lid", 2, None), ("Knob", 3, None)],
                            [[0, 1], [2]], slice_info=slice_info)
        manifest = sc.inventory(path)
        job_path = self.work / "JOB.json"
        job_path.write_text(json.dumps({
            'downloaded_file': str(path), 'selected_plates': [1],
            'color_schema': 'part_slots_v1', 'color_manifest': manifest,
            'title': 'Discord warning round'}))
        return job_path

    def test_accepting_the_warning_saves_choices_and_records_the_ack(self):
        from .test_color_intake import state as discord_state
        from .test_discord_color_intake import Adapter
        from print_handy.core.discord_color_intake import collect
        job_path = self._job()
        # Mode (per-part), one slot question per part group, then the
        # warning question: accept it.
        adapter = Adapter([0, 0, 0, 0])
        result = asyncio.run(collect(job_path, discord_state(), adapter,
                                     'offline', 'colors-warn-accept', timeout=1))
        self.assertEqual(result['status'], 'COLOR_CHOICES_SAVED')
        warning_questions = [q for q in adapter.questions
                             if 'Filament warnings' in str(q.get('question', ''))]
        self.assertEqual(len(warning_questions), 1)
        self.assertIn('disagree', warning_questions[0]['question'])
        saved = json.loads(job_path.read_text())
        self.assertIn('color_decisions', saved)
        ack = saved['filament_warning_acknowledgement']
        self.assertTrue(ack['acknowledged'])
        self.assertEqual(ack['via'], 'discord_color_choices')
        self.assertTrue(any('disagree' in w for w in ack['warnings']))

    def test_cancelling_the_warning_saves_nothing(self):
        from .test_color_intake import state as discord_state
        from .test_discord_color_intake import Adapter
        from print_handy.core.discord_color_intake import collect
        from print_handy.core.link_intake import PromptCancelled
        job_path = self._job()
        # Same questions, but the warning question is cancelled.
        adapter = Adapter([0, 0, 0, 1])
        with self.assertRaises(PromptCancelled):
            asyncio.run(collect(job_path, discord_state(), adapter,
                                'offline', 'colors-warn-decline', timeout=1))
        saved = json.loads(job_path.read_text())
        self.assertNotIn('color_decisions', saved)
        self.assertNotIn('filament_warning_acknowledgement', saved)


if __name__ == "__main__":
    unittest.main()
