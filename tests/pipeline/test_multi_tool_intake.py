"""Per-toolhead colour intake for multi_tool printers (#346, Creator 5 / 5 Pro).

Offline: fake Discord answers by matching choice text, slicing is stubbed at
the prepare_link seam (the real slice path is covered at the orca_adapter
level below), and the recorded state carries one spool per toolhead.
"""
import asyncio
import json
import stat
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy import printers, settings
from print_handy.core import model_job as mj
from print_handy.core import orca_adapter, prepared_intake, source_colors
from print_handy.printers.base import MultiColorUnsupported

from .test_source_colors import source

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "tests" / "corpus" / "cases"
MODELS = ("flashforge.creator5", "flashforge.creator5_pro")


def creator_config(model_key):
    return settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": model_key})


@pytest.fixture(params=MODELS, ids=["creator5", "creator5_pro"])
def creator(request, monkeypatch):
    cfg = creator_config(request.param)
    monkeypatch.setattr(settings, "current", lambda: cfg)
    return request.param


def tool_state():
    """Recorded hardware state: one spool per toolhead, keyed like the IFS map."""
    return {'bed_mm': [256, 256], 'max_print_height_mm': 256,
            'nozzle': {'diameter_mm': 0.4, 'type': 'hardened_steel'},
            'temps_c': {'nozzle': 220, 'bed': 60},
            'spool': {'material': 'PLA', 'spoolman_id': 10, 'name': 'white', 'color': 'white',
                      'density': 1.24, 'diameter_mm': 1.75},
            'filament_slots': {'1': {'color': 'white', 'material': 'PLA', 'spoolman_id': 10, 'hex': 'FFFFFF',
                                'physical_color_verified': True},
                          '2': {'color': 'black', 'material': 'PLA', 'spoolman_id': 11, 'hex': '000000',
                                'physical_color_verified': True},
                          '3': {'color': 'red', 'material': 'PLA', 'spoolman_id': 12, 'hex': 'FF0000',
                                'physical_color_verified': True},
                          '4': {'color': 'blue', 'material': 'PLA', 'spoolman_id': 13, 'hex': '0000FF',
                                'physical_color_verified': True}}}


def write_job(tmp_path, model_file, model_key=None):
    jp = tmp_path / 'JOB.json'
    job = {'model_id': '1', 'downloaded_file': str(model_file), 'color_schema': 'part_slots_v1',
           'audit': {'plates': 1}}
    if model_key:
        job['printer_model'] = model_key
    jp.write_text(json.dumps(job))
    return jp


# ---------------------------------------------------------------- packet / CLI gate (model_job.intake_color_choices)

def test_packet_questions_offer_toolheads(creator, tmp_path):
    jp = write_job(tmp_path, source(tmp_path), creator)
    assert mj.intake_color_choices(jp, tool_state()) is False  # packet printed, questions recorded
    saved = json.loads(jp.read_text())
    assert saved['color_questions']
    for question in saved['color_questions']:
        labels = [c['label'] for c in question['choices']]
        assert labels and all(label.startswith('Toolhead ') for label in labels), labels
        assert not any('Slot' in label for label in labels)


def test_per_part_answers_record_toolhead_picks(creator, tmp_path):
    src = source(tmp_path)
    manifest = source_colors.inventory(src)
    rows = source_colors.canonical_prompts(manifest)
    # Shell (extruder 1) -> toolhead 2, Eyes (extruder 2) -> toolhead 4; palette rows agree.
    picks = {1: 2, 2: 4}
    slots = [picks[row['source_extruder']] for row in rows]
    jp = write_job(tmp_path, src, creator)
    assert mj.intake_color_choices(jp, tool_state(), mode='per_part', slots=slots)
    saved = json.loads(jp.read_text())
    choices = saved['color_decisions']['choices']
    assert {key: sel['slot'] for key, sel in choices.items()} == {row['key']: picks[row['source_extruder']]
                                                                 for row in rows}
    # The recorded picks are an identity tool mapping for the adapter, unchanged.
    adapter = printers.get(creator, creator_config(creator))
    mapping = {str(sel['slot'] - 1): sel['slot'] for sel in choices.values()}
    assert adapter.normalize_tool_mapping(mapping, tool_state()) == mapping


def test_five_colours_refused_but_single_colour_still_open(creator, tmp_path):
    src = CASES / 'five-colors-over-slots' / 'model.3mf'
    jp = write_job(tmp_path, src, creator)
    with pytest.raises(MultiColorUnsupported, match='5 toolheads|toolheads'):
        mj.intake_color_choices(jp, tool_state(), mode='per_part', slots=[1, 2, 3, 4, 1])
    # One toolhead printing everything needs no assignment: still allowed.
    assert mj.intake_color_choices(jp, tool_state(), mode='single_color', slots=[2])
    saved = json.loads(jp.read_text())
    assert saved['color_decisions']['selection']['slot'] == 2


