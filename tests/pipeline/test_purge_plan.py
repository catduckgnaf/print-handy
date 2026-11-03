"""ZMod purge awareness (#832): detection, the one purge plan, and what follows from it.

Never contacts a printer: Moonraker's answers are faked at the two seams the code reads through
(purge._fetch for plugins.cfg / object lists / save_variables, printer_state._get for the live
mode a slice reads).
"""
import copy
import inspect
import json
from types import SimpleNamespace as S

import pytest

from print_handy import doctor, printers, settings
from print_handy.core import model_job, plate_merge, purge, source_colors
from print_handy.core.background_slice import slice_binding
from print_handy.webui import status as webui_status

from . import test_ifs_purge_mode as ipm
from .test_plate_merge import STATE, _source

# What the maintainer's printer reports (issue #832): QuickSwap, timelapse and recommend enabled,
# the rest installed but not enabled.
PLUGINS_CFG = """# ZMod plugins: one include per enabled plugin
[include plugins/quickswap/*.cfg]
[include plugins/timelapse/*.cfg]
[include plugins/recommend/*.cfg]
#[include plugins/nopoop/*.cfg]
#[include plugins/bambufy/*.cfg]
;[include plugins/lesswaste/*.cfg]
# [include plugins/ifs_jacker/*.cfg]
"""
BASE = 'http://printer.test:7125'


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(purge, '_cache_path', lambda pid, data=None: tmp_path / f'{pid}-zmod.json')


def ad5x():
    return ipm.ad5x()


def fake_printer(monkeypatch, *, use_trash=1, plugins_cfg=PLUGINS_CFG, objects=None, tools=4, fail=()):
    """Fake the read-only GETs the detection makes; returns the list of URLs asked for."""
    asked = []

    def fetch(url, timeout):
        asked.append(url)
        if any(part in url for part in fail):
            raise OSError('unreachable')
        if 'save_variables' in url:
            variables = {'display_off': 1, 'allowed_tool_count': tools}
            if use_trash is not None:
                variables['use_trash_on_print'] = use_trash
            return json.dumps({'result': {'status': {'save_variables': {'variables': variables}}}})
        if url.endswith('plugins.cfg'):
            if plugins_cfg is None:
                raise OSError('not found')
            return plugins_cfg
        if url.endswith('/printer/objects/list'):
            return json.dumps({'result': {'objects': objects or []}})
        raise AssertionError(url)
    monkeypatch.setattr(purge, '_fetch', fetch)
    return asked


def data_with(**purge_settings):
    data = copy.deepcopy(settings.current())
    zmod = purge_settings.pop('zmod_plugins', None)
    data['purge'].update(purge_settings)
    if zmod is not None:
        data['zmod']['plugins'] = zmod
    return data


def plan_for(monkeypatch, cache=None, adapter=None, **purge_settings):
    monkeypatch.setattr(purge, 'read_cache', lambda pid, data=None: dict(cache or {}))
    return purge.resolve('flashforge-ad5x', data_with(**purge_settings), adapter=adapter or ad5x())


# ------------------------------------------------------------------ detection

def test_plugins_cfg_lists_only_enabled_includes():
    assert purge.parse_plugins_cfg(PLUGINS_CFG) == ['quickswap', 'timelapse', 'recommend']
    assert purge.parse_plugins_cfg('') == []
    assert purge.parse_plugins_cfg(None) == []


def test_objects_list_fallback_names_known_plugins():
    objects = ['gcode_macro _QS_A_CHANGE_FILAMENT', 'gcode_macro QS_IFS_CALIBRATION_SPEED', 'extruder',
               'ifs_jacker', 'gcode_macro IFS_JACKER_CHECK_PRESENCE', 'gcode_macro BAMBUFY_PURGE']
    assert purge.plugins_from_objects(objects) == ['quickswap', 'ifs_jacker', 'bambufy']
    assert purge.plugins_from_objects(['extruder', 'heater_bed']) == []
    assert purge.plugins_from_objects(None) == []


