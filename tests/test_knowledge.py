"""Offline tests for the root registries, lookup engine and troubleshooting flows (issue #169).

No printer, network or Discord is involved: the lookup and flow code is read-only
by construction, and the dispatch tests use a fake adapter that records sends.
"""
import ast
import asyncio
import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import knowledge, printers
from print_handy.bot import commands as bot_commands, llm
from print_handy.core import prompts
from print_handy.knowledge import FlowError, RegistryError

ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins/print-feedback/__init__.py"
spec = importlib.util.spec_from_file_location("print_feedback_knowledge", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


@pytest.fixture(scope="module")
def registry():
    return knowledge.load_registry()


# --------------------------------------------------------------------- registries

def test_registries_load_and_validate(registry):
    assert len(registry.flows) == 5
    assert len(registry.symptoms) >= 5
    assert {e.firmware for e in registry.firmware_entries} == {"zmod", "klipper", "marlin", "prusa"}


def test_printer_registry_matches_code_profiles(registry):
    """The root printer registry must not drift from print_handy/printers."""
    assert {p.key for p in registry.printers} == set(printers.MODELS)
    for entry in registry.printers:
        model = printers.MODELS[entry.key]
        defaults = model.defaults()["printer"]
        assert entry.label == model.label
        assert entry.vendor == model.vendor.key
        assert entry.firmware == defaults["firmware"]
        assert entry.connection == model.vendor.connection
        assert entry.bed_mm == tuple(float(v) for v in defaults["bed_mm"])
        assert entry.max_height_mm == float(defaults["max_print_height_mm"])
        if entry.tools:
            # Toolhead machines record tools, not IFS slots: the model's tool_count
            # is the authority and its filament_slots stays 1 (see #301 stage 2).
            assert entry.tools == int(defaults["tool_count"])
            assert entry.filament_slots == 0
        else:
            assert entry.filament_slots == int(defaults["filament_slots"])
        # The color architecture (issue #656): registry and MODEL agree, and the
        # recorded counts match the class.
        assert entry.color_system == model.color_system
        assert (entry.filament_slots > 1) == (entry.color_system == "multi_slot")
        assert (entry.tools > 1) == (entry.color_system == "multi_tool")
        assert (ROOT / entry.profile).is_file()


def test_broken_registry_is_rejected(tmp_path):
    for name in ("knowledge", "firmware", "printers"):
        shutil.copytree(ROOT / name, tmp_path / name)
    flows = tmp_path / "knowledge" / "troubleshooting.toml"
    flows.write_text(flows.read_text().replace('next = "look"', 'next = "no-such-node"', 1))
    with pytest.raises(RegistryError, match="unknown node"):
        knowledge.load_registry(tmp_path)


def test_missing_registry_is_rejected(tmp_path):
    with pytest.raises(RegistryError, match="missing"):
        knowledge.load_registry(tmp_path)


# ------------------------------------------------- firmware split (issue #644)

def _copy_registry_tree(tmp_path):
    for name in ("knowledge", "firmware", "printers"):
        shutil.copytree(ROOT / name, tmp_path / name)
    return tmp_path


def _entry_block(path, entry_id):
    """The [[entry]] block for entry_id in a commands file, as raw text."""
    chunks = path.read_text().split("[[entry]]")
    for chunk in chunks[1:]:
        if f'id = "{entry_id}"' in chunk:
            return "[[entry]]" + chunk
    raise AssertionError(f"{entry_id} not in {path}")


# The entries deliberately scope-widened after the split, compared below
# with only the printers and notes fields set aside; each widening is
# asserted exactly in its own tests. Issue #647 widened the seven Marlin
# entries from the generic preset to every registered Marlin model, each
# with its source note. The Snapmaker U1 change widened the two generic
# Klipper start/end entries to snapmaker.u1 (its templates emit that
# pair), asserted exactly in tests/test_macros.py. Everything else in
# the pre-split set is still pinned field for field below.
SPLIT_FIXTURE_WIDENED_BY_647 = {
    "marlin.g28", "marlin.g29", "marlin.m420", "marlin.m851", "marlin.m600",
    "marlin.m500", "marlin.heating-failed",
    "klipper.print-start", "klipper.print-end",
}


def test_split_registry_matches_pre_split_entries(registry):
    """The per-firmware split is content-preserving: the merged registry holds
    exactly the pre-split commands.toml entries (fixture captured from the
    single file as it stood on main after #645 landed, so it pins the
    51-entry set the split actually replaced), field for field, and no
    shipped entry gained a pack value. Issue #647 later grew the registry on
    purpose: its added entries sit outside the fixture (pinned by the #647
    tests), and the seven Marlin entries it widened are compared here with
    only the scope and notes fields set aside — those two changes are
    asserted exactly in the #647 tests."""
    fixture = json.loads((ROOT / "tests" / "knowledge_pre_split_commands.json").read_text())
    expected = {row["id"]: row for row in fixture}
    assert len(expected) == 51
    actual = {e.id: {
        "id": e.id, "name": e.name, "aliases": list(e.aliases), "firmware": e.firmware,
        "kind": e.kind, "printers": list(e.printers), "command": e.command,
        "description": e.description, "safety": e.safety, "notes": e.notes,
    } for e in registry.firmware_entries}
    assert set(expected) <= set(actual)
    for entry_id, row in expected.items():
        if entry_id in SPLIT_FIXTURE_WIDENED_BY_647:
            got, want = dict(actual[entry_id]), dict(row)
            for field in ("printers", "notes"):
                got.pop(field)
                want.pop(field)
            assert got == want, entry_id
        else:
            assert actual[entry_id] == row, entry_id
    assert all(e.pack == "" for e in registry.firmware_entries)


def test_duplicate_entry_id_across_files_rejected(tmp_path):
    root = _copy_registry_tree(tmp_path)
    commands = root / "firmware" / "commands"
    block = _entry_block(commands / "klipper.toml", "klipper.print-start")
    marlin = commands / "marlin.toml"
    marlin.write_text(marlin.read_text() + "\n" + block)
    with pytest.raises(RegistryError, match="duplicated"):
        knowledge.load_registry(root)


def test_entry_in_wrong_firmware_file_rejected(tmp_path):
    root = _copy_registry_tree(tmp_path)
    commands = root / "firmware" / "commands"
    klipper = commands / "klipper.toml"
    block = _entry_block(klipper, "klipper.print-start")
    klipper.write_text(klipper.read_text().replace(block, ""))
    marlin = commands / "marlin.toml"
    marlin.write_text(marlin.read_text() + "\n" + block)
    with pytest.raises(RegistryError, match="disagrees with the file"):
        knowledge.load_registry(root)


FIXTURE_PACK_ENTRY = """
[[entry]]
id = "klipper.fixture-pack-probe"
name = "FIXTURE_PACK_PROBE"
aliases = ["fixture pack probe"]
firmware = "klipper"
kind = "macro"
printers = ["*"]
pack = "fixture-pack"
command = "FIXTURE_PACK_PROBE"
description = "Fixture-only entry proving the pack field round-trips (issue #644)."
safety = "Fixture only; it describes nothing real and is never sent anywhere."
"""


def test_pack_round_trips_through_loader_and_lookup(tmp_path):
    root = _copy_registry_tree(tmp_path)
    klipper = root / "firmware" / "commands" / "klipper.toml"
    klipper.write_text(klipper.read_text() + FIXTURE_PACK_ENTRY)
    reg = knowledge.load_registry(root)
    entry = next(e for e in reg.firmware_entries if e.id == "klipper.fixture-pack-probe")
    assert entry.pack == "fixture-pack"

    def firmware_ids(**kwargs):
        return [h.item.id for h in reg.lookup("fixture pack probe", **kwargs) if h.kind == "firmware"]

    assert "klipper.fixture-pack-probe" in firmware_ids()
    assert "klipper.fixture-pack-probe" in firmware_ids(pack="fixture-pack")
    assert "klipper.fixture-pack-probe" in firmware_ids(pack="Fixture Pack")
    assert "klipper.fixture-pack-probe" not in firmware_ids(pack="other-pack")
    # pack="" is the stock filter: pack-carrying entries are kept out of it.
    assert "klipper.fixture-pack-probe" not in firmware_ids(pack="")


def test_pack_must_be_a_slug(tmp_path):
    root = _copy_registry_tree(tmp_path)
    klipper = root / "firmware" / "commands" / "klipper.toml"
    klipper.write_text(klipper.read_text()
                       + FIXTURE_PACK_ENTRY.replace('pack = "fixture-pack"', 'pack = "Fixture Pack!"'))
    with pytest.raises(RegistryError, match="pack must be a lowercase slug"):
        knowledge.load_registry(root)

def _copy_registries(tmp_path):
    for name in ("knowledge", "firmware", "printers"):
        shutil.copytree(ROOT / name, tmp_path / name)
    return tmp_path / "printers" / "registry"


def test_printer_registry_duplicate_key_across_files_is_rejected(tmp_path):
    registry_dir = _copy_registries(tmp_path)
    sovol = registry_dir / "sovol.toml"
    block = sovol.read_text().split("\n[[printer]]", 2)[1]
    with open(registry_dir / "sovol_marlin.toml", "a") as handle:
        handle.write("\n[[printer]]" + block.replace('"sovol.', '"sovol_marlin.', 1)
                     .replace('vendor = "sovol"', 'vendor = "sovol_marlin"', 1))
    with pytest.raises(RegistryError, match="key duplicated"):
        knowledge.load_registry(tmp_path)


def test_printer_registry_entry_in_wrong_vendor_file_is_rejected(tmp_path):
    registry_dir = _copy_registries(tmp_path)
    klipper = registry_dir / "klipper.toml"
    block = klipper.read_text().split("\n[[printer]]", 1)[1]
    klipper.write_text(klipper.read_text().split("\n[[printer]]", 1)[0])
    with open(registry_dir / "sovol.toml", "a") as handle:
        handle.write("\n[[printer]]" + block)
    with pytest.raises(RegistryError, match="must both be 'sovol'"):
        knowledge.load_registry(tmp_path)


def test_printer_registry_empty_directory_is_rejected(tmp_path):
    registry_dir = _copy_registries(tmp_path)
    for path in registry_dir.glob("*.toml"):
        path.unlink()
    with pytest.raises(RegistryError, match="printer registry missing or empty"):
        knowledge.load_registry(tmp_path)


# ----------------------------------------------------------------------- lookups

def top(registry, query, **kwargs):
    results = registry.lookup(query, **kwargs)
    assert results, query
    return results[0]


def test_lookup_zmod_ifs_load(registry):
    hit = top(registry, "zmod ifs load")
    assert hit.kind == "firmware" and hit.item.id == "zmod.ifs-load"


def test_lookup_resolves_across_families(registry):
    assert top(registry, "probe calibrate", firmware="klipper").item.id == "klipper.probe-calibrate"
    assert top(registry, "M600").item.id == "marlin.m600"
    assert top(registry, "change zcolor").item.id == "zmod.change-zcolor"
    assert top(registry, "heating failed").item.id == "marlin.heating-failed"


STAGE3_IDS = {
    "zmod.coldpull", "zmod.load-filament", "zmod.unload-filament", "zmod.load-material",
    "zmod.clear-nozzle", "zmod.set-extruder-slot", "zmod.set-active-spool",
    "zmod.clear-active-spool", "zmod.save-zmod-data", "zmod.get-zmod-data",
    "zmod.nozzle-control", "zmod.pid-tune-extruder", "zmod.pid-tune-bed", "zmod.zshaper",
    "zmod.display-off", "zmod.display-on", "zmod.print-handy",
    "klipper.pause", "klipper.resume", "klipper.cancel-print",
    "klipper.set-pause-next-layer", "klipper.set-pause-at-layer", "klipper.set-print-stats-info",
}


def test_stage3_registry_growth_is_complete_and_provenanced(registry):
    """Stage 3 of #291: every added entry loads, is a macro, and names its source."""
    # 51 entries through #645; issue #647 added five more (the Prusa family
    # and the Marlin sensor error), pinned by the #647 tests below.
    assert len(registry.firmware_entries) == 56
    by_id = {e.id: e for e in registry.firmware_entries}
    assert STAGE3_IDS <= set(by_id)
    for entry_id in STAGE3_IDS:
        entry = by_id[entry_id]
        assert entry.kind == "macro", entry_id
        assert entry.notes, entry_id
        assert ("@" in entry.notes) or ("print-handy.cfg" in entry.notes), entry_id


ALL_PRINTERS_IDS = {
    "klipper.creality-start-print", "klipper.creality-end-print",
    "klipper.qidi-print-start", "klipper.sovol-start-print",
    "klipper.sovol-zero-end-print", "klipper.sovol-sv08-clean-nozzle",
}


def test_all_printers_registry_growth_is_complete_and_provenanced(registry):
    """Macro coverage for every Moonraker family: each added entry loads, is a
    macro, names its source, and is scoped to registered model keys only."""
    by_id = {e.id: e for e in registry.firmware_entries}
    assert ALL_PRINTERS_IDS <= set(by_id)
    for entry_id in ALL_PRINTERS_IDS:
        entry = by_id[entry_id]
        assert entry.kind == "macro", entry_id
        assert entry.notes and "Source" in entry.notes, entry_id
        assert entry.printers and "*" not in entry.printers, entry_id
        assert set(entry.printers) <= set(printers.MODELS), entry_id


# ------------------------------------------------------------- issue #647

NONMOONRAKER_IDS = {
    "prusa.m862-model-check", "prusa.m862-feature-check", "prusa.m863-tool-remap",
    "prusa.print-check-refusal", "marlin.mintemp-maxtemp",
}

# Non-Moonraker models whose decided state is a recorded verdict in
# docs/MACROS.md rather than scoped entries (issue #647): Bambu's HMS codes
# live in the Bambu adapter's generated lookup, and SDCP and the stock
# Flashforge API are integration protocols with no published operator
# command or error vocabulary to describe.
VERDICT_ONLY_MODELS = (
    {k for k in printers.MODELS if k.startswith("bambulab.")}
    | {"elegoo_sdcp.centauricarbon", "flashforge_stock.ad5x"}
)


def test_nonmoonraker_registry_growth_is_complete_and_provenanced(registry):
    """Issue #647: every entry the non-Moonraker pass added loads, names its
    source, and is scoped to registered model keys only."""
    by_id = {e.id: e for e in registry.firmware_entries}
    assert NONMOONRAKER_IDS <= set(by_id)
    for entry_id in NONMOONRAKER_IDS:
        entry = by_id[entry_id]
        assert entry.notes and "Source" in entry.notes, entry_id
        assert entry.printers and "*" not in entry.printers, entry_id
        assert set(entry.printers) <= set(printers.MODELS), entry_id
    # Tool remapping is the XL's alone among the registered Prusa models.
    assert by_id["prusa.m863-tool-remap"].printers == ("prusa.xl",)
    assert by_id["marlin.mintemp-maxtemp"].kind == "error"
    assert by_id["prusa.print-check-refusal"].kind == "error"


def test_every_marlin_preset_resolves_the_standard_marlin_set(registry):
    """The Marlin entries are generic Marlin knowledge; #647 scopes each one
    to exactly the registered Marlin model set, so a named preset resolves
    the same descriptions the generic octoprint.marlin preset always did.
    The heater entry is the one exception: it is scoped to every printer."""
    marlin_models = {p.key for p in registry.printers if p.firmware_family == "marlin"}
    assert len(marlin_models) == 21
    for entry in registry.firmware_entries:
        if entry.firmware != "marlin":
            continue
        if entry.id == "marlin.heat-commands":
            assert entry.printers == ("*",)
        else:
            assert set(entry.printers) == marlin_models, entry.id


def test_every_nonmoonraker_model_is_in_a_decided_state(registry):
    """Each non-Moonraker model either resolves at least one scoped entry or
    is a verdict-only family whose verdict docs/MACROS.md records (#647)."""
    scoped = set()
    for entry in registry.firmware_entries:
        scoped.update(k for k in entry.printers if k != "*")
    for printer in registry.printers:
        if printer.connection == "moonraker":
            continue
        if printer.key in VERDICT_ONLY_MODELS:
            assert printer.key not in scoped, printer.key
        else:
            assert printer.key in scoped, printer.key
    macros = (ROOT / "docs" / "MACROS.md").read_text()
    for phrase in ("Bambu Lab HMS codes in the registry",
                   "Elegoo SDCP commands and errors",
                   "Stock Flashforge commands and errors"):
        assert phrase in macros


def test_no_family_borrows_another_families_entries(registry):
    """Printer-scoped classification (#614) holds across the #647 families:
    Prusa's checks resolve for Prusa models only, a named Marlin preset
    resolves the Marlin set, and neither side borrows the other's entries."""
    def firmware_ids(query, **kwargs):
        return [h.item.id for h in registry.lookup(query, **kwargs) if h.kind == "firmware"]

    assert "prusa.m862-model-check" in firmware_ids("M862.3", printer="prusa.mk4s")
    assert "prusa.m862-model-check" not in firmware_ids("M862.3", printer="octoprint.ender3")
    assert "prusa.m862-model-check" not in firmware_ids("M862.3", printer="flashforge.ad5x")
    assert "marlin.g28" in firmware_ids("G28", printer="octoprint.ender3")
    assert "marlin.heating-failed" in firmware_ids("heating failed", printer="sovol_marlin.sv06")
    assert top(registry, "MINTEMP", printer="octoprint.kobramax").item.id == "marlin.mintemp-maxtemp"
    assert "marlin.g28" not in firmware_ids("G28", printer="prusa.mk4s")
    assert top(registry, "M862.3", firmware="prusa").item.id == "prusa.m862-model-check"
    assert "prusa.m862-model-check" not in firmware_ids("M862.3", firmware="marlin")


def test_lookup_stage3_entries_by_name_and_alias(registry):
    assert top(registry, "COLDPULL").item.id == "zmod.coldpull"
    assert top(registry, "cold pull").item.id == "zmod.coldpull"
    assert top(registry, "set extruder slot").item.id == "zmod.set-extruder-slot"
    assert top(registry, "save zmod data").item.id == "zmod.save-zmod-data"
    assert top(registry, "PRINT_HANDY").item.id == "zmod.print-handy"
    assert top(registry, "pause next layer").item.id == "klipper.set-pause-next-layer"
    assert top(registry, "pause at layer").item.id == "klipper.set-pause-at-layer"
    # The Stage 3 growth must not re-top the pinned cross-family M600 lookup:
    # no same-name macro entry was added for it on purpose.
    assert top(registry, "M600").item.id == "marlin.m600"


def test_lookup_printer_filter_excludes_other_families(registry):
    hits = registry.lookup("start print", printer="flashforge.ad5x")
    ids = [h.item.id for h in hits if h.kind == "firmware"]
    assert "zmod.start-print" in ids and "klipper.print-start" not in ids


def test_lookup_bad_firmware_filter_rejected(registry):
    with pytest.raises(RegistryError):
        registry.lookup("mesh", firmware="reprap")


def test_lookup_symptom_finds_its_flow(registry):
    text = knowledge.lookup_text("first layer not sticking", registry=registry)
    assert "/print-troubleshoot first-layer" in text
    assert text.endswith(knowledge.ADVISORY_NOTE)


def test_lookup_text_for_command_and_unknown_query(registry):
    text = knowledge.lookup_text("M851", registry=registry)
    assert "M851" in text and "Safety:" in text
    unknown = knowledge.lookup_text("warp drive calibration", registry=registry)
    assert "Nothing in the registries matches" in unknown
    assert knowledge.lookup_text("", registry=registry).endswith(knowledge.ADVISORY_NOTE)


# ------------------------------------------------------------------------- flows

def test_flow_first_step_carries_safety_exactly_once():
    session = knowledge.start_flow("clog")
    first = session.step()
    assert first.safety and "burn" in " ".join(first.safety)
    session.answer(first.choices[0])
    assert session.step().safety == ()


def test_flow_branching_first_layer():
    far = knowledge.start_flow("first-layer")
    far.answer("Yes, it is clean")
    step = far.answer("Lines are round with gaps between them")
    assert step.kind == "advice" and "too far" in step.text

    close = knowledge.start_flow("first-layer")
    close.answer("No, or not sure")  # advice node: wash the bed, then continue
    assert close.step().node_id == "wash-bed"
    close.advance()
    step = close.answer("Lines are smeared flat, transparent, or ridged")
    assert "too close" in step.text


def test_flow_unknown_answer_rejected():
    session = knowledge.start_flow("ringing")
    with pytest.raises(FlowError):
        session.answer("banana")


def test_every_flow_completes_by_always_taking_the_first_option():
    for flow_id in ("first-layer", "clog", "heat-creep", "bed-level", "ringing"):
        session = knowledge.start_flow(flow_id)
        for _ in range(40):
            step = session.step()
            if step.done:
                break
            if step.kind == "question":
                session.answer(step.choices[0])
            else:
                session.advance()
        assert session.step().done, flow_id


def test_unknown_flow_rejected():
    with pytest.raises(FlowError, match="unknown troubleshooting flow"):
        knowledge.start_flow("warp-drive")


# --------------------------------------------------------------- no-command proof

def test_knowledge_package_has_no_printer_or_network_imports():
    forbidden = ("print_handy.printers", "print_handy.hermes", "print_handy.core",
                 "requests", "urllib", "socket", "subprocess", "discord")
    package = ROOT / "print_handy" / "knowledge"
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not any(name == f or name.startswith(f + ".") for f in forbidden), (path, name)


# -------------------------------------------------------------------- bot wiring

def test_bot_commands_registered():
    assert bot_commands.by_name("print-lookup") is not None
    assert bot_commands.by_name("print-troubleshoot") is not None
    assert bot_commands.by_name("print-macros") is not None
    assert bot_commands.as_text("print-lookup", {"query": "M600"}) == "/print-lookup M600"
    assert bot_commands.as_text("print-troubleshoot", {"flow": "clog"}) == "/print-troubleshoot clog"
    assert bot_commands.as_text("print-macros", {}) == "/print-macros"
    help_text = bot_commands.help_text()
    assert "/print-lookup" in help_text and "/print-troubleshoot" in help_text
    assert "/print-macros" in help_text


def test_llm_may_only_name_the_read_only_commands():
    assert llm.allowed_command("/print-lookup m600") == "/print-lookup m600"
    assert llm.allowed_command("/print-troubleshoot clog") == "/print-troubleshoot clog"
    assert llm.allowed_command("/print-troubleshoot delete everything") is None
    assert llm.allowed_command("/print-macros") == "/print-macros"
    assert llm.allowed_command("/print-macros run PRINT_START") is None


class FakeAdapter:
    """Records everything the dispatch tries to show the user; has no printer API at all."""

    def __init__(self):
        self.sent = []
        self.clarified = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, **kwargs):
        self.clarified.append(kwargs["question"])
        return S(success=True)


