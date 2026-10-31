"""Drive a corpus model through the Discord intake the way the gateway does, with a fake Discord.

A case is a folder with one supported model file (`.3mf` or `.stl`) and case.toml. The driver loads the real print-feedback plugin and calls
its model_link_dispatch hook with a dropped-file event, the same entry point the gateway uses.
The fake adapter answers every button question from the case's [[click]] rules. Nothing contacts
Moonraker, Spoolman or a printer: the start step runs the real print_next gates against a stub
print-now script.

Two tiers (see tests/corpus/README or the main README):
  mock tier  - the Orca CLI is replaced by a small fake that writes valid G-code, and the plate fit
               probe is modelled in Python. Runs everywhere, no Orca needed.
  real tier  - PRINT_HANDY_REAL_SLICER=1: the real Orca CLI slices and probes. Work happens under
               <repo>/.slicer-work because the Flatpak sandbox cannot see /tmp.

This module does not import pytest, so tests/corpus/test_corpus.py and the gateway-environment child
process (run_in_gateway_env) both use it.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import re
import stat
import sys
import threading
import tempfile
import time
import tomllib
import types
import uuid
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

from print_handy import settings

CORPUS = Path(__file__).resolve().parent
CASES = CORPUS / "cases"
PLUGIN = settings.ROOT / "plugins" / "print-feedback" / "__init__.py"
WORK = settings.ROOT / ".slicer-work"
FIXTURE_STATE = settings.ROOT / "tests" / "pipeline" / "fixtures" / "printer_hardware.json"
SUPPORT_FEATURE = re.compile(r"^;\s*(?:FEATURE|TYPE):\s*Support", re.I | re.M)
MODEL_FILE_EXTS = {".3mf", ".stl"}


def find_model(case_dir):
    """The one model file in a case folder. model.3mf still wins when several exist."""
    found = [p for p in Path(case_dir).iterdir() if p.is_file() and p.suffix.lower() in MODEL_FILE_EXTS]
    if not found:
        raise FileNotFoundError(
            f"{case_dir} has no model file ({', '.join(sorted(MODEL_FILE_EXTS))})")
    preferred = [p for p in found if p.name == "model.3mf"]
    return preferred[0] if preferred else found[0]


class TimedEvents(list):
    """The run's event log with a monotonic timestamp per entry (times[i] belongs to self[i])."""

    def __init__(self, *args):
        super().__init__(*args)
        self.t0 = time.monotonic()
        self.times = [self.t0] * len(self)

    def append(self, item):
        super().append(item)
        self.times.append(time.monotonic())

    def timeline(self):
        return [(round(t - self.t0, 3), e[0], str(e[1])[:80] if len(e) > 1 else "") for t, e in zip(self.times, self)]


class UnansweredPrompt(AssertionError):
    """The case has no [[click]] rule for a question the bot asked."""


def private_corpus_root():
    """Return the opted-in local corpus root, refusing repository paths and CI use."""
    configured = os.environ.get("PRINT_HANDY_PRIVATE_CORPUS")
    if not configured:
        return None
    if os.environ.get("CI", "").strip().casefold() in {"1", "true", "yes"}:
        raise RuntimeError("private corpus is local-only and cannot be selected in CI")
    root = Path(configured).expanduser().resolve()
    if root.is_relative_to(settings.ROOT.resolve()):
        raise ValueError("private corpus root must be outside the repository")
    if not root.is_dir():
        raise FileNotFoundError(f"private corpus root does not exist or is not a directory: {root}")
    return root


def is_private_case(folder):
    """Whether a case resolves under the explicitly configured local corpus."""
    root = private_corpus_root()
    return root is not None and Path(folder).resolve().is_relative_to(root)


def case_dirs():
    public = sorted(p.parent for p in CASES.glob("*/case.toml"))
    root = private_corpus_root()
    if root is None:
        return public
    public_paths = {folder.resolve() for folder in public}
    private = []
    for manifest in root.glob("*/case.toml"):
        folder = manifest.parent.resolve()
        if not folder.is_relative_to(root):
            raise ValueError("private corpus cases must remain under their root")
        if folder in public_paths:
            raise ValueError("private corpus overlaps public cases")
        private.append(folder)
    if not private:
        raise ValueError(f"private corpus has no case.toml files: {root}")
    return public + sorted(private)


