"""Offline tests for the /print-design dispatch handler (#388 Stage 1).

No Discord, no network, no real model: the plugin runs against a fake
adapter and gateway (the /print-live test style), the design model is a
scripted fake plugged into the plugin's transport seam, and the clarify
wait is patched to press the button each test chooses. Real-OpenSCAD
turns are skipped where openscad/xvfb-run are absent.
"""
import asyncio
import importlib.util
import shutil
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.core import design, prepared_intake, printer_state
from print_handy.core import prompts as core_prompts
from print_handy.hermes import _config

ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins/print-feedback/__init__.py"
spec = importlib.util.spec_from_file_location("print_feedback_design", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

GOOD = ("plate_z = 3;\n$fn = 48;\ndifference() {\n"
        "  translate([-20, -10, 0]) cube([40, 20, plate_z]);\n"
        "  for (x = [-15, 15]) translate([x, 0, -1]) cylinder(h = plate_z + 2, d = 4);\n}\n")
THICKER = GOOD.replace("plate_z = 3", "plate_z = 5")

needs_scad = pytest.mark.skipif(not (shutil.which("openscad") and shutil.which("xvfb-run")),
                                reason="openscad/xvfb-run not installed")


class FakeAdapter:
    def __init__(self):
        self.sent = []
        self.clarified = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, chat_id=None, question=None, choices=None,
                           clarify_id=None, session_key=None, metadata=None):
        self.clarified.append((question, list(choices or [])))
        return S(success=True)


def _event(text, platform="discord", user_id="42"):
    source = S(platform=S(value=platform), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id=user_id, chat_id="c", thread_id=None)
    return S(source=source, text=text, internal=False, message_id="m1", metadata={})


def _gateway(adapter, authorized=True):
    return S(_is_user_authorized_for_source=lambda s: authorized,
             _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "session-key")


def scripted(replies):
    calls = []

    def complete(messages):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    complete.calls = calls
    return complete


@pytest.fixture
def design_env(tmp_path):
    cfg = {"bot": {"mode": "standalone"},
           "design": {"enabled": True, "chat_url": "", "model": "", "api_key_env": "",
                      "timeout_s": 180, "repair_attempts": 3},
           "llm": {"chat_url": "", "model": "", "api_key_env": "LLM_API_KEY", "timeout_s": 20},
           "paths": {"cache": tmp_path / "cache", "hermes_home": tmp_path},
           "printer": {"bed_mm": [220, 220]},
           "discord": {"question_timeout_s": 5}}
    previous = _config.cfg()  # the session test settings from tests/conftest.py; restore, never clear
    _config.set_override(cfg)
    yield cfg
    _config.set_override(previous)


def _dispatch(text, adapter, fake, choice, platform="discord", authorized=True):
    # Pin the prompts gateway to the core registry for the dispatch.
    with mock.patch.object(plugin, "_design_transport", return_value=(fake, "http")), \
            mock.patch.object(core_prompts, "gateway", return_value=core_prompts), \
            mock.patch.object(core_prompts, "wait_for_response", return_value=choice):
        return asyncio.run(plugin.design_dispatch(
            _event(text, platform=platform), _gateway(adapter, authorized)))


# ------------------------------------------------------- transport (#524 Stage 3)
#
# The HTTP transport ([design] inheriting [llm]) is the only design lane:
# the ctx.llm fallback for endpoint-less Hermes installs is deleted with
# the mode that needed it.

def _transport_cfg(*, http_endpoint=False):
    return {"bot": {"mode": "standalone"},
            "design": {"chat_url": "http://design.invalid/v1" if http_endpoint else "",
                       "model": "sol" if http_endpoint else "", "api_key_env": "",
                       "timeout_s": 180},
            "llm": {"chat_url": "", "model": "", "api_key_env": "LLM_API_KEY",
                    "timeout_s": 20}}


def test_design_transport_uses_the_http_endpoint(monkeypatch):
    seen = {}
    monkeypatch.setattr(design, "http_complete",
                        lambda cfg, messages: seen.update(messages=messages) or "http reply")
    complete, kind = plugin._design_transport(_transport_cfg(http_endpoint=True))
    assert kind == "http" and complete is not None
    out = asyncio.run(complete([{"role": "user", "content": "a cube"}]))
    assert out == "http reply"
    assert seen["messages"] == [{"role": "user", "content": "a cube"}]


def test_design_transport_llm_endpoint_counts(monkeypatch):
    # [design] empty but [llm] configured: the inherited endpoint resolves.
    cfg = _transport_cfg()
    cfg["llm"].update(chat_url="http://llm.invalid/v1", model="luna")
    complete, kind = plugin._design_transport(cfg)
    assert kind == "http" and complete is not None


def test_design_transport_without_an_endpoint_is_none():
    complete, kind = plugin._design_transport(_transport_cfg())
    assert complete is None and kind == ""


def test_disabled_design_answers_not_set_up(design_env):
    design_env["design"]["enabled"] = False
    adapter = FakeAdapter()
    out = _dispatch("/print-design a plate", adapter, scripted([GOOD]), design.CANCEL_LABEL)
    assert out == {"action": "skip", "reason": "design not enabled"}
    assert "not set up" in adapter.sent[-1] and "DESIGN__ENABLED" in adapter.sent[-1]


def test_enabled_but_no_model_answers_not_configured(design_env):
    adapter = FakeAdapter()
    with mock.patch.object(plugin, "_design_transport", return_value=(None, "")):
        out = asyncio.run(plugin.design_dispatch(_event("/print-design a plate"),
                                                 _gateway(adapter)))
    assert out["reason"] == "design model not configured"
    assert "no design model is configured" in adapter.sent[-1]


def test_bare_command_shows_usage(design_env):
    adapter = FakeAdapter()
    out = _dispatch("/print-design", adapter, scripted([GOOD]), design.CANCEL_LABEL)
    assert out["reason"] == "design usage shown"
    assert "Usage: /print-design" in adapter.sent[-1]


