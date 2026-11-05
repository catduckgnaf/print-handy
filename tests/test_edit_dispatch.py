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

from print_handy.bot import edit_chat
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
    monkeypatch.setattr(edit_chat, "_edit_probe", fake_probe)

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

    monkeypatch.setattr(edit_chat, "_edit_reenter", fake_reenter)
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


def test_reentry_failure_restores_the_original_job(edit_env, monkeypatch):
    # Regression (Codex P2 on PR #908): when re-entry raised, the
    # handler wrote the MUTATED job back while telling the user the job
    # was unchanged — later edits/undo then operated on an edit the
    # user was told had failed.
    async def failing_reenter(event, gateway, source, adapter, job):
        raise RuntimeError("intake blew up")

    monkeypatch.setattr(edit_chat, "_edit_reenter", failing_reenter)
    before = json.loads(edit_env.job_path.read_text())
    result = run(plugin.edit_dispatch(_event("add tree supports"), edit_env.gateway))
    assert result["reason"] == "edit re-entry failed"
    assert any("unchanged" in text for text in edit_env.adapter.sent)
    restored = json.loads(edit_env.job_path.read_text())
    assert restored == before  # no applied settings, no edit record
    assert "edits" not in restored and "edit_settings" not in restored


def test_out_of_range_typed_request_gets_the_rejection(edit_env):
    # Regression (Codex P2 on PR #908): resolve_request raises PlanError
    # for typed values outside the plan bounds (infill 200% here); the
    # dispatch must answer with the actionable rejection, not abort.
    before = edit_env.job_path.read_bytes()
    result = run(plugin.edit_dispatch(_event("infill 200%"), edit_env.gateway))
    assert result["reason"] == "edit request refused"
    assert any("Nothing was changed" in text for text in edit_env.adapter.sent)
    assert edit_env.job_path.read_bytes() == before
    assert not edit_env.probes and not edit_env.reentered


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


def test_ai_lane_uses_the_design_endpoint(edit_env, monkeypatch):
    from print_handy.core import design
    calls = []
    monkeypatch.setattr(design, "resolve_http_config",
                        lambda cfg: {"chat_url": "http://ai.invalid/v1/chat", "model": "m",
                                     "api_key_env": "", "timeout_s": 5})

    def fake_complete(cfg, messages):
        calls.append(messages)
        return '{"kind": "infill", "percent": 33}'

    monkeypatch.setattr(design, "http_complete", fake_complete)
    result = run(plugin.edit_dispatch(_event("strengthen the infill pattern", user_id="42"),
                                      edit_env.gateway))
    assert result["reason"] == "reentered"
    assert len(calls) == 1 and calls[0][0]["role"] == "user"
    assert "strengthen the infill pattern" in calls[0][0]["content"]
    assert edit_env.reentered[0]["edits"][-1]["lane"] == "ai"


def test_ai_endpoint_failure_falls_back_to_the_vocabulary(edit_env, monkeypatch):
    from print_handy.core import design
    monkeypatch.setattr(design, "resolve_http_config",
                        lambda cfg: {"chat_url": "http://ai.invalid/v1/chat", "model": "m",
                                     "api_key_env": "", "timeout_s": 5})

    def broken(cfg, messages):
        raise OSError("endpoint unreachable")

    monkeypatch.setattr(design, "http_complete", broken)
    result = run(plugin.edit_dispatch(_event("strengthen the infill pattern"), edit_env.gateway))
    assert result["reason"] == "edit vocabulary shown"
    assert not edit_env.reentered


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


def _mark_unsupported_slice(edit_env, model=None):
    job = json.loads(edit_env.job_path.read_text())
    job["provenance"] = {"supports_on": False, "slice_warnings": {
        "support_needed": True, "objects": ["BLACK BODY"],
        "support_texts": ["Floating regions detected"]}}
    if model is not None:
        job["downloaded_file"] = str(model)
    edit_env.job_path.write_text(json.dumps(job))


def test_supports_question_is_answered_not_turned_into_an_edit(edit_env):
    """'does this need supports?' resolves to a supports plan in the vocabulary;
    the question lane must answer it instead of re-slicing."""
    _mark_unsupported_slice(edit_env)
    edit_env.answers = ["Print without supports"]
    result = run(plugin.edit_dispatch(_event("does this need supports?"), edit_env.gateway))
    assert result["reason"] == "question answered: print without supports"
    question, choices = edit_env.confirms[0]
    assert "Supports are off for this slice." in question
    assert "> Floating regions detected" in question
    assert choices == ["Re-slice with supports", "Print without supports"]
    assert json.loads(edit_env.job_path.read_text())["support_decision"]["supports"] is False
    assert not edit_env.reentered and not edit_env.probes


