"""Scale and copy-count choices offered in Discord. No slicer: fit checks are stubbed."""
import unittest

from print_handy.core import orca_adapter as oa
from print_handy.core import quantity_selection as qs
import math


class FeatureClearanceTests(unittest.TestCase):
    def test_margin_applied_once_and_features_add_to_edge_only(self):
        state = {'bed_mm': [220, 220], 'max_print_height_mm': 220,
                 'resolved_hop_types': ['Normal Lift'],
                 'brim_policy': {'type': 'outer_only', 'width_mm': 2}}
        self.assertEqual(oa.required_feature_edge(state), 2)
        for brim_type in ('outer_only', 'brim_ears', 'outer_and_inner', 'inner_only'):
            self.assertEqual(oa.required_feature_edge(
                {'brim_policy': {'type': brim_type, 'width_mm': 2}}), 2)
        self.assertEqual(oa.required_feature_edge(
            {'brim_policy': {'type': 'no_brim', 'width_mm': 2}}), 0)
        from print_handy.core.split_fit import _declared_exclusions
        from unittest.mock import patch
        fake_fact = type('Fact', (), {'value': ((0, 0), (10, 0), (10, 20), (0, 20))})()
        fake_model = type('Model', (), {'adapter': type('Adapter', (), {
            'facts': {'bed_exclude_area': fake_fact}})})()
        with patch('print_handy.core.split_fit.settings.current', return_value={'printer': {'model': 'x'}}), \
             patch('print_handy.printers.MODELS', {'x': fake_model}):
            self.assertEqual(_declared_exclusions(), [(0, 0, 10, 20)])
        self.assertEqual(oa._usable(state), (214.0, 214.0, 220.0))
        self.assertEqual(oa._usable(state, single=False), (210.0, 210.0, 220.0))

    def test_feature_sweep_and_brim_are_added_once_to_distinct_margins(self):
        state = {'bed_mm': [220, 220], 'max_print_height_mm': 220,
                 'resolved_hop_types': ['Spiral Lift'], 'z_hop': 0.4, 'travel_slope': 3,
                 'brim_policy': {'type': 'outer_only', 'width_mm': 2}}
        sweep = 0.4 / math.tan(math.radians(3)) / (2 * math.pi)
        required = 2 + sweep
        self.assertAlmostEqual(oa.required_feature_edge(state), required)
        self.assertAlmostEqual(oa._usable(state)[0], 220 - 2 * (1 + required))
        self.assertAlmostEqual(oa._usable(state, single=False)[0], 220 - 2 * (3 + required))
        self.assertAlmostEqual(oa._usable(state, single=False)[0], 220 - 2*3 - 2*required)
        self.assertAlmostEqual(oa.resolved_travel_sweep({'resolved_hop_types': ['Normal Lift'], 'z_hop': 0.4, 'travel_slope': 3}), 0)

    def test_scale_fit_keeps_10mm_part_gap_distinct_from_edge_clearance(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.object(oa, 'source_triangles', return_value=None), \
             patch.object(oa, 'orca_plate_capacity', return_value=1) as capacity:
            oa.verified_scale_choices(__import__('pathlib').Path(tmp) / 'tiny.stl',
                                     {'bed_mm': [220, 220], 'resolved_hop_types': ['Normal Lift']})
        self.assertEqual(capacity.call_args.kwargs['copy_limit'], 1)
        self.assertNotIn('spacing_mm', capacity.call_args.kwargs)  # 10 mm packing gap belongs only to multi-copy layout.
        self.assertIsNone(capacity.call_args.kwargs['brim_policy'])

    def test_fit_question_names_part_extent_options_and_future_cut_issue(self):
        from print_handy.core.prepared_intake import fit_question
        text = fit_question('Model', {'scales': [0.95], 'parts': [{'name': 'Stand', 'extent_mm': [130, 230, 30],
                             'orientations_checked': ['authored XY', 'best yaw 90°']} ]})
        self.assertIn('Stand (130 × 230 × 30 mm)', text)
        self.assertNotIn('Orientations checked', text)
        self.assertIn('95%', text)
        self.assertNotIn('issues/83', text)  # no GitHub links or option essays in chat

    def test_fit_question_explains_an_individually_oversized_part(self):
        from print_handy.core.prepared_intake import fit_question
        text = fit_question('Model', {'scales': [0.956], 'parts': [{
            'name': 'Stand', 'extent_mm': [130, 230, 30], 'fits_full_size': False,
            'blocking_reason': 'footprint 130 x 230 mm exceeds the usable 204 x 204 mm bed '
                               'at every checked yaw (best 130 x 230 mm at 0 degrees)'}]})
        self.assertIn('does not fit a plate on its own', text)
        self.assertIn('does not shrink this part', text)
        self.assertIn('Supports can help overhangs but do not make a part smaller', text)
        self.assertIn('95.6%', text)  # shrink stays a separate, labelled option

    def test_fit_question_does_not_call_a_yaw_fitting_part_blocking(self):
        from print_handy.core.prepared_intake import fit_question
        text = fit_question('Model', {'scales': [], 'parts': [{
            'name': 'Rail', 'extent_mm': [230, 50, 20], 'fits_full_size': True,
            'blocking_reason': None}]})
        self.assertNotIn('does not fit a plate on its own', text)
        self.assertNotIn('does not shrink this part', text)

    def test_painted_scale_search_uses_small_coarse_ladder(self):
        from print_handy.core.model_job import PAINTED_SCALE_STEPS
        self.assertLessEqual(len(PAINTED_SCALE_STEPS), 12)
        self.assertEqual(PAINTED_SCALE_STEPS[0], 1.0)
        self.assertEqual(PAINTED_SCALE_STEPS[-1], 0.5)

    def test_painted_scale_tries_the_exact_ceiling_before_the_ladder(self):
        # The confirm prompt's Scale button records arbitrary cumulative
        # scales (99%, 105%); the painted fit must try that exact size
        # first, not snap to the nearest ladder step below it.
        from unittest.mock import patch
        from print_handy.core import color_geometry, model_job as mj

        def fake_export(base):
            def export(src, out, view, object_ids=None, scale=1.0, tower_mm=None):
                return None, {'parts': [{'bounds_mm': [[0, base * scale], [0, 10], [0, 10]]}]}
            return export

        limit = mj.BED_LIMIT_MM
        with patch.object(color_geometry, 'export_color_geometry', fake_export(limit)):
            # 99% fits as-is: the exact ceiling wins over the 95% ladder step.
            self.assertEqual(mj.painted_plate_scale('src', None, {'1'}, ceiling=0.99), 0.99)
            # Below the ladder floor the exact ceiling is still tried.
            self.assertEqual(mj.painted_plate_scale('src', None, {'1'}, ceiling=0.4), 0.4)
        with patch.object(color_geometry, 'export_color_geometry', fake_export(limit / 0.97)):
            # 99% does not fit but 95% does: the ladder fallback still applies.
            self.assertEqual(mj.painted_plate_scale('src', None, {'1'}, ceiling=0.99), 0.95)

    def test_largest_fitting_scale_searches_below_the_refused_ceiling(self):
        from unittest.mock import patch
        probed = []

        def capacity(src, state, scale=1.0, **kw):
            probed.append(scale)
            return 1 if scale <= 1.04 else 0

        with patch.object(oa, 'orca_plate_capacity', capacity):
            self.assertEqual(oa.largest_fitting_scale('src', {}, 1.10), 1.04)
        self.assertNotIn(1.10, probed)  # the refused ceiling is never re-probed
        with patch.object(oa, 'orca_plate_capacity', lambda *a, **kw: 0):
            self.assertIsNone(oa.largest_fitting_scale('src', {}, 1.10))
        # A fit that only exists just above the 50% floor is still found.
        with patch.object(oa, 'orca_plate_capacity',
                          lambda src, state, scale=1.0, **kw: 1 if scale <= 0.505 else 0):
            self.assertEqual(oa.largest_fitting_scale('src', {}, 1.10), 0.505)
        # Below the 50% floor there is no search at all.
        self.assertIsNone(oa.largest_fitting_scale('src', {}, 0.4))

    @staticmethod
    def _linear_walk_oracle(fits, ceiling):
        """The pre-bisection search (top-down 1% walk, then 0.1% bisect), kept as the oracle."""
        import math
        top = math.floor((float(ceiling) - 1e-9) * 1000)
        if top < 500:
            return None
        if fits(top):
            return top / 1000.0
        coarse = top - top % 10
        if coarse == top:
            coarse -= 10
        found = None
        step = coarse
        while step >= 500:
            if fits(step):
                found = step
                break
            step -= 10
        if found is None:
            return None
        lo, hi = found, min(found + 10, top)
        while hi - lo > 1:
            middle = (lo + hi) // 2
            if fits(middle):
                lo = middle
            else:
                hi = middle
        return lo / 1000.0

    def test_largest_fitting_scale_matches_the_linear_walk_with_far_fewer_probes(self):
        from unittest.mock import patch
        worst_new = worst_old = 0
        for ceiling in (1.10, 1.5, 1.05, 1.0, 0.75, 0.52, 0.501):
            top = int((ceiling - 1e-9) * 1000)
            for limit in list(range(490, top + 2)):
                old_probes, new_probes = [], []

                def capacity(src, state, scale=1.0, **kw):
                    new_probes.append(scale)
                    return 1 if round(scale * 1000) <= limit else 0

                def old_fits(step):
                    old_probes.append(step)
                    return step <= limit

                expected = self._linear_walk_oracle(old_fits, ceiling)
                with patch.object(oa, 'orca_plate_capacity', capacity):
                    got = oa.largest_fitting_scale('src', {}, ceiling)
                self.assertEqual(got, expected, (ceiling, limit))
                worst_new = max(worst_new, len(new_probes))
                worst_old = max(worst_old, len(old_probes))
        # 1.5 x ceiling: the walk needs up to ~105 probes; the bisect at most 14.
        self.assertLessEqual(worst_new, 14)
        self.assertGreater(worst_old, 50)

    def test_largest_fitting_scale_rechecks_a_flaky_boundary_and_walks_up(self):
        from unittest.mock import patch
        seen = {}

        def capacity(src, state, scale=1.0, **kw):
            step = round(scale * 1000)
            seen[step] = seen.get(step, 0) + 1
            if step == 900:  # nondeterministic: refused the first time, fits the second
                return 0 if seen[step] == 1 else 1
            return 1 if step <= 920 else 0

        with patch.object(oa, 'orca_plate_capacity', capacity):
            got = oa.largest_fitting_scale('src', {}, 1.10)
        # The bisect alone would settle below 900; the second look walks up to 92%.
        self.assertEqual(got, 0.92)

    def test_largest_fitting_scale_raises_when_the_slicer_keeps_failing(self):
        from unittest.mock import patch
        calls = []

        def broken(src, state, scale=1.0, **kw):
            calls.append(scale)
            raise RuntimeError('orca aborted')

        with patch.object(oa, 'orca_plate_capacity', broken):
            with self.assertRaises(RuntimeError):
                oa.largest_fitting_scale('src', {}, 1.10)
        self.assertEqual(len(calls), 2)  # one retry, then the failure surfaces

        flaky = {'n': 0}

        def once(src, state, scale=1.0, **kw):
            flaky['n'] += 1
            if flaky['n'] == 1:
                raise RuntimeError('one-off abort')
            return 1 if scale <= 1.04 else 0

        with patch.object(oa, 'orca_plate_capacity', once):
            self.assertEqual(oa.largest_fitting_scale('src', {}, 1.10), 1.04)

    def test_fine_search_deferred_for_painted_multi_plate_jobs(self):
        from unittest.mock import patch
        with patch.object(oa, '_source_plate_count', return_value=2), \
             patch.object(oa, 'source_triangles', return_value=None):
            self.assertEqual(oa.verified_scale_choices('painted.3mf', {'resolved_hop_types': ['Normal Lift']}), [])

    def test_explicit_brim_policy_is_selected_for_tight_fit(self):
        from unittest.mock import patch
        with patch.object(oa, 'verified_scale_choices', side_effect=lambda _s, _st, brim_policy=None: [0.95] if brim_policy['width_mm'] == 1 else []), \
             patch.object(oa, 'rotation_alternatives', return_value=[]):
            options = oa.fit_options('stand.stl', {'z_hop_types': ['Normal Lift']})
        self.assertEqual(options['brim_policy'], {'type': 'outer_only', 'width_mm': 1.0, 'label': 'Outer-only brim, 1 mm'})

    def test_operator_brim_override_is_respected(self):
        from unittest.mock import patch
        with patch.object(oa, 'verified_scale_choices', return_value=[0.9]) as verify, \
             patch.object(oa, 'rotation_alternatives', return_value=[]):
            options = oa.fit_options('stand.stl', {'z_hop_types': ['Normal Lift']}, brim_override=4)
        self.assertEqual(options['brim_policy']['width_mm'], 4.0)
        self.assertEqual(verify.call_args.kwargs['brim_policy']['width_mm'], 4.0)

    def test_spiral_sweep_is_radius_formula(self):
        state = {'resolved_hop_types': ['Spiral Lift'], 'z_hop': 0.4, 'travel_slope': 3}
        import math
        self.assertAlmostEqual(oa.resolved_travel_sweep(state), 0.4 / math.tan(math.radians(3)) / (2 * math.pi))


class ScaleLabelTests(unittest.TestCase):
    def test_whole_and_fractional_percent(self):
        self.assertEqual(oa.scale_label(1), "100%")
        self.assertEqual(oa.scale_label(0.75), "75%")
        self.assertEqual(oa.scale_label(0.986), "98.6%")


class ScaleLadderTests(unittest.TestCase):
    def test_too_small_to_offer(self):
        self.assertEqual(oa._scale_ladder(0.51), [])

    def test_large_maximum_offers_75_and_50(self):
        self.assertEqual(oa._scale_ladder(0.9), [0.9, 0.75, 0.5])

    def test_medium_sits_between_max_and_half(self):
        self.assertEqual(oa._scale_ladder(0.7), [0.7, 0.6, 0.5])

    def test_choices_never_repeat(self):
        ladder = oa._scale_ladder(0.52)
        self.assertEqual(len(set(ladder)), 3)


class ConfirmScaleTests(unittest.TestCase):
    def test_finds_largest_fitting_tenth_of_a_percent(self):
        self.assertEqual(oa._confirm_scale(lambda s: s <= 0.873, 0.95), 0.873)

    def test_bound_that_fits_is_returned_without_search(self):
        calls = []
        self.assertEqual(oa._confirm_scale(lambda s: calls.append(s) or True, 0.9), 0.9)
        self.assertEqual(calls, [0.9])

    def test_bound_under_half_size_gives_up(self):
        self.assertIsNone(oa._confirm_scale(lambda s: True, 0.4))

    def test_one_orca_crash_is_retried(self):
        seen = set()

        def flaky(scale):
            if scale not in seen:
                seen.add(scale)
                raise RuntimeError("bad_rational")
            return True
        self.assertEqual(oa._confirm_scale(flaky, 0.8), 0.8)

    def test_broken_orca_is_not_reported_as_does_not_fit(self):
        def broken(scale):
            raise RuntimeError("orca died")
        with self.assertRaises(RuntimeError):
            oa._confirm_scale(broken, 0.9)


class QuantityTests(unittest.TestCase):
    def test_buttons(self):
        self.assertEqual(oa.quantity_buttons(0), [1])
        self.assertEqual(oa.quantity_buttons(2), [1, 2])
        self.assertEqual(oa.quantity_buttons(6), [1, 2, 6])

    def test_options(self):
        self.assertEqual(qs.quantity_options(1), [("One copy", 1)])
        self.assertEqual(qs.quantity_options(5)[-1], ("Max (5 copies)", 5))

    def test_parse_accepts_plain_phrasing(self):
        self.assertEqual(qs.parse_quantity("3", 4), 3)
        self.assertEqual(qs.parse_quantity("Print 2 copies", 4), 2)

    def test_parse_rejects_vague_or_too_many(self):
        with self.assertRaises(ValueError):
            qs.parse_quantity("a few", 4)
        with self.assertRaisesRegex(ValueError, "will not fit"):
            qs.parse_quantity("9", 4)


class MultiPlateFitFailureTests(unittest.TestCase):
    def test_uninspectable_multi_plate_raises_instead_of_reporting_one_copy(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from print_handy.core import model_job as mj
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'broken.3mf'
            source.write_bytes(b'not a zip')
            with mock.patch.object(mj, 'plate_manifest', return_value={'plates': [{'object_ids': [1]}]}):
                with self.assertRaises(RuntimeError):
                    oa._multi_plate_capacity(source, {'bed_mm': [220, 220], 'max_print_height_mm': 220})


class TestScaleSearches(unittest.TestCase):
    def test_fraction_is_of_the_converted_size_with_a_half_floor(self):
        from unittest.mock import patch
        from print_handy.core import orca_adapter as oa
        probed = []

        def capacity(source, state, scale=1.0, **kw):
            probed.append(scale)
            return 1 if scale <= 25.4 * 0.86 else 0

        with patch.object(oa, 'orca_plate_capacity', capacity):
            # An inch size that fits at 86%: probes run at 25.4 x fraction.
            self.assertEqual(oa.largest_fitting_fraction('src', {}, 25.4, copy_limit=1), 0.86)
        self.assertTrue(all(s <= 25.4 for s in probed))
        with patch.object(oa, 'orca_plate_capacity', lambda s, st, scale=1.0, **kw: 0):
            self.assertIsNone(oa.largest_fitting_fraction('src', {}, 25.4, copy_limit=1))

    def test_painted_fit_reports_the_blocking_plate_layout(self):
        from unittest.mock import patch
        from print_handy.core import color_geometry, model_job as mj

        def export(src, out, view, object_ids=None, scale=1.0, tower_mm=None):
            return None, {'parts': [{'bounds_mm': [[0, 500 * scale], [0, 100 * scale], [0, 10]]}],
                          'build_bounds_mm': [(0.0, 500 * scale), (0.0, 100 * scale), (0.0, 10.0)]}

        with patch.object(color_geometry, 'export_color_geometry', export):
            fit = mj.painted_fit('src', None, [{'id': 1, 'object_ids': ['1']}])
        self.assertIsNone(fit['scale'])
        # Measured at the 50% floor: the size that still did not fit.
        self.assertEqual(fit['blocking_layout']['extent_mm'], [250.0, 50.0])
        self.assertEqual(fit['blocking_layout']['scale'], 0.5)
        self.assertGreater(fit['blocking_layout']['tower_mm'], 0)

    def test_painted_fit_layout_is_measured_without_a_fitting_tower(self):
        from unittest.mock import patch
        from print_handy.core import color_geometry, model_job as mj

        def export(src, out, view, object_ids=None, scale=1.0, tower_mm=None):
            # The real export raises when no prime tower position clears
            # the parts; a towerless measurement of the same parts succeeds
            # and carries the same bounds.
            if tower_mm != 0:
                raise RuntimeError('no prime tower position clears every part')
            return None, {'parts': [{'bounds_mm': [[0, 500 * scale], [0, 100 * scale], [0, 10]]}],
                          'build_bounds_mm': [(0.0, 500 * scale), (0.0, 100 * scale), (0.0, 10.0)]}

        with patch.object(color_geometry, 'export_color_geometry', export):
            fit = mj.painted_fit('src', None, [{'id': 1, 'object_ids': ['1']}])
        self.assertIsNone(fit['scale'])
        # The tower-blocked no-fit case still reports its measured extent
        # and the tower size it could not place.
        self.assertEqual(fit['blocking_layout']['extent_mm'], [250.0, 50.0])
        self.assertGreater(fit['blocking_layout']['tower_mm'], 0)

    def test_painted_fit_without_a_measurable_layout_omits_it(self):
        from unittest.mock import patch
        from print_handy.core import color_geometry, model_job as mj

        def broken(src, out, view, object_ids=None, scale=1.0, tower_mm=None):
            raise RuntimeError('mesh parse exploded')

        with patch.object(color_geometry, 'export_color_geometry', broken):
            fit = mj.painted_fit('src', None, [{'id': 1, 'object_ids': ['1']}])
        self.assertIsNone(fit['scale'])
        self.assertNotIn('blocking_layout', fit)
