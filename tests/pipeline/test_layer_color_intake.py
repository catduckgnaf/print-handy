"""Height-based color changes at intake and at slice time (#870).

Discord flow (prepared_intake, slicing stubbed like the other intake tests):
a multi-slot printer is asked about the height colors like any other color;
a single-extruder printer is offered pauses for a manual swap or one color
with the loss named — never a silent single color. At slice time the pause
choice reaches the geometry handed to Orca as pause entries.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import printers, settings
from print_handy.core import model_job, orca_adapter, prepared_intake, source_colors
from print_handy.printers.base import MultiColorUnsupported

from .test_color_intake import state as ad5x_state
from .test_forgebridge_edge_cases import layered
from .test_prepared_intake import Adapter, env  # noqa: F401  (env is a fixture)


def _copy(source_path):
    def download(job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / "ghost.3mf"
        out.write_bytes(Path(source_path).read_bytes())
        return out
    return download


def _run(env, picks, st, monkeypatch, source_path, mid="layered"):
    monkeypatch.setattr(model_job, "printables_download", _copy(source_path))
    event = S(source=S(profile="3dprinting", chat_id="c1", thread_id=None, user_id="u"),
              message_id=mid, text="https://www.printables.com/model/1-ghost")
    adapter = Adapter(picks)
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, st, env.root, timeout=1))
    receipt = json.loads(next(env.root.glob("*.json")).read_text())
    return adapter, result, receipt


def _saved_job(env):
    return json.loads(next((env.tmp / "archive").rglob("JOB.json")).read_text())


def _use_model(monkeypatch, key):
    data = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": key})
    adapter = printers.get(key, data)
    monkeypatch.setattr(printers, "current", lambda config=None: adapter)
    monkeypatch.setattr(settings, "current", lambda: data)
    return adapter


# ---------------------------------------------------------------- multi-slot: asked like any color

def test_ad5x_asks_about_height_colors_and_records_the_picks(env, monkeypatch):  # noqa: F811
    source_path = layered(env.tmp / "chroma.3mf")
    # Keep the height colors; Tower -> White, height filaments -> Black, White;
    # one copy; cancel at the final preview (decisions are already saved).
    adapter, _result, _receipt = _run(env, [0, 0, 1, 0, 0, 3], ad5x_state(), monkeypatch, source_path)
    first = adapter.questions[0]
    assert "height-based colors" in first["question"]
    assert first["choices"][0] == "Keep the height-based colors"
    asked = [q["question"] for q in adapter.questions]
    assert any("height-based color change to" in q for q in asked), asked
    decisions = _saved_job(env)["color_decisions"]
    assert decisions["mode"] == "per_part"
    assert {key for key in decisions["choices"] if key.startswith("filament:")} == {"filament:2", "filament:3"}


# ---------------------------------------------------------------- single extruder: pause or named loss

def _ad5m_state():
    st = ad5x_state()
    st.pop("filament_slots")
    return st


def test_ad5m_offers_pauses_or_one_color_and_records_pauses(env, monkeypatch):  # noqa: F811
    _use_model(monkeypatch, "flashforge.ad5m")
    source_path = layered(env.tmp / "chroma.3mf")
    adapter, _result, _receipt = _run(env, [0, 0, 0, 3], _ad5m_state(), monkeypatch, source_path,
                                      mid="ad5m-pause")
    first = adapter.questions[0]
    assert "changes color by height" in first["question"]
    assert "one extruder" in first["question"]
    assert first["choices"][:2] == ["Pause at each color change (swap filament by hand)",
                                    "One color for everything"]
    assert "starting color" in adapter.questions[1]["question"]
    decisions = _saved_job(env)["color_decisions"]
    assert decisions["mode"] == "single_color"
    assert decisions["height_colors"] == "pause"


def test_ad5m_one_color_choice_names_the_loss(env, monkeypatch):  # noqa: F811
    _use_model(monkeypatch, "flashforge.ad5m")
    source_path = layered(env.tmp / "chroma.3mf")
    adapter, _result, _receipt = _run(env, [1, 0, 0, 3], _ad5m_state(), monkeypatch, source_path,
                                      mid="ad5m-one")
    assert "the height colors are lost" in adapter.questions[0]["question"]
    decisions = _saved_job(env)["color_decisions"]
    assert decisions["mode"] == "single_color"
    assert decisions["height_colors"] == "one_color"


# ---------------------------------------------------------------- the CLI gate (model_job)

def _ad5m_job(tmp_path, decisions=None):
    source_path = layered(tmp_path / "chroma.3mf")
    job = {"downloaded_file": str(source_path), "color_schema": "part_slots_v1",
           "printer_model": "flashforge.ad5m"}
    if decisions:
        job["color_decisions"] = decisions
    jp = tmp_path / "JOB.json"
    jp.write_text(json.dumps(job))
    return jp


def _single_state():
    return {"nozzle": {"diameter_mm": 0.4, "type": "hardened_steel"},
            "temps_c": {"nozzle": 220, "bed": 60},
            "spool": {"material": "PLA", "spoolman_id": 10, "name": "white", "color": "white",
                      "density": 1.24, "diameter_mm": 1.75},
            "filament_slots": {}}


def test_cli_gate_stops_a_single_extruder_job_without_a_height_choice(tmp_path):
    with pytest.raises(MultiColorUnsupported, match="changes color by height"):
        model_job.intake_color_choices(_ad5m_job(tmp_path), _single_state())


def test_cli_gate_accepts_a_recorded_pause_choice(tmp_path):
    src = layered(tmp_path / "chroma.3mf")
    manifest = source_colors.inventory(src)
    decisions = {"schema": "part_slots_v1", "mode": "single_color",
                 "source_sha256": manifest["source_sha256"], "height_colors": "pause",
                 "selection": {"slot": 1, "color": "white", "material": "PLA", "spoolman_id": 10}}
    assert model_job.intake_color_choices(_ad5m_job(tmp_path, decisions), _single_state()) is True


# ------------------------------------------------------- the production slice job honours the choice

def _ad5m_slice_job(tmp_path, source_path, decisions):
    job = {"downloaded_file": str(source_path), "printer_model": "flashforge.ad5m",
           "audit": {"plates": 1, "size_x": "20", "size_y": "20", "size_z": "12"}}
    if decisions is not None:
        job["color_decisions"] = decisions
    jp = tmp_path / "JOB.json"
    jp.write_text(json.dumps(job))
    return jp


def _ad5m_st():
    st = _single_state()
    st["bed_mm"] = [220, 220]
    st["max_print_height_mm"] = 220
    st["filament_slots"] = {"1": {"color": "white", "material": "PLA", "spoolman_id": 10,
                                  "hex": "FFFFFF", "physical_color_verified": True}}
    return st


def _pause_decisions(manifest):
    return {"schema": "part_slots_v1", "mode": "single_color",
            "source_sha256": manifest["source_sha256"], "height_colors": "pause",
            "selection": {"slot": 1, "color": "white", "material": "PLA", "spoolman_id": 10}}


def test_slice_job_slices_the_pause_choice_on_a_single_extruder(tmp_path, monkeypatch):
    _use_model(monkeypatch, "flashforge.ad5m")
    source_path = layered(tmp_path / "chroma.3mf")
    manifest = source_colors.inventory(source_path)
    jp = _ad5m_slice_job(tmp_path, source_path, _pause_decisions(manifest))
    captured = {}

    def fake_slice_model(src, out_dir, st, brim, **kw):
        captured.update(kw)
        out = Path(out_dir) / "plate_1.gcode"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("G1 X1 Y1\n")
        return out, {}, []
    monkeypatch.setattr(model_job.printer_state, "load", lambda *a, **k: _ad5m_st())
    monkeypatch.setattr(model_job, "_verify_routed", lambda *a, **k: None)
    monkeypatch.setattr(orca_adapter, "slice_model", fake_slice_model)
    model_job.slice_job(jp)
    # The recorded choice reached the slicer instead of being dropped or refused.
    assert captured["color_decisions"]["height_colors"] == "pause"
    assert captured["color_decisions"]["mode"] == "single_color"


def test_slice_job_still_refuses_a_height_color_source_with_no_choice(tmp_path, monkeypatch):
    _use_model(monkeypatch, "flashforge.ad5m")
    source_path = layered(tmp_path / "chroma.3mf")
    jp = _ad5m_slice_job(tmp_path, source_path, None)
    monkeypatch.setattr(model_job.printer_state, "load", lambda *a, **k: _ad5m_st())
    monkeypatch.setattr(orca_adapter, "slice_model",
                        lambda *a, **k: pytest.fail("slicer must not run"))
    with pytest.raises(MultiColorUnsupported):
        model_job.slice_job(jp)


# ---------------------------------------------------------------- slice time: pauses reach Orca

def test_pause_choice_slices_as_pauses_on_one_tool(tmp_path, monkeypatch):
    from print_handy.core import slice_guard
    monkeypatch.setattr(orca_adapter, "pre_slice", lambda **k: slice_guard.Result())
    monkeypatch.setattr(orca_adapter, "post_slice", lambda *a, **k: slice_guard.Result())
    monkeypatch.setattr(orca_adapter.sg, "enforce_approved_baseline", lambda out, **k: slice_guard.Result())
    seen = {}

    def run(command, **_kwargs):
        import zipfile
        with zipfile.ZipFile(command[-1]) as archive:
            seen["xml"] = archive.read("Metadata/custom_gcode_per_layer.xml").decode()
        out = Path(command[command.index("--outputdir") + 1]) / "plate_1.gcode"
        out.write_text("; generated by OrcaSlicer 2.4.2\nM104 S0\nG90\nG1 X1 Y1 Z0.2 E1\nEND_PRINT\n" * 40)
        return S(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(orca_adapter.subprocess, "run", run)
    cfg = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "flashforge.ad5m"})
    adapter = printers.get("flashforge.ad5m", cfg)
    source_path = layered(tmp_path / "chroma.3mf")
    manifest = source_colors.inventory(source_path)
    decisions = {"schema": "part_slots_v1", "mode": "single_color",
                 "source_sha256": manifest["source_sha256"], "height_colors": "pause",
                 "selection": {"slot": 1, "color": "white", "material": "PLA", "spoolman_id": 10}}
    st = _single_state()
    st["filament_slots"] = {"1": {"color": "white", "material": "PLA", "spoolman_id": 10,
                                  "hex": "FFFFFF", "physical_color_verified": True}}
    from print_handy.core import spoolman_client
    with mock.patch.object(spoolman_client, "get_spool", return_value=None):
        _out, provenance, _configs = orca_adapter.slice_model(
            source_path, tmp_path / "slice", st, adapter=adapter, color_decisions=decisions)
    assert provenance["tool_mapping"] == {"0": 1}
    import re
    entries = re.findall(r"<layer [^>]*>", seen["xml"])
    assert len(entries) == 2
    assert all('type="1"' in entry for entry in entries)