def test_double_assigning_a_toolhead_is_refused(creator, tmp_path):
    src = source(tmp_path)
    rows = source_colors.canonical_prompts(source_colors.inventory(src))
    slots = [1] * len(rows)  # two different source colours onto toolhead 1
    jp = write_job(tmp_path, src, creator)
    with pytest.raises(ValueError, match='one toolhead prints one color'):
        mj.intake_color_choices(jp, tool_state(), mode='per_part', slots=slots)


def test_single_colour_source_needs_no_questions(creator, tmp_path):
    # No packet, no refusal: a single-colour Creator job gains no new questions.
    jp = write_job(tmp_path, CASES / 'single-part' / 'model.3mf', creator)
    assert mj.intake_color_choices(jp, tool_state()) is True


# ---------------------------------------------------------------- Discord collect flow (discord_color_intake)

class PickAdapter:
    """Answers each clarify by picking the choice whose text contains the next wanted fragment."""

    def __init__(self, wants):
        self.wants = iter(wants)
        self.questions = []
        self.sent = []

    async def send_clarify(self, **kw):
        self.questions.append(kw)
        want = next(self.wants)
        answer = next(c for c in kw['choices'] if want in c)
        import tools.clarify_gateway as cm
        cm.resolve_gateway_clarify(kw['clarify_id'], answer)
        return S(success=True)

    async def send(self, **kw):
        self.sent.append(kw)
        return S(success=True)


def test_collect_asks_per_toolhead_and_never_reoffers_a_taken_toolhead(creator, tmp_path):
    from print_handy.core.discord_color_intake import collect
    src = source(tmp_path)
    jp = write_job(tmp_path, src, creator)
    manifest = source_colors.inventory(src)
    (jp).write_text(json.dumps({**json.loads(jp.read_text()), 'color_manifest': manifest}))
    # Mode: preserve per part. Shell -> the black toolhead (2), Eyes -> the red
    # toolhead (3).
    adapter = PickAdapter(['Preserve colors', 'Black', 'Red'])
    result = asyncio.run(collect(jp, tool_state(), adapter, 'offline', 'creator-colors', timeout=1))
    assert result['status'] == 'COLOR_CHOICES_SAVED'
    colour_questions = adapter.questions[1:]
    assert len(colour_questions) == 2
    for question in colour_questions:
        assert not any('Slot' in c for c in question['choices']), question['choices']
    # After Shell took the black toolhead for its (different) colour, the Eyes
    # question must not offer it again.
    eyes_choices = colour_questions[1]['choices']
    assert not any('Black' in c for c in eyes_choices), eyes_choices
    saved = json.loads(jp.read_text())['color_decisions']
    by_extruder = {}
    for row in source_colors.canonical_prompts(manifest):
        by_extruder[row['source_extruder']] = saved['choices'][row['key']]['slot']
    assert by_extruder == {1: 2, 2: 3}


def test_collect_refuses_five_colours(creator, tmp_path):
    from print_handy.core.discord_color_intake import collect
    src = CASES / 'five-colors-over-slots' / 'model.3mf'
    jp = write_job(tmp_path, src, creator)
    manifest = source_colors.inventory(src)
    jp.write_text(json.dumps({**json.loads(jp.read_text()), 'color_manifest': manifest}))
    adapter = PickAdapter(['Preserve colors'])
    with pytest.raises(MultiColorUnsupported, match='toolheads'):
        asyncio.run(collect(jp, tool_state(), adapter, 'offline', 'creator-five', timeout=1))
    assert len(adapter.questions) == 1  # only the mode question was asked


def test_duplicate_spool_colours_are_labelled_by_toolhead(creator, tmp_path):
    from print_handy.core.discord_color_intake import collect
    state = tool_state()
    state['filament_slots']['3'] = {'color': 'black', 'material': 'PLA', 'spoolman_id': 12, 'hex': '111111',
                               'physical_color_verified': True}
    src = source(tmp_path)
    jp = write_job(tmp_path, src, creator)
    manifest = source_colors.inventory(src)
    jp.write_text(json.dumps({**json.loads(jp.read_text()), 'color_manifest': manifest}))
    adapter = PickAdapter(['Preserve colors', '(Toolhead 2)', '(Toolhead 3)'])
    result = asyncio.run(collect(jp, state, adapter, 'offline', 'creator-dupes', timeout=1))
    assert result['status'] == 'COLOR_CHOICES_SAVED'
    offered = [c for q in adapter.questions for c in q['choices']]
    assert 'Black PLA (Toolhead 2)' in offered and 'Black PLA (Toolhead 3)' in offered
    assert not any('Slot' in c for c in offered)