def test_conflicts_are_notes_for_quickswap_with_bambufy_or_lesswaste():
    assert purge.conflicts(['quickswap', 'bambufy']) and purge.conflicts(['lesswaste', 'quickswap'])
    assert purge.conflicts(['quickswap', 'nopoop']) == []
    assert purge.conflicts(['bambufy', 'lesswaste']) == []


def test_refresh_reads_the_maintainers_printer_and_caches_it(monkeypatch):
    asked = fake_printer(monkeypatch)
    record = purge.refresh({'moonraker_url': BASE}, ad5x(), 'flashforge-ad5x')
    assert record['use_trash_on_print'] == 1 and record['allowed_tool_count'] == 4
    assert record['plugins'] == ['quickswap', 'timelapse', 'recommend'] and record['plugins_source'] == 'plugins.cfg'
    assert purge.read_cache('flashforge-ad5x')['use_trash_on_print'] == 1
    assert asked == [f'{BASE}/printer/objects/query?save_variables', f'{BASE}/server/files/config/mod_data/plugins.cfg']


def test_refresh_falls_back_to_the_objects_list(monkeypatch):
    fake_printer(monkeypatch, plugins_cfg=None, objects=['gcode_macro _QS_A_CHANGE_FILAMENT', 'ifs_jacker'])
    record = purge.refresh({'moonraker_url': BASE}, ad5x(), 'flashforge-ad5x')
    assert record['plugins'] == ['quickswap', 'ifs_jacker'] and record['plugins_source'] == 'objects list'


def test_refresh_failure_is_unknown_not_an_error_and_keeps_the_last_reading(monkeypatch):
    fake_printer(monkeypatch)
    purge.refresh({'moonraker_url': BASE}, ad5x(), 'flashforge-ad5x')
    fake_printer(monkeypatch, fail=('save_variables',))
    record = purge.refresh({'moonraker_url': BASE}, ad5x(), 'flashforge-ad5x')
    assert 'could not read' in record['error'] and record['use_trash_on_print'] == 1  # last reading kept
    fake_printer(monkeypatch, plugins_cfg=None, fail=('/printer/objects/list',))
    record = purge.refresh({'moonraker_url': BASE}, ad5x(), 'flashforge-ad5x')
    assert record['plugins'] is None  # plugins unknown, mode still read
    assert purge.refresh({}, ad5x(), 'flashforge-ad5x')['error']  # no URL: says so, never raises


def test_refresh_skips_a_printer_without_zmod(monkeypatch):
    asked = fake_printer(monkeypatch)
    assert purge.refresh({'moonraker_url': BASE}, S(has_native_screen=False), 'x') == {}
    assert asked == []


# ------------------------------------------------------------------ the plan

@pytest.mark.parametrize('cache, settings_, mode, tower', [
    ({'use_trash_on_print': 1}, {}, 'printer_chute', False),       # the maintainer's printer
    ({'use_trash_on_print': 0}, {}, 'prime_tower', True),           # nopoop keeps the tower
    ({'use_trash_on_print': 2}, {}, 'slicer_chute', True),
    ({}, {}, 'unknown', True),                                       # nothing read: today's behavior
    ({'use_trash_on_print': None}, {}, 'unknown', True),
    ({'use_trash_on_print': 1}, {'prime_tower': 'on'}, 'printer_chute', True),
    ({'use_trash_on_print': 0}, {'prime_tower': 'off'}, 'prime_tower', False),
    ({}, {'prime_tower': 'off'}, 'unknown', False),
    ({'use_trash_on_print': 0}, {'mode': 'printer_chute'}, 'printer_chute', False),   # settings win
    ({'use_trash_on_print': 1}, {'mode': 'prime_tower'}, 'prime_tower', True),
    ({}, {'mode': 'printer_chute'}, 'printer_chute', False),                           # detection failed
    ({}, {'mode': 'slicer_chute'}, 'slicer_chute', True),
    ({'use_trash_on_print': 1}, {'mode': 'auto', 'prime_tower': 'auto'}, 'printer_chute', False),
])
def test_resolve_every_mode_and_override(monkeypatch, cache, settings_, mode, tower):
    plan = plan_for(monkeypatch, cache, **settings_)
    assert (plan.mode, plan.prime_tower, plan.zmod) == (mode, tower, True)


