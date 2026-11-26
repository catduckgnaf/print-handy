"""Offline OctoPrint protocol checks, never contacts a printer."""
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest

from print_handy.printers.octoprint.client import OctoPrintClient, OctoPrintError


def client():
    c = OctoPrintClient('http://octoprint.invalid', 'test-key')
    c.request = Mock(side_effect=[
        {'state': {'flags': {'operational': True, 'ready': True}}}, {}, {}, {}])
    return c


def test_heat_commands_precede_file_start():
    c = client()
    c.start('part.gcode', 60, 210)
    assert c.request.call_args_list[2].args == ('POST', '/api/printer/command', {'commands': ['M190 S60', 'M109 S210']})
    assert c.request.call_args_list[3].args == ('POST', '/api/files/local/part.gcode', {'command': 'select', 'print': True})
    assert len(c.request.call_args_list) == 4


def test_busy_refuses_before_mutation():
    c = client()
    c.request.side_effect = [{'state': {'flags': {'operational': True, 'ready': True, 'paused': True}}}, {}]
    with pytest.raises(OctoPrintError, match='not idle'):
        c.start('part.gcode', 60, 210)
    assert all(call.args[0] == 'GET' for call in c.request.call_args_list)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), True, -1, 301, '210'])
def test_bad_temperature_never_contacts_transport(value):
    c = client()
    with pytest.raises(OctoPrintError, match='invalid temperature'):
        c.start('part.gcode', 60, value)
    c.request.assert_not_called()


def test_failed_heat_request_does_not_start_or_retry():
    c = client()
    c.request.side_effect = [{'state': {'flags': {'operational': True, 'ready': True}}}, {}, OctoPrintError('failure')]
    with pytest.raises(OctoPrintError):
        c.start('part.gcode', 60, 210)
    assert len(c.request.call_args_list) == 3


def test_request_auth_and_empty_response():
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b''
    opener = Mock(return_value=response)
    c = OctoPrintClient('http://octoprint.invalid/base', 'test-key', opener=opener)
    assert c.request('POST', '/api/job', {'command': 'cancel'}) == {}
    req = opener.call_args.args[0]
    assert req.full_url == 'http://octoprint.invalid/base/api/job'
    assert req.get_header('X-api-key') == 'test-key'
    assert json.loads(req.data) == {'command': 'cancel'}


def _adapter():
    from print_handy import printers, settings
    model = 'octoprint.marlin'
    return printers.get(model, settings.load(local=False, environ={'PRINT_HANDY__PRINTER__MODEL': model}))


_FIXTURES = Path(__file__).parent.parent / 'fixtures/printers/octoprint-2.0.1'


def _fixture(name):
    return json.loads((_FIXTURES / name).read_text())


