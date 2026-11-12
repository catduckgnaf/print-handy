"""#1189 reopened (owner decision, catduck, 2026-11-10): the three refusals
#1190 kept as carve-outs become accept-and-print warnings. No filament
condition hard-blocks a print.

1. A used filament with no slot chosen prints from the slot already chosen
   for that source filament, else the slot chosen for the plate's first
   part with one, else the printer's active or first loaded slot.
2. A slot number past the printer's slot count prints from the active or
   first loaded slot.
3. Color choices never made print every part single-color from the active
   or first loaded slot, like a recorded single-color decision.

Each fallback is deterministic and named in the warning text; the warning
joins the #1190 round (FilamentWarningPending), the acceptance binds to the
exact warnings, the source hash and the plates, nothing slices without it,
and a reslice presents the warnings again. The substitution happens in the
decisions before slicing, so the G-code names only slots the printer has.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy import printers
from print_handy.core import model_job as mj
from print_handy.core import source_colors as sc
from tests.core.test_plate_usage import write_source, two_plate_source


def _slot(n, color, spool):
    return {'color': color, 'material': 'PLA', 'spoolman_id': spool,
            'physical_color_verified': True}


# The owner's printer: AD5X, four IFS slots, one shared nozzle.
STATE = {'bed_mm': [220, 220], 'max_print_height_mm': 250,
         'nozzle': {'diameter_mm': 0.4},
         'spool': {'name': 'PLA', 'material': 'PLA', 'density': 1.24},
         'temps_c': {'nozzle': 210, 'bed': 60},
         'filament_slots': {'1': _slot(1, 'white', 10), '2': _slot(2, 'black', 11),
                            '3': _slot(3, 'red', 12), '4': _slot(4, 'blue', 13)}}


def _sel(slot):
    rec = STATE['filament_slots'][str(slot)]
    return {'slot': slot, 'color': rec['color'], 'material': 'PLA', 'spoolman_id': rec['spoolman_id']}


class FallbackSlotTests(unittest.TestCase):
    """Which slot the fallback names, from the hardware record print start uses."""

    def test_active_slot_wins_when_it_is_loaded(self):
        self.assertEqual(mj.filament_fallback_slot({**STATE, 'active_slot': 3}, 4),
                         (3, "slot 3, the printer's active slot"))

    def test_first_loaded_slot_when_no_active_slot(self):
        state = {**STATE, 'filament_slots': {'2': _slot(2, 'black', 11), '4': _slot(4, 'blue', 13)}}
        self.assertEqual(mj.filament_fallback_slot(state, 4),
                         (2, "slot 2, the printer's first loaded slot"))

    def test_active_slot_that_is_not_loaded_is_not_used(self):
        state = {**STATE, 'active_slot': 3,
                 'filament_slots': {'2': _slot(2, 'black', 11), '4': _slot(4, 'blue', 13)}}
        self.assertEqual(mj.filament_fallback_slot(state, 4)[0], 2)

    def test_slot_past_the_limit_is_never_the_fallback(self):
        state = {**STATE, 'active_slot': 6,
                 'filament_slots': {'6': _slot(6, 'x', 1), '3': _slot(3, 'red', 12)}}
        self.assertEqual(mj.filament_fallback_slot(state, 4)[0], 3)

    def test_no_loaded_slot_information_uses_slot_1_and_says_so(self):
        state = {'filament_slots': {}, 'spool': {}}
        slot, phrase = mj.filament_fallback_slot(state, 4)
        self.assertEqual(slot, 1)
        self.assertIn('no loaded-slot information', phrase)


class FallbackRuleTests(unittest.TestCase):
    """filament_fallbacks pins each rule and is deterministic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)
        self.manifest = sc.inventory(two_plate_source(self.work / "two.3mf"))
        self.parts = {p['name']: p for p in self.manifest['parts']}

    def tearDown(self):
        self.tmp.cleanup()

    def _key(self, name):
        return 'part:' + self.parts[name]['key']

    def _decisions(self, choices):
        return {'source_sha256': self.manifest['source_sha256'], 'schema': 'part_slots_v1',
                'mode': 'per_part', 'choices': choices}

    def test_no_condition_returns_the_decisions_unchanged(self):
        decisions = self._decisions({self._key(n): _sel(i + 1)
                                     for i, n in enumerate(('Base', 'Lid', 'Knob'))})
        self.assertEqual(mj.filament_fallbacks(self.manifest, decisions, ['1', '2'], STATE, 4),
                         (decisions, []))

    def test_unmapped_part_takes_the_base_part_slot(self):
        # Plate 1 holds Base (filament 1) and Lid (filament 2); Lid has no
        # slot: it prints from the slot chosen for Base, the plate's first part.
        decisions = self._decisions({self._key('Base'): _sel(3)})
        effective, warnings = mj.filament_fallbacks(self.manifest, decisions, ['1'], STATE, 4)
        self.assertEqual(effective['choices'][self._key('Lid')]['slot'], 3)
        self.assertEqual(warnings, ['plate 1: source filament 2 is used but has no slot chosen '
                                    '(part Lid); it prints from slot 3, the slot chosen for part Base'])
        self.assertEqual(effective['fallback']['original'], decisions)
        self.assertEqual(effective['fallback']['added_keys'], [self._key('Lid')])
        self.assertEqual(sc.plate_readiness_problems(self.manifest, effective, ['1'], slot_capacity=4), [])

    def test_unmapped_part_with_no_part_slot_on_the_plate_takes_the_fallback_slot(self):
        decisions = self._decisions({self._key('Base'): _sel(1), self._key('Lid'): _sel(2)})
        effective, warnings = mj.filament_fallbacks(self.manifest, decisions, ['2'],
                                                    {**STATE, 'active_slot': 4}, 4)
        self.assertEqual(effective['choices'][self._key('Knob')]['slot'], 4)
        self.assertEqual(warnings, ['plate 2: source filament 3 is used but has no slot chosen '
                                    "(part Knob); it prints from slot 4, the printer's active slot"])

    def test_unmapped_part_takes_its_own_filament_slot_when_a_sibling_has_one(self):
        path = write_source(self.work / "siblings.3mf",
                            [("Base", 1, None), ("Lid", 1, None), ("Knob", 2, None)], [[0, 1, 2]])
        manifest = sc.inventory(path)
        parts = {p['name']: p for p in manifest['parts']}
        decisions = {'source_sha256': manifest['source_sha256'], 'schema': 'part_slots_v1',
                     'mode': 'per_part', 'choices': {'part:' + parts['Knob']['key']: _sel(1),
                                                     'part:' + parts['Lid']['key']: _sel(4)}}
        effective, warnings = mj.filament_fallbacks(manifest, decisions, ['1'], STATE, 4)
        self.assertEqual(effective['choices']['part:' + parts['Base']['key']]['slot'], 4)
        self.assertIn('slot 4, the slot already chosen for source filament 1', warnings[0])

    def test_slot_past_the_limit_takes_the_fallback_slot(self):
        decisions = self._decisions({self._key('Base'): _sel(1), self._key('Lid'): _sel(2),
                                     self._key('Knob'): {**_sel(4), 'slot': 7}})
        effective, warnings = mj.filament_fallbacks(self.manifest, decisions, None, STATE, 4)
        self.assertEqual(effective['choices'][self._key('Knob')], _sel(1))
        self.assertEqual(warnings, ['slot 7 is chosen for part Knob, but the printer has only 4 '
                                    "slots; it prints from slot 1, the printer's first loaded slot instead"])

    def test_choices_never_made_print_single_color_from_the_fallback_slot(self):
        effective, warnings = mj.filament_fallbacks(self.manifest, None, None,
                                                    {**STATE, 'active_slot': 2}, 4, choices_needed=True)
        self.assertEqual(effective['mode'], 'single_color')
        self.assertEqual(effective['selection'], _sel(2))
        self.assertEqual(warnings, ['color choices were never made for this print; every part prints '
                                    "single-color from slot 2, the printer's active slot"])
        # Not needed (a source with nothing to choose): nothing is invented.
        self.assertEqual(mj.filament_fallbacks(self.manifest, None, None, STATE, 4), (None, []))

    def test_fallback_is_deterministic_and_identified_by_the_original(self):
        decisions = self._decisions({self._key('Base'): _sel(3)})
        first = mj.filament_fallbacks(self.manifest, decisions, ['1'], STATE, 4)
        again = mj.filament_fallbacks(self.manifest, first[0], ['1'], STATE, 4)
        self.assertEqual(first, again)
        self.assertEqual(mj.decisions_digest(first[0]), mj.decisions_digest(decisions))

    def test_decisions_bound_to_another_source_are_never_repaired(self):
        decisions = self._decisions({self._key('Base'): {**_sel(1), 'slot': 9}})
        decisions['source_sha256'] = 'another'
        self.assertEqual(mj.filament_fallbacks(self.manifest, decisions, ['1'], STATE, 4),
                         (decisions, []))
        with self.assertRaisesRegex(RuntimeError, 'print readiness blocked: .*not bound'):
            mj._enforce_filament_readiness({}, self.manifest, decisions, ['1'],
                                           slot_capacity=4, state=STATE)