def test_supports_question_reslices_with_supports_on_request(edit_env):
    _mark_unsupported_slice(edit_env)
    edit_env.answers = ["Re-slice with supports"]
    result = run(plugin.edit_dispatch(_event("Do I need supports for this?"), edit_env.gateway))
    assert result["reason"] == "question answered: re-slice with supports"
    assert edit_env.reentered[0]["support_decision"]["supports"] is True


def test_supports_question_reports_modeled_supports(edit_env):
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "modeled_supports_shelf.3mf"
    _mark_unsupported_slice(edit_env, fixture)
    edit_env.answers = ["Print without supports"]
    run(plugin.edit_dispatch(_event("does it need supports?"), edit_env.gateway))
    question = edit_env.confirms[0][0]
    assert "This file has modeled supports" in question
    assert "0.20 mm below" in question and "0.22 mm below" in question


def test_size_and_fit_questions_are_answered(edit_env):
    run(plugin.edit_dispatch(_event("how big is it?"), edit_env.gateway))
    run(plugin.edit_dispatch(_event("will it fit on the bed?"), edit_env.gateway))
    sent = edit_env.adapter.sent
    assert any("40.0 x 20.0 x 10.0 mm" in m for m in sent)
    assert any(m.startswith("Yes.") for m in sent)
    assert not edit_env.confirms and not edit_env.reentered


def test_size_and_fit_questions_respect_orientation_and_scale(edit_env):
    job = json.loads(edit_env.job_path.read_text())
    job["orientation"] = {"dims_full_mm": [20.0, 40.0, 10.0], "rotation": [0, 0, 90]}
    job["scale"] = 0.5
    edit_env.job_path.write_text(json.dumps(job))
    run(plugin.edit_dispatch(_event("how big is it?"), edit_env.gateway))
    sent = edit_env.adapter.sent
    assert any("10.0 x 20.0 x 5.0 mm" in m for m in sent)


def test_question_without_a_job_is_left_to_chat(edit_env):
    for path in edit_env.root.glob("*.json"):
        path.unlink()
    assert run(plugin.edit_dispatch(_event("does this need supports?"), edit_env.gateway)) is None


def _ai_settings(monkeypatch, **ai):
    from print_handy import settings
    monkeypatch.setattr(settings, "current", lambda: {"ai": ai})
    monkeypatch.setattr(edit_chat, "_EDIT_AI_CALLS", {})
    monkeypatch.setattr(edit_chat, "_EDIT_MEMORY", {})


def test_ai_allowlist_refuses_users_not_listed(monkeypatch):
    _ai_settings(monkeypatch, allowed_users=["7"], edit_requests_per_hour=0)
    assert edit_chat._edit_ai_allowed(S(user_id="7"))[0] is True
    assert edit_chat._edit_ai_allowed(S(user_id="42")) == (False, "not on the AI allowlist")


def test_empty_allowlist_keeps_the_lane_open(monkeypatch):
    _ai_settings(monkeypatch, allowed_users=[], edit_requests_per_hour=0)
    assert edit_chat._edit_ai_allowed(S(user_id="42"))[0] is True


def test_ai_rate_limit_is_per_user_per_hour(monkeypatch):
    _ai_settings(monkeypatch, allowed_users=[], edit_requests_per_hour=2)
    assert edit_chat._edit_ai_allowed(S(user_id="1"), now=0)[0]
    assert edit_chat._edit_ai_allowed(S(user_id="1"), now=10)[0]
    assert edit_chat._edit_ai_allowed(S(user_id="1"), now=20) == (False, "AI edit rate limit reached")
    assert edit_chat._edit_ai_allowed(S(user_id="2"), now=20)[0]
    assert edit_chat._edit_ai_allowed(S(user_id="1"), now=3601)[0]


def test_refused_user_never_reaches_the_endpoint(edit_env, monkeypatch):
    from print_handy.core import design
    _ai_settings(monkeypatch, allowed_users=["7"], edit_requests_per_hour=0)
    monkeypatch.setattr(design, "resolve_http_config",
                        lambda cfg: {"chat_url": "http://ai.invalid/v1/chat", "model": "m"})
    monkeypatch.setattr(design, "http_complete",
                        lambda cfg, messages: pytest.fail("endpoint called for a refused user"))
    result = run(plugin.edit_dispatch(_event("strengthen the infill pattern", user_id="42"),
                                      edit_env.gateway))
    assert result["reason"] == "edit vocabulary shown"


def test_mentions_are_stripped_before_the_ai_prompt():
    assert edit_chat._sanitize_mentions("<@123> make it <@&9> stronger @everyone <#55>") \
        == "make it stronger"


