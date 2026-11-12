"""Offline tests for live macro discovery and the /print-macros handler (issue #291).

No printer or network: discovery and runs run against recorded Moonraker
response shapes through a fake transport, and the dispatch tests use a fake
adapter that records sends, in the style of the /print-lookup tests in
tests/test_knowledge.py. Stage 1 tests prove the listing's only request is
one read-only GET; stage 2 tests prove a run makes exactly one POST, with
the exact confirmed script line, and only after both idle checks pass and
the operator confirms.
"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import knowledge, macros, printers
from print_handy.runtime import spool_sync

ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins/print-feedback/__init__.py"
spec = importlib.util.spec_from_file_location("print_feedback_macros", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

# A recorded-shape Moonraker answer: one "gcode_macro <NAME>" object per macro
# in the printer's config, mixed in with the ordinary Klipper objects.
OBJECTS = {
    "result": {
        "objects": [
            "toolhead", "extruder", "heater_bed", "fan", "virtual_sdcard",
            "gcode_macro PRINT_START", "gcode_macro PRINT_END",
            "gcode_macro START_PRINT", "gcode_macro WIPE_NOZZLE_CUSTOM",
            "gcode_macro _PARK_HELPERS", "gcode_macro _PRINT_HANDY_VARS",
            "bed_mesh", "gcode_move",
        ]
    }
}


class FakeTransport:
    """The spool_sync.moonraker call shape, recording every call."""

    def __init__(self, payload=None, error=None):
        self.payload = OBJECTS if payload is None else payload
        self.error = error
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.error is not None:
            raise self.error
        return self.payload


# --------------------------------------------------------------------- parsing

def test_parse_reads_the_result_objects_shape():
    assert macros.parse_macro_names(OBJECTS) == [
        "PRINT_START", "PRINT_END", "START_PRINT", "WIPE_NOZZLE_CUSTOM",
        "_PARK_HELPERS", "_PRINT_HANDY_VARS"]


def test_parse_accepts_result_list_and_bare_list_shapes():
    shapes = ({"result": ["gcode_macro A_MACRO", "toolhead"]},
              ["gcode_macro A_MACRO", "toolhead"],
              {"result": {"objects": ["gcode_macro A_MACRO", "toolhead"]}})
    for payload in shapes:
        assert macros.parse_macro_names(payload) == ["A_MACRO"]


def test_parse_dedupes_and_skips_a_nameless_macro_object():
    payload = {"result": {"objects": ["gcode_macro", "gcode_macro DUP", "gcode_macro DUP"]}}
    assert macros.parse_macro_names(payload) == ["DUP"]


@pytest.mark.parametrize("payload", [
    None, "nope", 42, {}, {"result": None}, {"result": "PRINT_START"},
    {"result": {"objects": "PRINT_START"}}, {"result": {"status": {}}},
    {"result": {"objects": ["gcode_macro OK", 7]}},
])
def test_parse_rejects_malformed_responses(payload):
    with pytest.raises(macros.MacroError):
        macros.parse_macro_names(payload)


# --------------------------------------------------------------- classification

@pytest.fixture(scope="module")
def registry():
    return knowledge.load_registry()


def test_classify_splits_known_printer_defined_and_internal(registry):
    listing = macros.classify(
        ["PRINT_START", "START_PRINT", "WIPE_NOZZLE_CUSTOM", "_PARK_HELPERS"], registry=registry)
    known = {name: entry for name, entry in listing.known}
    assert set(known) == {"PRINT_START", "START_PRINT"}
    assert known["PRINT_START"].id == "klipper.print-start"
    assert known["START_PRINT"].id == "zmod.start-print"
    assert listing.printer_defined == ("WIPE_NOZZLE_CUSTOM",)
    assert listing.internal == ("_PARK_HELPERS",)


def test_classify_stage3_entries_are_known(registry):
    listing = macros.classify(
        ["COLDPULL", "PAUSE", "PRINT_HANDY", "SET_PAUSE_AT_LAYER", "SET_ACTIVE_SPOOL"],
        registry=registry)
    known = {name: entry.id for name, entry in listing.known}
    assert known == {
        "COLDPULL": "zmod.coldpull",
        "PAUSE": "klipper.pause",
        "PRINT_HANDY": "zmod.print-handy",
        "SET_PAUSE_AT_LAYER": "klipper.set-pause-at-layer",
        "SET_ACTIVE_SPOOL": "zmod.set-active-spool",
    }
    assert listing.printer_defined == ()
    assert listing.internal == ()


def test_classify_matches_case_insensitively(registry):
    listing = macros.classify(["print_start"], registry=registry)
    assert [name for name, _ in listing.known] == ["print_start"]


def test_classify_never_calls_commands_or_errors_known_macros(registry):
    # BED_MESH_CALIBRATE and SAVE_CONFIG are registry *commands*, not macros:
    # a printer defining macros with those names is printer-defined, not known.
    listing = macros.classify(["BED_MESH_CALIBRATE", "SAVE_CONFIG"], registry=registry)
    assert listing.known == ()
    assert listing.printer_defined == ("BED_MESH_CALIBRATE", "SAVE_CONFIG")


# ------------------------------------------- printer-scoped matching (issue #612)

# The operator-named Sovol Zero macro-pack macros that share names with
# registry entries for other printers (rpcyan586/svzero-profiles, pinned in
# issue #612), plus the two generic Klipper names.
SVZERO_SHARED_NAMES = ["START_PRINT", "END_PRINT", "LOAD_FILAMENT", "UNLOAD_FILAMENT",
                       "CANCEL_PRINT", "PRINT_START"]


def test_classify_scoped_never_borrows_another_printers_entry(registry):
    listing = macros.classify(SVZERO_SHARED_NAMES, registry=registry, printer="klipper.generic")
    known = {name: entry.id for name, entry in listing.known}
    assert known == {"PRINT_START": "klipper.print-start",
                     "CANCEL_PRINT": "klipper.cancel-print"}
    assert set(listing.printer_defined) == {"START_PRINT", "END_PRINT",
                                            "LOAD_FILAMENT", "UNLOAD_FILAMENT"}


def test_classify_scoped_to_the_ad5x_keeps_its_zmod_entries(registry):
    listing = macros.classify(SVZERO_SHARED_NAMES, registry=registry, printer="flashforge.ad5x")
    known = {name: entry.id for name, entry in listing.known}
    assert known["START_PRINT"] == "zmod.start-print"
    assert known["END_PRINT"] == "zmod.end-print"
    assert known["LOAD_FILAMENT"] == "zmod.load-filament"
    # The generic Klipper PRINT_START entry is not scoped to the AD5X either:
    # scoping cuts both ways, so it is printer-defined there, not borrowed.
    assert "PRINT_START" in listing.printer_defined


def test_classify_unscoped_keeps_the_name_only_behaviour(registry):
    # Callers that cannot name a printer keep the pre-#612 behaviour.
    listing = macros.classify(["START_PRINT"], registry=registry)
    assert listing.known[0][1].id == "zmod.start-print"


# --------------------------------- macro coverage for every Moonraker family

def _applicable_macro_ids(registry, model_key):
    return {e.id for e in registry.firmware_entries
            if e.kind == "macro" and ("*" in e.printers or model_key in e.printers)}


def test_every_moonraker_model_has_macro_coverage_except_the_inline_verdicts(registry):
    # The Web UI printer picker is the registry itself, so this is the
    # coverage of every model a user can pick: every Moonraker model has at
    # least one scoped macro entry, except the two families whose profiles
    # run a fully inline start sequence and which have no verified vendor
    # macro set - the Elegoo Neptune and Anycubic Kobra 2 families, the
    # recorded NO-ADD verdicts in docs/MACROS.md.
    for key, model in printers.MODELS.items():
        if model.vendor.connection != "moonraker":
            continue
        ids = _applicable_macro_ids(registry, key)
        if key.startswith(("elegoo.", "anycubic.")):
            assert ids == set(), key
        else:
            assert ids, key


def test_snapmaker_u1_macro_scope_is_exactly_the_print_start_pair(registry):
    # The exact assertion for the klipper.print-start / print-end scope
    # widening (the two entries sit in the widened set in
    # tests/test_knowledge.py, where their printers/notes fields are set
    # aside): the U1 gains exactly that pair and nothing else.
    assert _applicable_macro_ids(registry, "snapmaker.u1") == {
        "klipper.print-start", "klipper.print-end"}
    for entry in registry.firmware_entries:
        if entry.id in ("klipper.print-start", "klipper.print-end"):
            assert "snapmaker.u1" in entry.printers


def test_classify_scoped_describes_each_familys_own_start_macro(registry):
    cases = [
        ("creality.k1", "START_PRINT", "klipper.creality-start-print"),
        ("creality.ender3v3", "END_PRINT", "klipper.creality-end-print"),
        ("qidi.xplus4", "PRINT_START", "klipper.qidi-print-start"),
        ("sovol.sv08", "START_PRINT", "klipper.sovol-start-print"),
        ("sovol.sv08", "CLEAN_NOZZLE", "klipper.sovol-sv08-clean-nozzle"),
        ("sovol.zero", "END_PRINT", "klipper.sovol-zero-end-print"),
        ("sovol.sv07", "PRINT_START", "klipper.print-start"),
        ("flashforge.ad5m", "COLDPULL", "zmod.coldpull"),
        ("flashforge.ad5m_pro", "START_PRINT", "zmod.start-print"),
        ("flashforge.creator5", "SET_PRINT_STATS_INFO", "klipper.set-print-stats-info"),
        ("flashforge.creator5_pro", "END_PRINT", "zmod.end-print"),
        ("snapmaker.u1", "PRINT_START", "klipper.print-start"),
        ("snapmaker.u1", "PRINT_END", "klipper.print-end"),
    ]
    for model_key, name, entry_id in cases:
        listing = macros.classify([name], registry=registry, printer=model_key)
        known = {n: e.id for n, e in listing.known}
        assert known.get(name) == entry_id, (model_key, name, known)


def test_classify_scoped_neptune_borrows_nothing(registry):
    # The Neptune verdict: its config may define these names, but no other
    # family's entry describes them (the #612 rule, applied to the gap).
    listing = macros.classify(["START_PRINT", "PRINT_START"], registry=registry,
                              printer="elegoo.neptune4")
    assert listing.known == ()
    assert set(listing.printer_defined) == {"START_PRINT", "PRINT_START"}


def test_classify_scoped_kobra2_borrows_nothing(registry):
    # The Kobra 2 verdict is the same shape: Print Handy's profiles for the
    # family are fully inline and its guard treats START_PRINT as foreign,
    # so no other family's START_PRINT entry may describe a Kobra 2 listing.
    listing = macros.classify(["START_PRINT", "PRINT_START"], registry=registry,
                              printer="anycubic.kobra2pro")
    assert listing.known == ()
    assert set(listing.printer_defined) == {"START_PRINT", "PRINT_START"}


# ------------------------------------------------------------------- discovery

def test_discover_makes_exactly_one_read_only_get():
    transport = FakeTransport()
    listing = macros.discover(transport)
    assert transport.calls == [("GET", "/printer/objects/list", None)]
    assert {name for name, _ in listing.known} == {"PRINT_START", "PRINT_END", "START_PRINT"}
    assert listing.printer_defined == ("WIPE_NOZZLE_CUSTOM",)
    assert listing.internal == ("_PARK_HELPERS", "_PRINT_HANDY_VARS")


def test_discover_wraps_transport_failures():
    with pytest.raises(macros.MacroError):
        macros.discover(FakeTransport(error=OSError("unreachable")))


def test_discover_rejects_a_malformed_response():
    with pytest.raises(macros.MacroError):
        macros.discover(FakeTransport(payload={"result": {"status": {}}}))


# ------------------------------------------------------------------ formatting

def test_listing_text_shows_registry_safety_and_no_invented_descriptions(registry):
    listing = macros.classify(["PRINT_START", "WIPE_NOZZLE_CUSTOM", "_PARK_HELPERS"], registry=registry)
    text = macros.listing_text(listing)
    entry = next(e for e in registry.firmware_entries if e.id == "klipper.print-start")
    assert "**PRINT_START**" in text and entry.description in text
    assert f"Safety: {entry.safety}" in text
    assert "WIPE_NOZZLE_CUSTOM" in text
    assert "no description for these" in text
    assert "_PARK_HELPERS" in text and "not operator macros" in text
    assert text.endswith(macros.READ_ONLY_NOTE)


def test_listing_text_for_a_printer_with_no_macros():
    text = macros.listing_text(macros.MacroListing())
    assert "no macros" in text and text.endswith(macros.READ_ONLY_NOTE)


def test_not_moonraker_text_is_plain_and_names_the_connection():
    text = macros.not_moonraker_text("bambulab_lan")
    assert "no Moonraker macro namespace" in text and "bambulab_lan" in text
    assert text.endswith("Nothing was sent to the printer.")


# --------------------------------------------------------------------- handler

class FakeAdapter:
    """Records everything the dispatch tries to show the user; has no printer API at all."""

    def __init__(self):
        self.sent = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)


def _event(text):
    source = S(platform=S(value="discord"), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id="u", chat_id="c", thread_id=None)
    return S(source=source, text=text, internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "session-key")


def _configured(connection):
    return mock.patch.object(printers, "current", return_value=S(connection=connection))


def test_macros_dispatch_lists_the_discovered_macros():
    adapter = FakeAdapter()
    transport = FakeTransport()
    with _configured("moonraker"), mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.macros_dispatch(_event("/print-macros"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print macros handled"}
    assert transport.calls == [("GET", "/printer/objects/list", None)]
    message = adapter.sent[-1]
    assert "**PRINT_START**" in message and "WIPE_NOZZLE_CUSTOM" in message
    assert "_PARK_HELPERS" in message and message.endswith(macros.READ_ONLY_NOTE)


def test_macros_dispatch_explains_non_moonraker_printers_without_calling_out():
    adapter = FakeAdapter()
    transport = FakeTransport()
    with _configured("bambulab_lan"), mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.macros_dispatch(_event("/print-macros"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print macros not applicable to this printer"}
    assert transport.calls == []
    assert "no Moonraker macro namespace" in adapter.sent[-1]


@pytest.mark.parametrize("connection", ["prusalink", "elegoo_sdcp", "flashforge_stock", "octoprint"])
def test_macros_dispatch_gates_every_non_moonraker_connection(connection):
    adapter = FakeAdapter()
    transport = FakeTransport()
    with _configured(connection), mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.macros_dispatch(_event("/print-macros"), _gateway(adapter)))
    assert out["reason"] == "print macros not applicable to this printer"
    assert transport.calls == []


def test_macros_dispatch_reports_an_unreachable_moonraker_and_sends_nothing():
    adapter = FakeAdapter()
    transport = FakeTransport(error=OSError("unreachable"))
    with _configured("moonraker"), mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.macros_dispatch(_event("/print-macros"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print macros handled"}
    assert adapter.sent[-1] == macros.COULD_NOT_READ_TEXT
    # The one attempted call was the read-only GET; no macro was ever posted.
    assert transport.calls == [("GET", "/printer/objects/list", None)]


def test_macros_dispatch_reports_a_malformed_response_the_same_way():
    adapter = FakeAdapter()
    transport = FakeTransport(payload={"error": {"message": "nope"}})
    with _configured("moonraker"), mock.patch.object(spool_sync, "moonraker", transport):
        asyncio.run(plugin.macros_dispatch(_event("/print-macros"), _gateway(adapter)))
    assert adapter.sent[-1] == macros.COULD_NOT_READ_TEXT


def test_macros_dispatch_ignores_other_text():
    assert asyncio.run(plugin.macros_dispatch(_event("/print-macrosx"), _gateway(FakeAdapter()))) is None
    assert asyncio.run(plugin.macros_dispatch(_event("print-macros"), _gateway(FakeAdapter()))) is None


# ------------------------------------------------------------------ stage 2: run

STATE_SHAPE = {"result": {"status": {"print_stats": {"state": "standby"}}}}


class RunTransport(FakeTransport):
    """Fake transport with a print state (repeatable sequence) and a script POST."""

    def __init__(self, states=("standby",), post_error=None, state_payload=None, **kwargs):
        super().__init__(**kwargs)
        self.states = list(states)
        self.post_error = post_error
        self.state_payload = state_payload
        self.posts = []

    def __call__(self, method, path, body=None):
        if path == macros.PRINT_STATE_PATH:
            self.calls.append((method, path, body))
            if self.state_payload is not None:
                return self.state_payload
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"result": {"status": {"print_stats": {"state": state}}}}
        if method == "POST":
            self.calls.append((method, path, body))
            self.posts.append(body)
            if self.post_error is not None:
                raise self.post_error
            return {"result": "ok"}
        return super().__call__(method, path, body)


def test_print_state_reads_the_print_stats_shape():
    assert macros.print_state(RunTransport()) == "standby"
    assert macros.print_state(RunTransport(states=("Printing",))) == "printing"


def test_print_state_rejects_an_unreadable_response():
    with pytest.raises(macros.MacroError):
        macros.print_state(RunTransport(state_payload={"result": {"status": {}}}))
    with pytest.raises(macros.MacroError):
        macros.print_state(FakeTransport(error=OSError("unreachable")))


def test_prepare_run_canonicalises_the_name_and_builds_the_exact_script():
    transport = RunTransport()
    prepared = macros.prepare_run(transport, "print_start")
    assert prepared.name == "PRINT_START"
    assert prepared.script == "PRINT_START"
    assert prepared.entry is not None and prepared.entry.id == "klipper.print-start"
    assert transport.calls == [("GET", "/printer/objects/list", None),
                               ("GET", macros.PRINT_STATE_PATH, None)]
    assert transport.posts == []


def test_prepare_run_scoped_carries_no_borrowed_entry(registry):
    # On klipper.generic a discovered START_PRINT is the printer's own macro
    # (e.g. Sovol's), never ZMod's: the prompt must say it is not described.
    prepared = macros.prepare_run(RunTransport(), "START_PRINT",
                                  registry=registry, printer="klipper.generic")
    assert prepared.entry is None
    assert macros.NOT_DESCRIBED_WORDING in macros.run_prompt_text(prepared)


def test_prepare_run_scoped_to_the_ad5x_keeps_the_zmod_entry(registry):
    prepared = macros.prepare_run(RunTransport(), "START_PRINT",
                                  registry=registry, printer="flashforge.ad5x")
    assert prepared.entry is not None and prepared.entry.id == "zmod.start-print"


def test_prepare_run_appends_validated_arguments_in_order():
    prepared = macros.prepare_run(RunTransport(), "START_PRINT",
                                  ["BED_TEMP=60", "EXTRUDER_TEMP=210.5", "MESH=default"])
    assert prepared.script == "START_PRINT BED_TEMP=60 EXTRUDER_TEMP=210.5 MESH=default"


@pytest.mark.parametrize("token", ["BED_TEMP", "BED_TEMP=", "=60", "BED TEMP=60",
                                   "BED_TEMP=60;M104", 'BED_TEMP="60"', "BED_TEMP=6 0"])
def test_prepare_run_rejects_a_malformed_argument(token):
    with pytest.raises(macros.MacroRefusal):
        macros.prepare_run(RunTransport(), "PRINT_START", [token])


def test_prepare_run_rejects_a_duplicate_argument():
    with pytest.raises(macros.MacroRefusal):
        macros.prepare_run(RunTransport(), "PRINT_START", ["SPEED=1", "speed=2"])


def test_prepare_run_refuses_an_unknown_name_and_sends_nothing():
    transport = RunTransport()
    with pytest.raises(macros.MacroRefusal) as refused:
        macros.prepare_run(transport, "NOT_A_MACRO")
    assert "does not define a macro named" in str(refused.value)
    assert transport.posts == []


def test_prepare_run_refuses_an_internal_macro():
    transport = RunTransport()
    with pytest.raises(macros.MacroRefusal) as refused:
        macros.prepare_run(transport, "_park_helpers")
    assert "internal helper macro" in str(refused.value)
    assert transport.posts == []


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_prepare_run_refuses_while_the_printer_is_busy(state):
    transport = RunTransport(states=(state,))
    with pytest.raises(macros.MacroRefusal) as refused:
        macros.prepare_run(transport, "PRINT_START")
    assert state in str(refused.value)
    assert transport.posts == []


def test_prepare_run_refuses_an_unreadable_state():
    with pytest.raises(macros.MacroError):
        macros.prepare_run(RunTransport(state_payload={"result": {}}), "PRINT_START")


def test_prepare_run_reports_an_unreachable_printer_as_could_not_read():
    with pytest.raises(macros.MacroError) as failed:
        macros.prepare_run(RunTransport(error=OSError("unreachable")), "PRINT_START")
    assert str(failed.value) == macros.RUN_COULD_NOT_READ_TEXT


def test_run_prompt_for_a_known_macro_carries_the_registry_safety(registry):
    prepared = macros.prepare_run(RunTransport(), "PRINT_START", registry=registry)
    prompt = macros.run_prompt_text(prepared)
    entry = next(e for e in registry.firmware_entries if e.id == "klipper.print-start")
    assert "`PRINT_START`" in prompt
    assert f"Safety: {entry.safety}" in prompt


def test_run_prompt_for_a_printer_defined_macro_says_not_described():
    prepared = macros.prepare_run(RunTransport(), "WIPE_NOZZLE_CUSTOM")
    assert prepared.entry is None
    prompt = macros.run_prompt_text(prepared)
    assert macros.NOT_DESCRIBED_WORDING in prompt
    assert "Safety:" not in prompt


def test_send_run_rechecks_state_then_posts_the_exact_line_once():
    transport = RunTransport()
    prepared = macros.prepare_run(transport, "WIPE_NOZZLE_CUSTOM", ["SPEED=2"])
    message = macros.send_run(transport, prepared)
    assert transport.calls == [("GET", "/printer/objects/list", None),
                               ("GET", macros.PRINT_STATE_PATH, None),
                               ("GET", macros.PRINT_STATE_PATH, None),
                               ("POST", "/printer/gcode/script", {"script": "WIPE_NOZZLE_CUSTOM SPEED=2"})]
    assert message.startswith("Sent `WIPE_NOZZLE_CUSTOM SPEED=2`")


def test_send_run_refuses_when_the_recheck_finds_the_printer_busy():
    transport = RunTransport(states=("standby", "printing"))
    prepared = macros.prepare_run(transport, "PRINT_START")
    with pytest.raises(macros.MacroRefusal):
        macros.send_run(transport, prepared)
    assert transport.posts == []


def test_send_run_refuses_when_the_recheck_is_unreadable():
    class FlakyState(RunTransport):
        def __init__(self):
            super().__init__()
            self.state_calls = 0

        def __call__(self, method, path, body=None):
            if path == macros.PRINT_STATE_PATH:
                self.state_calls += 1
                if self.state_calls > 1:
                    self.calls.append((method, path, body))
                    return {"result": {"status": {}}}
            return super().__call__(method, path, body)

    transport = FlakyState()
    prepared = macros.prepare_run(transport, "PRINT_START")
    with pytest.raises(macros.MacroRefusal):
        macros.send_run(transport, prepared)
    assert transport.posts == []


def test_send_run_reports_a_failed_post_as_uncertain_never_as_ran():
    transport = RunTransport(post_error=OSError("connection reset"))
    prepared = macros.prepare_run(transport, "PRINT_START")
    with pytest.raises(macros.MacroError) as failed:
        macros.send_run(transport, prepared)
    assert "not known whether the printer received" in str(failed.value)


# ------------------------------------------------------------- handler: run flow

class ConfirmRecorder:
    """Stands in for the plugin's _confirm: records the prompt, answers as queued."""

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    async def __call__(self, adapter, source, metadata, prefix, question, choices, timeout, live_key=None):
        self.prompts.append((question, choices))
        return self.answer