class SliceJobFallbackTests(unittest.TestCase):
    """Each condition through slice_job: pending, accept, slices with the named fallback."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, job, state=STATE):
        job_path = self.work / "JOB.json"
        job_path.write_text(json.dumps({
            'audit': {'plates': 2, 'size_x': '10', 'size_y': '10', 'size_z': '10'},
            'title': 'Fallback', **job}))
        captured = []

        def fake_slice(job_, job_path_, src, manifest, decisions, st, brim):
            captured.append((json.loads(json.dumps(job_)), decisions))
            return Path("result.gcode")

        patches = (mock.patch.object(mj, "ARCHIVE", self.work),
                   mock.patch.object(mj.printer_state, "load", return_value=state),
                   mock.patch.object(mj.printer_state, "check", return_value=(True, [], {})),
                   mock.patch.object(mj, "slice_multi_plate", side_effect=fake_slice))
        return job_path, captured, patches

    def _round(self, job_path, captured, patches):
        """Pending with nothing sliced, then accept, then one slice; returns the pending warnings."""
        with patches[0], patches[1], patches[2], patches[3]:
            with self.assertRaises(mj.FilamentWarningPending) as caught:
                mj.slice_job(job_path)
            self.assertEqual(captured, [], 'nothing slices without an acceptance')
            saved = json.loads(job_path.read_text())
            self.assertEqual(saved['filament_warnings_pending']['warnings'], caught.exception.warnings)
            mj.record_filament_warning_acknowledgement(job_path)
            self.assertEqual(mj.slice_job(job_path), Path("result.gcode"))
            self.assertEqual(len(captured), 1)
            # A reslice presents the warnings again: the acceptance was used.
            with self.assertRaises(mj.FilamentWarningPending) as again:
                mj.slice_job(job_path)
            self.assertEqual(again.exception.warnings, caught.exception.warnings)
            self.assertEqual(len(captured), 1)
        return caught.exception

    def test_used_filament_with_no_slot_chosen(self):
        source = two_plate_source(self.work / "two.3mf")
        manifest = sc.inventory(source)
        parts = {p['name']: p for p in manifest['parts']}
        # Plate 1 is selected; its Lid part was never given a slot.
        decisions = {'source_sha256': manifest['source_sha256'], 'schema': 'part_slots_v1',
                     'mode': 'per_part', 'choices': {'part:' + parts['Base']['key']: _sel(2)}}
        job_path, captured, patches = self._run({
            'downloaded_file': str(source), 'selected_plates': [1],
            'color_schema': 'part_slots_v1', 'color_decisions': decisions})
        pending = self._round(job_path, captured, patches)
        self.assertEqual(pending.warnings, ['plate 1: source filament 2 is used but has no slot chosen '
                                            '(part Lid); it prints from slot 2, the slot chosen for part Base'])
        self.assertEqual(pending.selected_plates, ['1'])
        self.assertEqual(pending.source_sha256, manifest['source_sha256'])
        sliced_job, sliced = captured[0]
        self.assertEqual(sliced['choices']['part:' + parts['Lid']['key']]['slot'], 2)
        self.assertEqual(sliced_job['filament_fallback']['warnings'], pending.warnings)
        # JOB.json keeps the operator's own decisions; the artifact binds to them.
        self.assertEqual(json.loads(job_path.read_text())['color_decisions'], decisions)
        self.assertEqual(mj.decisions_digest(sliced), mj.decisions_digest(decisions))

    def test_slot_past_what_the_printer_has_loaded(self):
        source = two_plate_source(self.work / "two.3mf")
        manifest = sc.inventory(source)
        parts = {p['name']: p for p in manifest['parts']}
        decisions = {'source_sha256': manifest['source_sha256'], 'schema': 'part_slots_v1',
                     'mode': 'per_part',
                     'choices': {'part:' + parts['Base']['key']: _sel(1),
                                 'part:' + parts['Lid']['key']: _sel(2),
                                 'part:' + parts['Knob']['key']: {**_sel(4), 'slot': 5}}}
        job_path, captured, patches = self._run({
            'downloaded_file': str(source), 'color_schema': 'part_slots_v1',
            'color_decisions': decisions}, state={**STATE, 'active_slot': 3})
        pending = self._round(job_path, captured, patches)
        self.assertEqual(pending.warnings, ['slot 5 is chosen for part Knob, but the printer has only 4 '
                                            "slots; it prints from slot 3, the printer's active slot instead"])
        _, sliced = captured[0]
        self.assertEqual(sliced['choices']['part:' + parts['Knob']['key']], _sel(3))
        self.assertEqual({row['slot'] for row in sliced['choices'].values()}, {1, 2, 3})

    def test_color_choices_never_made(self):
        # Before #1189 this source refused at the choices gate ("part/color
        # choices required"); it now prints single-color from the fallback.
        source = two_plate_source(self.work / "two.3mf")
        job_path, captured, patches = self._run({'downloaded_file': str(source)})
        pending = self._round(job_path, captured, patches)
        self.assertEqual(pending.warnings, ['color choices were never made for this print; every part '
                                            "prints single-color from slot 1, the printer's first loaded slot"])
        sliced_job, sliced = captured[0]
        self.assertEqual(sliced['mode'], 'single_color')
        self.assertEqual(sliced['selection'], _sel(1))
        # Like a recorded one-color decision: the job's slot selection names
        # the slot, and the recorded recipe matches the job print start reads.
        self.assertEqual(sliced_job['filament_selection'], _sel(1))
        saved = json.loads(job_path.read_text())
        self.assertNotIn('color_decisions', saved)
        self.assertEqual(saved['filament_selection'], _sel(1))
        with mock.patch.object(mj.printer_state, "load", return_value=STATE):
            self.assertFalse(mj._recipe_changed(sliced_job, saved))

    def test_ad5x_slot_past_4_falls_back_to_a_loaded_slot_and_passes_tool_verification(self):
        # The owner's printer: a stale choice names IFS slot 6 on the AD5X.
        adapter = printers.current()
        self.assertEqual((adapter.key, adapter.slot_count), ('flashforge.ad5x', 4))
        source = write_source(self.work / "one.3mf", [("Base", 1, None), ("Lid", 2, None)], [[0, 1]])
        manifest = sc.inventory(source)
        parts = {p['name']: p for p in manifest['parts']}
        decisions = {'source_sha256': manifest['source_sha256'], 'schema': 'part_slots_v1',
                     'mode': 'per_part',
                     'choices': {'part:' + parts['Base']['key']: _sel(1),
                                 'part:' + parts['Lid']['key']: {**_sel(2), 'slot': 6}}}
        state = {**STATE, 'active_slot': 4}
        job_path, captured, patches = self._run({
            'downloaded_file': str(source), 'color_schema': 'part_slots_v1',
            'color_decisions': decisions, 'audit': {'plates': 1, 'size_x': '10', 'size_y': '10',
                                                    'size_z': '10'}}, state=state)
        pending = self._round(job_path, captured, patches)
        self.assertIn("it prints from slot 4, the printer's active slot instead", pending.warnings[0])
        _, sliced = captured[0]
        view = mj._per_part_view(manifest, sliced)
        slots = sorted(set(mj._part_slots(view, manifest).values()))
        self.assertEqual(slots, [1, 4])
        mapping = {str(tool): slot for tool, slot in enumerate(slots)}
        # The G-code selects one tool per mapped slot; print start's tool
        # verification accepts it because every tool maps to a loaded,
        # verified IFS slot the printer has.
        gcode = ''.join(f'T{tool}\nG1 X10 Y10\n' for tool in range(len(slots)))
        tools = {str(t): mapping[str(t)] for t in sorted(mj._gcode_tools(gcode) | {0})}
        self.assertEqual(adapter.normalize_tool_mapping(tools, state), {'0': 1, '1': 4})
        # The operator's original slot would have failed that verification.
        with self.assertRaises(RuntimeError):
            adapter.normalize_tool_mapping({'0': 1, '1': 6}, state)


class DiscordFallbackRoundTests(unittest.TestCase):
    """The Discord color flow shows a fallback warning once, as the #1190 round."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_color_flow_presents_the_fallback_once_and_saves_the_operators_choices(self):
        from .test_color_intake import state as discord_state
        from .test_discord_color_intake import Adapter
        from print_handy.core.discord_color_intake import collect
        path = two_plate_source(self.work / "two.3mf")
        manifest = sc.inventory(path)
        job_path = self.work / "JOB.json"
        job_path.write_text(json.dumps({
            'downloaded_file': str(path), 'selected_plates': [1],
            'color_schema': 'part_slots_v1', 'color_manifest': manifest,
            'title': 'Discord fallback round'}))
        # The usage analysis says plate 1 also uses filament 3 (a slicer
        # record the questions do not cover): the readiness rows find it with
        # no slot, and the fallback names the slot it prints from.
        with mock.patch.object(sc, 'unmapped_plate_filaments',
                               side_effect=lambda m, d, s: [('1', 3, [])]
                               if 'filament:3' not in (d.get('choices') or {}) else []):
            adapter = Adapter([0, 0, 0, 0])
            result = asyncio.run(collect(job_path, discord_state(), adapter,
                                         'offline', 'colors-fallback', timeout=1))
        self.assertEqual(result['status'], 'COLOR_CHOICES_SAVED')
        rounds = [q for q in adapter.questions if 'Filament warnings' in str(q.get('question', ''))]
        self.assertEqual(len(rounds), 1)
        self.assertIn('source filament 3 is used but has no slot chosen', rounds[0]['question'])
        self.assertIn("the printer's first loaded slot", rounds[0]['question'])
        saved = json.loads(job_path.read_text())
        self.assertNotIn('fallback', saved['color_decisions'])
        self.assertNotIn('filament:3', saved['color_decisions']['choices'])
        ack = saved['filament_warning_acknowledgement']
        self.assertEqual(ack['via'], 'discord_color_choices')
        self.assertTrue(any('source filament 3' in w for w in ack['warnings']))