def test_explicit_printer_chute_is_honored_when_detection_fails(monkeypatch):
    plan = plan_for(monkeypatch, {}, mode='printer_chute')
    assert plan.mode == 'printer_chute' and plan.towerless and plan.refusal is None
    assert 'settings' in plan.source


def test_slicer_chute_and_towerless_nopoop_are_refused_with_a_plain_message(monkeypatch):
    chute2 = plan_for(monkeypatch, {'use_trash_on_print': 2})
    assert 'USE_TRASH_ON_PRINT=2' in chute2.refusal and 'Nothing was sliced' in chute2.refusal
    assert plan_for(monkeypatch, {}, mode='slicer_chute').refusal
    nothing_purges = plan_for(monkeypatch, {'use_trash_on_print': 0}, prime_tower='off')
    assert 'nothing would purge' in nothing_purges.refusal
    assert plan_for(monkeypatch, {'use_trash_on_print': 1}).refusal is None


def test_flush_multiplier_and_plugins_come_from_settings_and_detection(monkeypatch):
    plan = plan_for(monkeypatch, {'use_trash_on_print': 1, 'plugins': ['quickswap']}, flush_multiplier=0.2)
    assert plan.flush_multiplier == 0.2 and plan.plugins == ('quickswap',)
    assert plan_for(monkeypatch, {'plugins': ['nopoop']}, zmod_plugins=['QuickSwap']).plugins == ('quickswap',)
    assert plan_for(monkeypatch, {'use_trash_on_print': 1}).flush_multiplier is None


def test_a_printer_without_zmod_is_unchanged(monkeypatch):
    plan = plan_for(monkeypatch, {'use_trash_on_print': 1}, adapter=S(has_native_screen=False),
                    mode='printer_chute', flush_multiplier=0.2)
    assert (plan.zmod, plan.prime_tower, plan.flush_multiplier, plan.recipe()) == (False, True, None, None)


def test_live_resolve_reads_the_printer_not_the_cache(monkeypatch):
    ipm.fake_moonraker(monkeypatch, 1)
    monkeypatch.setattr(purge, 'read_cache', lambda pid, data=None: {'use_trash_on_print': 0})
    live = purge.resolve(state={'moonraker_url': BASE}, adapter=ad5x(), live=True)
    cached = purge.resolve(state={'moonraker_url': BASE}, adapter=ad5x())
    assert (live.mode, cached.mode) == ('printer_chute', 'prime_tower')


def test_tower_width_is_zero_without_a_tower(monkeypatch):
    from print_handy.core import color_geometry
    chute = plan_for(monkeypatch, {'use_trash_on_print': 1})
    tower = plan_for(monkeypatch, {'use_trash_on_print': 0})
    assert purge.tower_mm(0.4, chute) == 0.0
    assert purge.tower_mm(0.4, tower) == color_geometry.effective_tower_mm(0.4) > 0
    assert purge.tower_mm(0.6, None) == color_geometry.effective_tower_mm(0.6)


# ------------------------------------------------------------------ the fit check

def _two_wide_parts(tmp_path):
    """Two 200 x 100 mm parts in two slots: they fit a 220 mm bed only when no tower is reserved."""
    source = tmp_path / 's.3mf'
    _source(source, {'1': (200, 100, 10), '2': (200, 100, 10)}, [['1', '2']])
    manifest = source_colors.inventory(source)
    slots = {p['key']: (1 if p['object_id'] == '1' else 2) for p in manifest['parts']}
    return source, manifest, model_job.plate_manifest(source)['plates'], slots