def test_unauthorized_and_unknown_platforms_fall_through(design_env):
    adapter = FakeAdapter()
    assert _dispatch("/print-design a plate", adapter, scripted([GOOD]),
                     design.CANCEL_LABEL, authorized=False) is None
    assert _dispatch("/print-design a plate", adapter, scripted([GOOD]),
                     design.CANCEL_LABEL, platform="irc") is None
    assert _dispatch("just chatting", adapter, scripted([GOOD]),
                     design.CANCEL_LABEL) is None
    assert adapter.sent == []


# ------------------------------------------------- close-out: alias, auto-on, Telegram/Matrix

def test_design_alias_shows_the_same_usage(design_env):
    adapter = FakeAdapter()
    out = _dispatch("/design", adapter, scripted([GOOD]), design.CANCEL_LABEL)
    assert out["reason"] == "design usage shown"
    assert "Usage: /print-design" in adapter.sent[-1]


def test_design_alias_answers_not_set_up_when_explicitly_off(design_env):
    design_env["design"]["enabled"] = False
    adapter = FakeAdapter()
    out = _dispatch("/design a plate", adapter, scripted([GOOD]), design.CANCEL_LABEL)
    assert out == {"action": "skip", "reason": "design not enabled"}
    assert "not set up" in adapter.sent[-1]


def test_auto_posture_without_an_endpoint_answers_not_set_up(design_env):
    # enabled unset (auto) + no endpoint anywhere: same answer as #422 pinned.
    design_env["design"]["enabled"] = None
    adapter = FakeAdapter()
    with mock.patch.object(plugin, "_design_transport", return_value=(None, "")):
        out = asyncio.run(plugin.design_dispatch(_event("/print-design a plate"),
                                                 _gateway(adapter)))
    assert out["reason"] == "design not enabled"
    assert "not set up" in adapter.sent[-1] and "DESIGN__ENABLED" in adapter.sent[-1]


def test_auto_posture_with_an_endpoint_runs(design_env):
    # enabled unset (auto) + a transport (endpoint configured): design is on.
    design_env["design"]["enabled"] = None
    adapter = FakeAdapter()
    out = _dispatch("/design", adapter, scripted([GOOD]), design.CANCEL_LABEL)
    assert out["reason"] == "design usage shown"


@pytest.mark.parametrize("platform", ["telegram", "matrix"])
def test_design_command_is_served_on_telegram_and_matrix(design_env, platform):
    adapter = FakeAdapter()
    out = _dispatch("/print-design", adapter, scripted([GOOD]), design.CANCEL_LABEL,
                    platform=platform)
    assert out["reason"] == "design usage shown"
    assert "Usage: /print-design" in adapter.sent[-1]


@pytest.mark.parametrize("platform", ["telegram", "matrix"])
def test_design_on_telegram_and_matrix_answers_not_set_up_without_an_endpoint(
        design_env, platform):
    design_env["design"]["enabled"] = None
    adapter = FakeAdapter()
    with mock.patch.object(plugin, "_design_transport", return_value=(None, "")):
        out = asyncio.run(plugin.design_dispatch(
            _event("design me a hook", platform=platform), _gateway(adapter)))
    assert out["reason"] == "design not enabled"
    assert "not set up" in adapter.sent[-1]


def test_telegram_and_matrix_stay_pure_bots_for_ordinary_chat():
    # Design is a separate configured lane; it must not wire the [llm] chat
    # fallback on the pure-bot platforms (#164 posture, unchanged).
    from print_handy.bot import matrix as matrix_mod
    from print_handy.bot import telegram as telegram_mod
    assert telegram_mod.StandaloneAdapter.uses_llm is False
    assert matrix_mod.StandaloneAdapter.uses_llm is False
    assert matrix_mod.StandaloneAdapter.supports_buttons is False


def test_design_alias_is_in_the_commands_table():
    from print_handy.bot import commands
    canonical = commands.by_name("print-design")
    assert commands.by_name("design") is canonical
    assert commands.as_text("design", {"prompt": "a hook"}) == "/design a hook"
    assert commands.as_text("print-design", {"prompt": "a hook"}) == "/print-design a hook"
    registered = dict(commands.registrations())
    assert registered["design"] is canonical and registered["print-design"] is canonical
    assert "`/design`" in commands.help_text()


@needs_scad
@pytest.mark.parametrize("platform", ["telegram", "matrix"])
def test_design_turn_runs_on_telegram_and_matrix(design_env, platform):
    # A full turn through the platform-neutral prompt path: the fake
    # adapter stands in for the platform adapter (Telegram buttons, Matrix
    # numbered lists both resolve through the shared prompt registry).
    adapter = FakeAdapter()
    out = _dispatch("/design a 40x20x3 plate with two 4 mm holes", adapter,
                    scripted([GOOD]), design.CANCEL_LABEL, platform=platform)
    assert out["reason"] == "design cancelled"
    post = next(content for content in adapter.sent if "Design turn 1" in content)
    assert "40 × 20 × 3" in post
    assert adapter.clarified[-1][1] == [design.SLICE_LABEL, design.CHANGE_LABEL,
                                        design.CANCEL_LABEL]


@needs_scad
@pytest.mark.parametrize("reply,reason", [("2", "design awaiting revision"),
                                          ("3", "design cancelled")])