if __name__ == "__main__":
    unittest.main()


class MaterialRoundTests_1336(SliceJobFallbackTests):
    """#1336: a saved selection the slot no longer matches, and abrasive filament on a brass
    nozzle, join the #1190 round instead of refusing (both refused on base 1d452a58)."""

    def test_saved_selection_drift_warns_and_slices_for_what_the_slot_holds_now(self):
        source = write_source(self.work / "one.3mf", [("Base", 1, None)], [[0]])
        swapped = {**STATE, 'filament_slots': {**STATE['filament_slots'],
                                               '2': {'color': 'green', 'material': 'PETG', 'spoolman_id': 99,
                                                     'physical_color_verified': True}}}
        manifest = sc.inventory(source)
        decisions = {'source_sha256': manifest['source_sha256'], 'mode': 'single_color', 'selection': _sel(2)}
        job_path, captured, patches = self._run({'downloaded_file': str(source), 'filament_selection': _sel(2),
                                                 'color_decisions': decisions,
                                                 'audit': {'plates': 1, 'size_x': '10', 'size_y': '10',
                                                           'size_z': '10'}}, state=swapped)
        drift = ('slot 2 now holds green PETG (spool #99); the job chose black PLA (spool #11). '
                 'Accepting slices it for what slot 2 holds now')
        with patches[0], patches[1], patches[2], patches[3],                 mock.patch("print_handy.core.orca_adapter.slice_model", side_effect=AssertionError("must not slice")):
            with self.assertRaises(mj.FilamentWarningPending) as caught:
                mj.slice_job(job_path)
            self.assertIn(drift, caught.exception.warnings)
            mj.record_filament_warning_acknowledgement(job_path)
            try:
                mj.slice_job(job_path)
            except AssertionError as reached:  # the accepted run got as far as slicing
                self.assertEqual(str(reached), "must not slice")
        saved = json.loads(job_path.read_text())
        self.assertEqual((saved['filament_selection']['spoolman_id'], saved['filament_selection']['material']),
                         (99, 'PETG'))

    def test_abrasive_on_brass_warns_bluntly_and_the_accepted_slice_runs(self):
        from print_handy.core import material_accept
        source = two_plate_source(self.work / "two.3mf")
        wood = {**STATE, 'nozzle': {'diameter_mm': 0.4, 'type': 'brass'},
                'filament_slots': {**STATE['filament_slots'],
                                   '1': {**_slot(1, 'white', 10), 'material': 'Wood PLA'}}}
        job_path, captured, patches = self._run({'downloaded_file': str(source),
                                                 'filament_selection': {**_sel(1), 'material': 'Wood PLA'}},
                                                state=wood)
        granted = []
        real = patches[3]
        patches = patches[:3] + (mock.patch.object(
            mj, "slice_multi_plate",
            side_effect=lambda *a: (granted.append(material_accept.accepted('abrasive|Wood PLA|brass')),
                                    real.kwargs['side_effect'](*a))[1]),)
        pending = self._round(job_path, captured, patches)
        self.assertIn('Wood PLA is abrasive and the fitted nozzle is brass: this filament will wear out the '
                      'brass nozzle', pending.warnings)
        # The pre-slice guard (inside the slicer) is told the acceptance for exactly this run.
        self.assertEqual(granted, [True])
        self.assertFalse(material_accept.accepted('abrasive|Wood PLA|brass'))