def test_summary_wording_names_toolheads(creator, tmp_path):
    from print_handy.runtime import discord_print_summary as summary
    gcode = tmp_path / 'plate_1.gcode'
    gcode.write_text('; estimated printing time (normal mode) = 1h 0m 0s\nG1 X1\n')
    job = {'printer_model': creator, 'title': 'Ghost Stand', 'gcode': str(gcode),
           'derived_plates': [{'gcode': str(gcode), 'plate_id': 1, 'slot': 3, 'part_keys': ['part:a']}],
           'color_decisions': {'mode': 'per_part',
                               'choices': {'part:a': {'slot': 3, 'color': 'red', 'material': 'PLA'}}}}
    text = summary.format_slice_summary(job)
    assert 'red PLA, toolhead 3' in text
    assert 'slot' not in text.lower()
    started = summary.format_started(job, {'gcode': str(gcode), 'plate': 1, 'parts': ['Ghost Stand'],
                                           'slot': 3, 'color': 'red', 'material': 'PLA',
                                           'filament_g': None, 'seconds': 3600})
    assert 'Toolhead 3: red PLA' in started


# ---------------------------------------------------------------- full prepare_link flow (Hermes intake)

class FlowAdapter:
    """prepare_link adapter: answers questions by fragment, records everything."""

    def __init__(self, wants):
        self.wants = list(wants)
        self.picks = iter(())  # the test-adapter seam: _with_stop runs work without a Stop prompt
        self.questions = []
        self.sent = []
        self.log = []

    async def send_clarify(self, **kw):
        self.questions.append(kw)
        if len(kw.get('choices') or []) == 1:
            self.log.append(kw['question'])
        want = self.wants.pop(0)
        answer = next(c for c in kw['choices'] if want in c)
        import tools.clarify_gateway as cm
        cm.resolve_gateway_clarify(kw['clarify_id'], answer)
        return S(success=True)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        self.log.append(content)
        return S(success=True)


@pytest.fixture
def flow_env(creator, tmp_path, monkeypatch):
    from .helpers import external
    print_next = external("print_next")
    root = tmp_path / 'receipts'
    monkeypatch.setattr(mj, 'ARCHIVE', tmp_path / 'archive')
    monkeypatch.setattr(mj, 'printables_inventory', lambda url: {
        'site': 'printables', 'slug': 'ghost', 'title': 'Ghost Stand',
        'sources': [{'name': 'ghost.3mf', 'instance_id': 1}]})
    holder = {}

    def download(job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / 'ghost.3mf'
        out.write_bytes(holder['bytes'])
        return out
    monkeypatch.setattr(mj, 'printables_download', download)
    monkeypatch.setattr(mj, 'audit', lambda path: {'plates': 1})
    from print_handy.core import orca_adapter as oa
    monkeypatch.setattr(oa, 'orca_plate_capacity', lambda path, st: 1)

    async def thread_heavy(func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    monkeypatch.setattr(prepared_intake, '_heavy', thread_heavy)

    sliced = []

    def fake_slice(job_path, recipe_sha256=None):
        data = json.loads(Path(job_path).read_text())
        picks = sorted({sel['slot'] for sel in (data.get('color_decisions') or {}).get('choices', {}).values()})
        data['printer_model'] = creator
        data['tool_mapping'] = {str(slot - 1): slot for slot in picks}
        gcode = Path(job_path).parent / 'sliced' / 'plate_1.gcode'
        gcode.parent.mkdir(parents=True, exist_ok=True)
        gcode.write_text('; estimated printing time (normal mode) = 1h 0m 0s\nG1 X1\n')
        data['derived_plates'] = [{'gcode': str(gcode), 'plate_id': 1, 'slot': None}]
        mj.write_job(Path(job_path).parent, data)
        sliced.append(str(gcode))
        return [gcode]
    monkeypatch.setattr(mj, 'slice_job', fake_slice)

    stub = tmp_path / 'print-now-stub'
    stub.write_text('#!/bin/sh\necho "started $2"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    real_start = print_next.start

    async def start(target):
        return await real_start(target, model_job=stub, idle_check=lambda: None, timeout=1,
                                claims_dir=tmp_path / 'claims')
    monkeypatch.setattr(print_next, 'start', start)
    monkeypatch.setattr(prepared_intake.print_summary, 'find_job',
                        lambda filename, *args: (json.loads(Path(filename).parents[1].joinpath('JOB.json').read_text()),
                                                 {'gcode': str(Path(filename)), 'plate': 1, 'parts': ['Ghost Stand'],
                                                  'slot': 2, 'color': 'black', 'material': 'PLA',
                                                  'filament_g': None, 'seconds': 3600}))
    return S(root=root, sliced=sliced, model=creator, holder=holder)


def run_flow(flow_env, src_bytes, wants):
    flow_env.holder['bytes'] = src_bytes
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='m1', text='https://www.printables.com/model/1-ghost')
    adapter = FlowAdapter(wants)
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, tool_state(), flow_env.root, timeout=1))
    receipt = json.loads(next(flow_env.root.glob('*.json')).read_text())
    return adapter, result, receipt


