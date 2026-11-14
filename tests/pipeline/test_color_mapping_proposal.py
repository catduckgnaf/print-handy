"""#1473: proposed and remembered color mappings.

The proposal groups parts by author color, matches each group against the
loaded spools (author color and part-name colors), and is only ever a
default: nothing is applied without the operator's tap. An accepted mapping
is remembered per source hash + loaded-spool set and offered first on a
repost; a changed spool set never reuses it.
"""
import asyncio
import json
import zipfile
from types import SimpleNamespace as S

import pytest

from print_handy.core import source_colors
from print_handy.core.discord_color_intake import collect


def plaque(tmp_path):
    """The issue's file: named parts whose names and author colors agree."""
    p = tmp_path / 'plaque.3mf'
    with zipfile.ZipFile(p, 'w') as z:
        z.writestr('Metadata/project_settings.config', json.dumps(
            {'filament_colour': ['#161616', '#CD7F32'], 'filament_type': ['PLA', 'PLA']}))
        z.writestr('Metadata/model_settings.config',
                   '<config><object id="1"><metadata value="Plaque" key="name"/>'
                   '<part id="2"><metadata value="01_PLAQUE_BLACK" key="name"/><metadata value="1" key="extruder"/></part>'
                   '<part id="3"><metadata key="name" value="02_LETTERS_AND_TRIM_BRONZE"/><metadata key="extruder" value="2"/></part>'
                   '<part id="4"><metadata key="name" value="03_FOOT_LEFT_BLACK"/><metadata key="extruder" value="1"/></part>'
                   '<part id="5"><metadata key="name" value="04_FOOT_RIGHT_BLACK"/><metadata key="extruder" value="1"/></part>'
                   '</object><plate><metadata key="plater_id" value="1"/><model_instance>'
                   '<metadata key="object_id" value="1"/></model_instance></plate></config>')
        z.writestr('3D/3dmodel.model',
                   '<model><resources><object id="1"><components><component objectid="2"/>'
                   '<component objectid="3"/><component objectid="4"/><component objectid="5"/></components></object>'
                   '<object id="2"><mesh/></object><object id="3"><mesh/></object>'
                   '<object id="4"><mesh/></object><object id="5"><mesh/></object></resources>'
                   '<build><item objectid="1"/></build></model>')
    return p


SPOOLS = [{'slot': 1, 'color': 'Black', 'material': 'PLA+', 'spoolman_id': 10},
          {'slot': 2, 'color': 'Bronze', 'material': 'PLA+', 'spoolman_id': 11},
          {'slot': 3, 'color': 'White', 'material': 'PLA', 'spoolman_id': 12}]


def plaque_state():
    return {'spool': {'material': 'PLA', 'spoolman_id': 10, 'name': 'black'},
            'filament_slots': {'1': {'color': 'Black', 'material': 'PLA+', 'spoolman_id': 10},
                               '2': {'color': 'Bronze', 'material': 'PLA+', 'spoolman_id': 11},
                               '3': {'color': 'White', 'material': 'PLA', 'spoolman_id': 12}}}


class Adapter:
    """Answers clarifies by index, like the other color-intake test adapters."""

    def __init__(self, picks):
        self.picks = iter(picks)
        self.questions = []

    async def send_clarify(self, **kwargs):
        self.questions.append(kwargs)
        answer = kwargs['choices'][next(self.picks)]
        import tools.clarify_gateway as cm
        cm.resolve_gateway_clarify(kwargs['clarify_id'], answer)
        return S(success=True)

    async def send(self, **kwargs):
        return S(success=True)


def _job(tmp_path, p):
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'downloaded_file': str(p), 'color_schema': 'part_slots_v1',
                              'color_manifest': source_colors.inventory(p)}))
    return jp


# ---------------------------------------------------------------- core

def test_parts_sharing_an_author_color_are_one_group(tmp_path):
    manifest = source_colors.inventory(plaque(tmp_path))
    groups = source_colors.color_groups(manifest)
    assert len(groups) == 2
    black = next(g for g in groups if g['color_words'] == ('black',))
    assert len(black['keys']) == 3  # plaque + both feet are one decision
    assert black['label'] == 'Plaque, Foot Left and Foot Right'


def test_proposal_matches_author_color_and_part_names(tmp_path):
    manifest = source_colors.inventory(plaque(tmp_path))
    proposal = source_colors.propose_mapping(manifest, SPOOLS)
    assert proposal['complete'] and proposal['matched'] == 2
    assert source_colors.mapping_summary(proposal['groups']) == (
        'Plaque, Foot Left and Foot Right: Black PLA+, Letters and Trim: Bronze PLA+')


