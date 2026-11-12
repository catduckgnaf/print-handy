"""Issue #17: per-plate and per-part estimates, aggregate totals, and the split-plan estimate fields."""
import json

from print_handy.core import prepared_intake
from print_handy.runtime import discord_print_summary as summary
from print_handy.runtime import print_records as records


def _gcode(path, duration, grams):
    path.write_text(f"; estimated printing time (normal mode) = {duration}\n; total filament used [g] = {grams}\nG1 X1\n")
    return str(path)


def _job(tmp_path, copies=1, third_known=True):
    parts = [{'key': 'a', 'name': 'Stand.stl', 'source_extruder': 1, 'volume_mm3': 30000},
             {'key': 'b', 'name': 'Clip.stl', 'source_extruder': 1, 'volume_mm3': 10000, 'author_color': 'Crimson'},
             {'key': 'c', 'name': 'Lid.stl', 'source_extruder': 2},
             {'key': 'd', 'name': 'Base.stl', 'source_extruder': 2}]
    sel = {'a': ('Black', 1), 'b': ('Black', 1), 'c': ('White', 2), 'd': ('White', 2)}
    return {'title': 'Desk set', 'copies': copies, 'color_manifest': {'parts': parts},
            'color_decisions': {'mode': 'per_part', 'choices': {
                k: {'slot': slot, 'color': color, 'material': 'PLA'} for k, (color, slot) in sel.items()}},
            'derived_plates': [
                {'gcode': _gcode(tmp_path / 'p1.gcode', '1h 30m', 40.0), 'plate_id': 1, 'slot': 1, 'part_keys': ['a', 'b']},
                {'gcode': _gcode(tmp_path / 'p2.gcode', '45m', 20.0), 'plate_id': 2, 'slot': 2, 'part_keys': ['c']},
                {'gcode': _gcode(tmp_path / 'p3.gcode', '2h', 10.0) if third_known else str(tmp_path / 'missing.gcode'),
                 'plate_id': 3, 'slot': 2, 'part_keys': ['d']}]}


def test_totals_sum_every_plate_not_plate_one(tmp_path):
    text = summary.format_estimate_breakdown(_job(tmp_path))
    assert "Total: ~4h 15m printing (+~5 min warmup per plate), ~70 g filament" in text
    assert "Plate 1: ~1h 30m" in text and "Plate 2: ~45m" in text and "Plate 3: ~2h 0m" in text


def test_part_grams_are_shares_never_the_repeated_total(tmp_path):
    plates = summary.plate_estimates(_job(tmp_path))
    first = {p['name']: p['filament_g'] for p in plates[0]['parts']}
    assert first == {'Stand': 30.0, 'Clip': 10.0}  # volume split of the 40 g plate
    assert sum(first.values()) == plates[0]['filament_g']
    assert all(p['filament_g'] != plates[0]['filament_g'] for p in plates[0]['parts'])
    text = summary.format_estimate_breakdown(_job(tmp_path))
    assert "Clip (Crimson PLA, ~10 g)" in text and "Stand (Black PLA, ~30 g)" in text  # author color kept


def _single(tmp_path, copies):
    """Single plate job: Orca clones the copies into the plate, so the G-code covers them."""
    gcode = _gcode(tmp_path / 'one.gcode', '1h', 12.0)
    return {'title': 'Hook', 'copies': copies, 'filament_selection': {'slot': 1, 'color': 'Black', 'material': 'PLA'},
            'color_manifest': {'parts': [{'key': 'a', 'name': 'Hook.stl', 'source_extruder': 1}]},
            'gcode': gcode, 'sliced_copies': {'gcode': gcode, 'copies': copies}}


def test_quantity_change_updates_totals(tmp_path):
    one = summary.format_estimate_breakdown(_single(tmp_path, 1))
    two = summary.format_estimate_breakdown(_single(tmp_path, 2))
    assert "for 2 copies" in two and "for 2 copies" not in one
    assert "Hook x2" in two and "Hook x2" not in one


def test_split_plates_never_claim_copies_their_gcode_lacks(tmp_path):
    # Per part slicing makes one copy per plate whatever was requested; do not promise two.
    text = summary.format_estimate_breakdown(_job(tmp_path, copies=2))
    assert "for 2 copies" not in text and "Stand x2" not in text
    assert "Total: ~4h 15m printing" in text


def test_requested_copies_without_a_cloning_slice_count_as_one(tmp_path):
    # Multi-plate single color (merged or per plate) slicing records no clone count.
    job = _single(tmp_path, 3)
    job.pop('sliced_copies')
    assert "for 3 copies" not in summary.format_estimate_breakdown(job)
    stale = _single(tmp_path, 3)
    stale['sliced_copies']['gcode'] = str(tmp_path / 'older-slice.gcode')  # a resliced job
    assert "for 3 copies" not in summary.format_estimate_breakdown(stale)


def test_same_name_in_different_colors_stays_separate(tmp_path):
    job = _job(tmp_path)
    job['color_manifest']['parts'][1]['name'] = 'Stand.stl'  # second part on plate 1 shares the name
    job['color_manifest']['parts'][1].pop('author_color')
    job['color_decisions']['choices']['b']['color'] = 'Red'
    rows = {(p['name'], p['color']): p['filament_g'] for p in summary.plate_estimates(job)[0]['parts']}
    assert rows == {('Stand', 'Black'): 30.0, ('Stand', 'Red'): 10.0}


