"""OctoPrint client timelapse surface (#373): listing + raw download.

The fake opener records requests and serves canned payloads; no network.
"""
import json

import pytest

from print_handy.printers.octoprint.client import OctoPrintClient, OctoPrintError


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _client(payloads, seen):
    def opener(req, timeout=None):
        seen.append(req)
        return _Response(payloads.pop(0))
    return OctoPrintClient("http://octo.test:5000", "SECRETKEY", opener=opener)


def test_timelapse_movies_parses_the_listing():
    seen = []
    client = _client([json.dumps({"movies": [{"name": "a.mp4", "url": "/dl/a.mp4"}],
                                   "unrendered": []}).encode()], seen)
    assert client.timelapse_movies() == [{"name": "a.mp4", "url": "/dl/a.mp4"}]
    assert seen[0].full_url == "http://octo.test:5000/api/timelapse"
    assert seen[0].get_header("X-api-key") == "SECRETKEY"


def test_timelapse_movies_tolerates_a_missing_key():
    client = _client([b"{}"], [])
    assert client.timelapse_movies() == []


def test_download_returns_raw_bytes_with_auth():
    seen = []
    client = _client([b"\x00\x01video"], seen)
    assert client.download("/downloads/timelapse/a.mp4") == b"\x00\x01video"
    assert seen[0].full_url == "http://octo.test:5000/downloads/timelapse/a.mp4"
    assert seen[0].get_header("X-api-key") == "SECRETKEY"


@pytest.mark.parametrize("path", ["http://evil.test/x", "//evil.test/x", "relative.mp4", ""])
def test_download_refuses_non_relative_paths(path):
    client = _client([], [])
    with pytest.raises(OctoPrintError):
        client.download(path)


def test_download_failure_never_leaks_the_key():
    def opener(req, timeout=None):
        raise OSError("connection refused for SECRETKEY")
    client = OctoPrintClient("http://octo.test:5000", "SECRETKEY", opener=opener)
    with pytest.raises(OctoPrintError) as excinfo:
        client.download("/downloads/timelapse/a.mp4")
    assert "SECRETKEY" not in str(excinfo.value)


# --- #391 stage 2: the upload path itself -----------------------------------
#
# This module pinned the timelapse listing/download surface (#373); the
# upload() method — the print path's one mutation — had no client-level
# tests. Pin its shape: one multipart POST, the server's word on where
# the file landed is verified before anything may start from it, a
# timeout is a plain not-retried error, and bad names never reach the
# transport.


def _upload_client(payloads, seen):
    return _client(payloads, seen)


def test_upload_posts_the_file_once_and_returns_the_remote_name(tmp_path):
    package = tmp_path / "benchy.gcode"
    package.write_bytes(b"G28\nG1 X1\n")
    seen = []
    client = _upload_client(
        [json.dumps({"files": {"local": {"origin": "local", "path": "benchy.gcode"}}}).encode()],
        seen)
    assert client.upload(package, "benchy.gcode") == "benchy.gcode"
    assert len(seen) == 1
    req = seen[0]
    assert req.full_url == "http://octo.test:5000/api/files/local"
    assert req.get_method() == "POST"
    assert req.get_header("X-api-key") == "SECRETKEY"
    assert req.get_header("Content-type").startswith("multipart/form-data; boundary=PrintHandy")
    assert b'filename="benchy.gcode"' in req.data
    assert b"G28\nG1 X1\n" in req.data


def test_upload_timeout_is_plain_not_retried_and_leaks_no_key(tmp_path):
    package = tmp_path / "benchy.gcode"
    package.write_bytes(b"G28\n")
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        raise TimeoutError("timed out for SECRETKEY")

    client = OctoPrintClient("http://octo.test:5000", "SECRETKEY", opener=opener)
    with pytest.raises(OctoPrintError) as excinfo:
        client.upload(package, "benchy.gcode")
    assert "not retried" in str(excinfo.value)
    assert "SECRETKEY" not in str(excinfo.value)
    assert len(calls) == 1  # an ambiguous mutation is never resent


@pytest.mark.parametrize("reported", [
    {"origin": "sdcard", "path": "benchy.gcode"},
    {"origin": "local", "path": "someone-else.gcode"},
    {"origin": "local"},
    {},
])
def test_upload_refuses_when_the_server_reports_a_different_target(tmp_path, reported):
    package = tmp_path / "benchy.gcode"
    package.write_bytes(b"G28\n")
    client = _upload_client([json.dumps({"files": {"local": reported}}).encode()], [])
    with pytest.raises(OctoPrintError, match="target mismatch; nothing started"):
        client.upload(package, "benchy.gcode")


@pytest.mark.parametrize("bad", ["", "../benchy.gcode", "sub/benchy.gcode",
                                 'quo"te.gcode', "back\\slash.gcode", "new\nline.gcode"])
def test_upload_refuses_bad_filenames_before_any_request(tmp_path, bad):
    package = tmp_path / "benchy.gcode"
    package.write_bytes(b"G28\n")
    seen = []
    client = _upload_client([], seen)
    with pytest.raises(OctoPrintError, match="invalid OctoPrint upload filename"):
        client.upload(package, bad)
    assert seen == []


def test_set_targets_posts_one_command_per_requested_heater():
    seen = []
    client = _client([b"", b""], seen)
    client.set_targets(hotend_c=210, bed_c=60)
    assert [req.full_url for req in seen] == [
        "http://octo.test:5000/api/printer/tool",
        "http://octo.test:5000/api/printer/bed",
    ]
    assert json.loads(seen[0].data) == {"command": "target", "targets": {"tool0": 210}}
    assert json.loads(seen[1].data) == {"command": "target", "target": 60}


def test_set_targets_single_heater_and_zero_is_off():
    seen = []
    client = _client([b""], seen)
    client.set_targets(bed_c=0)
    assert len(seen) == 1
    assert json.loads(seen[0].data) == {"command": "target", "target": 0}


@pytest.mark.parametrize("kwargs", [{}, {"hotend_c": "210"}, {"hotend_c": True},
                                    {"bed_c": -1}, {"bed_c": 301},
                                    {"hotend_c": float("nan")}])
def test_set_targets_refuses_bad_values_before_any_request(kwargs):
    seen = []
    client = _client([], seen)
    with pytest.raises(OctoPrintError):
        client.set_targets(**kwargs)
    assert seen == []