def test_proposal_uses_a_part_name_color_when_the_author_color_has_no_spool(tmp_path):
    manifest = source_colors.inventory(plaque(tmp_path))
    spools = [{'slot': 1, 'color': 'Bronze', 'material': 'PLA+', 'spoolman_id': 11},
              {'slot': 2, 'color': 'Red', 'material': 'PLA', 'spoolman_id': 12}]
    proposal = source_colors.propose_mapping(manifest, spools)
    bronze = next(g for g in proposal['groups'] if g['color_words'] == ('orange',) or 'bronze' in g['name_words'])
    assert bronze['slot'] == 1  # matched by the BRONZE in the part name


def test_proposal_never_guesses_between_duplicate_spools_or_across_materials(tmp_path):
    manifest = source_colors.inventory(plaque(tmp_path))
    dupes = [{'slot': 1, 'color': 'Black', 'material': 'PLA+', 'spoolman_id': 10},
             {'slot': 2, 'color': 'Black', 'material': 'PLA+', 'spoolman_id': 11},
             {'slot': 3, 'color': 'Bronze', 'material': 'PLA+', 'spoolman_id': 12}]
    proposal = source_colors.propose_mapping(manifest, dupes)
    black = next(g for g in proposal['groups'] if g['color_words'] == ('black',))
    assert black['slot'] is None and not proposal['complete']
    petg = [{'slot': 1, 'color': 'Black', 'material': 'PETG', 'spoolman_id': 10},
            {'slot': 2, 'color': 'Bronze', 'material': 'PLA+', 'spoolman_id': 11}]
    proposal = source_colors.propose_mapping(manifest, petg)
    black = next(g for g in proposal['groups'] if g['color_words'] == ('black',))
    assert black['slot'] is None  # a different known material is a change, not a match


def test_remembered_mapping_round_trip_and_spool_set_guard(tmp_path):
    manifest = source_colors.inventory(plaque(tmp_path))
    store = tmp_path / 'mappings.json'
    proposal = source_colors.propose_mapping(manifest, SPOOLS)
    choices = {k: {'slot': g['slot']} for g in proposal['groups'] for k in g['keys']}
    source_colors.remember_color_mapping(manifest, SPOOLS, choices, path=store)
    remembered = source_colors.remembered_color_mapping(manifest, SPOOLS, path=store)
    assert remembered is not None
    assert remembered['choices'] == {k: v['slot'] for k, v in choices.items()}
    changed = [dict(s) for s in SPOOLS]
    changed[0] = {'slot': 1, 'color': 'Red', 'material': 'PLA', 'spoolman_id': 99}
    assert source_colors.remembered_color_mapping(manifest, changed, path=store) is None
    swapped = [dict(s, spoolman_id=42) if s['slot'] == 2 else dict(s) for s in SPOOLS]
    assert source_colors.remembered_color_mapping(manifest, swapped, path=store) is None


# ---------------------------------------------------------------- Discord collect flow

def test_one_tap_accepts_the_proposed_mapping(tmp_path):
    async def run():
        p = plaque(tmp_path)
        jp = _job(tmp_path, p)
        adapter = Adapter([0])  # Use these colors
        result = await collect(jp, plaque_state(), adapter, 'offline', 'colors-proposal', timeout=1)
        assert len(adapter.questions) == 1
        assert adapter.questions[0]['choices'][:3] == [
            'Use these colors', 'Change colors', 'One color for everything']
        assert 'Plaque, Foot Left and Foot Right: Black PLA+' in adapter.questions[0]['question']
        saved = json.loads(jp.read_text())['color_decisions']
        slots = {k: v['slot'] for k, v in saved['choices'].items()}
        assert set(slots.values()) == {1, 2}
        assert result['mode'] == 'per_part'
    asyncio.run(run())


def test_repost_offers_same_colors_as_last_time_first(tmp_path):
    async def run():
        p = plaque(tmp_path)
        jp = _job(tmp_path, p)
        await collect(jp, plaque_state(), Adapter([0]), 'offline', 'colors-first', timeout=1)
        jp2 = tmp_path / 'repost' / 'JOB.json'
        jp2.parent.mkdir()
        jp2.write_text(json.dumps({'downloaded_file': str(p), 'color_schema': 'part_slots_v1',
                                   'color_manifest': source_colors.inventory(p)}))
        adapter = Adapter([0])
        await collect(jp2, plaque_state(), adapter, 'offline', 'colors-repost', timeout=1)
        assert len(adapter.questions) == 1
        assert adapter.questions[0]['choices'][0] == 'Same colors as last time'
        assert 'Same colors as last time' in adapter.questions[0]['question']
        saved = json.loads(jp2.read_text())['color_decisions']
        assert {v['slot'] for v in saved['choices'].values()} == {1, 2}
    asyncio.run(run())