def test_prepare_link_two_colours_completes_with_toolhead_wording(creator, flow_env, tmp_path):
    src = source(tmp_path)
    # Choose per part; Shell -> black toolhead, Eyes -> red toolhead; then
    # slice and print.
    wants = ['Choose colors', 'Black', 'Red', 'Slice and print']
    adapter, result, receipt = run_flow(flow_env, src.read_bytes(), wants)
    assert receipt['status'] == 'STARTED', receipt
    assert flow_env.sliced
    job = json.loads(Path(receipt['job_json']).read_text())
    choices = job['color_decisions']['choices']
    assert {sel['slot'] for sel in choices.values()} == {2, 3}
    asked = [q['question'] for q in adapter.questions]
    offered = [c for q in adapter.questions for c in q['choices']]
    # The start summary names the plate's toolhead, never a slot.
    assert any('Toolhead 2: black PLA' in text for text in adapter.sent), adapter.sent
    assert not any('Slot' in text for text in adapter.sent + asked + offered), (adapter.sent, asked)


def test_prepare_link_four_colours_completes(creator, flow_env):
    src = CASES / 'four-colors-one-plate' / 'model.3mf'
    # Four parts, four source colours -> toolheads 1..4 in question order.
    wants = ['Choose colors', 'White', 'Black', 'Red', 'Blue', 'Slice and print']
    adapter, result, receipt = run_flow(flow_env, src.read_bytes(), wants)
    assert receipt['status'] == 'STARTED', receipt
    job = json.loads(Path(receipt['job_json']).read_text())
    assert {sel['slot'] for sel in job['color_decisions']['choices'].values()} == {1, 2, 3, 4}
    offered = [c for q in adapter.questions for c in q['choices']]
    assert not any('Slot' in c for c in offered)


def test_prepare_link_five_colours_refused_after_mode_pick(creator, flow_env):
    src = CASES / 'five-colors-over-slots' / 'model.3mf'
    adapter, result, receipt = run_flow(flow_env, src.read_bytes(), ['Choose colors'])
    assert receipt['status'] == 'FAILED'
    assert 'toolheads' in receipt['error'] and '5 colors' in receipt['error']
    assert not receipt['sliced'] and not flow_env.sliced
    assert len(adapter.questions) == 1  # only the mode question was asked


# ---------------------------------------------------------------- slicing through the four-extruder templates