def test_unknown_estimates_are_labeled_not_zero(tmp_path):
    text = summary.format_estimate_breakdown(_job(tmp_path, third_known=False))
    assert "Plate 3: time unknown" in text
    assert "at least ~2h 15m printing, 1 plate unknown" in text
    assert "at least 60 g filament" in text


def test_model_page_estimate_fills_missing_plate_and_scales_with_copies(tmp_path):
    job = _single(tmp_path, 2)
    job['gcode'] = job['sliced_copies']['gcode'] = str(tmp_path / 'missing.gcode')
    job['selected_source'] = {'plates': [{'prediction_s': 3600, 'weight_g': 8}]}
    plates = summary.plate_estimates(job)
    assert plates[0]['seconds'] == 7200 and plates[0]['basis'] == 'model page' and plates[0]['filament_g'] == 16.0


def test_many_plates_stay_short(tmp_path):
    job = _job(tmp_path)
    job['derived_plates'] = job['derived_plates'][:1] * 14
    text = summary.format_estimate_breakdown(job)
    assert "+4 more plates" in text and len(text) < 1990


def test_split_plan_records_estimates_so_remaining_duration_is_known(tmp_path):
    job = _job(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps(job))
    manifest = {'parts': job['color_manifest']['parts']}
    gcodes = [r['gcode'] for r in job['derived_plates']]
    plan = prepared_intake.write_split_plan(jp, job, manifest, gcodes, 'intake-est', plans_dir=tmp_path)
    assert [p['plate_estimate_seconds'] for p in plan['parts']] == [5400, 2700, 7200]
    assert [p['estimated_part_seconds'] for p in plan['parts']] == [5400, 2700, 7200]
    assert [p['estimated_filament_g'] for p in plan['parts']] == [40.0, 20.0, 10.0]
    text = records.format_multipart_summary(plan)
    assert "Combined remaining duration: ~4h 15m" in text


def test_gcode_metadata_reads_the_bambu_studio_header(tmp_path):
    """A Bambu Studio plate has no 'estimated printing time (normal mode)' line; the header form counts (#271).
    The header is modelled as the real slicer writes it (#434): a bare '; BambuStudio <version>' line —
    no '; generated by' line — and filament weight as '; total filament weight [g] : X', which the
    grams reader parses since #444 (this test asserted the grams unknown until then)."""
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("; HEADER_BLOCK_START\n; BambuStudio 02.08.02.52\n"
                     "; model printing time: 14h 22m 10s; total estimated time: 14h 31m 37s\n"
                     "; total filament weight [g] : 312.40\n; HEADER_BLOCK_END\nG1 X1\n")
    assert summary.gcode_metadata(gcode) == (51730, 312.4)
    message = summary.format_confirmed_print(model="Vase", seconds=summary.gcode_seconds(gcode), kind="started")
    assert "Estimated total: ~14h 27m (printing: 14h 22m)" in message and "unknown" not in message
    gcode.write_text("; total filament used [g] = 312.40\n; total estimated time: 9m\nG1 X1\n")
    assert summary.gcode_metadata(gcode) == (540, 312.4)


def test_estimate_breakdown_shows_bambu_studio_grams(tmp_path):
    """#444: a real-format Bambu Studio plate shows its grams, not 'filament unknown'.

    Header block copied from an actual Bambu Studio 2.8.2.61 A1 slice (20 mm cube).
    """
    gcode = tmp_path / 'plate_1.gcode'
    gcode.write_text("; HEADER_BLOCK_START\n; BambuStudio 02.08.02.61\n"
                     "; model printing time: 16m 18s; total estimated time: 22m 34s\n"
                     "; total layer number: 100\n"
                     "; total filament length [mm] : 1263.89\n"
                     "; total filament volume [cm^3] : 3040.00\n"
                     "; total filament weight [g] : 3.77\n"
                     "; filament: 1\n; HEADER_BLOCK_END\nG1 X1\n")
    job = {'title': 'Cube', 'copies': 1,
           'color_manifest': {'parts': [{'key': 'a', 'name': 'Cube.stl', 'source_extruder': 1}]},
           'derived_plates': [{'gcode': str(gcode), 'plate_id': 1, 'slot': 1, 'part_keys': ['a']}]}
    text = summary.format_estimate_breakdown(job)
    assert "3.77 g" in text and "filament unknown" not in text and "grams unknown" not in text



def test_cloned_copies_show_the_gcodes_own_totals_everywhere(tmp_path):
    """#438: cloned copies live in the G-code, so its totals ARE the job totals — never multiplied again."""
    job = _single(tmp_path, 3)
    _gcode(tmp_path / "one.gcode", "3h", 36.0)  # the real cloned slice: all 3 copies in the file
    sliced = summary.format_slice_summary(job)
    assert "Total filament: 36 g" in sliced and "108 g" not in sliced
    assert "Estimated total: ~3h 5m (printing: 3h 0m)" in sliced
    assert "Approx. finish if started now:" in sliced
    started = summary.format_started(job, summary.plates_of(job)[0])
    assert "Filament: 36 g" in started and "108 g" not in started
    assert "Estimated total: ~3h 5m (printing: 3h 0m)" in started
    assert "Approx. finish:" in started
    breakdown = summary.format_estimate_breakdown(job)
    assert "Total: ~3h 0m printing (+~5 min warmup), ~36 g filament for 3 copies" in breakdown
