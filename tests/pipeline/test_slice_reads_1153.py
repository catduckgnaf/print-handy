"""Slice-path reads of the finalized G-code (#1153, slice-path parsing).

slice_model read the whole finalized G-code three times for three read-only
checks (the post-slice guard, the vendor start-code check and the printer's
own refusals). It now reads it once after finalizing and hands the text to
all three. The protected-settings baseline scan skips toolpath lines before
its regex. Neither change may move a verdict.
"""
from __future__ import annotations

import io
import json
import pathlib
import re
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from print_handy.core import model_job, orca_adapter, slice_guard
from tests.pipeline.test_feature_filament_remap_1155 import _decisions, _fake_orca, _painted_source

ROOT = Path(__file__).resolve().parents[2]


def _slice_counting_reads(tmp_path, wrap=None):
    source = _painted_source(tmp_path / "painted.3mf")
    state = model_job.printer_state.load()
    _manifest, decisions = _decisions(source, {1: 1, 2: 3})
    reads = []
    real_read_text = pathlib.Path.read_text

    def read_text(self, *args, **kwargs):
        if self.suffix == ".gcode":
            reads.append(self.name)
        return real_read_text(self, *args, **kwargs)

    spool = io.BytesIO(json.dumps({"archived": False, "filament": {"material": "PLA"}}).encode())
    with patch.object(pathlib.Path, "read_text", read_text), \
            patch.object(orca_adapter.subprocess, "run", side_effect=_fake_orca({})), \
            patch.object(urllib.request, "urlopen", return_value=spool):
        if wrap:
            wrap()
        out, provenance, _configs = orca_adapter.slice_model(source, tmp_path / "out", state,
                                                             color_decisions=decisions)
    return reads, out, provenance


def test_slice_reads_the_finalized_gcode_once_for_its_read_only_checks(tmp_path):
    seen = {}
    real_post, real_start = orca_adapter.post_slice, orca_adapter.start_code_problems

    def post(gcode, **kwargs):
        seen["post"] = gcode
        return real_post(gcode, **kwargs)

    def start(text, vendor):
        seen["start"] = text
        return real_start(text, vendor)

    with patch.object(orca_adapter, "post_slice", post), \
            patch.object(orca_adapter, "start_code_problems", start):
        reads, out, provenance = _slice_counting_reads(tmp_path)
    # One read for the generator identity before finalizing (finalizing
    # rewrites the file), one after it for every read-only check. The three
    # checks used to read it once each: four reads per slice.
    assert reads == ["plate_1.gcode", "plate_1.gcode"]
    final = Path(out).read_text(errors="replace")
    assert seen["post"] == final
    assert seen.get("start", final) == final
    assert provenance["tool_mapping"] == {"0": 1, "1": 3}


def _old_effective_config(text):
    """slice_guard._effective_config's line loop before #1153, frozen."""
    found = {}
    for line in text.splitlines():
        m = re.match(r"^;\s*([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
        if m and m.group(1) in slice_guard.PROTECTED_KEYS:
            found[m.group(1)] = m.group(2)
    return found


def _gcode_texts():
    texts = [p.read_text(errors="replace") for p in sorted((ROOT / "tests").rglob("*.gcode"))]
    keys = sorted(slice_guard.PROTECTED_KEYS)
    texts.append("\n".join(
        [f"; {keys[0]} = a", f";{keys[0]}=b", f"  ; {keys[1]} = indented", f"G1 X1 ; {keys[1]} = trailing",
         f"\r; {keys[2]} = after a bare CR", f"; {keys[2]}\t=\ttabs", f";\t{keys[3]} = tab lead",
         "; not_protected = 1", f"\x0b; {keys[3]} = vertical tab", f"; {keys[0]} = last wins", ""]))
    return texts


def test_baseline_config_scan_matches_the_removed_loop():
    texts = _gcode_texts()
    assert len(texts) > 1
    hits = 0
    for text in texts:
        new = slice_guard._effective_config(text)
        assert new == _old_effective_config(text)
        hits += bool(new)
    assert hits, "the inputs must carry protected keys"


@pytest.mark.parametrize("edit, blocked", [(None, False), ("START_PRINT EXTRA=1", True)])
def test_baseline_verdicts_are_unchanged(tmp_path, edit, blocked):
    key = "machine_start_gcode"
    baseline = tmp_path / "baseline.json"
    gcode = tmp_path / "plate.gcode"
    gcode.write_text("G1 X1\n" * 100 + f"; {key} = START_PRINT\n")
    baseline.write_text(json.dumps(slice_guard._baseline(slice_guard._effective_config(gcode))))
    if edit:
        gcode.write_text("G1 X1\n" * 100 + f"; {key} = {edit}\n")
    result = slice_guard.enforce_approved_baseline(gcode, path=baseline)
    assert (result.status == "blocked") is blocked


def _short_text_longer_than_a_file_name():
    """Finalized G-code text under 4096 characters whose stat as a path fails.

    slice_model hands post_slice the text (#1153). A single path component
    over 255 bytes makes os.stat raise ENAMETOOLONG on Linux, which Python
    3.12's Path.is_file does not swallow: post_slice used to stat any short
    string as a candidate file name and crashed every small slice in CI.
    """
    from tests.core.test_settings_provenance import GOOD_GCODE
    text = GOOD_GCODE + "; filament_z_hop_types = Normal Lift,Normal Lift\n" + "; " + "x" * 400 + "\n"
    assert len(text) < 4096 and "\n" in text
    return text


@pytest.fixture
def stat_like_python_3_12(monkeypatch):
    """Path.is_file raising ENAMETOOLONG as Python 3.12 does on Linux.

    Python 3.14 swallows the error and answers False, so without this the
    crash would only show on 3.12 runners.
    """
    import errno
    real_is_file = pathlib.Path.is_file

    def is_file(self):
        if any(len(part) > 255 for part in self.parts):
            raise OSError(errno.ENAMETOOLONG, "File name too long", str(self))
        return real_is_file(self)

    monkeypatch.setattr(pathlib.Path, "is_file", is_file)


def test_post_slice_reads_short_gcode_text_as_text(stat_like_python_3_12):
    result = slice_guard.post_slice(_short_text_longer_than_a_file_name(), tool_slots={0: 1})
    assert result.status == "ok", result.blocked


def test_baseline_scan_reads_short_gcode_text_as_text(tmp_path, stat_like_python_3_12):
    text = _short_text_longer_than_a_file_name()
    assert slice_guard._effective_config(text) == _old_effective_config(text)
    assert slice_guard._gcode_file(text) is None
    path = tmp_path / "plate.gcode"
    path.write_text(text)
    assert slice_guard._gcode_file(str(path)) == path
    assert slice_guard._gcode_file(path) == path
    assert slice_guard._gcode_file(tmp_path / "missing.gcode") is None