def _run_dispatch(text, transport, confirm, connection="moonraker"):
    adapter = FakeAdapter()
    with _configured(connection), mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(plugin, "_confirm", confirm):
        out = asyncio.run(plugin.macros_dispatch(_event(text), _gateway(adapter)))
    return out, adapter


def test_macros_run_happy_path_posts_the_confirmed_line_once():
    transport = RunTransport()
    confirm = ConfirmRecorder(macros.RUN_LABEL)
    out, adapter = _run_dispatch("/print-macros run wipe_nozzle_custom SPEED=2", transport, confirm)
    assert out == {"action": "skip", "reason": "print macros run handled"}
    assert transport.posts == [{"script": "WIPE_NOZZLE_CUSTOM SPEED=2"}]
    question, choices = confirm.prompts[0]
    assert "`WIPE_NOZZLE_CUSTOM SPEED=2`" in question
    assert macros.NOT_DESCRIBED_WORDING in question
    assert choices == [macros.RUN_LABEL, macros.CANCEL_LABEL]
    assert adapter.sent[-1].startswith("Sent `WIPE_NOZZLE_CUSTOM SPEED=2`")


def test_macros_run_prompt_for_a_known_macro_includes_its_safety_note(registry):
    transport = RunTransport()
    confirm = ConfirmRecorder(macros.CANCEL_LABEL)
    _run_dispatch("/print-macros run PRINT_START", transport, confirm)
    entry = next(e for e in registry.firmware_entries if e.id == "klipper.print-start")
    assert f"Safety: {entry.safety}" in confirm.prompts[0][0]