def slice_capture(model_key, decisions, tmp_path, monkeypatch):
    from print_handy.core import slice_guard
    monkeypatch.setattr(orca_adapter, 'pre_slice', lambda **k: slice_guard.Result())
    monkeypatch.setattr(orca_adapter, 'post_slice', lambda *a, **k: slice_guard.Result())
    monkeypatch.setattr(orca_adapter.sg, 'enforce_approved_baseline', lambda out, **k: slice_guard.Result())
    calls = []

    def run(command, **_kwargs):
        calls.append(list(command))
        out = Path(command[command.index('--outputdir') + 1]) / 'plate_1.gcode'
        out.write_text('; generated by OrcaSlicer 2.4.2\nM104 S0\nG90\nG1 X1 Y1 Z0.2 E1\nEND_PRINT\n' * 40)
        return S(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(orca_adapter.subprocess, 'run', run)
    adapter = printers.get(model_key, creator_config(model_key))
    folder = tmp_path / 'slice'
    output, provenance, configs = orca_adapter.slice_model(
        CASES / 'four-colors-one-plate' / 'model.3mf', folder, tool_state(),
        adapter=adapter, color_decisions=decisions)
    return output, provenance, configs, calls, folder


def four_colour_decisions(tmp_path, picks):
    src = CASES / 'four-colors-one-plate' / 'model.3mf'
    manifest = source_colors.inventory(src)
    state = tool_state()
    choices = {}
    for row, slot in zip(source_colors.canonical_prompts(manifest), picks):
        record = state['filament_slots'][str(slot)]
        choices[row['key']] = {'slot': slot, 'color': record['color'], 'material': 'PLA',
                               'spoolman_id': record['spoolman_id']}
    return {'source_sha256': manifest['source_sha256'], 'mode': 'per_part',
            'schema': 'part_slots_v1', 'choices': choices}


@pytest.mark.parametrize('model_key', MODELS)
def test_slice_uses_identity_toolheads_and_four_profiles(model_key, tmp_path, monkeypatch):
    # Permuted picks (colour order 3,1,4,2): tools must follow the picks, not
    # a compacted order — tool T(n-1) is toolhead n.
    picks = [3, 1, 4, 2]
    output, provenance, configs, calls, folder = slice_capture(
        model_key, four_colour_decisions(tmp_path, picks), tmp_path, monkeypatch)
    assert provenance['tool_mapping'] == {'0': 1, '1': 2, '2': 3, '3': 4}
    adapter = printers.get(model_key, creator_config(model_key))
    assert adapter.normalize_tool_mapping(provenance['tool_mapping'], tool_state()) == provenance['tool_mapping']
    machine = json.loads((folder / 'machine.json').read_text())
    assert machine['single_extruder_multi_material'] == '0'  # the model's own multi-extruder template
    filaments = [p for p in configs if p.name.startswith('filament_tool_')]
    assert len(filaments) == 4
    by_tool = {p.name: json.loads(p.read_text()) for p in filaments}
    # Filament for tool 2 (index) describes toolhead 3's recorded spool (the first pick).
    assert by_tool['filament_tool_2.json']['name'] == 'Hermes toolhead 3 PLA'
    assert by_tool['filament_tool_2.json']['filament_colour'] == ['#FF0000']
    argv = calls[0]
    load = argv[argv.index('--load-filaments') + 1]
    assert len(load.split(';')) == 4


@pytest.mark.parametrize('model_key', MODELS)
def test_slice_keeps_one_nozzle_diameter_entry_per_template_extruder(model_key, tmp_path, monkeypatch):
    # P2-104: Orca pairs nozzle_diameter entries with extruders; collapsing
    # the four-entry template list to one entry loses the four-tool model
    # before the templates ever reach Orca.
    _output, _provenance, _configs, _calls, folder = slice_capture(
        model_key, four_colour_decisions(tmp_path, [1, 2, 3, 4]), tmp_path, monkeypatch)
    machine = json.loads((folder / 'machine.json').read_text())
    assert machine['nozzle_diameter'] == ['0.4', '0.4', '0.4', '0.4']


@pytest.mark.parametrize('model_key', MODELS)
def test_slice_two_colours_on_nonadjacent_toolheads(model_key, tmp_path, monkeypatch):
    # Only toolheads 2 and 4 used: the unused toolheads keep base profiles and
    # the used colours stay on their own toolheads (no compaction to 1 and 2).
    picks = [2, 4, 2, 4]
    output, provenance, configs, calls, folder = slice_capture(
        model_key, four_colour_decisions(tmp_path, picks), tmp_path, monkeypatch)
    assert provenance['tool_mapping'] == {'1': 2, '3': 4}
    filaments = {p.name: json.loads(p.read_text()) for p in configs if p.name.startswith('filament_tool_')}
    assert len(filaments) == 4
    assert filaments['filament_tool_1.json']['name'] == 'Hermes toolhead 2 PLA'
    assert filaments['filament_tool_3.json']['name'] == 'Hermes toolhead 4 PLA'
    assert 'Hermes toolhead' not in filaments['filament_tool_0.json']['name']
    assert 'Hermes toolhead' not in filaments['filament_tool_2.json']['name']


# ---------------------------------------------------------------------------
# DATA-3 (P1-73, P1-117): a single-colour decision naming a toolhead must
# slice ON that toolhead — identity-tool geometry and that toolhead's own
# filament profile — not silently on T0 with the active spool's profile.

def single_colour_decisions(slot, state=None):
    src = CASES / 'four-colors-one-plate' / 'model.3mf'
    manifest = source_colors.inventory(src)
    record = (state or tool_state())['filament_slots'][str(slot)]
    return {'source_sha256': manifest['source_sha256'], 'mode': 'single_color',
            'schema': 'part_slots_v1',
            'selection': {'slot': slot, 'color': record['color'], 'material': 'PLA',
                          'spoolman_id': record['spoolman_id']}}


def exported_extruders(folder):
    import re
    import zipfile
    with zipfile.ZipFile(folder / 'geometry.3mf') as archive:
        model_settings = archive.read('Metadata/model_settings.config').decode()
    return set(re.findall(r'key="extruder" value="(\d+)"', model_settings))


@pytest.mark.parametrize('model_key', MODELS)
def test_slice_single_colour_uses_the_picked_toolhead(model_key, tmp_path, monkeypatch):
    output, provenance, configs, calls, folder = slice_capture(
        model_key, single_colour_decisions(3), tmp_path, monkeypatch)
    # Tool T2 is toolhead 3, and every part's geometry rides on it.
    assert provenance['tool_mapping'] == {'2': 3}
    assert exported_extruders(folder) == {'3'}
    filaments = {p.name: json.loads(p.read_text()) for p in configs if p.name.startswith('filament_tool_')}
    assert len(filaments) == 4
    assert filaments['filament_tool_2.json']['name'] == 'Hermes toolhead 3 PLA'
    assert filaments['filament_tool_2.json']['filament_colour'] == ['#FF0000']
    assert 'Hermes toolhead' not in filaments['filament_tool_0.json']['name']
    argv = calls[0]
    assert len(argv[argv.index('--load-filaments') + 1].split(';')) == 4
    adapter = printers.get(model_key, creator_config(model_key))
    assert adapter.normalize_tool_mapping(provenance['tool_mapping'], tool_state()) == {'2': 3}
# XL helpers and the #767 regression test, carried with the cherry-picked
# fix (901a4f6): the helpers come from the same backlog lane, since the
# test needs a five-toolhead XL state and capture the Creator helpers
# cannot express.


def xl_state():
    state = tool_state()
    state['nozzle'] = {'diameter_mm': 0.4, 'type': 'brass'}
    state['bed_mm'] = [360, 360]
    state['max_print_height_mm'] = 360
    state['filament_slots']['5'] = {'color': 'green', 'material': 'PLA', 'spoolman_id': 14,
                                    'hex': '00FF00', 'physical_color_verified': True}
    return state


def xl_capture(source_file, decisions, state, tmp_path, monkeypatch, **slice_kwargs):
    """slice_model against the XL adapter with a stubbed Orca (slice_capture's XL twin)."""
    import shutil
    from print_handy.core import slice_guard
    cfg = settings.load(local=False, environ={'PRINT_HANDY__PRINTER__MODEL': 'prusa.xl',
                                              'PRINT_HANDY__PATHS__STATE_HOME': str(tmp_path / 'home')})
    monkeypatch.setattr(settings, 'current', lambda: cfg)
    adapter = printers.get('prusa.xl', cfg)
    # The XL's templates are operator-generated; stand in a real multi-extruder
    # Orca export (the Creator's shipped set) for the generated folder.
    templates = tmp_path / 'prusa_xl'
    templates.mkdir(exist_ok=True)
    shipped = ROOT / 'print_handy' / 'templates' / 'orca' / 'flashforge_creator5'
    for name in ('machine.json', 'process.json', 'filament.json'):
        shutil.copy(shipped / name, templates / name)
    monkeypatch.setattr(adapter, 'templates_dir', lambda: templates)
    monkeypatch.setattr(orca_adapter, 'pre_slice', lambda **k: slice_guard.Result())
    monkeypatch.setattr(orca_adapter, 'post_slice', lambda *a, **k: slice_guard.Result())
    monkeypatch.setattr(orca_adapter.sg, 'enforce_approved_baseline', lambda out, **k: slice_guard.Result())
    calls = []

    def run(command, **_kwargs):
        calls.append(list(command))
        out = Path(command[command.index('--outputdir') + 1]) / 'plate_1.gcode'
        # A Prusa-shaped Orca file: the vendor guard checks the toolpath
        # bounds, the recorded temperatures and the filament type on it.
        out.write_text('; generated by OrcaSlicer 2.4.2\n; nozzle_diameter = 0.4\n'
                       '; filament_type = PLA\n; nozzle_temperature = 220\n'
                       '; nozzle_temperature_initial_layer = 220\n; hot_plate_temp = 60\n'
                       'M140 S60\nM104 S220 T3\nT3\nG90\nM83\n;LAYER_CHANGE\n'
                       'G1 Z0.2\nG1 X10 Y10 E1\nG1 X20 Y20 E1\n'
                       '; MACHINE_END_GCODE_START\nM104 S0\nM140 S0\nM84\n')
        return S(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(orca_adapter.subprocess, 'run', run)
    folder = tmp_path / 'slice'
    output, provenance, configs = orca_adapter.slice_model(
        source_file, folder, state, adapter=adapter, color_decisions=decisions, **slice_kwargs)
    return output, provenance, configs, calls, folder, adapter


def test_slice_single_colour_xl_uses_the_picked_toolhead(tmp_path, monkeypatch):
    state = xl_state()
    output, provenance, configs, calls, folder, adapter = xl_capture(
        CASES / 'four-colors-one-plate' / 'model.3mf', single_colour_decisions(4, state),
        state, tmp_path, monkeypatch)
    # Tool T3 is toolhead 4 of five; the job carries one profile per toolhead.
    assert provenance['tool_mapping'] == {'3': 4}
    assert exported_extruders(folder) == {'4'}
    filaments = {p.name: json.loads(p.read_text()) for p in configs if p.name.startswith('filament_tool_')}
    assert len(filaments) == 5
    assert filaments['filament_tool_3.json']['name'] == 'Hermes toolhead 4 PLA'
    assert filaments['filament_tool_3.json']['filament_colour'] == ['#0000FF']
    assert adapter.normalize_tool_mapping(provenance['tool_mapping'], state) == {'3': 4}


def test_slice_xl_five_toolheads_is_not_capped_at_four_slots(tmp_path, monkeypatch):
    # Bonus (found in the DATA-3 lane): the geometry exporter's slot bound is
    # the AD5X's four IFS slots; a per-part XL job using all five toolheads —
    # an assignment intake approves — was refused as needing "one to four
    # IFS slots". The identity mapping is bounded by the toolhead count.
    state = xl_state()
    src = CASES / 'five-colors-over-slots' / 'model.3mf'
    manifest = source_colors.inventory(src)
    choices = {}
    for row in source_colors.canonical_prompts(manifest):
        slot = row['source_extruder']
        record = state['filament_slots'][str(slot)]
        choices[row['key']] = {'slot': slot, 'color': record['color'], 'material': 'PLA',
                               'spoolman_id': record['spoolman_id']}
    decisions = {'source_sha256': manifest['source_sha256'], 'mode': 'per_part',
                 'schema': 'part_slots_v1', 'choices': choices}
    output, provenance, configs, calls, folder, adapter = xl_capture(src, decisions, state,
                                                                     tmp_path, monkeypatch)
    assert provenance['tool_mapping'] == {'0': 1, '1': 2, '2': 3, '3': 4, '4': 5}
    assert adapter.normalize_tool_mapping(provenance['tool_mapping'], state) == provenance['tool_mapping']
    filaments = [p for p in configs if p.name.startswith('filament_tool_')]
    assert len(filaments) == 5


# ---------------------------------------------------------------------------
# DATA-3 at the model_job level: slice_job routes a single-colour toolhead
# pick through the merged multi-tool slice (P1-73), and the merge planner no
# longer refuses toolhead picks in a Creator-configured process (P1-72).

# Explicit-parameter stubs: they carry model_job's real signatures, so a changed
# signature fails these tests instead of vanishing into *args (2.2.5 review).
def _verify_gcode_offline_ok(gcode_path, state, adapter=None, recorded_temps=None):
    return {'ok': True}


def _verify_gcode_none(gcode_path, adapter=None, state=None, printer_id=None, recorded_temps=None):
    return None


def _recording_slice_model(mapping):
    calls = []

    def fake(source_, folder, state, brim=None, **kwargs):
        calls.append(kwargs)
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / 'plate_1.gcode'
        out.write_text('; estimated printing time (normal mode) = 1h 0m 0s\nG1 X1 Y1 Z0.2 E1\n')
        parts = source_colors.inventory(source_)['parts']
        return out, {'tool_mapping': mapping,
                     'geometry': {'parts': [{'part_key': p['key']} for p in parts]}}, []
    fake.calls = calls
    return fake


def test_slice_job_single_colour_slices_on_the_picked_toolhead(creator, tmp_path, monkeypatch):
    src = CASES / 'single-part' / 'model.3mf'
    manifest = source_colors.inventory(src)
    state = tool_state()
    selection = mj.resolve_filament_selection(state, dict(state['filament_slots']['2'], slot=2))
    job = {'model_id': '1', 'downloaded_file': str(src), 'printer_model': creator,
           'color_schema': 'part_slots_v1',
           'audit': {'plates': 1, 'size_x': '30', 'size_y': '30', 'size_z': '10'},
           'color_decisions': {'schema': 'part_slots_v1', 'mode': 'single_color',
                               'source_sha256': manifest['source_sha256'], 'selection': selection}}
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps(job))
    monkeypatch.setattr(mj.printer_state, 'load', lambda *a, **k: state)
    fake = _recording_slice_model({'1': 2})
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    monkeypatch.setattr(mj, 'verify_gcode_offline', _verify_gcode_offline_ok)
    monkeypatch.setattr(mj, 'verify_gcode', _verify_gcode_none)
    out = mj.slice_job(jp)
    # The merged multi-tool route ran (placement shifts were planned and
    # passed), not the generic tail that used to drop the pick.
    assert fake.calls and fake.calls[0].get('item_shifts') is not None
    saved = json.loads(jp.read_text())
    assert saved['tool_mapping'] == {'1': 2}
    assert saved['filament_selection']['slot'] == 2
    assert saved['plate_merge']['mode'] == 'merge'
    assert Path(out).exists()


def _single_colour_job(tmp_path, creator, **extra):
    src = CASES / 'single-part' / 'model.3mf'
    manifest = source_colors.inventory(src)
    state = tool_state()
    selection = mj.resolve_filament_selection(state, dict(state['filament_slots']['2'], slot=2))
    job = {'model_id': '1', 'downloaded_file': str(src), 'printer_model': creator,
           'color_schema': 'part_slots_v1',
           'audit': {'plates': 1, 'size_x': '30', 'size_y': '30', 'size_z': '10'},
           'color_decisions': {'schema': 'part_slots_v1', 'mode': 'single_color',
                               'source_sha256': manifest['source_sha256'], 'selection': selection},
           **extra}
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps(job))
    return jp, state