def test_ai_lane_remembers_a_short_per_user_conversation(monkeypatch):
    from print_handy.core import design
    _ai_settings(monkeypatch, allowed_users=[], edit_requests_per_hour=0)
    seen = []
    monkeypatch.setattr(design, "resolve_http_config", lambda cfg: {"chat_url": "http://x", "model": "m"})
    monkeypatch.setattr(design, "http_complete",
                        lambda cfg, messages: seen.append(list(messages)) or f"reply {len(seen)}")
    a = S(user_id="1", chat_id="c", thread_id=None)
    b = S(user_id="2", chat_id="c", thread_id=None)
    assert run(edit_chat._edit_ai_reply({}, a, "first")) == "reply 1"
    assert run(edit_chat._edit_ai_reply({}, a, "second")) == "reply 2"
    assert [m["content"] for m in seen[1]] == ["first", "reply 1", "second"]
    run(edit_chat._edit_ai_reply({}, b, "other user"))
    assert [m["content"] for m in seen[2]] == ["other user"]
    for i in range(10):
        run(edit_chat._edit_ai_reply({}, a, f"n{i}"))
    assert len(seen[-1]) <= edit_chat.MEMORY_TURNS * 2 + 1


def test_memory_expires(monkeypatch):
    monkeypatch.setattr(edit_chat, "_EDIT_MEMORY", {})
    key = ("c", None, "1")
    edit_chat._remember(key, "q", "a", now=0)
    assert edit_chat._recall(key, now=60)
    assert edit_chat._recall(key, now=edit_chat.MEMORY_TTL_S + 1) == []


@pytest.mark.parametrize("text, expected", [
    ("https://www.printables.com/model/1-x shrink this by 20%", "shrink this by 20%"),
    ("https://www.printables.com/model/1-x does this have supports?", "does this have supports?"),
    ("https://www.printables.com/model/1-x", None),
    ("https://www.printables.com/model/1-x print this", None),
])
def test_instruction_beside_a_link(text, expected):
    assert edit_chat.instruction_after_model(text, "https://www.printables.com/model/1-x") == expected


def test_link_plus_instruction_runs_intake_then_the_edit(edit_env, monkeypatch):
    """"<printables link> shrink this by 20%": intake first, then the edit
    plan applies to the job the intake just prepared."""
    import print_handy.core.prepared_intake as pi
    calls = []

    async def fake_intake(event, adapter, state, root, reuse=None, before_start=None, **kw):
        # The real flow: the job is prepared, then the edit hook runs, and
        # only an intake the hook did not take over goes on to the start choice.
        calls.append(("intake", event.text))
        handed = await before_start(edit_env.job_path, json.loads(edit_env.job_path.read_text()))
        if isinstance(handed, dict):
            return handed
        calls.append(("start offered", None))
        return {"action": "skip", "reason": "intake done"}

    monkeypatch.setattr(pi, "prepare_link", fake_intake)
    monkeypatch.setattr(plugin, "_routed_printer_id", lambda s, g: None)
    monkeypatch.setattr(plugin, "_undecided_printer_ids", lambda s, g: [])
    from print_handy.core import printer_state
    monkeypatch.setattr(printer_state, "load", lambda **kw: {})
    from print_handy.bot import recent_links
    monkeypatch.setattr(recent_links, "remember", lambda *a, **k: None)
    link = "https://www.printables.com/model/12345-bracket"
    result = run(plugin.model_link_dispatch(_event(f"{link} shrink this by 20%"), edit_env.gateway))
    # Codex P1 on #908: the edit applies before the start choice is offered,
    # so the start never comes up for the unedited job.
    assert calls == [("intake", f"{link} shrink this by 20%")]
    assert edit_env.confirms and "80%" in edit_env.confirms[0][0]
    assert edit_env.reentered and edit_env.reentered[0]["scale"] == pytest.approx(0.8)
    assert result["reason"] == "reentered"


