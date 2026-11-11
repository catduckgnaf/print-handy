import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace as S
import pytest
from print_handy.core import source_colors, model_job
from print_handy.core.discord_color_intake import collect
from .test_color_intake import source, state

class Adapter:
    def __init__(self, picks):
        self.picks = iter(picks)
        self.questions = []
        self.sent = []
    async def send_clarify(self, **kwargs):
        self.questions.append(kwargs)
        answer = kwargs['choices'][next(self.picks)]
        import tools.clarify_gateway as cm
        cm.resolve_gateway_clarify(kwargs['clarify_id'], answer)
        return S(success=True)
    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return S(success=True)

Transport = Adapter

def test_canonical_part_rows_save_distinct_slot_mapping(tmp_path):
    async def run():
        p = source(tmp_path)
        jp = tmp_path / 'JOB.json'
        jp.write_text(json.dumps({'downloaded_file': str(p), 'color_schema': 'part_slots_v1',
                                  'color_manifest': source_colors.inventory(p)}))
        adapter = Adapter([0, 1, 0, 0])
        result = await collect(jp, state(), adapter, 'offline', 'colors-offline', timeout=1)
        saved = json.loads(jp.read_text())['color_decisions']
        keys = list(saved['choices'])
        assert [saved['choices'][key]['slot'] for key in keys] == [3, 1]
        assert result['decisions'] == saved
        assert len(adapter.questions) == 3
        assert all('source filament' not in q['question'] for q in adapter.questions)
    asyncio.run(run())

def test_build_slot_labels_formats_cleanly_and_handles_duplicates():
    selections = [
        {'slot': 1, 'color': 'white', 'material': 'PLA'},
        {'slot': 2, 'color': 'pink', 'material': 'PLA'},
        {'slot': 3, 'color': 'black', 'material': 'PLA'},
        {'slot': 4, 'color': 'dark green', 'material': 'PLA'},
    ]
    labels = model_job.build_slot_labels(selections)
    assert labels == ['White PLA', 'Pink PLA', 'Black PLA', 'Dark Green PLA']
    assert model_job.build_slot_labels([{'slot': 2, 'color': 'Pink PLA', 'material': 'pla'}]) == ['Pink PLA']

    dup_selections = [
        {'slot': 1, 'color': 'black', 'material': 'PLA'},
        {'slot': 2, 'color': 'pink', 'material': 'PLA'},
        {'slot': 3, 'color': 'black', 'material': 'PLA'},
        {'slot': 4, 'color': 'dark green', 'material': 'PLA'},
    ]
    dup_labels = model_job.build_slot_labels(dup_selections)
    assert dup_labels == ['Black PLA (Slot 1)', 'Pink PLA', 'Black PLA (Slot 3)', 'Dark Green PLA']


def test_format_color_title_preserves_materials_and_capitalizes_words():
    assert model_job.format_color_title('white') == 'White'
    assert model_job.format_color_title('dark green') == 'Dark Green'
    assert model_job.format_color_title('shiny silk rainbow') == 'Shiny Silk Rainbow'


def test_all_stencils_same_color_uses_existing_single_color_collect_api(tmp_path):

    async def run():
        p = source(tmp_path)
        jp = tmp_path / 'JOB.json'
        jp.write_text(json.dumps({'downloaded_file': str(p), 'color_manifest': source_colors.inventory(p)}))
        adapter = Adapter([1, 0])  # one-color mode, then the selected configured slot
        result = await collect(jp, state(), adapter, 'offline', 'colors-all-stencils-one', timeout=1)
        saved = json.loads(jp.read_text())['color_decisions']
        source_colors.validate_decisions(source_colors.inventory(p), saved, state(), model_job.resolve_filament_selection)
        assert result['mode'] == 'single_color'
        assert result['slots'] == [1]
        assert saved['mode'] == 'single_color'
        assert saved['selection']['slot'] == 1
        assert len(adapter.questions) == 2
        assert adapter.questions[0]['choices'] == [
            'Preserve colors per stencil/source region', 'All stencils same color', 'Cancel']
        assert adapter.questions[1]['question'] == 'Choose the one color for all stencils'
    asyncio.run(run())


def test_legacy_prompts_preserve_existing_valid_round_trip(tmp_path):
    async def run():
        p = source(tmp_path)
        jp = tmp_path / 'JOB.json'
        jp.write_text(json.dumps({'downloaded_file': str(p), 'color_manifest': source_colors.inventory(p)}))
        adapter = Adapter([0, 0, 0, 1, 0])
        result = await collect(jp, state(), adapter, 'offline', 'colors-legacy', timeout=1)
        source_colors.validate_decisions(source_colors.inventory(p), result['decisions'], state(), model_job.resolve_filament_selection)
        assert result['mode'] == 'per_part'
        assert len(adapter.questions) == 5
    asyncio.run(run())

def test_existing_decisions_are_not_reasked_or_replayed(tmp_path):
    async def run():
        p = source(tmp_path)
        manifest = source_colors.inventory(p)
        selection = {'slot': 1, 'color': 'white', 'material': 'PLA', 'spoolman_id': 10}
        decisions = {'source_sha256': manifest['source_sha256'], 'mode': 'single_color', 'selection': selection}
        jp = tmp_path / 'JOB.json'
        jp.write_text(json.dumps({'downloaded_file': str(p), 'color_manifest': manifest, 'color_decisions': decisions}))
        adapter = Adapter([])
        with pytest.raises(ValueError, match='already exist'):
            await collect(jp, state(), adapter, 'offline', 'colors-replay', timeout=1)
        assert not adapter.questions
        assert json.loads(jp.read_text())['color_decisions'] == decisions
    asyncio.run(run())

