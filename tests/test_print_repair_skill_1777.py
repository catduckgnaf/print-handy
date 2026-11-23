"""The bundled print repair skill (#1777): files, tool schemas and the confined tool runner."""
import json
import logging
import re

import numpy as np
import pytest

from print_handy.core import layer_scan, support_clearance
from print_handy.skills import print_repair
from print_handy.skills.print_repair import tools

from tests.core.test_layer_scan import box

DIAGNOSIS = {'island_scan', 'overhang_scan', 'bodies', 'watertight', 'nonmanifold_edges',
             'underside_profile', 'clearance_report'}
REPAIR = {'trim_below_plane', 'gusset_45', 'fill_under_lip', 'straight_base',
          'extend_underside', 'sink', 'repair_mesh'}


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / 'source').mkdir()
    support_clearance._write_binary_stl(
        tmp_path / 'source' / 'part.stl',
        np.concatenate((box(0, 0, 0, 10, 10, 20), box(20, 0, 5, 30, 8, 7))))
    return tmp_path


def test_the_skill_loads_from_the_package():
    skill = print_repair.load()
    assert skill.name == 'print_repair'
    assert skill.instructions.startswith('# Print repair')
    assert set(skill.references) == {'bridges', 'defects-vs-design', 'orientation', 'printer-facts',
                                     'support-limits', 'worked-example-couch-organizer'}
    prompt = skill.system_prompt()
    for name, text in skill.references.items():
        assert f'## Reference: {name}' in prompt and text.strip() in prompt


def test_every_tool_named_in_the_instructions_is_registered():
    named = set(re.findall(r'`([a-z_]+)`', print_repair.load().instructions))
    assert named <= set(tools.TOOLS), named - set(tools.TOOLS)


@pytest.mark.parametrize('name', sorted(print_repair.load().references) + ['SKILL'])
def test_texts_are_short_and_carry_no_config_syntax(name):
    """References are under two pages and never hand end users config syntax."""
    skill = print_repair.load()
    text = skill.instructions if name == 'SKILL' else skill.references[name]
    assert len(text) < 6000 and len(text.splitlines()) < 110
    assert not re.search(r'^\s*\[[\w.\-]+\]\s*$', text, re.M), 'TOML table header'
    assert not re.search(r'^\s*[\w.]+\s*=\s*\S', text, re.M), 'key = value line'
    assert not re.search(r'PRINT_HANDY_|\.env\b|\.toml\b|\.ya?ml\b', text)
    assert '```' not in text


def test_schemas_are_openai_function_tools():
    schemas = tools.openai_tools()
    assert print_repair.load().tools == schemas
    names = [s['function']['name'] for s in schemas]
    assert set(names) == DIAGNOSIS | REPAIR and len(names) == len(set(names))
    for schema in schemas:
        assert schema['type'] == 'function'
        function = schema['function']
        assert function['description']
        params = function['parameters']
        assert params['type'] == 'object' and params['additionalProperties'] is False
        assert 'mesh' in params['required']
        assert set(params['required']) <= set(params['properties'])
        for prop in params['properties'].values():
            assert prop['type'] in ('string', 'number', 'array') and prop['description']
    json.dumps(schemas)


def test_island_scan_through_the_session(workspace):
    reply = print_repair.Session(workspace).run('island_scan', {'mesh': 'source/part.stl'})
    assert reply['ok'], reply
    result = reply['result']
    assert result['island_count'] == 1
    assert result['islands'][0]['z'] == pytest.approx(5.0)
    json.dumps(reply)


def test_every_tool_runs_through_the_session(workspace):
    session = print_repair.Session(workspace)
    extra = {'underside_profile': {'region': [20, 0, 30, 8]}}
    for name in sorted(DIAGNOSIS):
        reply = session.run(name, {'mesh': 'source/part.stl', **extra.get(name, {})})
        assert reply['ok'], (name, reply)
        json.dumps(reply)
    assert session.run('bodies', {'mesh': 'source/part.stl'})['result']['count'] == 2


def test_arguments_may_arrive_as_json_text(workspace):
    reply = print_repair.Session(workspace).run('bodies', '{"mesh": "source/part.stl"}')
    assert reply['ok'] and reply['result']['count'] == 2


@pytest.mark.parametrize('mesh,message', [
    ('../outside.stl', 'inside the job folder'),
    ('source/../../outside.stl', 'inside the job folder'),
    ('source/missing.stl', 'does not exist'),
    ('source/notes.txt', 'STL or 3MF'),
    ('', 'file path'),
])
def test_paths_stay_in_the_workspace(workspace, mesh, message):
    (workspace.parent / 'outside.stl').write_bytes((workspace / 'source' / 'part.stl').read_bytes())
    (workspace / 'source' / 'notes.txt').write_text('x')
    reply = print_repair.Session(workspace).run('bodies', {'mesh': mesh})
    assert reply == {'ok': False, 'error': reply['error']} and message in reply['error']


def test_absolute_paths_are_refused(workspace):
    absolute = str((workspace / 'source' / 'part.stl').resolve())
    reply = print_repair.Session(workspace).run('bodies', {'mesh': absolute})
    assert not reply['ok'] and 'relative' in reply['error']


