"""/print-pause review fixes (#1524 review).

Pins what the 2.3.4 /print-pause shipped without:

- the pause names its printer, the default one included, so a roster with
  more than one printer still pauses the default Moonraker printer;
- the adapter-control families pause through their own control, as the
  Web UI already did;
- a chat-typed pause that does not take never reposts a camera alert to
  the home chat and never says one was "posted above";
- in a shared channel an unreadable printer is "status unknown", never
  idle, and the pause lock is per printer, taken after the picker;
- nothing short of the typed command runs it: a bare mention word and
  the help menu do not.
"""
import asyncio
import copy
from types import SimpleNamespace as S

import pytest

from print_handy.bot import commands, mention, runtime
from print_handy.bot.handlers import common, print_pause, print_status
from print_handy.core import prompts
from print_handy.runtime import _config, family_control, print_alert_buttons as pab, print_stop

OPERATOR = "200000000000000002"
HOME = "100000000000000001"
GARAGE = "flashforge-ad5x"
WORKSHOP = "workshop-ad5x"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def two_printers(monkeypatch):
    data = copy.deepcopy(_config.cfg())
    base = copy.deepcopy(data["printers"][0])
    data["printers"] = [{**base, "id": GARAGE, "name": "Garage AD5X", "chat_id": ""},
                        {**base, "id": WORKSHOP, "name": "Workshop AD5X", "chat_id": HOME}]
    monkeypatch.setattr(_config, "_override", data)
    return data


class Gateway:
    def __init__(self, adapter, shared=None, routed=None):
        self._adapter, self._shared, self._routed = adapter, shared or {}, routed or {}

    def _is_user_authorized_for_source(self, source):
        return True

    def _delivery_adapter_for(self, source):
        return self._adapter

    def _session_key_for_source(self, source):
        return f"{source.chat_id}:{source.user_id}"

    def printer_id_for(self, source):
        return self._routed.get(str(source.chat_id))

    def shared_printer_ids_for(self, source):
        ids = self._shared.get(str(source.chat_id))
        return list(ids) if ids else None


class Adapter:
    def __init__(self, pick=None):
        self.sent, self.questions = [], []
        self.pick = pick or (lambda question, choices: None)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return S(success=True)

    async def send_clarify(self, chat_id, question, choices, clarify_id=None, session_key=None,
                           metadata=None):
        self.questions.append((question, list(choices)))
        answer = self.pick(question, list(choices))
        prompts.resolve_gateway_clarify(clarify_id, answer if answer is not None else prompts.CANCELLED)
        return S(success=True)


def event(text="/print-pause", chat_id=HOME):
    source = runtime.make_source(OPERATOR, chat_id, platform_name="discord")
    source.profile = common._hosting_profile()
    return S(source=source, text=text, internal=False, metadata={}, media_urls=[], message_id="m1")


def job(state="printing"):
    return {"state": state, "filename": "a.gcode", "layer": None, "total_layer": None,
            "progress": None, "print_duration": None}


class Control:
    def __init__(self, bambu=False):
        self.bambu, self.paused = bambu, []

    def is_bambu_lan(self):
        return self.bambu

    def pause(self, filename):
        self.paused.append(filename)
        return True, "Paused."


# ---------------------------------------------------------------- _pause routing

def test_pause_names_the_default_printer_on_a_multi_printer_roster(two_printers, monkeypatch):
    seen = []
    monkeypatch.setattr(family_control, "adapter_for", lambda pid=None: None)
    monkeypatch.setattr(pab, "handle_click", lambda action, alert, **kw: seen.append(alert) or "Paused.")
    assert print_status._pause(Control(), None, "a.gcode") == "Paused."
    assert seen[0]["printer_id"] == GARAGE and seen[0]["command"] is True
    # and the alert router resolves it to the default printer, not a refusal
    assert pab._alert_printer(seen[0]) == (None, None)


def test_family_printers_pause_through_their_own_control(monkeypatch):
    fake = S(connection="octoprint")
    monkeypatch.setattr(family_control, "adapter_for", lambda pid=None: fake)
    monkeypatch.setattr(family_control, "connection_of", lambda adapter: "octoprint")
    control = Control()
    assert print_status._pause(control, None, "a.gcode") == "Paused."
    assert control.paused == ["a.gcode"]


def test_family_control_pause_sends_once_and_reads_back():
    sent = []
    reads = iter([job("printing"), job("paused")])
    adapter = S(control=lambda action: sent.append(action))
    control = family_control.FamilyControl(adapter, status_fn=lambda: next(reads), sleep=lambda s: None)
    ok, text = control.pause("a.gcode", wait_s=5, poll_s=0)
    assert ok and sent == ["pause"] and text.startswith("Paused")


def test_family_control_pause_refuses_a_paused_print():
    adapter = S(control=lambda action: pytest.fail("nothing may be sent"))
    control = family_control.FamilyControl(adapter, status_fn=lambda: job("paused"))
    assert control.pause("a.gcode") == (False, "Already paused. Nothing sent.")


