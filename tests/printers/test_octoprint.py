"""Offline OctoPrint protocol checks, never contacts a printer."""
import json
from unittest.mock import Mock

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


def test_octoprint_201_connection_status_fixture_redacts_parameters():
    """Pin the 2.0.1 STATUS-only /api/connection contract offline."""
    from pathlib import Path

    fixture = Path(__file__).parent.parent / 'fixtures/printers/octoprint-2.0.1/connection-status-permission.json'
    response = json.loads(fixture.read_text())
    assert response['current']['parameters'] == {}
    assert response['current']['connector'] == 'bambu'


def test_octoprint_201_default_version_fixture_keeps_legacy_api_marker():
    """Without X-OctoPrint-Api-Version, 2.0.1 still documents api=0.1."""
    from pathlib import Path

    fixture = Path(__file__).parent.parent / 'fixtures/printers/octoprint-2.0.1/version-default-api.json'
    response = json.loads(fixture.read_text())
    assert response == {'api': '0.1', 'server': '2.0.1', 'text': 'OctoPrint 2.0.1'}


def test_status_does_not_query_connection_settings_or_disclose_connector_parameters():
    """OctoPrint 2.0.1 hides connector credentials without CONNECTION permission.

    Print Handy's read-only status contract needs only /api/printer and
    /api/job; it must not request /api/connection, whose pre-2.0.0 API
    shape can disclose connector parameters to STATUS-only accounts.
    """
    c = OctoPrintClient('http://octoprint.invalid', 'test-key')
    c.request = Mock(side_effect=[{}, {}])
    assert c.status() == {'printer': {}, 'job': {}}
    assert [call.args for call in c.request.call_args_list] == [
        ('GET', '/api/printer'), ('GET', '/api/job')]


def test_optional_firmware_identity_is_not_required_for_rest_status():
    """OctoPrint 2.0.1 fixes serial crashes for M115 without FIRMWARE_NAME.

    Its identity comes from serial M115, not the printer/job REST payloads
    consumed by this adapter. Keep the REST path valid when it is absent.
    """
    out = _adapter().normalize_status({'printer': {'state': {'flags': {'ready': True, 'operational': True}}}, 'job': {}})
    assert out['state'] == 'standby'
    assert 'firmware_name' not in out
    assert 'firmware_version' not in out


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
