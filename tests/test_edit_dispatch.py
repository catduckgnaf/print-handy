"""Offline tests for the chat edit dispatch hook (#903).

The plugin runs against a fake adapter/gateway (the /print-design test
style): the probe slice and the intake re-entry are faked at the hook's
own seams, the confirm button is scripted, and the AI lanes are fakes —
what is under test is the pipeline's discipline: resolve → confirm →
probe-verify → record → re-enter, and every refusal along the way.
"""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import edit_plan as ep
from print_handy.core import model_job as mj

ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins/print-feedback/__init__.py"
spec = importlib.util.spec_from_file_location("print_feedback_edit", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

STL = "solid x\nfacet normal 0 0 0\n outer loop\n  vertex 0 0 0\n  vertex 1 0 0\n  vertex 0 1 0\n endloop\nendfacet\nendsolid x\n"


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


@pytest.fixture
def edit_env(tmp_path, monkeypatch, request):
    root = tmp_path / "model-link-intakes"
    root.mkdir(parents=True)
    folder = tmp_path / "job1"
    folder.mkdir()
    model = folder / "model.stl"
    model.write_text(STL)
    job = {"downloaded_file": str(model), "title": "Bracket", "intake_id": "iid1",
           "selected_source": {"name": "model.stl"},
           "audit": {"size_x": 40, "size_y": 20, "size_z": 10, "parts": 1},
           "orientation": {}, "scale": 1.0}
    job_path = folder / "JOB.json"
    job_path.write_text(json.dumps(job))
    (root / "r1.json").write_text(json.dumps(
        {"chat_id": "c", "thread_id": None, "job_json": str(job_path), "status": "SLICED"}))
    monkeypatch.setattr(plugin, "_state_dir", lambda: tmp_path)
    from print_handy.hermes import _config
    cfg = {"agent": {}, "design": {}, "llm": {}, "discord": {"question_timeout_s": 5},
           "paths": {"cache": tmp_path / "cache"}}
    previous = _config.cfg()
    _config.set_override(cfg)
    request.addfinalizer(lambda: _config.set_override(previous))
    env = S(tmp_path=tmp_path, root=root, folder=folder, job_path=job_path, cfg=cfg,
            adapter=FakeAdapter(), confirms=[], reentered=[], probes=[])
    env.gateway = _gateway(env.adapter)

    answers = list(env.confirm_answers) if hasattr(env, "confirm_answers") else []

    async def fake_confirm(adapter, source, metadata, prefix, question, choices, timeout,
                           live_key=None, approval=False):
        env.confirms.append((question, list(choices)))
        return env.answers.pop(0)

    env.answers = ["Yes, apply the edit"]
    monkeypatch.setattr(plugin, "_confirm", fake_confirm)

    def fake_probe(job, plan):
        env.probes.append((job, plan))
        return env.probe_result

    env.probe_result = (True, ["ok: support toolpaths — 12 support feature lines"])
    monkeypatch.setattr(plugin, "_edit_probe", fake_probe)

    async def fake_reenter(event, gateway, source, adapter, job):
        env.reentered.append(job)
        # Simulate the flow's new intake: a receipt + job for this event,
        # carrying the edits over, with a sliced G-code file.
        new_folder = tmp_path / "job2"
        new_folder.mkdir(exist_ok=True)
        gcode = new_folder / "plate_1.gcode"
        gcode.write_text("G1 X0 Y0 Z0\n")
        new_job = dict(job)
        new_job["gcode"] = str(gcode)
        new_job["downloaded_file"] = job["downloaded_file"]
        new_path = new_folder / "JOB.json"
        new_path.write_text(json.dumps(new_job))
        iid = mj.intake_id_from_message(source, event.message_id)
        (root / (iid + ".json")).write_text(json.dumps(
            {"chat_id": "c", "thread_id": None, "job_json": str(new_path), "status": "SLICED"}))
        return {"action": "skip", "reason": "reentered"}

    monkeypatch.setattr(plugin, "_edit_reenter", fake_reenter)
    return env


def run(coro):
    return asyncio.run(coro)


def test_vocabulary_edit_confirms_verifies_records_and_reenters(edit_env):
    result = run(plugin.edit_dispatch(_event("add tree supports"), edit_env.gateway))
    assert result["reason"] == "reentered"
    question, _choices = edit_env.confirms[0]
    assert "re-slice with tree supports" in question  # the confirmation names the edit
    assert len(edit_env.probes) == 1  # probe slice ran before the job changed
    job = edit_env.reentered[0]
    assert job["edit_settings"]["supports"]["type"] == "tree"
    record = job["edits"][-1]
    assert record["lane"] == "fuzzy" and record["actor"] == "discord:42"
    assert record["plan"]["kind"] == "supports"
    # The provenance record was completed on the NEW job with the
    # verification result and the new G-code's hash.
    new_job = json.loads((edit_env.tmp_path / "job2" / "JOB.json").read_text())
    assert new_job["edits"][-1]["verification"] == edit_env.probe_result[1]
    assert new_job["edits"][-1]["output_sha256"] == ep.file_sha256(new_job["gcode"])
    assert any("Edit applied and verified" in text for text in edit_env.adapter.sent)


def test_declined_confirmation_changes_nothing(edit_env):
    edit_env.answers = ["Cancel"]
    before = edit_env.job_path.read_bytes()
    result = run(plugin.edit_dispatch(_event("add tree supports"), edit_env.gateway))
    assert result["reason"] == "edit declined"
    assert edit_env.job_path.read_bytes() == before
    assert not edit_env.probes and not edit_env.reentered


def test_failed_verification_reports_and_changes_nothing(edit_env):
    edit_env.probe_result = (False, ["FAILED: support toolpaths — no support features"])
    before = edit_env.job_path.read_bytes()
    result = run(plugin.edit_dispatch(_event("add tree supports"), edit_env.gateway))
    assert result["reason"] == "edit verification failed"
    assert any("did not verify" in text for text in edit_env.adapter.sent)
    assert edit_env.job_path.read_bytes() == before
    assert not edit_env.reentered


def test_bare_supports_offers_a_pick_list(edit_env):
    edit_env.answers = ["re-slice with tree supports", "Yes, apply the edit"]
    result = run(plugin.edit_dispatch(_event("supports"), edit_env.gateway))
    assert result["reason"] == "reentered"
    pick_question, pick_choices = edit_env.confirms[0]
    assert "Which edit did you mean" in pick_question
    assert "re-slice with tree supports" in pick_choices
    assert edit_env.reentered[0]["edit_settings"]["supports"]["type"] == "tree"


def test_no_current_job_and_unrelated_text(edit_env):
    edit_env.root.joinpath("r1.json").unlink()
    result = run(plugin.edit_dispatch(_event("add supports"), edit_env.gateway))
    assert result["reason"] == "edit: no current job"
    assert run(plugin.edit_dispatch(_event("hello there"), edit_env.gateway)) is None


def test_undo_restores_the_previous_state_and_reenters(edit_env):
    job = json.loads(edit_env.job_path.read_text())
    record = ep.record_edit(job, {"kind": "supports", "enabled": True, "type": "tree"},
                            actor="discord:42", lane="fuzzy", source_sha256="a" * 64)
    ep.apply_plan(job, record["plan"])
    edit_env.job_path.write_text(json.dumps(job))
    result = run(plugin.edit_dispatch(_event("undo"), edit_env.gateway))
    assert result["reason"] == "reentered"
    restored = edit_env.reentered[0]
    assert ep.supports_override(restored) is None
    assert restored["edits"][-1]["plan"]["kind"] == "undo"
    assert any("Undid the last edit" in text for text in edit_env.adapter.sent)


def _patch_design_ai(monkeypatch, reply):
    from print_handy.core import design
    monkeypatch.setattr(design, "resolve_http_config",
                        lambda cfg: {"chat_url": "http://ai.invalid/v1/chat", "model": "m",
                                     "api_key_env": "", "timeout_s": 5})
    monkeypatch.setattr(design, "http_complete", lambda cfg, messages: reply)


def test_ai_lane_proposes_a_plan_from_the_same_vocabulary(edit_env, monkeypatch):
    _patch_design_ai(monkeypatch, '{"kind": "infill", "percent": 40}')
    result = run(plugin.edit_dispatch(_event("strengthen the infill pattern"), edit_env.gateway))
    assert result["reason"] == "reentered"
    job = edit_env.reentered[0]
    assert job["edit_settings"]["infill_percent"] == 40
    assert job["edits"][-1]["lane"] == "ai"


def test_ai_slicer_setting_stays_a_plan_never_a_mesh_edit(edit_env, monkeypatch):
    _patch_design_ai(monkeypatch, '{"kind": "supports", "enabled": true, "type": "tree"}')
    result = run(plugin.edit_dispatch(_event("strengthen the infill pattern"), edit_env.gateway))
    assert result["reason"] == "reentered"
    assert edit_env.reentered[0]["edit_settings"]["supports"]["type"] == "tree"


def test_ai_out_of_range_plan_is_rejected(edit_env, monkeypatch):
    _patch_design_ai(monkeypatch, '{"kind": "infill", "percent": 400}')
    result = run(plugin.edit_dispatch(_event("strengthen the infill pattern"), edit_env.gateway))
    assert result["reason"] == "edit AI plan rejected"
    assert any("cannot use" in text for text in edit_env.adapter.sent)
    assert not edit_env.reentered


def test_ai_unavailable_shows_the_vocabulary(edit_env, monkeypatch):
    from print_handy.core import design
    monkeypatch.setattr(design, "resolve_http_config", lambda cfg: None)
    result = run(plugin.edit_dispatch(_event("strengthen the infill pattern"), edit_env.gateway))
    assert result["reason"] == "edit vocabulary shown"
    assert any("add supports" in text for text in edit_env.adapter.sent)


def test_identity_layer_gates_the_ai_lane(edit_env, monkeypatch):
    from print_handy.bot import agent as agent_lane
    edit_env.cfg["agent"] = {
        "identities": {"op": {"chat_url": "http://agent.invalid/v1/chat/completions",
                              "api_key_env": "OP_KEY", "model": "", "timeout_s": 5}},
        "users": {"42": "op"}, "allowlist": [], "fallback_identity": ""}
    calls = []

    def fake_ask(identity, text, conversation_id):
        calls.append((identity.name, text))
        return '{"kind": "infill", "percent": 33}'

    monkeypatch.setattr(agent_lane, "ask_as", fake_ask)
    result = run(plugin.edit_dispatch(_event("strengthen the infill pattern", user_id="42"),
                                      edit_env.gateway))
    assert result["reason"] == "reentered"
    assert calls and calls[0][0] == "op"
    assert edit_env.reentered[0]["edits"][-1]["lane"] == "ai"

    # A user outside the identity layer gets no AI call at all.
    calls.clear()
    edit_env.adapter.sent.clear()
    result = run(plugin.edit_dispatch(_event("strengthen the infill pattern", user_id="99"),
                                      edit_env.gateway))
    assert result["reason"] == "edit vocabulary shown"
    assert not calls


def test_geometry_route_refused_for_a_multipart_model(edit_env, monkeypatch):
    job = json.loads(edit_env.job_path.read_text())
    job["audit"]["parts"] = 2
    job["color_decisions"] = {"mode": "per_part", "choices": {}}
    edit_env.job_path.write_text(json.dumps(job))
    _patch_design_ai(monkeypatch, '{"route": "geometry", "instruction": "make it taller"}')
    result = run(plugin.edit_dispatch(_event("strengthen the infill pattern"), edit_env.gateway))
    assert result["reason"] == "geometry edit refused for multi-part model"
    assert any("multi-part" in text for text in edit_env.adapter.sent)
    assert not edit_env.reentered


def test_copies_refused_for_a_multipart_job(edit_env):
    job = json.loads(edit_env.job_path.read_text())
    job["audit"]["parts"] = 2
    edit_env.job_path.write_text(json.dumps(job))
    result = run(plugin.edit_dispatch(_event("print 3 copies"), edit_env.gateway))
    assert result["reason"] == "edit copies refused for multi-part job"
    assert not edit_env.confirms and not edit_env.reentered