def test_a_typed_pause_that_does_not_take_never_reposts_a_camera_alert(monkeypatch):
    from print_handy import printer_control
    from print_handy.runtime import print_control as pc
    reposted = []
    monkeypatch.setattr(pab, "_alert_printer", lambda alert: (None, None))
    monkeypatch.setattr(pab, "_repost_alert", lambda *a, **k: reposted.append(a) or "")
    monkeypatch.setattr(printer_control, "for_printer",
                        lambda pid=None: S(is_bambu_lan=lambda: False, send_pause=lambda: None))
    monkeypatch.setattr(pc, "status", lambda **kw: {"klippy": "ready", **job("printing")})
    monkeypatch.setattr(pab, "_poll_for_paused", lambda *a, **k: job("printing"))
    text = pab.handle_click("pause", {"kind": "visual", "filename": "a.gcode", "command": True})
    assert not reposted
    assert "posted above" not in text and "Say /print-pause to try again." in text
    # an alert button press keeps its replacement alert
    text = pab.handle_click("pause", {"kind": "visual", "filename": "a.gcode"})
    assert reposted and "posted above" in text


# ------------------------------------------------------------ shared-channel target

def _stub_pause(monkeypatch, statuses, paused):
    def fake_status(printer_id=None, camera=True):
        value = statuses[printer_id or GARAGE]
        if isinstance(value, Exception):
            raise value
        return dict(value)
    monkeypatch.setattr(print_stop, "status", fake_status)
    monkeypatch.setattr(print_pause, "_pause",
                        lambda control, pid, filename: paused.append(pid) or "Paused.")


def test_an_unreadable_printer_is_asked_about_never_counted_as_idle(two_printers, monkeypatch):
    paused = []
    _stub_pause(monkeypatch, {GARAGE: TimeoutError("no answer"), WORKSHOP: job()}, paused)
    adapter = Adapter()  # nobody answers the picker
    gateway = Gateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})
    out = run(print_pause.print_pause_dispatch(event(), gateway))
    assert out["reason"] == "shared channel: no pause target decided"
    assert not paused, "the printing printer was not paused behind the unreadable one's back"
    question, choices = adapter.questions[0]
    assert any("status unknown" in c for c in choices) and any("printing" in c for c in choices)


def test_only_unreadable_printers_never_claim_nothing_is_printing(two_printers, monkeypatch):
    paused = []
    _stub_pause(monkeypatch, {GARAGE: TimeoutError("x"), WORKSHOP: job("standby")}, paused)
    adapter = Adapter()
    run(print_pause.print_pause_dispatch(event(), Gateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})))
    assert not any("no print running" in text for text in adapter.sent)


def test_one_printing_printer_with_every_read_ok_is_paused_without_asking(two_printers, monkeypatch):
    paused = []
    _stub_pause(monkeypatch, {GARAGE: job("standby"), WORKSHOP: job()}, paused)
    adapter = Adapter()
    out = run(print_pause.print_pause_dispatch(event(), Gateway(adapter, shared={HOME: [GARAGE, WORKSHOP]})))
    assert out["reason"] == "deterministic print pause handled"
    assert paused == [WORKSHOP] and not adapter.questions


def test_the_pause_lock_is_per_printer_and_not_held_by_an_open_picker(two_printers, monkeypatch):
    paused = []
    _stub_pause(monkeypatch, {GARAGE: job(), WORKSHOP: job()}, paused)
    common._seen[("print-pause", GARAGE)] = 0.0  # a pause for the garage is in flight
    try:
        adapter = Adapter()
        gateway = Gateway(adapter, routed={HOME: WORKSHOP})
        out = run(print_pause.print_pause_dispatch(event(), gateway))
        assert out["reason"] == "deterministic print pause handled" and paused == [WORKSHOP]
        assert ("print-pause", WORKSHOP) not in common._seen
    finally:
        common._seen.pop(("print-pause", GARAGE), None)


# ------------------------------------------------------- surfaces that never run it

def test_a_bare_mention_word_never_runs_an_at_once_command():
    assert mention.command_text("pause") == "pause"
    assert mention.command_text("pause the print") == "pause the print"
    assert mention.command_text("stop") == "/print-stop"  # confirmed commands still map


def test_the_help_menu_never_offers_an_at_once_command():
    names = [c.name for c in commands.help_menu_commands()]
    assert "print-pause" not in names
    assert commands.at_once_names() == frozenset({"/print-pause"}) == runtime._RUNS_AT_ONCE


def test_pause_status_reads_skip_the_bambu_camera(monkeypatch):
    grabbed = []
    lan = S(lan_configured=lambda: True, client=lambda: S(snapshot=lambda: {}),
            normalize_status=lambda snap: job())
    monkeypatch.setattr(print_stop, "_lan_adapter", lambda *a: lan)
    from print_handy.runtime import bambu_watch
    monkeypatch.setattr(bambu_watch, "status_camera", lambda adapter: grabbed.append(1) or (None, ""))
    print_stop.status(camera=False)
    assert not grabbed
    print_stop.status()
    assert grabbed == [1]