def test_fit_reserves_no_tower_for_a_printer_chute_and_the_old_tower_otherwise(tmp_path, monkeypatch):
    source, manifest, plates, slots = _two_wide_parts(tmp_path)
    state = json.loads(STATE.read_text())
    chute = plan_for(monkeypatch, {'use_trash_on_print': 1})
    tower = plan_for(monkeypatch, {'use_trash_on_print': 0})
    unknown = plan_for(monkeypatch, {})
    reserved = []
    real_pack = plate_merge.pack
    monkeypatch.setattr(plate_merge, 'pack', lambda *a, **k: reserved.append(k.get('reserve_y')) or real_pack(*a, **k))
    monkeypatch.setattr(purge, 'resolve', lambda *a, **k: chute)
    merged = plate_merge.plan(source, manifest, plates, slots, state)
    assert merged['mode'] == 'merge' and 'prime_tower_xy_mm' not in merged
    assert reserved[0] == 0.0
    for old in (tower, unknown):
        reserved.clear()
        monkeypatch.setattr(purge, 'resolve', lambda *a, _p=old, **k: _p)
        split = plate_merge.plan(source, manifest, plates, slots, state)
        assert split['mode'] == 'split' and 'prime tower' in split['reason']
        assert reserved[0] > 45


# ------------------------------------------------------------------ the Orca slice

def test_chute_slice_has_no_prime_tower_and_records_the_plan(tmp_path, monkeypatch):
    ipm.fake_moonraker(monkeypatch, 1)
    machine, process, provenance, _ = ipm.slice_ad5x(tmp_path, monkeypatch, [1, 2, 1, 2])
    assert process['enable_prime_tower'] == '0' and process['purge_in_prime_tower'] == '0'
    # Towerless means no tower position at all; the template width stays because Orca
    # range-checks prime_tower_width even when the tower is off.
    assert 'wipe_tower_x' not in process and process['prime_tower_width'] != '0'
    assert provenance['geometry']['prime_tower_xy_mm'] is None
    assert provenance['geometry']['purge_plan']['mode'] == 'printer_chute'
    assert provenance['geometry']['purge_plan']['prime_tower'] is False


def test_prime_tower_on_keeps_the_tower_for_a_chute_printer(tmp_path, monkeypatch):
    ipm.fake_moonraker(monkeypatch, 1)
    monkeypatch.setitem(settings.current()['purge'], 'prime_tower', 'on')
    _, process, provenance, _ = ipm.slice_ad5x(tmp_path, monkeypatch, [1, 2, 1, 2])
    assert process['enable_prime_tower'] == '1' and process['prime_tower_width'] != '0'
    assert provenance['geometry']['purge_plan']['prime_tower'] is True


def test_flush_multiplier_reaches_the_orca_process(tmp_path, monkeypatch):
    ipm.fake_moonraker(monkeypatch, 1)
    monkeypatch.setitem(settings.current()['purge'], 'flush_multiplier', 0.2)
    _, process, _, _ = ipm.slice_ad5x(tmp_path, monkeypatch, [1, 2, 1, 2])
    assert process['flush_multiplier'] == ['0.2']
    ipm.fake_moonraker(monkeypatch, 0)
    _, nopoop, _, _ = ipm.slice_ad5x(tmp_path / 'nopoop', monkeypatch, [1, 2, 1, 2])
    assert nopoop['flush_multiplier'] == ['0.2'] and nopoop['enable_prime_tower'] == '1'


def test_slicer_chute_setting_refuses_to_slice(tmp_path, monkeypatch):
    ipm.fake_moonraker(monkeypatch, 1)
    monkeypatch.setitem(settings.current()['purge'], 'mode', 'slicer_chute')
    with pytest.raises(RuntimeError, match='Nothing was sliced'):
        ipm.slice_ad5x(tmp_path, monkeypatch, [1, 2, 1, 2])
    assert not (tmp_path / 'slice' / 'plate_1.gcode').exists()