def test_slice_job_single_colour_toolhead_pick_refuses_copies(creator, tmp_path, monkeypatch):
    # The merged toolhead path slices the authored geometry once; a copies
    # request refuses with the reason stated instead of silently printing one.
    jp, state = _single_colour_job(tmp_path, creator, copies=2)
    monkeypatch.setattr(mj.printer_state, 'load', lambda *a, **k: state)
    fake = _recording_slice_model({'1': 2})
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    with pytest.raises(RuntimeError, match='copies, resizing and rotation are not supported'):
        mj.slice_job(jp)
    assert not fake.calls


def test_slice_job_single_colour_toolhead_pick_refuses_a_resize(creator, tmp_path, monkeypatch):
    jp, state = _single_colour_job(tmp_path, creator, scale=0.9)
    monkeypatch.setattr(mj.printer_state, 'load', lambda *a, **k: state)
    fake = _recording_slice_model({'1': 2})
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    with pytest.raises(RuntimeError, match='copies, resizing and rotation are not supported'):
        mj.slice_job(jp)
    assert not fake.calls


def test_merge_for_toolhead_picks_survives_a_creator_process(creator, tmp_path, monkeypatch):
    from print_handy.core import plate_merge
    # plate_merge imported FILAMENT_SLOTS=1 in a Creator-configured process;
    # per-part picks on toolheads 2 and 4 must still merge (P1-72).
    monkeypatch.setattr(plate_merge, 'FILAMENT_SLOTS', 1)
    from .test_plate_merge import _source as box_source
    src = tmp_path / 'two-plates.3mf'
    box_source(src, {'1': (20, 20, 10), '2': (20, 20, 10)}, [['1'], ['2']])
    manifest = source_colors.inventory(src)
    state = tool_state()
    choices = {}
    for part, slot in zip(manifest['parts'], [2, 4]):
        record = state['filament_slots'][str(slot)]
        choices['part:' + part['key']] = {'slot': slot, 'color': record['color'], 'material': 'PLA',
                                          'spoolman_id': record['spoolman_id']}
    decisions = {'source_sha256': manifest['source_sha256'], 'mode': 'per_part',
                 'schema': 'part_slots_v1', 'choices': choices}
    job = {'model_id': '1', 'downloaded_file': str(src), 'printer_model': creator,
           'color_schema': 'part_slots_v1', 'audit': {'plates': 2},
           'color_decisions': decisions}
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps(job))
    monkeypatch.setattr(mj.printer_state, 'load', lambda *a, **k: state)
    fake = _recording_slice_model({'1': 2, '3': 4})
    monkeypatch.setattr(orca_adapter, 'slice_model', fake)
    monkeypatch.setattr(mj, 'verify_gcode_offline', _verify_gcode_offline_ok)
    monkeypatch.setattr(mj, 'verify_gcode', _verify_gcode_none)
    out = mj.slice_job(jp)
    assert Path(out).exists()
    saved = json.loads(jp.read_text())
    assert saved['plate_merge']['mode'] == 'merge'
    assert saved['tool_mapping'] == {'1': 2, '3': 4}