def test_changed_spool_set_does_not_offer_the_stale_mapping(tmp_path):
    async def run():
        p = plaque(tmp_path)
        jp = _job(tmp_path, p)
        await collect(jp, plaque_state(), Adapter([0]), 'offline', 'colors-first2', timeout=1)
        other = plaque_state()
        other['filament_slots']['2'] = {'color': 'Red', 'material': 'PLA', 'spoolman_id': 21}
        jp2 = tmp_path / 'repost' / 'JOB.json'
        jp2.parent.mkdir()
        jp2.write_text(json.dumps({'downloaded_file': str(p), 'color_schema': 'part_slots_v1',
                                   'color_manifest': source_colors.inventory(p)}))
        adapter = Adapter([2])  # whatever the first question is, never "same as last time"
        with pytest.raises(Exception):
            await collect(jp2, other, adapter, 'offline', 'colors-changed-set', timeout=1)
        assert 'Same colors as last time' not in adapter.questions[0]['choices']
    asyncio.run(run())


def test_proposal_is_never_applied_without_a_tap(tmp_path):
    async def run():
        p = plaque(tmp_path)
        jp = _job(tmp_path, p)
        before = jp.read_text()
        adapter = Adapter([3])  # Cancel, appended after the three offer choices
        from print_handy.core.link_intake import PromptCancelled
        with pytest.raises(PromptCancelled):
            await collect(jp, plaque_state(), adapter, 'offline', 'colors-cancel', timeout=1)
        assert jp.read_text() == before  # no decisions saved
        assert source_colors.remembered_color_mapping(
            source_colors.inventory(p), SPOOLS) is None  # and nothing remembered
    asyncio.run(run())


def test_change_asks_one_question_per_color_group_not_per_part(tmp_path):
    async def run():
        p = plaque(tmp_path)
        jp = _job(tmp_path, p)
        # Change colors; black group -> Bronze (label index 1), bronze group -> Black (index 0).
        adapter = Adapter([1, 1, 0])
        await collect(jp, plaque_state(), adapter, 'offline', 'colors-change', timeout=1)
        assert len(adapter.questions) == 3  # offer + 2 color groups, not offer + 4 parts
        saved = json.loads(jp.read_text())['color_decisions']
        slots = {k: v['slot'] for k, v in saved['choices'].items()}
        black_keys = [k for k, v in slots.items() if v == 2]
        assert len(black_keys) == 3  # the three black parts moved together
    asyncio.run(run())


# ---------------------------------------------------------------- batch asks

def test_ask_batch_uses_one_batch_call_when_the_adapter_offers_it():
    async def run():
        from print_handy.core import link_intake
        calls = []

        class BatchAdapter:
            async def send_clarify_batch(self, chat_id, question, groups, session_key, metadata=None):
                calls.append(groups)
                import tools.clarify_gateway as cm
                for group in groups:
                    cm.resolve_gateway_clarify(group['clarify_id'], group['choices'][1])
                return S(success=True)

        source = S(chat_id='c1', thread_id=None)
        groups = [{'label': 'Plaque', 'choices': ['Black PLA+', 'Bronze PLA+']},
                  {'label': 'Trim', 'choices': ['Black PLA+', 'Bronze PLA+']}]
        picks = await link_intake.ask_batch(BatchAdapter(), source, 'batch-key',
                                            'Choose:', groups, timeout=5)
        assert picks == [1, 1]
        assert len(calls) == 1 and len(calls[0]) == 2
    asyncio.run(run())


def test_ask_batch_falls_back_to_one_question_per_group():
    async def run():
        from print_handy.core import link_intake

        class PlainAdapter:
            def __init__(self):
                self.questions = []

            async def send_clarify(self, **kwargs):
                self.questions.append(kwargs)
                import tools.clarify_gateway as cm
                cm.resolve_gateway_clarify(kwargs['clarify_id'], kwargs['choices'][0])
                return S(success=True)

        adapter = PlainAdapter()
        source = S(chat_id='c1', thread_id=None)
        groups = [{'label': 'Plaque', 'choices': ['Black PLA+']},
                  {'label': 'Trim', 'choices': ['Bronze PLA+']}]
        picks = await link_intake.ask_batch(adapter, source, 'batch-fallback',
                                            'Choose:', groups, timeout=5)
        assert picks == [0, 0]
        assert len(adapter.questions) == 2
    asyncio.run(run())