@pytest.mark.parametrize('name,arguments,message', [
    ('rm_rf', {}, 'unknown tool'),
    ('bodies', {}, 'missing argument'),
    ('bodies', {'mesh': 'source/part.stl', 'force': True}, 'unknown argument'),
    ('island_scan', {'mesh': 'source/part.stl', 'layer_mm': 'thick'}, 'must be a number'),
    ('island_scan', {'mesh': 'source/part.stl', 'layer_mm': True}, 'must be a number'),
    ('island_scan', {'mesh': 'source/part.stl', 'layer_mm': 0}, 'must be above'),
    ('island_scan', {'mesh': 'source/part.stl', 'layer_mm': 5}, 'at most'),
    ('overhang_scan', {'mesh': 'source/part.stl', 'angle_deg': 90}, 'below'),
    ('underside_profile', {'mesh': 'source/part.stl', 'region': [0, 0, 1]}, 'wrong number'),
    ('underside_profile', {'mesh': 'source/part.stl', 'region': [0, 0, 'x', 1]}, 'must be a number'),
    ('bodies', '{not json', 'not valid JSON'),
    ('bodies', ['source/part.stl'], 'must be an object'),
])
def test_bad_calls_come_back_as_readable_errors(workspace, name, arguments, message):
    reply = print_repair.Session(workspace).run(name, arguments)
    assert not reply['ok'] and message in reply['error'], reply


def test_an_unexpected_failure_is_logged_and_reported(workspace, monkeypatch, caplog):
    def explode(path):
        raise RuntimeError('kernel on fire')
    monkeypatch.setattr(layer_scan, 'load', explode)
    with caplog.at_level(logging.ERROR, logger=tools.logger.name):
        reply = print_repair.Session(workspace).run('bodies', {'mesh': 'source/part.stl'})
    assert reply == {'ok': False, 'error': 'bodies failed: RuntimeError: kernel on fire'}
    assert 'print repair tool bodies failed' in caplog.text


def test_a_mesh_is_loaded_once_per_session(workspace, monkeypatch):
    calls = []
    real = layer_scan.load
    monkeypatch.setattr(layer_scan, 'load', lambda path: calls.append(path) or real(path))
    session = print_repair.Session(workspace)
    for name in ('bodies', 'island_scan', 'overhang_scan', 'watertight'):
        assert session.run(name, {'mesh': 'source/part.stl'})['ok']
    assert len(calls) == 1


def test_an_oversized_mesh_is_refused_before_loading(workspace, monkeypatch):
    calls = []
    monkeypatch.setattr(layer_scan, 'load', lambda path: calls.append(path))
    monkeypatch.setattr(tools, 'MAX_MESH_BYTES', 10)
    reply = print_repair.Session(workspace).run('bodies', {'mesh': 'source/part.stl'})
    assert not reply['ok'] and 'is larger than' in reply['error'], reply
    assert calls == []


def test_long_lists_are_clipped_and_the_rest_counted(workspace, monkeypatch):
    support_clearance._write_binary_stl(
        workspace / 'source' / 'three.stl',
        np.concatenate((box(0, 0, 0, 10, 10, 10), box(20, 0, 0, 25, 5, 5), box(40, 0, 0, 42, 2, 2))))
    monkeypatch.setattr(tools, 'MAX_ITEMS', 1)
    result = print_repair.Session(workspace).run('bodies', {'mesh': 'source/three.stl'})['result']
    assert result['count'] == 3
    assert len(result['bodies']) == 1 and result['bodies_not_listed'] == 2
    assert result['bodies'][0]['volume_mm3'] == pytest.approx(1000.0)


def test_a_short_list_carries_no_not_listed_count(workspace):
    result = print_repair.Session(workspace).run('bodies', {'mesh': 'source/part.stl'})['result']
    assert 'bodies_not_listed' not in result


def test_clearance_report_shape(workspace):
    reply = print_repair.Session(workspace).run('clearance_report',
                                                {'mesh': 'source/part.stl', 'first_layer_mm': 0.2})
    assert reply['ok'], reply
    result = reply['result']
    assert set(result) >= {'risky', 'codes', 'first_layer_mm', 'findings', 'regions'}
    assert isinstance(result['risky'], bool) and isinstance(result['codes'], list)
    assert result['first_layer_mm'] == 0.2
    for item in result['findings'] + result['regions']:
        assert not {'footprint', 'fixes', 'material'} & set(item)


def test_the_workspace_must_exist(tmp_path):
    with pytest.raises(tools.ToolError):
        print_repair.Session(tmp_path / 'missing')


def test_tools_never_write(workspace):
    before = {p: p.stat().st_mtime_ns for p in workspace.rglob('*')}
    session = print_repair.Session(workspace)
    for name in sorted(DIAGNOSIS):
        extra = {'region': [20, 0, 30, 8]} if name == 'underside_profile' else {}
        session.run(name, {'mesh': 'source/part.stl', **extra})
    assert {p: p.stat().st_mtime_ns for p in workspace.rglob('*')} == before


def test_the_image_build_context_keeps_the_skill():
    """The image runs the /app copy of the build context, so .dockerignore must not drop the skill."""
    import fnmatch
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    patterns = [line.strip() for line in (root / '.dockerignore').read_text().splitlines()
                if line.strip() and not line.startswith('#') and not line.startswith('!')]
    files = [p.relative_to(root).as_posix() for p in (root / 'print_handy' / 'skills').rglob('*')
             if p.is_file() and '__pycache__' not in p.parts]
    assert any(name.endswith('SKILL.md') for name in files)
    for name in files:
        parts = name.split('/')
        prefixes = ['/'.join(parts[:i]) for i in range(1, len(parts) + 1)]
        for pattern in patterns:
            bare = pattern[3:] if pattern.startswith('**/') else pattern
            hit = [p for p in prefixes if fnmatch.fnmatchcase(p, pattern)
                   or (pattern.startswith('**/') and fnmatch.fnmatchcase(p.rsplit('/', 1)[-1], bare))]
            assert not hit, (name, pattern)