# The bed fit probes the intake offloads; their summed duration is the fit-check time (#594).
FIT_CHECK_STEPS = frozenset({"fit_options", "orca_plate_capacity", "per_part_fit_options",
                             "full_size_split_possible", "probe_single_plate_rearrange", "full_size_one_plate"})


def load_case(folder):
    folder = Path(folder)
    data = tomllib.loads((folder / "case.toml").read_text())
    return S(name=folder.name, dir=folder, model=find_model(folder), data=data,
             clicks=data.get("click", []), expect=data.get("expect", {}))


# ----------------------------------------------------------------------------- fake Discord

def install_clarify_stand_in():
    """Hermes' clarify gateway is not installed in CI. Use it when present, else an in-memory copy
    with the four calls the intake makes (register, wait_for_response, resolve_gateway_clarify, CANCELLED)."""
    try:
        import tools.clarify_gateway  # noqa: F401
        return
    except ImportError:
        pass
    lock, prompts = threading.Lock(), {}

    class Entry:
        def __init__(self):
            self.event, self.answer = threading.Event(), None

    def entry(cid):
        with lock:
            return prompts.setdefault(cid, Entry())

    def resolve_gateway_clarify(cid, answer):
        e = entry(cid)
        e.answer = answer
        e.event.set()
        return True

    def wait_for_response(cid, timeout=None):
        e = entry(cid)
        # The stand-in simulates prompt expiry quickly: an unanswered prompt must
        # fail closed in tests, not block for the production wait (up to an hour).
        got = e.event.wait(10 if timeout is None else min(timeout, 10))
        with lock:
            prompts.pop(cid, None)
        return e.answer if got else None

    module = types.ModuleType("tools.clarify_gateway")
    module.CANCELLED = "__cancelled__"
    module.register = lambda cid, session_key, question, choices: entry(cid) and None
    module.resolve_gateway_clarify = resolve_gateway_clarify
    module.wait_for_response = wait_for_response
    package = sys.modules.get("tools") or types.ModuleType("tools")
    package.clarify_gateway = module
    sys.modules["tools"] = package
    sys.modules["tools.clarify_gateway"] = module


class FakeAdapter:
    """The two DiscordAdapter calls the intake makes. A click is an immediate clarify resolution."""

    def __init__(self, clicks, events=None):
        self.clicks = clicks
        self.events = events if events is not None else []
        self.questions = []   # {'question', 'choices', 'picked'}
        self.sent = []

    def pick(self, question, choices):
        for rule in self.clicks:
            if str(rule["question"]).casefold() not in question.casefold():
                continue
            want = str(rule["button"]).casefold()
            for index, label in enumerate(choices):
                if want in label.casefold():
                    return index
        raise UnansweredPrompt(
            "no [[click]] rule answers this question; add one to case.toml\n"
            f"question: {question}\nbuttons: {choices}")

    async def send_clarify(self, **kw):
        import tools.clarify_gateway as cm
        index = self.pick(kw["question"], kw["choices"])
        self.questions.append({"question": kw["question"], "choices": list(kw["choices"]),
                               "picked": kw["choices"][index]})
        self.events.append(("prompt", kw["question"]))
        cm.resolve_gateway_clarify(kw["clarify_id"], kw["choices"][index])
        return S(success=True)

    # The production DiscordAdapter.send signature: a stray text= raises TypeError.
    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        self.events.append(("send", content))
        return S(success=True, message_id=None)


def load_plugin(name="corpus_print_feedback"):
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def drop_event(model, case_name, message_id="corpus-message-1"):
    """What the gateway hands the hook for one dropped model: a cached path preserving its file type."""
    cache = Path(model).parent / "discord-cache"
    cache.mkdir(exist_ok=True)
    dropped = cache / f"doc_0123456789ab_{case_name}{Path(model).suffix.lower()}"
    dropped.write_bytes(Path(model).read_bytes())
    source = S(platform=S(value="discord"), profile="3dprinting", user_id="corpus-user", chat_id="corpus-chat",
               thread_id=None, is_bot=False, profile_route_rejected=False)
    return S(source=source, text="", media_urls=[str(dropped)], message_id=message_id, metadata={}, internal=False)