def test_slice_single_colour_decisions_on_an_stl_keep_the_plain_path(tmp_path, monkeypatch):
    # An STL carries no tool assignment channel: a recorded single-colour
    # decision must not push it into the 3MF identity export (which would
    # fail); it slices exactly as a plain single-colour STL does today.
    from print_handy.core import slice_guard
    monkeypatch.setattr(orca_adapter, 'pre_slice', lambda **k: slice_guard.Result())
    monkeypatch.setattr(orca_adapter, 'post_slice', lambda *a, **k: slice_guard.Result())
    monkeypatch.setattr(orca_adapter.sg, 'enforce_approved_baseline', lambda out, **k: slice_guard.Result())

    def run(command, **_kwargs):
        out = Path(command[command.index('--outputdir') + 1]) / 'plate_1.gcode'
        out.write_text('; generated by OrcaSlicer 2.4.2\nM104 S0\nG90\nG1 X1 Y1 Z0.2 E1\nEND_PRINT\n' * 40)
        return S(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(orca_adapter.subprocess, 'run', run)
    src = CASES / 'stl-ascii' / 'model.stl'
    manifest = source_colors.inventory(src)
    record = tool_state()['filament_slots']['2']
    decisions = {'source_sha256': manifest['source_sha256'], 'mode': 'single_color',
                 'schema': 'part_slots_v1',
                 'selection': {'slot': 2, 'color': record['color'], 'material': 'PLA',
                               'spoolman_id': record['spoolman_id']}}
    adapter = printers.get('flashforge.creator5', creator_config('flashforge.creator5'))
    output, provenance, configs = orca_adapter.slice_model(
        src, tmp_path / 'slice', tool_state(), adapter=adapter, color_decisions=decisions)
    assert Path(output).exists()
    assert provenance['tool_mapping'] is None  # no identity export for an STL


def test_slice_single_colour_toolhead_pick_rotation_refusal_names_the_real_reason(tmp_path, monkeypatch):
    # 2.2.5 review: the identity path refuses a whole-model rotation, and the message must not call
    # this whole-model single-colour slice "only supported for whole-model single-color slicing".
    state = xl_state()
    with pytest.raises(ValueError, match="slices a 3MF on a multi-tool printer's toolhead"):
        xl_capture(CASES / 'four-colors-one-plate' / 'model.3mf', single_colour_decisions(4, state),
                   state, tmp_path, monkeypatch, rotation=[0, -1, 0, 1, 0, 0, 0, 0, 1])