def test_link_plus_declined_edit_continues_the_intake(edit_env, monkeypatch):
    """A declined edit leaves the prepared job unchanged and the intake goes on."""
    import print_handy.core.prepared_intake as pi
    calls = []

    async def fake_intake(event, adapter, state, root, reuse=None, before_start=None, **kw):
        handed = await before_start(edit_env.job_path, json.loads(edit_env.job_path.read_text()))
        if isinstance(handed, dict):
            return handed
        calls.append("start offered")
        return {"action": "skip", "reason": "intake done"}

    monkeypatch.setattr(pi, "prepare_link", fake_intake)
    monkeypatch.setattr(plugin, "_routed_printer_id", lambda s, g: None)
    monkeypatch.setattr(plugin, "_undecided_printer_ids", lambda s, g: [])
    from print_handy.core import printer_state
    monkeypatch.setattr(printer_state, "load", lambda **kw: {})
    from print_handy.bot import recent_links
    monkeypatch.setattr(recent_links, "remember", lambda *a, **k: None)
    edit_env.answers = ["Cancel"]
    link = "https://www.printables.com/model/12345-bracket"
    result = run(plugin.model_link_dispatch(_event(f"{link} shrink this by 20%"), edit_env.gateway))
    assert calls == ["start offered"] and result["reason"] == "intake done"
    assert not edit_env.reentered
    assert not json.loads(edit_env.job_path.read_text()).get("edits")


def test_file_plus_question_answers_after_intake(edit_env, monkeypatch):
    """"<3mf attachment> does this have supports?" is answered from the new job."""
    import print_handy.core.prepared_intake as pi

    async def fake_intake(event, adapter, state, root, reuse=None, **kw):
        return {"action": "skip", "reason": "intake done"}

    monkeypatch.setattr(pi, "prepare_link", fake_intake)
    monkeypatch.setattr(pi, "model_file", lambda event: "/tmp/x.3mf" if getattr(event, "media_urls", None) else None)
    monkeypatch.setattr(plugin, "_routed_printer_id", lambda s, g: None)
    monkeypatch.setattr(plugin, "_undecided_printer_ids", lambda s, g: [])
    from print_handy.core import printer_state
    monkeypatch.setattr(printer_state, "load", lambda **kw: {})
    edit_env.answers = ["Print without"]
    event = _event("does this have supports?")
    event.media_urls = ["/tmp/x.3mf"]
    event.media_types = ["model/3mf"]
    run(plugin.model_link_dispatch(event, edit_env.gateway))
    assert any("support" in (m or "").lower() for m in edit_env.adapter.sent + [q for q, _ in edit_env.confirms])


def test_copies_probe_turns_on_object_labels(tmp_path, monkeypatch):
    """Codex P2 on #908: the shipped AD5X profiles have labels off, so the
    copies probe slice turns them on; the real job is not changed."""
    model = tmp_path / "model.stl"
    model.write_text(STL)
    job = {"downloaded_file": str(model), "audit": {"size_x": 40, "size_y": 20, "size_z": 10}}
    seen = {}

    def fake_slice_job(path):
        seen["job"] = json.loads(Path(path).read_text())
        gcode = Path(path).parent / "plate.gcode"
        gcode.write_text("; printing object box id:1 copy 0\nG1 X1 E1\n"
                         "; printing object box id:1 copy 1\nG1 X2 E2\n")
        return [str(gcode)]

    monkeypatch.setattr(mj, "slice_job", fake_slice_job)
    ok, lines = edit_chat._edit_probe(job, {"kind": "copies", "count": 2})
    assert ok, lines
    assert seen["job"]["edit_settings"]["label_objects"] is True
    assert ep.slice_edits(seen["job"])["label_objects"] is True
    assert "edit_settings" not in job


def test_fit_answer_checks_the_printer_height(edit_env, monkeypatch):
    """Codex P2 on #908: bed_mm has two values; the height limit is separate."""
    from print_handy import settings
    job = json.loads(edit_env.job_path.read_text())
    job["audit"] = {"size_x": 20, "size_y": 20, "size_z": 1000, "parts": 1}
    edit_env.job_path.write_text(json.dumps(job))
    monkeypatch.setattr(settings, "current",
                        lambda: {"printer": {"bed_mm": [220.0, 220.0], "max_print_height_mm": 220.0}})
    from print_handy.core import printer_state

    def no_state(**_kw):
        raise FileNotFoundError("no saved printer state")

    monkeypatch.setattr(printer_state, "load", no_state)
    run(plugin.edit_dispatch(_event("will it fit on the bed?"), edit_env.gateway))
    sent = edit_env.adapter.sent
    assert any(m.startswith("No.") and "220 x 220 x 220 mm" in m for m in sent), sent


def test_supports_question_choice_is_an_approval(edit_env, monkeypatch):
    """Codex P2 on #908: the supports safety choice needs approval users."""
    _mark_unsupported_slice(edit_env)
    flags = []

    async def fake_confirm(adapter, source, metadata, prefix, question, choices, timeout,
                           live_key=None, approval=False):
        flags.append(approval)
        return "Print without supports"

    monkeypatch.setattr(plugin, "_confirm", fake_confirm)
    run(plugin.edit_dispatch(_event("does this need supports?"), edit_env.gateway))
    assert flags == [True]