# ----------------------------------------------------------------------------- mock tier models

FAKE_GCODE = """\
; HEADER_BLOCK_START
; generated by OrcaSlicer 2.4.2
; HEADER_BLOCK_END
; estimated printing time (normal mode) = 1h 0m 0s
; nozzle_diameter = 0.4
; enable_support = {support}
; filament_type = {types}
M190 S60
M104 S210 T0
START_PRINT BED_TEMP=60 EXTRUDER_TEMP=210 MESH=MESH_DATA
G90
{tools}; FEATURE: Outer wall
G1 X100 Y100 Z0.2 F3000
G1 X120 Y120 E1
{support_lines}END_PRINT
"""


def fake_orca_run(real_run, orca_command, events=None):
    """subprocess.run stand-in. Only a slice may reach Orca in the mock tier; the fit probe is modelled."""
    def run(cmd, *args, **kwargs):
        if list(cmd[:len(orca_command)]) != list(orca_command):
            return real_run(cmd, *args, **kwargs)
        assert "--slice" in cmd, f"mock tier: Orca was asked to do more than slice: {cmd}"
        if events is not None:
            events.append(("slice", list(cmd)))
        out = Path(cmd[cmd.index("--outputdir") + 1])
        machine, process = (Path(p) for p in cmd[cmd.index("--load-settings") + 1].split(";")[:2])
        filaments = cmd[cmd.index("--load-filaments") + 1].split(";")
        support = str(json.loads(process.read_text()).get("enable_support", "0"))
        tools = "".join(f"T{i}\n" for i in range(len(filaments))) if len(filaments) > 1 else ""
        text = FAKE_GCODE.format(
            support=support, types=";".join(["PLA"] * len(filaments)), tools=tools,
            support_lines="; FEATURE: Support\nG1 X110 Y110 E2\n" if support == "1" else "")
        (out / "plate_1.gcode").write_text(text)
        return S(returncode=0, stdout="", stderr="")
    return run