def _event(text):
    source = S(platform=S(value="discord"), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id="u", chat_id="c", thread_id=None)
    return S(source=source, text=text, internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "session-key")


def test_lookup_dispatch_answers_without_a_printer():
    adapter = FakeAdapter()
    out = asyncio.run(plugin.lookup_dispatch(_event("/print-lookup zmod ifs load"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print lookup handled"}
    assert "_INSERT_PRUTOK_IFS" in adapter.sent[-1]
    assert adapter.sent[-1].endswith(knowledge.ADVISORY_NOTE)
    assert adapter.clarified == []


def test_lookup_dispatch_ignores_other_text():
    assert asyncio.run(plugin.lookup_dispatch(_event("/print-lookupx"), _gateway(FakeAdapter()))) is None
    assert asyncio.run(plugin.lookup_dispatch(_event("print-lookup m600"), _gateway(FakeAdapter()))) is None


class _ScriptedPrompts:
    CANCELLED = prompts.CANCELLED

    def __init__(self, answers):
        self.answers = list(answers)

    def register(self, *args):
        pass

    def resolve_gateway_clarify(self, *args):
        return True

    def wait_for_response(self, clarify_id, timeout=None):
        return self.answers.pop(0) if self.answers else None


def test_troubleshoot_dispatch_walks_the_clog_flow_and_sends_nothing():
    adapter = FakeAdapter()
    answers = ["Nothing comes out, extruder clicks or grinds", "Path is fine", "Yes, clean stream"]
    with mock.patch.object(prompts, "gateway", return_value=_ScriptedPrompts(answers)):
        out = asyncio.run(plugin.troubleshoot_dispatch(_event("/print-troubleshoot clog"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "troubleshoot handled"}
    joined = "\n".join(adapter.sent)
    assert "safety first" in joined and "burn" in joined
    assert "/print-coldpull" in joined  # the action is a pointer to the guarded command, not an action here
    assert "walkthrough complete" in adapter.sent[-1]
    assert adapter.sent[-1].endswith(knowledge.ADVISORY_NOTE)
    assert len(adapter.clarified) == 3  # three diagnostic questions, no other prompts


def test_troubleshoot_dispatch_can_be_stopped_at_the_first_question():
    adapter = FakeAdapter()
    with mock.patch.object(prompts, "gateway", return_value=_ScriptedPrompts([plugin.TROUBLESHOOT_STOP_LABEL])):
        out = asyncio.run(plugin.troubleshoot_dispatch(_event("/print-troubleshoot heat-creep"), _gateway(adapter)))
    assert out["reason"] == "troubleshoot handled"
    assert "Troubleshooting stopped" in adapter.sent[-1]


def test_troubleshoot_dispatch_lists_flows_without_an_argument():
    adapter = FakeAdapter()
    out = asyncio.run(plugin.troubleshoot_dispatch(_event("/print-troubleshoot"), _gateway(adapter)))
    assert out["reason"] == "troubleshoot usage shown"
    for flow_id in ("first-layer", "clog", "heat-creep", "bed-level", "ringing"):
        assert f"/print-troubleshoot {flow_id}" in adapter.sent[-1]
    assert adapter.clarified == []


# ------------------------------------------------------- #468 fuzzy lookup

# Typo corpus (stage 1): realistic misspellings of registry language, pinned
# to the item a person means and the measured score band. Bands are the
# Stage 0 report's: 85–99 high-confidence near-match, 65–84 suggestion
# material. A fuzzy hit never scores 100 — that stays exact-only. Since
# P2-220 a fuzzy-only hit is also capped at 84 (below the deterministic
# substring tier), so a pure typo with no deterministic component reports
# in the suggestion band even when it is the lookup's only candidate.
TYPO_CORPUS = [
    ("adheison", "flow", "first-layer", 65, 84),
    ("nozzel clog", "flow", "clog", 85, 99),
    ("bed adheison", "flow", "first-layer", 65, 99),
    ("nozzel", "flow", "clog", 65, 99),
    ("ofset", "flow", "first-layer", 65, 99),
]


@pytest.mark.parametrize("query,kind,ident,low,high", TYPO_CORPUS)
def test_lookup_typo_corpus(registry, query, kind, ident, low, high):
    hit = top(registry, query)
    assert hit.kind == kind, query
    assert getattr(hit.item, "id", getattr(hit.item, "key", None)) == ident, query
    assert low <= hit.score <= high, (query, hit.score)


@pytest.mark.parametrize("query", ["pizza", "weather", "banana", "fan"])
def test_lookup_false_friend_corpus_returns_nothing(registry, query):
    """Unrelated words must stay no-match: the fuzzy floor (65) holds against
    the whole registry, so a nonsense query gets the same empty result as
    before #468 — the false-friend rate target for unrelated text is zero."""
    assert registry.lookup(query) == []


def test_lookup_known_false_friend_stays_in_the_suggestion_band(registry):
    """Measured honestly: "stringng" is a typo of "stringing", which has no
    registry entry; the nearest string is "ringing". It surfaces at 80 —
    inside the 65–84 suggestion band, never high-confidence — as a ranked
    candidate the reader can judge. Lookup is read-only, so the cost of the
    miss is one extra line, and no surface may treat a band-65–84 score as
    an instruction to act."""
    hit = top(registry, "stringng")
    assert hit.kind == "symptom" and hit.item.id == "ringing-on-walls"
    assert 65 <= hit.score <= 84


def test_lookup_fuzzy_does_not_regrade_exact_matches(registry):
    assert top(registry, "clog").score == 100
    assert top(registry, "M600").score == 100
    # A plain-language exact phrase still resolves exactly, ahead of any
    # fuzzy candidates the new tier adds below it.
    hit = top(registry, "first layer not sticking")
    assert hit.kind == "symptom" and hit.item.id == "first-layer-not-sticking"
    assert hit.score == 100


def test_print_source_is_registered_read_only_and_answers_without_a_site(tmp_path, monkeypatch):
    # /print-source (#464): a read-only report of which model sites are
    # connected. It checks saved credentials only, never contacts a site.
    from print_handy.core import site_accounts
    assert bot_commands.by_name("print-source") is not None
    assert "/print-source" in bot_commands.help_text()
    assert llm.allowed_command("/print-source") == "/print-source"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(site_accounts, "_jwt_expiry", lambda token: None)
    adapter = FakeAdapter()
    out = asyncio.run(plugin.source_dispatch(_event("/print-source"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print source handled"}
    assert adapter.sent[-1].startswith("**Model sources**")
    assert "Printables" in adapter.sent[-1] and "GrabCAD" in adapter.sent[-1]
    assert asyncio.run(plugin.source_dispatch(_event("/print-sourcex"), _gateway(FakeAdapter()))) is None


def test_lookup_printer_filter_scopes_wildcards_to_the_printers_firmware(registry):
    """A "*" entry means every printer ON THAT FIRMWARE, not every registered
    printer (P2-77): a Marlin printer must not be offered Klipper wildcard
    commands, and a printer whose family is "none" gets no wildcard commands
    at all — while a wildcard still resolves for a printer on its own
    firmware."""
    def firmware_ids(query, **kwargs):
        return [h.item.id for h in registry.lookup(query, **kwargs) if h.kind == "firmware"]

    assert "klipper.probe-calibrate" not in firmware_ids("probe calibrate", printer="octoprint.marlin")
    assert "klipper.bed-mesh-calibrate" not in firmware_ids("bed mesh calibrate",
                                                            printer="flashforge_stock.ad5x")
    assert "marlin.heat-commands" not in firmware_ids("heat nozzle", printer="creality.ender3v3")
    # Same-firmware wildcards still resolve.
    assert "klipper.probe-calibrate" in firmware_ids("probe calibrate", printer="creality.ender3v3")
    assert "marlin.heat-commands" in firmware_ids("heat nozzle", printer="octoprint.marlin")


def test_lookup_fuzzy_hit_cannot_outrank_a_substring_match(registry):
    """Typo tolerance is the last resort (P2-220): a fuzzy-only candidate
    stays below the deterministic substring tier, so a correctly spelled
    partial command name keeps the winner it had before the fuzzy tier."""
    hits = [h for h in registry.lookup("print stats") if h.kind == "firmware"]
    assert hits[0].item.id == "klipper.set-print-stats-info"
    by_id = {h.item.id: h.score for h in hits}
    assert by_id["klipper.print-start"] < by_id["klipper.set-print-stats-info"]
