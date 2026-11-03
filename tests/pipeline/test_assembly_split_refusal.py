"""#820: an assembly that has to split is refused in plain words before any plate is exported."""
import json

import pytest

from print_handy import diagnostics
from print_handy.core import model_job, slice_failure, slice_refusal, source_colors, worker_errors
from tests.corpus.make_models import box, write_assembly_3mf

PALETTE = [('#FFFFFF', 'PLA'), ('#000000', 'PLA'), ('#FF69B4', 'PLA')]


def _assembly(tmp_path, bodies):
    source = tmp_path / 'assembly.3mf'
    write_assembly_3mf(source, bodies, PALETTE[:len(bodies)])
    manifest = source_colors.inventory(source)
    # One slot per body, the way the planner's split hands them to the per-slot exporter.
    groups = [{'slot': str(i + 1), 'part_keys': [part['key']]}
              for i, part in enumerate(manifest['parts'])]
    return source, manifest, groups


def test_assembly_split_into_slot_plates_is_refused_with_its_size_and_the_way_out(tmp_path):
    source, manifest, groups = _assembly(tmp_path, [('Base', 1, box(214, 214, 4)), ('Ring', 2, box(150, 150, 1)),
                                                    ('Eye', 3, box(30, 30, 1))])
    with pytest.raises(slice_refusal.SliceRefusal) as caught:
        model_job.refuse_assembly_split(
            source, manifest, groups, {'bed_mm': [220, 220]},
            {'mode': 'split', 'reason': '1 parts do not fit one 220x220 mm bed with 5 mm spacing and a prime tower'})
    refusal = caught.value
    assert 'one assembly of 3 colors (214 x 214 x 4 mm on a 220 x 220 mm bed)' in refusal.plain
    assert 'cannot be split onto separate plates' in refusal.plain
    assert 'smaller' in refusal.next_step and 'One color for everything' in refusal.next_step
    assert refusal.fix is None  # no blind retry and no one-tap fix: nothing changes on a second try
    assert diagnostics.classify(refusal) == diagnostics.Category.CONFIGURATION
    post = slice_failure.post(refusal)
    assert 'PH_SLICING_CONFIGURATION' in post and 'PROGRAMMING' not in post
    back = worker_errors.decode(json.loads(json.dumps(worker_errors.encode(refusal))))
    assert back.plain == refusal.plain


def test_assembly_kept_on_one_slot_or_plain_objects_are_not_refused(tmp_path):
    source, manifest, groups = _assembly(tmp_path, [('Base', 1, box(100, 100, 4)), ('Ring', 2, box(50, 50, 1))])
    one_slot = [{'slot': '1', 'part_keys': [key for group in groups for key in group['part_keys']]}]
    assert model_job.refuse_assembly_split(source, manifest, one_slot, {'bed_mm': [220, 220]}) is None
    # Parts of different build objects split freely; only a shared object is protected.
    apart = {'parts': [{'key': 'a', 'object_id': '1'}, {'key': 'b', 'object_id': '2'}]}
    assert model_job.refuse_assembly_split(
        source, apart, [{'slot': '1', 'part_keys': ['a']}, {'slot': '2', 'part_keys': ['b']}],
        {'bed_mm': [220, 220]}) is None


def test_refusal_without_a_readable_size_still_explains(tmp_path):
    source, manifest, groups = _assembly(tmp_path, [('Base', 1, box(214, 214, 4)), ('Ring', 2, box(150, 150, 1))])
    source.write_bytes(b'not a zip')
    with pytest.raises(slice_refusal.SliceRefusal) as caught:
        model_job.refuse_assembly_split(source, manifest, groups, {})
    assert 'one assembly of 2 colors. The planner could not keep' in caught.value.plain


@pytest.mark.parametrize('reason,says,not_says,step', [
    ('1 parts do not fit one 220x220 mm bed with 5 mm spacing and a prime tower',
     'does not leave room', None, 'Make the model smaller'),
    ('mixed materials across slots cannot share one IFS job (slot 1 PLA, slot 2 PETG)',
     'materials that cannot print in one job', 'room', 'one material family'),
    ('Print parts separately was chosen', 'Printing its parts separately was chosen', 'room', 'print together'),
    ('author notes ask for separate printing ("print separately")', 'Printing its parts separately was chosen',
     'room', 'print together'),
    ('Resized per-part job: one fitted plate per slot', 'It was resized', 'room', 'original size'),
    ('the merge exporter cannot place this geometry (x)', 'could not keep all of its colors', 'room',
     'different size or arrangement'),
    (None, 'could not keep all of its colors', 'room', 'different size or arrangement'),
])
def test_refusal_wording_follows_the_planner_reason(tmp_path, reason, says, not_says, step):
    source, manifest, groups = _assembly(tmp_path, [('Base', 1, box(40, 40, 4)), ('Ring', 2, box(20, 20, 1))])
    with pytest.raises(slice_refusal.SliceRefusal) as caught:
        model_job.refuse_assembly_split(source, manifest, groups, {'bed_mm': [220, 220]},
                                        {'mode': 'split', 'reason': reason} if reason else None)
    refusal = caught.value
    assert says in refusal.plain and step in refusal.next_step
    assert 'cannot be split onto separate plates' in refusal.plain
    if not_says:
        assert not_says not in refusal.plain and 'smaller' not in refusal.next_step
    assert 'one assembly of 2 colors (40 x 40 x 4 mm' in refusal.plain