def test_quickswap_with_printer_chute_slices_towerless_and_launches_without_a_warning(tmp_path, monkeypatch):
    """The maintainer's working setup: use_trash_on_print=1 with QuickSwap enabled. No QuickSwap check exists."""
    ipm.fake_moonraker(monkeypatch, 1)
    monkeypatch.setattr(purge, 'read_cache', lambda pid, data=None: {'use_trash_on_print': 1, 'plugins': ['quickswap']})
    plan = purge.resolve(state={'moonraker_url': BASE}, adapter=ad5x(), live=True)
    assert plan.mode == 'printer_chute' and plan.towerless and plan.plugins == ('quickswap',)
    assert plan.refusal is None and purge.conflicts(plan.plugins) == []
    _, process, _, _ = ipm.slice_ad5x(tmp_path, monkeypatch, [1, 2, 1, 2])
    assert process['enable_prime_tower'] == '0'
    said = []
    purge.require_launch_safe('; enable_prime_tower = 0\nT0\n', {'moonraker_url': BASE}, said.append)
    assert said == []


# ------------------------------------------------------------------ the recipe

def test_recipe_changes_with_the_purge_plan(monkeypatch):
    assert printers.current().has_native_screen, 'the default test printer is the AD5X'
    job = {'color_decisions': {'mode': 'single_color', 'source_sha256': 's',
                               'selection': {'slot': 1, 'color': 'white', 'material': 'PLA'}}, 'scale': 1.0}

    def binding(cache, **cfg):
        monkeypatch.setattr(purge, 'read_cache', lambda pid, data=None: dict(cache))
        for key, value in cfg.items():
            monkeypatch.setitem(settings.current()['purge'], key, value)
        return slice_binding(job, 's')
    chute = binding({'use_trash_on_print': 1})
    assert chute == binding({'use_trash_on_print': 1})
    assert chute != binding({'use_trash_on_print': 0})
    assert chute != binding({'use_trash_on_print': 1}, prime_tower='on')
    assert chute != binding({'use_trash_on_print': 1}, prime_tower='auto', flush_multiplier=0.2)
    assert json.loads(chute)['purge'] == {'mode': 'printer_chute', 'prime_tower': False, 'flush_multiplier': None}


# ------------------------------------------------------------------ the launch guard

TOWERLESS = '; generated by OrcaSlicer\nT0\n; enable_prime_tower = 0\nT1\n'
WITH_TOWER = '; generated by OrcaSlicer\nT0\n; enable_prime_tower = 1\n'


def test_launch_guard_refuses_a_towerless_gcode_only_when_the_printer_reports_nopoop(monkeypatch):
    ipm.fake_moonraker(monkeypatch, 0)
    with pytest.raises(SystemExit) as refused:
        purge.require_launch_safe(TOWERLESS, {'moonraker_url': BASE}, lambda *_: None)
    text = str(refused.value)
    assert 'use_trash_on_print=1' in text and 'use_trash_on_print=0' in text and 'Nothing uploaded or started' in text
    purge.require_launch_safe(WITH_TOWER, {'moonraker_url': BASE}, lambda *_: None)  # a tower purges: fine


def test_launch_guard_lets_a_single_color_towerless_gcode_through_in_nopoop(monkeypatch):
    # Codex on #834: a single-slot file has no color change to purge, so nopoop is irrelevant.
    ipm.fake_moonraker(monkeypatch, 0)
    said = []
    purge.require_launch_safe('; enable_prime_tower = 0\nT0\nG1 X1\n', {'moonraker_url': BASE}, said.append)
    assert said == []


def test_towerless_geometry_places_no_tower():
    # Codex on #834: zero tower width means no tower to place, not a zero-width tower to clear.
    from print_handy.core import color_geometry
    whole_bed = [((0.0, 220.0), (0.0, 220.0), (0.0, 10.0))]
    assert color_geometry.prime_tower_candidates(whole_bed, tower=0) == []
    assert color_geometry.prime_tower_xy(whole_bed, tower=0) is None
    centered = [1.0, 2.0, 0.0]
    assert color_geometry._tower_friendly_delta([(0, 200), (0, 200), (0, 5)], centered, tower=0) == centered
    with pytest.raises(RuntimeError):
        color_geometry.prime_tower_candidates(whole_bed, tower=45)


