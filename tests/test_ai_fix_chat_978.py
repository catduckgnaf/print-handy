"""Issue #978 chat tests: an uploaded file + "fix this" runs the repair
flow (repaired and previewed before any slicing, original untouched, job
re-enters the intake), and a bare G-code file sliced for a different
printer is refused plainly with a request for the source model.

The plugin/edit harness follows tests/test_edit_dispatch.py: fake adapter,
scripted confirm, faked intake re-entry; the heavy tier is a fake
ai_routes.call, and the mesh repair itself runs for real.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy import ai_routes
from print_handy.bot import edit_chat
from print_handy.core import ai_fix, prepared_intake, support_clearance

ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins/print-feedback/__init__.py"
spec = importlib.util.spec_from_file_location("print_feedback_fix", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, **kwargs):
        return S(success=True)


def _event(text, user_id="42"):
    source = S(platform=S(value="discord"), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id=user_id, chat_id="c", thread_id=None)
    return S(source=source, text=text, internal=False, message_id="m1", metadata={})


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True,
             _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "session-key")


def _open_cube(path):
    """A non-manifold (open-top) cube STL: the #978 repair fixture."""
    import numpy as np
    s = 10.0
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
         (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 3, 2), (0, 2, 1), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]  # no top: a 4-edge hole
    tris = np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=float)
    support_clearance._write_binary_stl(path, tris)
    return path


@pytest.fixture
def fix_env(tmp_path, monkeypatch, request):
    root = tmp_path / "model-link-intakes"
    root.mkdir(parents=True)
    folder = tmp_path / "job1"
    folder.mkdir()
    model = _open_cube(folder / "model.stl")
    job = {"downloaded_file": str(model), "title": "Open Box", "intake_id": "iid1",
           "selected_source": {"name": "model.stl"},
           "audit": {"size_x": 10, "size_y": 10, "size_z": 10, "parts": 1},
           "orientation": {}, "scale": 1.0}
    job_path = folder / "JOB.json"
    job_path.write_text(json.dumps(job))
    (root / "r1.json").write_text(json.dumps(
        {"chat_id": "c", "thread_id": None, "job_json": str(job_path), "status": "SLICED"}))
    monkeypatch.setattr(plugin, "_state_dir", lambda: tmp_path)
    from print_handy.hermes import _config
    cfg = {"agent": {}, "design": {}, "llm": {}, "discord": {"question_timeout_s": 5},
           "paths": {"cache": tmp_path / "cache"},
           "ai": {"heavy": {"chat_url": "http://127.0.0.1:11434/v1/chat/completions",
                            "model": "heavy-model"}}}
    previous = _config.cfg()
    _config.set_override(cfg)
    request.addfinalizer(lambda: _config.set_override(previous))
    env = S(tmp_path=tmp_path, root=root, folder=folder, job_path=job_path, cfg=cfg,
            adapter=FakeAdapter(), confirms=[], reentered=[])
    env.gateway = _gateway(env.adapter)
    env.answers = [ai_fix.APPLY_LABEL]

    async def fake_confirm(adapter, source, metadata, prefix, question, choices, timeout,
                           live_key=None, approval=False):
        env.confirms.append((question, list(choices), approval))
        return env.answers.pop(0)

    monkeypatch.setattr(plugin, "_confirm", fake_confirm)

    async def fake_reenter(event, gateway, source, adapter, job):
        env.reentered.append(job)
        return {"action": "skip", "reason": "re-entered (fake)"}

    monkeypatch.setattr(edit_chat, "_edit_reenter", fake_reenter)
    monkeypatch.setattr(ai_routes, "call", lambda tier, messages, **kw: (
        json.dumps({"plans": [], "geometry": None, "mesh_repair": True,
                    "summary": "close the open top"}), "heavy-model"))
    return env


def test_uploaded_nonmanifold_stl_fix_this_is_repaired_and_previewed(fix_env):
    original_bytes = Path(fix_env.job_path.parent / "model.stl").read_bytes()
    assert ai_fix.mesh_facts(fix_env.folder / "model.stl")["watertight"] is False
    result = asyncio.run(plugin.edit_dispatch(_event("fix this"), fix_env.gateway))
    assert result == {"action": "skip", "reason": "re-entered (fake)"}
    # The fix was shown before applying, with the repair outcome in the text.
    assert len(fix_env.confirms) == 1
    question, choices, approval = fix_env.confirms[0]
    assert "repair the mesh" in question and "watertight" in question
    assert choices == [ai_fix.APPLY_LABEL, "Cancel"]
    assert approval is False  # asking for a fix is not a print approval
    # A preview of the fixed model was posted before the (faked) re-slice.
    assert any("Preview of the fixed model" in (m or "") for m in fix_env.adapter.sent)
    # The re-entered job points at a repaired COPY; the original is untouched.
    assert len(fix_env.reentered) == 1
    job = fix_env.reentered[0]
    fixed = Path(job["downloaded_file"])
    assert fixed != fix_env.folder / "model.stl" and fixed.is_file()
    assert ai_fix.mesh_facts(fixed)["watertight"] is True
    assert (fix_env.folder / "model.stl").read_bytes() == original_bytes
    assert job["ai_fixes"][0]["source_file_before"] == str(fix_env.folder / "model.stl")
    assert job["ai_fixes"][0]["source_file_after"] == str(fixed)