def model_capacity(real_multi_plate, source_triangles, plate_count):
    """Python stand-in for orca_plate_capacity: how many copies fit the bed, 0 when none."""
    def capacity(source, state, spacing_mm=10.0, edge_margin_mm=None, scale=1.0, copy_limit=16,
                 allow_rotations=True, rotation=None, supports=None, brim_policy=None):
        source = Path(source)
        if source.suffix.lower() == ".3mf" and rotation is None and plate_count(source) > 1:
            return real_multi_plate(source, state, scale=float(scale))
        tris = source_triangles(source)
        if rotation is not None:
            from print_handy.core import orca_adapter
            tris = [[orca_adapter._rotate(p, rotation) for p in tri] for tri in tris]
        extent = [(max(p[a] for t in tris for p in t) - min(p[a] for t in tris for p in t)) * float(scale)
                  for a in range(3)]
        bed = state["bed_mm"]
        usable = (bed[0] - 2.0, bed[1] - 2.0)
        if extent[2] > float(state["max_print_height_mm"]) + 0.05:
            return 0
        if not ((extent[0] <= usable[0] and extent[1] <= usable[1])
                or (extent[1] <= usable[0] and extent[0] <= usable[1])):
            return 0
        if copy_limit == 1:
            return 1
        across = int((bed[0] - 6.0 + spacing_mm) // (extent[0] + spacing_mm))
        down = int((bed[1] - 6.0 + spacing_mm) // (extent[1] + spacing_mm))
        return max(1, min(int(copy_limit), across * down))
    return capacity


# ----------------------------------------------------------------------------- the run

def _stub_print_now(folder):
    stub = Path(folder) / "print-now-stub"
    stub.write_text('#!/bin/sh\necho "started $2"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


BUILT = "built"  # default: printer state produced by the real create_spool and bind_slot flows (#594)


def run_case(case, work, *, real_slicer=False, state_file=BUILT, in_gateway_env=False, transport="hermes", audit_started=None, auto=True):
    """Run the case end to end and return what happened. `work` is a fresh directory.

    Everything the flow writes (archive, receipts, claims) goes under `work`, except state in
    in_gateway_env mode, where settings, printer_state and the plugin's state dir come from the process
    environment exactly as the gateway sees them.

    transport="hermes" calls the plugin hook the way the gateway does, with the FakeAdapter.
    transport="standalone" posts the file to the standalone bot (print_handy.bot) on a fake discord.py
    client: real adapter, real buttons, clicks resolved through the core prompt registry.
    """
    if transport not in ("hermes", "standalone"):
        raise ValueError(f"unknown transport {transport!r}")
    if transport == "standalone" and in_gateway_env:
        raise ValueError("the gateway environment check is Hermes only")
    if transport == "hermes":
        install_clarify_stand_in()
    spoolman = None
    if state_file == BUILT and not in_gateway_env:
        # The slot map the journey slices from is made by production spool code, not handwritten JSON.
        from . import spool_fixture
        Path(work).mkdir(parents=True, exist_ok=True)
        (Path(work) / "built-state").mkdir(exist_ok=True)
        state_file, spoolman = spool_fixture.build_state(Path(work) / "built-state", FIXTURE_STATE)
    spoolman_calls_built = len(spoolman.calls) if spoolman else 0
    from print_handy.core import background_slice, model_job, orca_adapter, prepared_intake, printer_state, source_colors
    from print_handy.hermes import _config, print_next, print_records

    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    stub = _stub_print_now(work)
    model_copy = work / case.model.name
    model_copy.write_bytes(case.model.read_bytes())
    events = TimedEvents()
    adapter = FakeAdapter(case.clicks, events)
    starts = []
    slice_starts = []
    real_background_start = background_slice.BackgroundSlice.start

    async def observe_slice_start(runner, job_path, binding, slice_func):
        slice_starts.append(("slice_start", str(job_path)))
        events.append(("slice_start", str(job_path)))
        return await real_background_start(runner, job_path, binding, slice_func)

    real_start = print_next.start

    async def start(target):
        starts.append(target)
        return await real_start(target, model_job=stub, idle_check=lambda: None, timeout=10,
                                claims_dir=work / "claims")

    async def thread_heavy(func, *args, **kwargs):
        # The production _heavy runs steps in a child process with its own settings. Keep them in
        # this process so the fakes below apply to them.
        if getattr(func, "__module__", "").endswith("model_job") and getattr(func, "__name__", "") == "audit":
            if audit_started is not None:
                audit_started.append(("audit_start", time.monotonic()))
            events.append(("audit_start", str(args[0]) if args else ""))
        if getattr(func, "__name__", "") in FIT_CHECK_STEPS:
            events.append(("fit_check_start", func.__name__))
            try:
                return await asyncio.to_thread(func, *args, **kwargs)
            finally:
                events.append(("fit_check_end", func.__name__))
        return await asyncio.to_thread(func, *args, **kwargs)

    def offline_verify(out):
        # verify_gcode asks Moonraker for the display mode. The offline check is the same minus that.
        return model_job.verify_gcode_offline(out, printer_state.load())

    def find_job(filename, *args):
        path = sorted(Path(model_job.ARCHIVE).rglob("JOB.json"))[-1]
        return json.loads(path.read_text()), {"gcode": str(filename), "plate": 1, "parts": [case.name],
                                              "slot": 1, "color": "white", "material": "PLA",
                                              "filament_g": None, "seconds": 3600}

    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        patch = lambda *a, **k: enter(mock.patch.object(*a, **k))  # noqa: E731
        if not in_gateway_env:
            patch(printer_state, "STATE", str(state_file))
            patch(_config, "state_dir", lambda: work / "state")
        archive = (WORK / f"{case.name}-{uuid.uuid4().hex[:8]}") if real_slicer else work / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        patch(model_job, "ARCHIVE", archive)
        patch(print_records, "PLANS_DIR", work / "plans")
        patch(model_job, "START_CLAIMS", work / "claims.json")
        patch(model_job, "CONTEXT", work / "context.json")
        patch(model_job, "BOUNDS_CACHE", work / "bounds-cache")
        patch(prepared_intake, "_heavy", thread_heavy)
        patch(background_slice.BackgroundSlice, "start", observe_slice_start)
        patch(model_job, "verify_gcode", offline_verify)
        patch(print_next, "start", start)
        patch(prepared_intake.print_summary, "find_job", find_job)
        enter(mock.patch("urllib.request.urlopen", side_effect=AssertionError("corpus flow reached the network")))
        if real_slicer:
            # Flatpak Orca cannot see /tmp, and the fit probe writes its scratch files to tempfile's dir.
            (WORK / "tmp").mkdir(parents=True, exist_ok=True)
            patch(tempfile, "tempdir", str(WORK / "tmp"))
        else:
            patch(orca_adapter.subprocess, "run", fake_orca_run(orca_adapter.subprocess.run, orca_adapter.ORCA_COMMAND, events))
            patch(orca_adapter, "orca_plate_capacity",
                  model_capacity(orca_adapter._multi_plate_capacity, orca_adapter.source_triangles,
                                 orca_adapter._source_plate_count))
        plugin = load_plugin()
        if transport == "standalone":
            adapter, outcome = _run_standalone(case, work, model_copy, plugin, events, patch, auto=auto)
        else:
            gateway = S(_is_user_authorized_for_source=lambda s: True, _session_key_for_source=lambda s: "corpus-session",
                        _delivery_adapter_for=lambda s: adapter)
            event = drop_event(model_copy, case.name)
            if in_gateway_env:
                plugin._deliver_receipt = _noop_receipt
            outcome = asyncio.run(plugin.model_link_dispatch(event, gateway))
        state_root = plugin._state_dir()
        receipts = sorted((state_root / "model-link-intakes").glob("*.json"))
        receipt = json.loads(receipts[-1].read_text()) if receipts else {}
        job = json.loads(Path(receipt["job_json"]).read_text()) if receipt.get("job_json") else {}
        state = printer_state.load()
    return S(outcome=outcome, receipt=receipt, job=job, state=state, adapter=adapter, starts=starts, events=events,
             slice_starts=slice_starts, state_root=state_root, receipts=receipts, archive=archive,
             gcodes=[Path(g) for g in receipt.get("gcodes", [])], audit_started=audit_started,
             spoolman=spoolman, spoolman_calls_built=spoolman_calls_built, state_file=state_file)


async def _noop_receipt(*args, **kwargs):
    return None


def _run_standalone(case, work, model, plugin, events, patch, *, auto=True):
    """Drop the model in a fake Discord channel and let the standalone bot handle the message."""
    import copy
    from print_handy.bot.router import Router
    from print_handy.bot.runtime import BotCore
    from print_handy.core import prepared_intake, prompts
    from print_handy.hermes import _config
    from . import fake_discord

    discord = fake_discord.FakeDiscord(case.clicks, events=events, auto=auto)
    cfg = copy.deepcopy(_config.cfg())
    cfg["bot"]["mode"] = "standalone"
    cfg["discord"].update(chat_id=str(discord.channel.id), allowed_user_ids=str(discord.operator.id),
                          approval_user_id="")
    cfg["paths"]["cache"] = Path(work) / "cache"
    cfg["llm"].update(chat_url="", model="")
    # Both are process globals the bot sets; restore them so later Hermes-path tests see the defaults.
    patch(prepared_intake, "STOP_SENDER", None)
    patch(prompts, "_active", False)
    # BotCore.activate() routes standalone buttons to the real core registry, not
    # tools.clarify_gateway. Keep that registry and its cleanup semantics, but
    # apply the same ten-second expiry as the Hermes corpus stand-in.
    wait_for_response = prompts.wait_for_response

    def short_wait(cid, timeout=None):
        return wait_for_response(cid, 10 if timeout is None else min(timeout, 10))

    patch(prompts, "wait_for_response", short_wait)
    core = BotCore(discord.client, cfg, router=Router(plugin))
    message = discord.message(attachments=[fake_discord.FakeAttachment(model, f"{case.name}{Path(model).suffix.lower()}")])

    async def go():
        outcome = await core.handle_message(message)
        if discord.pending:
            await asyncio.gather(*discord.pending)
        return outcome

    return discord, asyncio.run(go())


# ----------------------------------------------------------------------------- inspecting a run

def part_names(run):
    return [p["name"] for p in run.job["color_manifest"]["parts"]]


def colors_by_part(run):
    """{part name: color} from the saved color decisions; a single color applies to every part."""
    decisions = run.receipt["color_decisions"]
    parts = run.job["color_manifest"]["parts"]
    if decisions["mode"] == "single_color":
        return {p["name"]: decisions["selection"]["color"] for p in parts}
    return {next(p["name"] for p in parts if p["key"] == key.removeprefix("part:")): row["color"]
            for key, row in decisions["choices"].items()}


def slice_support_setting(run):
    """enable_support in the process profile the slice actually used (from the job's slice_config)."""
    values = set()
    for plate in [run.job] + list(run.job.get("derived_plates") or []):
        for config in plate.get("slice_config") or []:
            path = Path(config)
            if path.name == "process.json" and path.is_file():
                values.add(str(json.loads(path.read_text()).get("enable_support", "0")))
    return values


def gcode_checks(run):
    """The offline G-code checks the pipeline itself runs, applied again to each final file."""
    from print_handy.core import slice_guard
    from print_handy.printers.flashforge.zmod import multicolor_contract
    rows = []
    for path in run.gcodes:
        text = path.read_text(errors="replace")
        verified = model_job_verify(path, run.state)
        mapping = multicolor_contract.embedded_mapping(text, run.state)
        guard = slice_guard.post_slice(path, tool_slots={int(t): s for t, s in (mapping or {}).items()})
        rows.append({"gcode": path, "verified": verified, "mapping": mapping, "guard": guard,
                     "support": bool(SUPPORT_FEATURE.search(text))})
    return rows


def model_job_verify(path, state):
    from print_handy.core import model_job
    return model_job.verify_gcode_offline(path, state)


def check_expectations(case, run):
    """Assert the case's [expect] table. Returns nothing; raises AssertionError with the difference."""
    want = case.expect
    receipt = run.receipt
    assert not getattr(run.adapter, "unanswered", None), "\n".join(run.adapter.unanswered)
    assert not getattr(run.adapter, "errors", None), run.adapter.errors
    assert receipt.get("status") == want.get("status", "STARTED"), (
        f"receipt status {receipt.get('status')!r}: {receipt.get('error')}\n{_transcript(run)}")
    if "parts" in want:
        assert sorted(part_names(run)) == sorted(want["parts"])
    if "source_plates" in want:
        assert int(run.job["audit"]["plates"]) == want["source_plates"]
    if "source_kind" in want:
        assert run.job["audit"]["kind"] == want["source_kind"]
    if "source_size_mm" in want:
        actual = [float(run.job["audit"][axis]) for axis in ("size_x", "size_y", "size_z")]
        assert all(abs(got - expected) <= 0.01 for got, expected in zip(actual, want["source_size_mm"])), actual
    if "colors" in want:
        got = {k: v.casefold() for k, v in colors_by_part(run).items()}
        assert got == {k: v.casefold() for k, v in want["colors"].items()}, got
    if "gcodes" in want:
        assert len(run.gcodes) == want["gcodes"], run.gcodes
    if "plate_mode" in want:
        assert (run.job.get("plate_merge") or {}).get("mode") == want["plate_mode"], run.job.get("plate_merge")
    scale = receipt.get("scale") or 1.0
    if "scale" in want:
        assert abs(scale - want["scale"]) < 1e-6, scale
    if "scale_range" in want:
        low, high = want["scale_range"]
        assert low <= scale <= high, f"scale {scale} outside {want['scale_range']}"
    if "prompts" in want:
        asked = " || ".join(q["question"] for q in run.adapter.questions)
        for text in want["prompts"]:
            assert text.casefold() in asked.casefold(), f"never asked about {text!r}\n{_transcript(run)}"
    if "supports" in want:
        used = slice_support_setting(run)
        assert used == {"1" if want["supports"] else "0"}, f"slice profile enable_support={used}"
    if "copies" in want:
        assert int(run.receipt.get("copies", 1)) == want["copies"], run.receipt
        assert int(run.job.get("sliced_copies", {}).get("copies", 1)) == want["copies"], run.job.get("sliced_copies")
    if "mesh_manifold" in want:
        assert run.job["audit"]["mesh_audit"]["manifold"] is want["mesh_manifold"]
    if "mesh_winding_inconsistencies" in want:
        assert run.job["audit"]["mesh_audit"]["winding_inconsistencies"] == want["mesh_winding_inconsistencies"]
    if want.get("started", True):
        assert len(run.starts) == 1, "the start button path did not run exactly once"
    for row in gcode_checks(run):
        assert row["guard"].status != "blocked", row["guard"].blocked
        assert row["mapping"], "G-code has no embedded tool to slot map"
        if "supports" in want:
            assert row["support"] == want["supports"], (
                f"G-code {'lacks' if want['supports'] else 'has'} support material")
        used_slots = sorted(set(row["mapping"].values()))
        if "slots" in want:
            assert len(used_slots) == want["slots"], used_slots


def _transcript(run):
    lines = [f"Q: {q['question']}\n   buttons {q['choices']}\n   clicked {q['picked']!r}" for q in run.adapter.questions]
    return "\n".join(lines + ["sent: " + repr(run.adapter.sent)])


# ----------------------------------------------------------------------------- gateway environment

def run_in_gateway_env(case, work, gateway_home, home, real_slicer=False):
    """Run the case in a fresh interpreter whose environment is the gateway's.

    The multiplexed gateway sets HERMES_HOME to the shared root for every profile (#57). Settings are
    read when modules import, so this needs its own process. The profile state dir must already hold
    printer_hardware.json. Returns the child's JSON report.
    """
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PRINT_HANDY__", "HERMES_"))}
    env.pop("PRINT_HANDY_CONFIG", None)
    env.pop("PRINT_HANDY_REAL_SLICER", None)  # set again below only when asked; the CI real tier exports it
    Path(work).mkdir(parents=True, exist_ok=True)
    empty = Path(work) / "empty.toml"
    empty.write_text("")
    env.update(HOME=str(home), HERMES_HOME=str(gateway_home), PRINT_HANDY_CONFIG=str(empty),
               PYTHONPATH=str(settings.ROOT), PYTHONDONTWRITEBYTECODE="1")
    if real_slicer:
        env["PRINT_HANDY_REAL_SLICER"] = "1"
    code = ("import json,sys;from tests.corpus import harness;"
            "print('REPORT '+json.dumps(harness._env_child(sys.argv[1], sys.argv[2])))")
    proc = subprocess.run([sys.executable, "-c", code, str(case.dir), str(work)], cwd=str(settings.ROOT), env=env,
                          capture_output=True, text=True, timeout=900)
    lines = [line for line in proc.stdout.splitlines() if line.startswith("REPORT ")]
    assert lines, f"child failed ({proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    return json.loads(lines[-1][len("REPORT "):])


def _env_child(case_dir, work):
    import os
    from print_handy.core import printer_state
    report = {"hermes_home_env": os.environ.get("HERMES_HOME"), "printer_state": str(printer_state.STATE),
              "settings_home": str(settings.current()["paths"]["hermes_home"])}
    try:
        case = load_case(case_dir)
        run = run_case(case, Path(work) / "run", real_slicer=os.environ.get("PRINT_HANDY_REAL_SLICER") == "1",
                       in_gateway_env=True)
    except Exception as exc:  # the parent asserts on the report, so say what broke instead of a traceback
        return {**report, "status": "CRASHED", "error": f"{type(exc).__name__}: {exc}", "receipts": []}
    return {**report, "state_root": str(run.state_root), "receipts": [str(p) for p in run.receipts],
            "status": run.receipt.get("status"), "error": run.receipt.get("error"), "transcript": _transcript(run)}