@pytest.mark.parametrize("answer", [macros.CANCEL_LABEL, None])
def test_macros_run_declined_or_timed_out_sends_nothing(answer):
    transport = RunTransport()
    confirm = ConfirmRecorder(answer)
    _out, adapter = _run_dispatch("/print-macros run PRINT_START", transport, confirm)
    assert transport.posts == []
    assert adapter.sent[-1] == "Macro not run. Nothing sent."


def test_macros_run_unknown_name_never_prompts_and_sends_nothing():
    transport = RunTransport()
    confirm = ConfirmRecorder(macros.RUN_LABEL)
    _out, adapter = _run_dispatch("/print-macros run NOPE_MACRO", transport, confirm)
    assert confirm.prompts == [] and transport.posts == []
    assert "does not define a macro named" in adapter.sent[-1]


def test_macros_run_internal_name_never_prompts_and_sends_nothing():
    transport = RunTransport()
    confirm = ConfirmRecorder(macros.RUN_LABEL)
    _out, adapter = _run_dispatch("/print-macros run _PARK_HELPERS", transport, confirm)
    assert confirm.prompts == [] and transport.posts == []
    assert "internal helper macro" in adapter.sent[-1]


def test_macros_run_busy_at_the_first_check_never_prompts():
    transport = RunTransport(states=("paused",))
    confirm = ConfirmRecorder(macros.RUN_LABEL)
    _out, adapter = _run_dispatch("/print-macros run PRINT_START", transport, confirm)
    assert confirm.prompts == [] and transport.posts == []
    assert "paused" in adapter.sent[-1]