def test_noop_geometry_fix_is_refused_in_the_upload_flow(fix_env, monkeypatch):
    """A geometry proposal that moves no vertex (extend at a height no
    vertex sits at) is refused as no fix: nothing is confirmed or applied
    and the job never re-enters the intake."""
    monkeypatch.setattr(ai_routes, "call", lambda tier, messages, **kw: (
        json.dumps({"plans": [], "geometry": {"kind": "extend", "mm": 5.0},
                    "mesh_repair": False, "summary": "extend the underside"}), "heavy-model"))
    result = asyncio.run(plugin.edit_dispatch(_event("fix this"), fix_env.gateway))
    assert result["reason"] == "repair would change nothing"
    assert any("would not change the model" in (m or "") for m in fix_env.adapter.sent)
    assert fix_env.confirms == [] and fix_env.reentered == []


def test_fix_this_with_ai_not_configured_notes_and_changes_nothing(fix_env, monkeypatch):
    from print_handy.hermes import _config
    cfg = dict(fix_env.cfg)
    cfg["ai"] = {"heavy": {}}
    _config.set_override(cfg)
    result = asyncio.run(plugin.edit_dispatch(_event("fix this"), fix_env.gateway))
    assert result["reason"] == "repair: AI not configured"
    assert any(ai_fix.UNCONFIGURED_NOTE in (m or "") for m in fix_env.adapter.sent)
    assert fix_env.reentered == [] and fix_env.confirms == []


def test_instruction_after_model_routes_fix_this_to_repair():
    assert edit_chat.instruction_after_model("fix this") == "fix this"
    assert edit_chat.instruction_after_model("make it 20% bigger") == "make it 20% bigger"
    assert edit_chat.instruction_after_model("print this") is None


# ------------------------------------------------------------- foreign G-code

class _SendAdapter:
    def __init__(self):
        self.sent = []
        self.questions = []

    async def send(self, chat_id=None, content=None, metadata=None, reply_to=None):
        self.sent.append(content)
        return S(success=True)

    async def send_clarify(self, **kw):
        self.questions.append(kw)
        # Resolve through the same gateway the ask path registered with: in
        # the full suite that is the Hermes clarify stand-in, not this
        # package's own registry.
        from print_handy.core import prompts
        prompts.gateway().resolve_gateway_clarify(kw["clarify_id"], kw["choices"][-1])  # Cancel
        return S(success=True)


def _gcode_event(tmp_path, name, header_model):
    path = tmp_path / name
    path.write_text(f"; generated by OrcaSlicer\n; printer_model = {header_model}\nG1 X1\n")
    source = S(chat_id="c", thread_id=None, user_id="u", platform=S(value="discord"))
    event = S(source=source, text="print this", message_id="mg", metadata={})
    return event, path


def test_foreign_gcode_is_refused_plainly_and_never_started(tmp_path, monkeypatch):
    from print_handy import printers
    monkeypatch.setattr(printers, "current",
                        lambda: S(key="AD5X", name="Flashforge Adventurer 5M Pro", model="AD5X"))
    event, path = _gcode_event(tmp_path, "benchy.gcode", "Prusa MK4")
    adapter = _SendAdapter()
    result = asyncio.run(prepared_intake.print_gcode_file(
        event, adapter, tmp_path / "receipts", path, timeout=1))
    assert result == {"action": "skip", "reason": "foreign G-code for another printer"}
    assert len(adapter.sent) == 1
    assert "cannot be safely retargeted" in adapter.sent[0]
    assert "source model" in adapter.sent[0] and "Prusa MK4" in adapter.sent[0]


def test_own_printer_gcode_is_not_called_foreign(tmp_path, monkeypatch):
    from print_handy import printers
    monkeypatch.setattr(printers, "current",
                        lambda: S(key="AD5X", name="Flashforge Adventurer 5M Pro", model="AD5X"))
    asked = []

    async def fake_ask(adapter, source, key, question, choices, timeout=None, **kw):
        asked.append((question, list(choices)))
        return list(choices).index("Cancel")

    # Stub the prompt itself: which clarify registry serves a question
    # depends on the surrounding suite, and this test only cares that the
    # own-printer drop reaches the yolo question instead of the refusal.
    monkeypatch.setattr(prepared_intake, "ask", fake_ask)
    event, path = _gcode_event(tmp_path, "own.gcode", "AD5X")
    adapter = _SendAdapter()
    result = asyncio.run(prepared_intake.print_gcode_file(
        event, adapter, tmp_path / "receipts", path, timeout=1))
    # Not foreign: it reaches the existing yolo question for sliced G-code,
    # which the stub answers with Cancel.
    assert result["reason"] == "operator declined printing sliced G-code as-is"
    assert "already-sliced G-code" in asked[0][0]
