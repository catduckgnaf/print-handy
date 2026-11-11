"""settings._read parses an unchanged file once (#1153: the /config parse cache).

The Web UI's /config page (webui.config.read_fields) and settings.load()
read every settings layer on every call. A parsed file is now reused while
its stat identity (size, mtime, ctime, inode) is unchanged, a file modified
inside the racy window is never stored, and every caller gets its own copy.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

from print_handy import settings
from print_handy.webui import config as webui_config

OLD = time.time() - 600


@pytest.fixture
def parses(monkeypatch):
    settings._READ_CACHE.clear()
    calls = []
    real = settings._read_uncached

    def counting(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(settings, "_read_uncached", counting)
    yield calls
    settings._READ_CACHE.clear()


def _write(path, text, mtime=OLD):
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_an_unchanged_file_is_parsed_once(tmp_path, parses):
    path = _write(tmp_path / "config.yaml", "printer:\n  name: Alpha\n")
    assert settings._read(path) == settings._read(path) == {"printer": {"name": "Alpha"}}
    assert parses == [str(path)]


def test_a_changed_file_is_parsed_again(tmp_path, parses):
    path = _write(tmp_path / "config.yaml", "printer:\n  name: Alpha\n")
    settings._read(path)
    _write(path, "printer:\n  name: Bravo-longer\n", mtime=OLD + 5)
    assert settings._read(path) == {"printer": {"name": "Bravo-longer"}}
    assert len(parses) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="ctime is the creation time on Windows")
def test_a_same_size_rewrite_with_its_old_mtime_restored_is_parsed_again(tmp_path, parses):
    path = _write(tmp_path / "config.yaml", "printer:\n  name: Alpha\n")
    settings._read(path)
    time.sleep(0.01)
    _write(path, "printer:\n  name: Bravo\n")  # same size, same mtime: the ctime still moves
    assert settings._read(path) == {"printer": {"name": "Bravo"}}
    assert len(parses) == 2


def test_a_freshly_written_file_is_never_cached(tmp_path, parses):
    path = tmp_path / "config.yaml"
    path.write_text("printer:\n  name: Alpha\n", encoding="utf-8")  # mtime: now, inside the racy window
    settings._read(path)
    settings._read(path)
    assert len(parses) == 2 and not settings._READ_CACHE


def test_callers_get_independent_copies(tmp_path, parses):
    path = _write(tmp_path / "config.yaml", "printer:\n  name: Alpha\n  slots: [1, 2]\n")
    first = settings._read(path)
    first["printer"]["slots"].append(3)
    first["printer"]["name"] = "changed"
    assert settings._read(path) == {"printer": {"name": "Alpha", "slots": [1, 2]}}


def test_errors_are_never_cached(tmp_path, parses):
    path = _write(tmp_path / "config.yaml", "printer: [unclosed\n")
    for _ in range(2):
        with pytest.raises(settings.SettingsError):
            settings._read(path)
    assert len(parses) == 2 and not settings._READ_CACHE
    with pytest.raises(settings.SettingsError):
        settings._read(tmp_path / "missing.yaml")


def test_the_config_page_read_parses_each_layer_once(tmp_path, parses):
    """read_fields twice: the second call reuses every layer the first one parsed."""
    local = _write(tmp_path / "config.yaml", "printer:\n  name: Alpha\n")
    environ = {"PRINT_HANDY_CONFIG": str(local)}
    first = webui_config.read_fields(local=local, environ=environ)
    parsed_once = list(parses)
    second = webui_config.read_fields(local=local, environ=environ)
    assert [(f.id, f.value, f.provenance) for f in first] == [(f.id, f.value, f.provenance) for f in second]
    assert str(local) in parsed_once
    assert parses == parsed_once