def test_design_prompt_answers_a_numbered_reply_through_the_real_matrix_adapter(
        design_env, reply, reason):
    """Regression (#426 review): Matrix has no buttons, so the design
    Slice/Change/Cancel prompt is answered by a typed numbered reply, which
    the bot core routes by the LIVE session key. The whole path runs for
    real here — the actual Matrix adapter, the actual prompt registry, no
    mocked wait — because the mocked-wait dispatch tests cannot see a
    registration-key mismatch: pre-fix the prompt was registered under the
    flow key only, replying with the number returned False and the
    confirmation timed out (surfacing as a cancel)."""
    import io
    import json
    import urllib.parse

    from print_handy.bot import matrix as matrix_mod

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    class Stub:
        def __init__(self):
            self.bodies = []

        def __call__(self, req, timeout=None):
            path = urllib.parse.urlsplit(req.full_url).path
            body = (json.loads(req.data) if req.data and
                    req.headers.get("Content-type") == "application/json" else None)
            if "/send/m.room.message/" in path:
                self.bodies.append((body or {}).get("body", ""))
                return _Response(json.dumps({"event_id": "$evt"}).encode())
            if path.endswith("/media/v3/upload"):
                return _Response(json.dumps({"content_uri": "mxc://example.org/m"}).encode())
            raise AssertionError(f"unexpected homeserver call: {req.method} {req.full_url}")

    stub = Stub()
    mx_access = "mx-test-access"
    adapter = matrix_mod.StandaloneAdapter(None, ["42"], homeserver="https://matrix.example.org",
                                           token=mx_access, opener=stub)
    live_key = "matrix:c::42"
    gateway = S(_is_user_authorized_for_source=lambda s: True,
                _delivery_adapter_for=lambda s: adapter,
                _session_key_for_source=lambda s: live_key)
    fake = scripted([GOOD])

    async def scenario():
        with mock.patch.object(plugin, "_design_transport", return_value=(fake, "http")), \
                mock.patch.object(core_prompts, "gateway", return_value=core_prompts):
            task = asyncio.create_task(plugin.design_dispatch(
                _event("/design a 40x20x3 plate with two 4 mm holes", platform="matrix"),
                gateway))
            for _ in range(600):
                if any("Reply with the number" in body for body in stub.bodies):
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("the design prompt never reached the room")
            answered = await adapter.answer_typed(live_key, reply)
            out = await asyncio.wait_for(task, 15)
            return answered, out

    answered, out = asyncio.run(scenario())
    assert answered is True
    assert out["reason"] == reason
    assert any(body.startswith("> Answered by message:") for body in stub.bodies)