def test_macros_run_busy_at_the_second_check_sends_nothing():
    transport = RunTransport(states=("standby", "printing"))
    confirm = ConfirmRecorder(macros.RUN_LABEL)
    _out, adapter = _run_dispatch("/print-macros run PRINT_START", transport, confirm)
    assert len(confirm.prompts) == 1 and transport.posts == []
    assert "printing" in adapter.sent[-1]


def test_macros_run_on_a_non_moonraker_printer_sends_nothing():
    transport = RunTransport()
    confirm = ConfirmRecorder(macros.RUN_LABEL)
    out, adapter = _run_dispatch("/print-macros run PRINT_START", transport, confirm,
                                 connection="bambulab_lan")
    assert out["reason"] == "print macros not applicable to this printer"
    assert transport.calls == [] and confirm.prompts == []
    assert "no Moonraker macro namespace" in adapter.sent[-1]


@pytest.mark.parametrize("text", ["/print-macros run", "/print-macros frobnicate"])
def test_macros_run_usage_errors_send_nothing(text):
    transport = RunTransport()
    confirm = ConfirmRecorder(macros.RUN_LABEL)
    out, adapter = _run_dispatch(text, transport, confirm)
    assert out == {"action": "skip", "reason": "print macros usage shown"}
    assert transport.calls == [] and confirm.prompts == []
    assert "Nothing was sent to the printer." in adapter.sent[-1]