def test_source_change_during_callback_refused(tmp_path):
    async def run():
        p = source(tmp_path)
        jp = tmp_path / 'JOB.json'
        jp.write_text(json.dumps({'downloaded_file': str(p), 'color_schema': 'part_slots_v1',
                                  'color_manifest': source_colors.inventory(p)}))
        class MutatingAdapter(Adapter):
            async def send_clarify(self, **kwargs):
                result = await super().send_clarify(**kwargs)
                p.write_bytes(p.read_bytes() + b'changed')
                return result
        with pytest.raises(ValueError, match='source changed'):
            await collect(jp, state(), MutatingAdapter([0, 0, 0]), 'offline', 'colors-changed', timeout=1)
        assert 'color_decisions' not in json.loads(jp.read_text())
    asyncio.run(run())


def test_cancel_on_first_question_saves_nothing(tmp_path):
    async def run():
        p = source(tmp_path)
        jp = tmp_path / 'JOB.json'
        jp.write_text(json.dumps({'downloaded_file': str(p), 'color_manifest': source_colors.inventory(p)}))
        before = jp.read_text()
        adapter = Adapter([2])  # the Cancel button appended after the two color choices
        from print_handy.core.link_intake import PromptCancelled
        with pytest.raises(PromptCancelled):
            await collect(jp, state(), adapter, 'offline', 'colors-cancel', timeout=1)
        assert jp.read_text() == before
        assert adapter.questions[0]['choices'][-1] == 'Cancel'
    asyncio.run(run())


def test_discord_flow_rechecks_the_source_by_hash_not_a_full_inventory(tmp_path, monkeypatch):
    # #1155: the post-selection "has the source changed" check needs only the
    # file hash; a full inventory() there re-decoded every painted triangle.
    p = source(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'downloaded_file': str(p), 'color_schema': 'part_slots_v1',
                              'color_manifest': source_colors.inventory(p)}))
    real = source_colors.inventory
    calls = []

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(source_colors, 'inventory', counting)
    asyncio.run(collect(jp, state(), Adapter([0, 1, 0, 0]), 'offline', 'colors-hash-only', timeout=1))
    # One read to build the questions, one inside intake_color_choices (the
    # canonical validator); none for the source-changed check.
    assert len(calls) == 2


def _count_inventory_passes(monkeypatch):
    """Count the real inventory passes: each one resolves the build's parts once."""
    real = source_colors.component_parts
    passes = []

    def counting(archive):
        passes.append(archive.filename)
        return real(archive)

    monkeypatch.setattr(source_colors, 'component_parts', counting)
    return passes


def test_discord_flow_inventories_the_source_once(tmp_path, monkeypatch):
    # #1155 paint pass cost: the question build and intake_color_choices each
    # ran a full inventory (every painted triangle decoded again). One flow
    # now shares one inventory of the source by its content hash.
    p = source(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'downloaded_file': str(p), 'color_schema': 'part_slots_v1',
                              'color_manifest': source_colors.inventory(p)}))
    passes = _count_inventory_passes(monkeypatch)
    result = asyncio.run(collect(jp, state(), Adapter([0, 1, 0, 0]), 'offline', 'colors-once', timeout=1))
    assert result['status'] == 'COLOR_CHOICES_SAVED'
    assert passes == [str(p)]
    # Nothing outlives the flow: a later call inventories again.
    source_colors.inventory(p)
    assert len(passes) == 2


def test_discord_flow_reinventories_a_source_changed_during_selection(tmp_path, monkeypatch):
    # The shared inventory is keyed by content, so a changed source is never
    # served the old manifest: it is inventoried again and refused.
    p = source(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'downloaded_file': str(p), 'color_schema': 'part_slots_v1',
                              'color_manifest': source_colors.inventory(p)}))
    with source_colors.inventory_scope():
        first = source_colors.inventory(p)
        first['parts'].clear()  # a caller's copy is its own
        assert source_colors.inventory(p)['parts']
        passes = _count_inventory_passes(monkeypatch)
        p.write_bytes(p.read_bytes() + b'changed')
        source_colors.inventory(p)
    assert passes == [str(p)]


def test_discord_flow_refuses_a_source_changed_during_selection(tmp_path):
    p = source(tmp_path)
    jp = tmp_path / 'JOB.json'
    jp.write_text(json.dumps({'downloaded_file': str(p), 'color_schema': 'part_slots_v1',
                              'color_manifest': source_colors.inventory(p)}))

    class Changing(Adapter):
        async def send_clarify(self, **kwargs):
            if len(self.questions) == 1:
                with open(p, 'ab') as handle:
                    handle.write(b'\0')
            return await super().send_clarify(**kwargs)

    with pytest.raises(ValueError, match='source changed during Discord selection'):
        asyncio.run(collect(jp, state(), Changing([0, 1, 0, 0]), 'offline', 'colors-changed', timeout=1))
    assert 'color_decisions' not in json.loads(jp.read_text())