def test_launch_guard_is_silent_in_chute_mode(monkeypatch):
    ipm.fake_moonraker(monkeypatch, 1)
    said = []
    purge.require_launch_safe(TOWERLESS, {'moonraker_url': BASE}, said.append)
    assert said == []


@pytest.mark.parametrize('value', [ipm.UNREADABLE, None, 2])
def test_launch_guard_only_informs_when_the_mode_is_unknown_or_two(monkeypatch, value):
    ipm.fake_moonraker(monkeypatch, value)
    said = []
    purge.require_launch_safe(TOWERLESS, {'moonraker_url': BASE}, said.append)
    assert len(said) == 1 and said[0].startswith('NOTE:')


def test_the_mapped_start_runs_the_launch_guard_before_any_printer_write():
    body = inspect.getsource(model_job.print_mapped_native)
    guard, upload = body.index('purge.require_launch_safe('), body.index('_zmod_upload_artifact(')
    assert guard < upload


# ------------------------------------------------------------------ IFS Jacker

def test_ifs_jacker_refuses_more_colors_than_the_modeled_slots(monkeypatch):
    monkeypatch.setattr(purge, 'read_cache', lambda pid, data=None: {'plugins': ['quickswap', 'ifs_jacker']})
    monkeypatch.setattr(source_colors, 'distinct_source_colours', lambda manifest: list(range(5)))
    adapter = S(has_native_screen=True, slot_count=4)
    with pytest.raises(printers.MultiColorUnsupported, match='IFS Jacker.*follow-up'):
        purge.require_jacker_capacity({}, adapter, {'moonraker_url': BASE})
    monkeypatch.setattr(source_colors, 'distinct_source_colours', lambda manifest: list(range(4)))
    purge.require_jacker_capacity({}, adapter, {'moonraker_url': BASE})


def test_without_a_jacker_or_zmod_nothing_is_refused(monkeypatch):
    monkeypatch.setattr(source_colors, 'distinct_source_colours', lambda manifest: list(range(6)))
    monkeypatch.setattr(purge, 'read_cache', lambda pid, data=None: {'plugins': ['quickswap']})
    purge.require_jacker_capacity({}, S(has_native_screen=True, slot_count=4), {})
    monkeypatch.setattr(purge, 'read_cache', lambda pid, data=None: {'plugins': ['ifs_jacker']})
    purge.require_jacker_capacity({}, S(has_native_screen=False, slot_count=4), {})


# ------------------------------------------------------------------ doctor and Web UI

def test_doctor_reports_the_detected_purge_and_plugins(monkeypatch):
    fake_printer(monkeypatch, objects=[])
    data = copy.deepcopy(settings.current())
    data['services']['moonraker_url'] = BASE
    line = doctor.check_purge(data)
    assert 'printer chute' in line and 'no prime tower' in line and 'quickswap' in line


def test_doctor_says_when_the_printer_cannot_be_read(monkeypatch):
    fake_printer(monkeypatch, fail=('save_variables',))
    data = copy.deepcopy(settings.current())
    data['services']['moonraker_url'] = BASE
    line = doctor.check_purge(data)
    assert 'could not be read' in line and 'prime tower' in line


def test_doctor_notes_a_quickswap_conflict_without_refusing(monkeypatch):
    fake_printer(monkeypatch, plugins_cfg=PLUGINS_CFG + '[include plugins/bambufy/*.cfg]\n')
    data = copy.deepcopy(settings.current())
    data['services']['moonraker_url'] = BASE
    assert 'not compatible' in doctor.check_purge(data)


def test_status_roster_shows_the_cached_purge_only_once_one_was_read(monkeypatch):
    data = copy.deepcopy(settings.current())
    data['services']['moonraker_url'] = BASE
    assert 'purge' not in webui_status._roster_section(data)[0]
    fake_printer(monkeypatch)
    purge.refresh({'moonraker_url': BASE}, ad5x(), settings.default_printer_id(data))
    row = webui_status._roster_section(data)[0]['purge']
    assert row['mode'] == 'printer_chute' and row['prime_tower'] is False and 'quickswap' in row['plugins']