@needs_scad
def test_turn_posts_preview_choices_and_change_it_revises(design_env):
    adapter = FakeAdapter()
    fake = scripted([GOOD, THICKER])
    out = _dispatch("/print-design a 40x20x3 plate with two 4 mm holes", adapter, fake,
                    design.CHANGE_LABEL)
    assert out["reason"] == "design awaiting revision"
    post = next(content for content in adapter.sent if "Design turn 1" in content)
    assert "MEDIA:" in post and "40 × 20 × 3" in post and "watertight" in post
    assert adapter.clarified[-1][1] == [design.SLICE_LABEL, design.CHANGE_LABEL,
                                        design.CANCEL_LABEL]
    root = design.sessions_root(design_env["paths"]["cache"])
    session = design.find_session_for_owner(root, "c::42")
    assert session and session["status"] == "awaiting_revision"
    # The next plain message is the revision; Cancel then clears the session.
    out = _dispatch("make it 5 mm thick", adapter, fake, design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    post = next(content for content in adapter.sent if "Design turn 2" in content)
    assert "40 × 20 × 5" in post
    assert design.find_session_for_owner(root, "c::42") is None
    # The revision turn showed the model the previous source.
    assert "plate_z = 3" in fake.calls[1][1]["content"]


@needs_scad
def test_slice_it_hands_the_stl_to_prepared_intake_once(design_env):
    adapter = FakeAdapter()
    handed = {}

    async def fake_prepare(event, adapter_, state, root, **kw):
        handed["event"] = event
        handed["root"] = root
        return {"action": "skip", "reason": "intake handled"}

    with mock.patch.object(prepared_intake, "prepare_link", side_effect=fake_prepare) as prepare, \
            mock.patch.object(printer_state, "load", return_value=S()):
        out = _dispatch("/print-design a plate", adapter, scripted([GOOD]),
                        design.SLICE_LABEL)
    assert prepare.call_count == 1  # exactly one handoff; the design flow never slices itself
    assert out == {"action": "skip", "reason": "intake handled"}
    event = handed["event"]
    root = design.sessions_root(design_env["paths"]["cache"])
    session_dirs = [p for p in root.iterdir() if p.is_dir()]
    assert event.media_urls == [str(session_dirs[0] / "turn-1.stl")]
    assert event.text == "" and event.message_id.startswith("design-")
    session = design.load_session(root, session_dirs[0].name)
    assert session["status"] == "handed_off"


@needs_scad
def test_failed_turn_reports_the_error_and_offers_no_choices(design_env):
    adapter = FakeAdapter()
    out = _dispatch("/print-design a plate", adapter,
                    scripted(["import('/etc/passwd');\ncube([1, 1, 1]);"]),
                    design.CANCEL_LABEL)
    assert out["reason"] == "design turn failed"
    assert "may not use" in adapter.sent[-1]
    assert adapter.clarified == []


# --------------------------------------------------------------------- Stage 2: editing + NL routing

EDIT_BODY = ("difference() {\n"
             "  scale([1, 1, 2]) base();\n"
             "  translate([0, 0, -1]) cylinder(h = 99, d = 4, $fn = 48);\n"
             "}\n")


def _box_triangles(dx, dy, dz, ox=0.0, oy=0.0, oz=0.0):
    v = [(ox, oy, oz), (ox + dx, oy, oz), (ox + dx, oy + dy, oz), (ox, oy + dy, oz),
         (ox, oy, oz + dz), (ox + dx, oy, oz + dz), (ox + dx, oy + dy, oz + dz),
         (ox, oy + dy, oz + dz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (3, 0, 4), (3, 4, 7)]
    return [[v[a], v[b], v[c]] for a, b, c in faces]


def _plate_stl(tmp_path, name="plate.stl"):
    from print_handy.core.geometry_layout import write_binary_stl
    path = tmp_path / name
    write_binary_stl(_box_triangles(40, 20, 3, -20, -10, 0), path)
    return path


def _media_event(text, *paths):
    event = _event(text)
    event.media_urls = [str(p) for p in paths]
    return event


def _dispatch_event(event, adapter, fake, choice):
    with mock.patch.object(plugin, "_design_transport", return_value=(fake, "http")), \
            mock.patch.object(core_prompts, "gateway", return_value=core_prompts), \
            mock.patch.object(core_prompts, "wait_for_response", return_value=choice):
        return asyncio.run(plugin.design_dispatch(event, _gateway(adapter)))


def _sessions(tmp_path):
    import json as _json
    folder = tmp_path / "cache" / "design-sessions"
    return [_json.loads(p.read_text()) for p in folder.glob("*/session.json")] \
        if folder.is_dir() else []


def test_design_parse_matrix():
    parse = plugin._design_parse
    assert parse("design me a hook") == ("create", "a hook")
    assert parse("Help me design a shelf, please") == ("create", "a shelf, please")
    assert parse("help me design") == ("ask", "")
    assert parse("design this") == ("context", "")
    assert parse("please redesign this model") == ("context", "please model")
    assert parse("CHANGE THIS to be taller") == ("context", "to be taller")
    assert parse("edit this") == ("context", "")
    for plain in ("print it", "designated driver on Friday", "redesign the kitchen",
                  "changes this week", "https://makerworld.com/en/models/1-x",
                  "just chatting"):
        assert parse(plain) is None, plain


@needs_scad
def test_command_with_dropped_stl_starts_a_mesh_edit(design_env, tmp_path):
    adapter = FakeAdapter()
    out = _dispatch_event(
        _media_event("/print-design double the height", _plate_stl(tmp_path)),
        adapter, scripted([EDIT_BODY]), design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    (session,) = _sessions(tmp_path)
    assert session["kind"] == "edit" and session["edit_mode"] == "mesh"
    assert adapter.sent[0] == plugin.DESIGN_WORKING_NOTE
    assert "as a mesh" in adapter.sent[1]
    assert "40 × 20 × 6" in adapter.sent[1]  # the doubled plate, from the render


@needs_scad
def test_command_with_dropped_scad_starts_a_source_edit(design_env, tmp_path):
    source = tmp_path / "widget.scad"
    source.write_text(GOOD)
    adapter = FakeAdapter()
    out = _dispatch_event(_media_event("/print-design make it thicker", source),
                          adapter, scripted([THICKER]), design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    (session,) = _sessions(tmp_path)
    assert session["kind"] == "edit" and session["edit_mode"] == "scad"
    assert adapter.sent[0] == plugin.DESIGN_WORKING_NOTE
    assert "in source form" in adapter.sent[1]
    assert "40 × 20 × 5" in adapter.sent[1]


@needs_scad
def test_nl_design_this_with_a_file_edits_it(design_env, tmp_path):
    adapter = FakeAdapter()
    out = _dispatch_event(
        _media_event("design this — double the height", _plate_stl(tmp_path)),
        adapter, scripted([EDIT_BODY]), design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    (session,) = _sessions(tmp_path)
    assert session["kind"] == "edit" and session["edit_mode"] == "mesh"


@needs_scad
def test_nl_design_me_creates(design_env, tmp_path):
    adapter = FakeAdapter()
    out = _dispatch_event(_event("design me a plate with two 4 mm holes"),
                          adapter, scripted([GOOD]), design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    (session,) = _sessions(tmp_path)
    assert session["kind"] == "create"
    assert "a plate with two 4 mm holes" in session["description"]


@needs_scad
def test_nl_link_in_the_message_is_fetched_and_edited(design_env, tmp_path):
    fetched = []

    def fake_fetch(url, folder):
        fetched.append(url)
        return _plate_stl(Path(folder), "linked.stl")

    adapter = FakeAdapter()
    with mock.patch.object(plugin, "_fetch_design_link", side_effect=fake_fetch):
        out = _dispatch_event(
            _event("redesign this https://www.printables.com/model/9-x — double the height"),
            adapter, scripted([EDIT_BODY]), design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    assert fetched == ["https://www.printables.com/model/9-x"]
    (session,) = _sessions(tmp_path)
    assert session["kind"] == "edit" and session["source"]["origin"] == "link"


@needs_scad
def test_nl_change_this_uses_the_recent_model_link(design_env, tmp_path):
    def fake_fetch(url, folder):
        return _plate_stl(Path(folder), "recent.stl")

    adapter = FakeAdapter()
    with mock.patch.object(plugin, "_recent_model_link",
                           return_value="https://www.printables.com/model/9-x"), \
            mock.patch.object(plugin, "_fetch_design_link", side_effect=fake_fetch):
        out = _dispatch_event(_event("change this to be twice as tall"),
                              adapter, scripted([EDIT_BODY]), design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    (session,) = _sessions(tmp_path)
    assert session["kind"] == "edit" and session["source"]["origin"] == "link"


def test_change_this_with_no_model_anywhere_is_not_consumed(design_env):
    adapter = FakeAdapter()
    out = _dispatch_event(_event("change this"), adapter, scripted([GOOD]),
                          design.CANCEL_LABEL)
    assert out is None
    assert adapter.sent == []


def test_design_this_with_no_model_asks_for_one(design_env):
    adapter = FakeAdapter()
    out = _dispatch_event(_event("design this"), adapter, scripted([GOOD]),
                          design.CANCEL_LABEL)
    assert out["reason"] == "design needs a model or a description"
    assert "Drop the file" in adapter.sent[-1]


@pytest.mark.parametrize("text", [
    "https://makerworld.com/en/models/123-thing",
    "print it",
    "/print-next",
    "just chatting about printers",
])
def test_non_trigger_messages_fall_through(design_env, text):
    adapter = FakeAdapter()
    assert _dispatch_event(_event(text), adapter, scripted([GOOD]),
                           design.CANCEL_LABEL) is None
    assert adapter.sent == []


def test_plain_file_drop_without_a_trigger_falls_through(design_env, tmp_path):
    adapter = FakeAdapter()
    assert _dispatch_event(_media_event("", _plate_stl(tmp_path)), adapter,
                           scripted([GOOD]), design.CANCEL_LABEL) is None
    assert adapter.sent == []


def test_nl_create_phrase_answers_not_set_up_when_design_is_off(design_env):
    # Criterion 7 (catduck's ruling): a design phrase gets the command's
    # "not set up" reply and is consumed, exactly like /print-design.
    design_env["design"]["enabled"] = False
    adapter = FakeAdapter()
    out = _dispatch_event(_event("design me a hook"), adapter, scripted([GOOD]),
                          design.CANCEL_LABEL)
    assert out == {"action": "skip", "reason": "design not enabled"}
    assert "not set up" in adapter.sent[-1] and "DESIGN__ENABLED" in adapter.sent[-1]
    assert _sessions(design_env["paths"]["cache"].parent) == []


def test_nl_edit_phrase_with_a_model_answers_not_set_up_when_design_is_off(
        design_env, tmp_path):
    design_env["design"]["enabled"] = False
    adapter = FakeAdapter()
    out = _dispatch_event(
        _media_event("change this to be twice as tall", _plate_stl(tmp_path)),
        adapter, scripted([GOOD]), design.CANCEL_LABEL)
    assert out == {"action": "skip", "reason": "design not enabled"}
    assert "not set up" in adapter.sent[-1]
    assert _sessions(tmp_path) == []


def test_nl_design_this_without_a_model_answers_not_set_up_when_design_is_off(
        design_env):
    design_env["design"]["enabled"] = False
    adapter = FakeAdapter()
    out = _dispatch_event(_event("design this"), adapter, scripted([GOOD]),
                          design.CANCEL_LABEL)
    assert out == {"action": "skip", "reason": "design not enabled"}
    assert "not set up" in adapter.sent[-1]


def test_nl_edit_phrase_without_a_model_is_not_consumed_when_design_is_off(
        design_env):
    # "change this" with no model anywhere was never a trigger, so the
    # not-set-up reply does not apply: the message is not ours.
    design_env["design"]["enabled"] = False
    adapter = FakeAdapter()
    out = _dispatch_event(_event("change this"), adapter, scripted([GOOD]),
                          design.CANCEL_LABEL)
    assert out is None
    assert adapter.sent == []


@pytest.mark.parametrize("text", [
    "https://makerworld.com/en/models/123-thing",
    "print it",
    "just chatting about printers",
])
def test_non_trigger_messages_fall_through_when_design_is_off(design_env, text):
    design_env["design"]["enabled"] = False
    adapter = FakeAdapter()
    assert _dispatch_event(_event(text), adapter, scripted([GOOD]),
                           design.CANCEL_LABEL) is None
    assert adapter.sent == []


def test_plain_file_drop_without_a_trigger_falls_through_when_design_is_off(
        design_env, tmp_path):
    design_env["design"]["enabled"] = False
    adapter = FakeAdapter()
    assert _dispatch_event(_media_event("", _plate_stl(tmp_path)), adapter,
                           scripted([GOOD]), design.CANCEL_LABEL) is None
    assert adapter.sent == []


def test_nl_trigger_answers_not_configured_when_enabled_without_a_model(
        design_env):
    adapter = FakeAdapter()
    with mock.patch.object(plugin, "_design_transport", return_value=(None, "")):
        out = asyncio.run(plugin.design_dispatch(_event("design me a hook"),
                                                 _gateway(adapter)))
    assert out["reason"] == "design model not configured"
    assert "no design model is configured" in adapter.sent[-1]


@needs_scad
def test_awaiting_revision_still_wins_over_trigger_words(design_env, tmp_path):
    adapter = FakeAdapter()
    fake = scripted([GOOD, THICKER])
    out = _dispatch_event(_event("/print-design a plate"), adapter, fake,
                          design.CHANGE_LABEL)
    assert out["reason"] == "design awaiting revision"
    out = _dispatch_event(_event("design this but 5 mm thick"), adapter, fake,
                          design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    assert "Design turn 2" in adapter.sent[-2]
    (session,) = _sessions(tmp_path)
    assert session["kind"] == "create" and session["turns"] == 2


def test_second_message_during_a_turn_is_refused_not_run_concurrently(design_env):
    """While one turn's model call is outstanding, a second owner message
    must not load the same session and run a concurrent turn over the same
    SCAD/STL files: it gets a busy refusal and the model is not called
    again until the first turn finishes (P2-151)."""
    async def scenario():
        gate = asyncio.Event()
        calls = []

        async def complete(messages):
            calls.append(messages)
            await gate.wait()
            return GOOD

        adapter1, adapter2 = FakeAdapter(), FakeAdapter()
        with mock.patch.object(plugin, "_design_transport", return_value=(complete, "http")), \
                mock.patch.object(core_prompts, "gateway", return_value=core_prompts), \
                mock.patch.object(core_prompts, "wait_for_response", return_value=design.CANCEL_LABEL):
            first = asyncio.create_task(plugin.design_dispatch(
                _event("/print-design a bracket"), _gateway(adapter1)))
            for _ in range(100):
                if calls:
                    break
                await asyncio.sleep(0.01)
            assert len(calls) == 1  # the first turn is inside its model call
            second = await plugin.design_dispatch(
                _event("/print-design another bracket"), _gateway(adapter2))
            assert second == {"action": "skip", "reason": "design turn already running"}
            assert any("already running" in (c or "") for c in adapter2.sent)
            assert len(calls) == 1  # no concurrent turn reached the model
            gate.set()
            await first
            # The claim is released once the turn is over: a fresh dispatch
            # reaches the model again.
            third_adapter = FakeAdapter()
            await plugin.design_dispatch(
                _event("/print-design a third bracket"), _gateway(third_adapter))
            assert len(calls) == 2

    asyncio.run(scenario())


def test_attached_model_wins_over_link_in_the_same_message(design_env, tmp_path):
    """A message carrying both an attached model and a model link must edit
    the attachment — the exact source the operator supplied — not fetch the
    link and edit the downloaded version (P2-156)."""
    attached = _plate_stl(tmp_path, "attached.stl")
    calls = []

    def fake_start_edit(self, owner, chat_id, description, source_path=None, **kwargs):
        calls.append((source_path, kwargs.get("link_url")))
        raise RuntimeError("stop after routing")

    fetched = []
    adapter = FakeAdapter()
    with mock.patch.object(design.DesignEngine, "start_edit_session", fake_start_edit), \
            mock.patch.object(plugin, "_fetch_design_link",
                              side_effect=lambda url, folder: fetched.append(url)):
        out = _dispatch_event(
            _media_event("redesign this https://www.printables.com/model/9-x — double the height",
                         attached),
            adapter, scripted([EDIT_BODY]), design.CANCEL_LABEL)
    assert out["reason"] == "design turn failed"  # stopped by the sentinel above
    assert fetched == []  # the link was never fetched
    assert len(calls) == 1
    source_path, link_url = calls[0]
    assert source_path is not None and Path(source_path).name == "attached.stl"
    assert link_url is None


# --------------------------------------------------------------------- #841: preview wording, params, baseline

def _fake_result(session, tmp_path, **overrides):
    session["turns"] = 1
    result = {"session": session,
              "scad_path": tmp_path / "turn-1.scad",
              "stl_path": tmp_path / "turn-1.stl",
              "preview_path": str(tmp_path / "turn-1.png"),
              "preview_error": None,
              "param_changes": [],
              "geometry": {"dimensions_mm": [40.0, 20.0, 3.0],
                           "abs_volume_mm3": 2400.0, "shell_count": 1,
                           "watertight": True, "manifold": True}}
    result.update(overrides)
    return result


def test_no_preview_post_names_the_reason_and_still_states_dimensions(design_env, tmp_path):
    async def fake_run_turn(self, session, instruction):
        return _fake_result(session, tmp_path, preview_path=None,
                            preview_error="openscad not on PATH")

    adapter = FakeAdapter()
    with mock.patch.object(design.DesignEngine, "run_turn", fake_run_turn):
        out = _dispatch("/print-design a plate", adapter, scripted([GOOD]),
                        design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    post = next(content for content in adapter.sent if "Design turn 1" in content)
    assert "(No preview: openscad not on PATH. Check the dimensions above before slicing.)" in post
    assert "40 × 20 × 3" in post  # the measured dimensions are on the card regardless
    assert "(No preview could be rendered" not in post


def test_turn_post_lists_the_changed_parameters(design_env, tmp_path):
    async def fake_run_turn(self, session, instruction):
        return _fake_result(session, tmp_path,
                            param_changes=[("plate_z", 3.0, 5.0)])

    adapter = FakeAdapter()
    with mock.patch.object(design.DesignEngine, "run_turn", fake_run_turn):
        _dispatch("/print-design a plate", adapter, scripted([GOOD]), design.CANCEL_LABEL)
    post = next(content for content in adapter.sent if "Design turn 1" in content)
    assert "plate_z: 3 → 5" in post


def test_dropped_file_with_no_instruction_uses_the_baseline_turn(design_env, tmp_path):
    calls = []

    async def fake_run_turn(self, session, instruction):
        calls.append("run_turn")
        raise AssertionError("an empty instruction must not run a model turn")

    def fake_baseline(self, session):
        calls.append("baseline")
        return _fake_result(session, tmp_path)

    adapter = FakeAdapter()
    with mock.patch.object(design.DesignEngine, "run_turn", fake_run_turn), \
            mock.patch.object(design.DesignEngine, "baseline_turn", fake_baseline):
        out = _dispatch_event(
            _media_event("/print-design", _plate_stl(tmp_path)),
            adapter, scripted([EDIT_BODY]), design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    assert calls == ["baseline"]


# ------------------------------------------- real OpenSCAD binary (expansion)

WARN_BAD = "cube([missing_var, 10, 10]);\n"


@needs_scad
def test_dispatch_repairs_a_warning_class_design_before_posting(design_env):
    # The first model reply fails --hardwarnings (unknown variable); the
    # engine's repair loop must feed the real OpenSCAD error back and post
    # the repaired turn, all through the real dispatch path.
    adapter = FakeAdapter()
    fake = scripted([WARN_BAD, GOOD])
    out = _dispatch("/print-design a 40x20x3 plate with two 4 mm holes", adapter,
                    fake, design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    assert len(fake.calls) == 2
    repair_prompt = fake.calls[1][1]["content"]
    # The real render error is fed back (its text currently lacks the
    # WARNING line itself — issue #1046).
    assert "The previous file failed to render" in repair_prompt
    assert "Current top level object is empty" in repair_prompt
    post = next(content for content in adapter.sent if "Design turn 1" in content)
    assert "40 × 20 × 3" in post


@needs_scad
def test_dispatch_posts_the_parametrized_dimensions_it_rendered(design_env):
    adapter = FakeAdapter()
    out = _dispatch("/print-design a small block", adapter,
                    scripted(["w = 26;\nd = 12;\nh = 4;\ncube([w, d, h]);\n"]),
                    design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    post = next(content for content in adapter.sent if "Design turn 1" in content)
    assert "26 × 12 × 4" in post


# ------------------------------------------- typed answers while the design question is open

def test_typed_answer_reaches_the_open_design_question_and_a_typed_change_revises(
        design_env, tmp_path):
    """While the Slice it / Change it / Cancel question is open, the owner's
    plain message is the answer to it (a Matrix number, a typed choice, or a
    change typed instead of pressing Change it). design_dispatch must let it
    fall through to the bot core's typed-answer routing instead of refusing
    it as "a design turn is already running", and a typed change must run as
    the next turn of the same design, not cancel it."""
    instructions = []

    async def fake_run_turn(self, session, instruction):
        instructions.append(instruction)
        result = _fake_result(session, tmp_path)
        session["turns"] = len(instructions)
        return result

    async def scenario():
        adapter = FakeAdapter()
        with mock.patch.object(plugin, "_design_transport", return_value=(scripted([GOOD]), "http")), \
                mock.patch.object(core_prompts, "gateway", return_value=core_prompts), \
                mock.patch.object(design.DesignEngine, "run_turn", fake_run_turn):
            task = asyncio.create_task(plugin.design_dispatch(
                _event("/print-design a plate"), _gateway(adapter)))

            async def open_prompt():
                for _ in range(300):
                    cid = core_prompts.open_prompt_for("session-key")
                    if cid:
                        return cid
                    await asyncio.sleep(0.01)
                raise AssertionError("the design question never opened")

            cid = await open_prompt()
            # The typed change is not refused by the busy guard ...
            typed = await plugin.design_dispatch(_event("make it 5 mm taller"), _gateway(adapter))
            assert typed is None
            assert not any("already running" in (c or "") for c in adapter.sent)
            # ... so the bot core answers the open prompt with it.
            assert core_prompts.resolve_gateway_clarify(cid, "make it 5 mm taller")
            cid2 = None
            for _ in range(300):
                cid2 = core_prompts.open_prompt_for("session-key")
                if cid2 and cid2 != cid:
                    break
                await asyncio.sleep(0.01)
            assert cid2 and cid2 != cid
            # A design command during the open question is still refused.
            busy = await plugin.design_dispatch(_event("/print-design a cup"), _gateway(adapter))
            assert busy == {"action": "skip", "reason": "design turn already running"}
            assert core_prompts.resolve_gateway_clarify(cid2, design.CANCEL_LABEL)
            out = await asyncio.wait_for(task, 10)
            return adapter, out

    adapter, out = asyncio.run(scenario())
    assert instructions == ["a plate", "make it 5 mm taller"]
    assert out["reason"] == "design cancelled"
    assert sum("Design turn" in (c or "") for c in adapter.sent) == 2


# ------------------------------------------- the reply, the one question, and undo

HOOK = ("// Part: Wall hook for a 25 mm rail\n// Size: 40 x 20 x 3 mm\n"
        "// Assumes: the rail is 25 mm wide and 3 mm thick\n"
        "w = 40; // overall width\nd = 20; // depth\nheight = 3; // plate thickness\n"
        "cube([w, d, height]);\n")


def _cube_runner(cmd, **kwargs):
    from print_handy.core.geometry_layout import write_binary_stl
    p = design.scad_params(Path(cmd[-1]).read_text(encoding="utf-8"))
    write_binary_stl(_box_triangles(p.get("w", 40), p.get("d", 20), p.get("height", 3)),
                     Path(cmd[cmd.index("-o") + 1]))
    import subprocess as _sp
    return _sp.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def fake_render():
    real_init = design.DesignEngine.__init__

    def init(self, *args, **kwargs):
        kwargs.setdefault("runner", _cube_runner)
        kwargs.setdefault("preview", lambda stl: None)
        real_init(self, *args, **kwargs)

    with mock.patch.object(design.DesignEngine, "__init__", init):
        yield


def test_the_reply_says_what_was_made_assumed_and_how_to_change_it(design_env, fake_render):
    adapter = FakeAdapter()
    out = _dispatch("/print-design a wall hook for a 25 mm rail", adapter, scripted([HOOK]),
                    design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    post = next(content for content in adapter.sent if "Design turn 1" in content)
    assert "**Design turn 1** — Wall hook for a 25 mm rail." in post
    assert "Measured: 40 × 20 × 3 mm" in post and "watertight" in post
    assert "Assumed: the rail is 25 mm wide and 3 mm thick." in post
    assert "Key parameters: w = 40 (overall width), d = 20 (depth), height = 3 (plate thickness)." in post
    assert '"make it 5 mm taller"' in post and '"undo"' in post
    assert "Heads-up" not in post  # the checks passed


def test_one_question_is_asked_then_the_answer_designs_the_part(design_env, fake_render):
    adapter = FakeAdapter()
    fake = scripted(["QUESTION: How wide is the rail?", HOOK])
    out = _dispatch("/print-design a hook for my rail", adapter, fake, design.CANCEL_LABEL)
    assert out["reason"] == "design asked a question"
    assert adapter.sent[-1].startswith("How wide is the rail?")
    assert adapter.clarified == []  # no Slice/Change/Cancel for a part that does not exist
    out = _dispatch("25 mm", adapter, fake, design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    assert "The user answered: 25 mm" in fake.calls[1][1]["content"]
    assert any("Design turn 1" in content for content in adapter.sent)


def test_typed_undo_goes_back_without_a_model_call(design_env, fake_render):
    adapter = FakeAdapter()
    fake = scripted([HOOK])
    _dispatch("/print-design a hook", adapter, fake, design.CHANGE_LABEL)
    _dispatch("make it 5 mm taller", adapter, fake, design.CHANGE_LABEL)
    out = _dispatch("undo", adapter, fake, design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    post = next(content for content in adapter.sent if "Design turn 3" in content)
    assert "Back to version 1" in post and "40 × 20 × 3" in post
    assert "height: 8 → 3" in post
    assert len(fake.calls) == 1


# ------------------------------------------- typed answers reach the design, not later hooks (#1206)

class TypedAdapter(FakeAdapter):
    """FakeAdapter with the platform adapter's typed-answer route."""

    async def answer_typed(self, session_key, text, user_id=None):
        cid = core_prompts.open_prompt_for(session_key)
        return bool(cid) and core_prompts.resolve_gateway_clarify(cid, str(text))


def test_design_hook_runs_before_the_edit_hook():
    hooks = list(plugin._DISPATCH_HOOKS)
    assert hooks.index(plugin.design_dispatch) < hooks.index(plugin.edit_dispatch)


def test_a_typed_change_at_the_open_question_is_claimed_by_the_design(design_env, tmp_path):
    """#1206 finding 1: returning None let edit_dispatch (later in the hook
    order) claim "make it 5 mm taller" before the bot core's typed-answer
    route ran, so the change went to the channel's job, not the design.
    design_dispatch must answer its own open question and claim the message."""
    instructions = []

    async def fake_run_turn(self, session, instruction):
        instructions.append(instruction)
        result = _fake_result(session, tmp_path)
        session["turns"] = len(instructions)
        return result

    async def scenario():
        adapter = TypedAdapter()
        with mock.patch.object(plugin, "_design_transport", return_value=(scripted([GOOD]), "http")), \
                mock.patch.object(core_prompts, "gateway", return_value=core_prompts), \
                mock.patch.object(design.DesignEngine, "run_turn", fake_run_turn):
            task = asyncio.create_task(plugin.design_dispatch(
                _event("/print-design a plate"), _gateway(adapter)))
            for _ in range(300):
                cid = core_prompts.open_prompt_for("session-key")
                if cid:
                    break
                await asyncio.sleep(0.01)
            assert cid
            typed = await plugin.design_dispatch(_event("make it 5 mm taller"), _gateway(adapter))
            assert typed == {"action": "skip", "reason": "typed answer to the design question"}
            for _ in range(300):
                cid2 = core_prompts.open_prompt_for("session-key")
                if cid2 and cid2 != cid:
                    break
                await asyncio.sleep(0.01)
            assert cid2 and cid2 != cid
            assert (await plugin.design_dispatch(_event("cancel"), _gateway(adapter)))["reason"] \
                == "typed answer to the design question"
            return await asyncio.wait_for(task, 10)

    out = asyncio.run(scenario())
    assert instructions == ["a plate", "make it 5 mm taller"]
    assert out["reason"] == "design cancelled"


@pytest.mark.parametrize("word", ["cancel", "Cancel.", "stop", "never mind", "3"])
def test_typed_cancel_words_at_the_question_cancel_without_a_model_call(design_env, fake_render, word):
    """#1206 finding 2: only the exact label cancelled; "cancel" ran a paid revision."""
    adapter = FakeAdapter()
    fake = scripted([HOOK])
    answers = iter([word, design.CANCEL_LABEL])
    with mock.patch.object(plugin, "_design_transport", return_value=(fake, "http")), \
            mock.patch.object(core_prompts, "gateway", return_value=core_prompts), \
            mock.patch.object(core_prompts, "wait_for_response",
                              side_effect=lambda *a, **k: next(answers)):
        out = asyncio.run(plugin.design_dispatch(_event("/print-design a hook"), _gateway(adapter)))
    assert out["reason"] == "design cancelled"
    assert len(fake.calls) == 1
    assert sum("Design turn" in (c or "") for c in adapter.sent) == 1


@pytest.mark.parametrize("word", ["slice it", "Slice", "print it", "1"])
def test_typed_slice_words_at_the_question_hand_off(design_env, fake_render, word):
    adapter = FakeAdapter()
    fake = scripted([HOOK])
    with mock.patch.object(plugin, "_design_handoff",
                           return_value={"action": "skip", "reason": "handed"}):
        out = _dispatch("/print-design a hook", adapter, fake, word)
    assert out["reason"] == "handed"
    assert len(fake.calls) == 1


def test_cancel_and_slice_words_after_change_it_are_not_revisions(design_env, fake_render):
    adapter = FakeAdapter()
    fake = scripted([HOOK])
    _dispatch("/print-design a hook", adapter, fake, design.CHANGE_LABEL)
    out = _dispatch("never mind", adapter, fake, design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    assert len(fake.calls) == 1
    _dispatch("/print-design a hook", adapter, fake, design.CHANGE_LABEL)
    with mock.patch.object(plugin, "_design_handoff",
                           return_value={"action": "skip", "reason": "handed"}) as handoff:
        out = _dispatch("slice it", adapter, fake, design.CANCEL_LABEL)
    assert out["reason"] == "handed"
    assert len(fake.calls) == 2
    assert Path(handoff.call_args.args[4]).is_file()


def test_typed_choice_is_narrow():
    assert design.typed_choice("SLICE IT!") == design.SLICE_LABEL
    assert design.typed_choice("change it") == design.CHANGE_LABEL
    assert design.typed_choice("2") == design.CHANGE_LABEL
    assert design.typed_choice("forget it") == design.CANCEL_LABEL
    for text in ("make it 5 mm taller", "cancel the hole", "4", "", None, "slice it thinner"):
        assert design.typed_choice(text) is None


# ------------------------------------------- a long design turn is never silent (#1206)

class WorkingAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.typing = 0

    async def signal_working(self, chat_id, metadata=None):
        self.typing += 1


def test_a_model_turn_is_acknowledged_and_shows_typing_until_it_posts(design_env, fake_render):
    """#1206 finding 6: a turn can take minutes (1 + repair_attempts model
    calls and renders) and nothing was posted until it ended."""
    adapter = WorkingAdapter()
    out = _dispatch("/print-design a hook", adapter, scripted([HOOK]), design.CANCEL_LABEL)
    assert out["reason"] == "design cancelled"
    ack = next(i for i, c in enumerate(adapter.sent) if c and c.startswith("Designing it"))
    turn = next(i for i, c in enumerate(adapter.sent) if c and "Design turn 1" in c)
    assert ack < turn
    assert adapter.typing >= 1


def test_undo_and_plain_parameter_edits_are_not_acknowledged(design_env, fake_render):
    adapter = WorkingAdapter()
    fake = scripted([HOOK])
    _dispatch("/print-design a hook", adapter, fake, design.CHANGE_LABEL)
    before = sum(1 for c in adapter.sent if c and c.startswith("Designing it"))
    _dispatch("make it 5 mm taller", adapter, fake, design.CHANGE_LABEL)
    _dispatch("undo", adapter, fake, design.CANCEL_LABEL)
    assert sum(1 for c in adapter.sent if c and c.startswith("Designing it")) == before == 1
    assert len(fake.calls) == 1


def test_will_call_model_matches_the_no_model_paths(tmp_path):
    eng = design.DesignEngine(tmp_path, scripted([HOOK]), cfg={})
    fresh = {"edit_mode": None}
    assert eng.will_call_model(fresh, "a hook", previous=None)
    assert not eng.will_call_model(fresh, "undo", previous=HOOK)
    assert not eng.will_call_model(fresh, "make it 5 mm taller", previous=HOOK)
    assert eng.will_call_model(fresh, "add a second hook", previous=HOOK)
    assert eng.will_call_model({"edit_mode": "mesh"}, "make it 5 mm taller", previous="base();")