class _Recorder:
    """Opener that serves canned responses and records every request made."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        body = json.dumps(self.responses.get(urlsplit(req.full_url).path, {})).encode()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = body
        return response


def test_status_reads_2_0_1_fixtures_and_never_requests_connection_endpoint():
    """Status uses only /api/printer and /api/job, so no connector parameters are fetched.

    OctoPrint 2.0.1's unversioned GET /api/connection still returns the serial
    port and baudrate to any STATUS-only key; the 2.0.0+ shape redacts parameters
    without CONNECTION permission. Print Handy sends no X-OctoPrint-Api-Version
    header and must not call the endpoint in either shape.
    """
    opener = _Recorder({'/api/printer': _fixture('printer-operational.json'), '/api/job': _fixture('job-idle.json')})
    c = OctoPrintClient('http://octoprint.invalid', 'test-key', opener=opener)
    snapshot = c.status()
    assert [urlsplit(r.full_url).path for r in opener.requests] == ['/api/printer', '/api/job']
    assert all(r.get_header('X-octoprint-api-version') is None for r in opener.requests)
    assert all(r.get_header('X-api-key') == 'test-key' for r in opener.requests)
    out = _adapter().normalize_status(snapshot)
    assert out['state'] == 'standby'
    assert out['hotend_c'] == 24.5
    assert out['bed_c'] == 23.1


def test_no_client_request_targets_connection_or_version_endpoints(tmp_path):
    """Every client method stays off /api/connection and /api/version."""
    gcode = tmp_path / 'part.gcode'
    gcode.write_bytes(b'G28\n')
    ready = {'state': {'flags': {'operational': True, 'ready': True}}}
    opener = _Recorder({
        '/api/printer': ready,
        '/api/files/local': {'files': {'local': {'origin': 'local', 'path': 'part.gcode'}}},
        '/api/timelapse': {'files': [], 'unrendered': []},
    })
    c = OctoPrintClient('http://octoprint.invalid', 'test-key', opener=opener)
    c.status()
    c.upload(gcode, 'part.gcode')
    c.start('part.gcode', 60, 210)
    c.timelapse_movies()
    c.timelapse_unrendered()
    c.download('/downloads/timelapse/movie.mp4')
    c.send_commands(['M105'])
    for action in ('pause', 'resume', 'cancel'):
        c.control(action)
    paths = [urlsplit(r.full_url).path for r in opener.requests]
    assert len(paths) >= 12
    assert not [p for p in paths if p.startswith(('/api/connection', '/api/version'))]


def test_octoprint_adapter_source_never_names_connection_endpoint():
    """Static guard: no module in the adapter package, including subpackages, names /api/connection."""
    root = Path(__file__).parent.parent.parent / 'print_handy/printers/octoprint'
    offenders = [str(f.relative_to(root)) for f in root.rglob('*.py') if '/api/connection' in f.read_text()]
    assert offenders == []


def test_octoprint_201_connection_fixture_shapes_are_recorded_only():
    """Fixture-shape check only: this does NOT run any Print Handy code.

    Print Handy has no /api/connection parser (the client tests above prove it
    never requests the endpoint), so these sanitized fixtures only record the
    upstream shapes that docs/OCTOPRINT-2.0.1-AUDIT.md describes. Upstream
    redaction is documented there, not tested here.
    """
    redacted = _fixture('connection-status-no-connection-permission.json')
    assert redacted['current']['parameters'] == {}
    assert redacted['current']['connector'] == 'bambu'
    legacy = _fixture('connection-status-legacy-unversioned.json')
    assert legacy['current']['port'] and legacy['current']['baudrate']
    assert 'parameters' not in legacy['current']


def test_status_without_firmware_identity_normalizes_and_emits_no_identity():
    """OctoPrint 2.0.1 fixes serial crashes for M115 without FIRMWARE_NAME.

    Identity comes from serial M115, not the printer/job REST payloads this
    adapter reads, so a snapshot without any firmware field is still valid and
    the adapter invents no identity.
    """
    printer = _fixture('printer-operational.json')
    assert not any('firmware' in key.lower() for key in printer)
    out = _adapter().normalize_status({'printer': printer, 'job': _fixture('job-idle.json')})
    assert out['state'] == 'standby'
    assert not [k for k in out if 'firmware' in k]


def test_status_reports_targets_and_remaining_time():
    out = _adapter().normalize_status({
        'printer': {'state': {'flags': {'printing': True}},
                    'temperature': {'tool0': {'actual': 214.6, 'target': 215.0},
                                    'bed': {'actual': 59.8, 'target': 60}}},
        'job': {'job': {'file': {'path': 'a.gcode'}},
                'progress': {'completion': 40.0, 'printTimeLeft': 3725}}})
    assert (out['hotend_target_c'], out['bed_target_c']) == (215.0, 60)
    assert out['remaining_min'] == 62
    assert (out['state'], out['progress'], out['hotend_c']) == ('printing', 0.4, 214.6)


@pytest.mark.parametrize('raw', [
    {},
    {'printer': {'temperature': {'tool0': {'target': None}, 'bed': {}}}, 'job': {'progress': {'printTimeLeft': None}}},
    {'printer': {'temperature': {'tool0': {'target': True}, 'bed': {'target': '60'}}},
     'job': {'progress': {'printTimeLeft': False}}},
])
def test_status_extended_fields_are_none_when_absent(raw):
    out = _adapter().normalize_status(raw)
    for key in ('hotend_target_c', 'bed_target_c', 'remaining_min'):
        assert key in out and out[key] is None, key