def test_llm_may_name_the_listing_but_never_a_run():
    # Stage 2 decision, matching the allow-list's treatment of the mutating
    # /print-again verbs: a state-changing, argument-bearing form is not
    # LLM-nameable. The operator types and confirms a run personally.
    from print_handy.bot import llm
    assert llm.allowed_command("/print-macros") == "/print-macros"
    assert llm.allowed_command("/print-macros run PRINT_START") is None


def test_bot_command_text_for_a_run():
    from print_handy.bot import commands as bot_commands
    assert bot_commands.as_text("print-macros", {"args": "run PRINT_START"}) == "/print-macros run PRINT_START"
    assert bot_commands.as_text("print-macros", {}) == "/print-macros"


# ------------------------------------------------------------ user hooks (#562)

def test_classify_lists_zmods_user_prepare_print_as_a_user_hook(registry):
    listing = macros.classify(["_USER_PREPARE_PRINT", "_PARK_HELPERS"], registry=registry)
    assert listing.user_hooks == ("_USER_PREPARE_PRINT",)
    assert listing.internal == ("_PARK_HELPERS",)
    text = macros.listing_text(listing)
    assert "Firmware hooks you can fill in" in text
    assert text.index("_USER_PREPARE_PRINT") < text.index("Internal helper macros")


def test_prepare_run_refuses_a_user_hook_with_its_own_reason():
    payload = {"result": {"objects": ["gcode_macro PRINT_START", "gcode_macro _USER_PREPARE_PRINT"]}}
    transport = RunTransport(payload=payload)
    with pytest.raises(macros.MacroRefusal) as refused:
        macros.prepare_run(transport, "_user_prepare_print")
    assert "firmware hook" in str(refused.value)
    assert transport.posts == []
