"""Drive a corpus model through the Discord intake, with a fake Discord.

A case is a folder with one supported model file (`.3mf` or `.stl`) and case.toml. The direct
driver loads the real print-feedback module and calls its model_link_dispatch hook with a
dropped-file event; the standalone driver posts the file to the real bot (print_handy.bot).
The fake adapter answers every button question from the case's [[click]] rules. Nothing contacts
Moonraker, Spoolman or a printer: the start step runs the real print_next gates against a stub
print-now script.

Two tiers (see tests/corpus/README or the main README):
  mock tier  - the Orca CLI is replaced by a small fake that writes valid G-code, and the plate fit
               probe is modelled in Python. Runs everywhere, no Orca needed.
  real tier  - PRINT_HANDY_REAL_SLICER=1: the real Orca CLI slices and probes. Work happens under
               <repo>/.slicer-work because the Flatpak sandbox cannot see /tmp.

This module does not import pytest.
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

from . import printer_overrides

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
    entry = data.get("entry") or {}
    # A typed-entry case still carries a generated model file (the generator inventory
    # covers every case folder); its runner simply never drops it.
    model = find_model(folder)
    return S(name=folder.name, dir=folder, model=model, data=data,
             clicks=data.get("click", []), expect=data.get("expect", {}), entry=entry)


def text_event(text):
    """What the bot hands the dispatch hooks for one typed chat message (no attachment)."""
    source = S(platform=S(value="discord"), profile="3dprinting", user_id="corpus-user", chat_id="corpus-chat",
               thread_id=None, is_bot=False, profile_route_rejected=False)
    return S(source=source, text=text, media_urls=[], message_id=f"corpus-typed-{uuid.uuid4().hex[:8]}",
             metadata={}, internal=False)


def dispatch_text(plugin, adapter, text):
    """Run one typed message through the real Router (all dispatch hooks, in order)."""
    from print_handy.bot.router import Router
    gateway = S(_is_user_authorized_for_source=lambda s: True,
                _session_key_for_source=lambda s: "corpus-session",
                _delivery_adapter_for=lambda s: adapter)
    return asyncio.run(Router(plugin).dispatch(text_event(text), gateway))


# ----------------------------------------------------------------------------- fake Discord

class ExploreLoop(AssertionError):
    """An explored path kept asking questions past MAX_PATH_QUESTIONS (a loop, not a journey)."""


MAX_PATH_QUESTIONS = 400  # a 166-part per-part color journey is real, not a loop
_CANCEL_LABEL = re.compile(r"^(cancel|stop|no\b|do not|don't|keep printing)|\(cancel\)", re.I)


class FakeAdapter:
    """The two DiscordAdapter calls the intake makes. A click is an immediate clarify resolution.

    ``overrides`` ({question index: button label}) answers the Nth question with that button
    instead of its [[click]] rule, the explorer's one-change-per-path walk (#1373). With
    ``fallback`` a question no rule answers takes its first non-cancel button instead of failing,
    so a path that reaches a question the default journey never sees can still finish.
    """

    def __init__(self, clicks, events=None, overrides=None, fallback=False):
        self.clicks = clicks
        self.events = events if events is not None else []
        self.questions = []   # {'question', 'choices', 'picked'}
        self.sent = []
        self.overrides = dict(overrides or {})
        self.fallback = fallback
        self.raised = []  # harness exceptions the intake may catch and log (#1373)

    def pick(self, question, choices):
        index = len(self.questions)
        if index >= MAX_PATH_QUESTIONS:
            exc = ExploreLoop(f"more than {MAX_PATH_QUESTIONS} questions on one path; last: {question[:200]}")
            self.raised.append(exc)
            raise exc
        wanted = self.overrides.get(index)
        if wanted is not None:
            if wanted in choices:
                return choices.index(wanted)
            raise UnansweredPrompt(f"explored button {wanted!r} is not offered at question {index}\n"
                                   f"question: {question}\nbuttons: {choices}")
        for rule in self.clicks:
            if str(rule["question"]).casefold() not in question.casefold():
                continue
            want = str(rule["button"]).casefold()
            for index, label in enumerate(choices):
                if want in label.casefold():
                    return index
        if self.fallback:
            for index, label in enumerate(choices):
                if not _CANCEL_LABEL.search(str(label)):
                    return index
            return 0
        raise UnansweredPrompt(
            "no [[click]] rule answers this question; add one to case.toml\n"
            f"question: {question}\nbuttons: {choices}")

    async def send_clarify(self, **kw):
        from print_handy.core import prompts as cm
        try:
            index = self.pick(kw["question"], kw["choices"])
        except (UnansweredPrompt, ExploreLoop) as exc:
            if exc not in self.raised:
                self.raised.append(exc)
            raise
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
    """What the bot hands the hook for one dropped model: a cached path preserving its file type."""
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
; nozzle_diameter = {nozzle}
; enable_support = {support}
; layer_height = {layer}
; initial_layer_print_height = {first_layer}
; sparse_infill_density = {infill}
; sparse_infill_pattern = {pattern}
; filament_type = {types}
M190 S{bed}
M104 S{hotend} T0
START_PRINT BED_TEMP={bed} EXTRUDER_TEMP={hotend} MESH=MESH_DATA
G90
{tools}; FEATURE: Outer wall
G1 X100 Y100 Z0.2 F3000
G1 X120 Y120 E1
{support_lines}END_PRINT
"""


def _profile_temperatures(filament_json):
    """(hotend, bed) as an Orca run would print them from one filament profile, 210/60 if unset."""
    try:
        data = json.loads(Path(filament_json).read_text())
    except (OSError, ValueError):
        return 210, 60

    def first(*keys, default):
        for key in keys:
            value = data.get(key)
            value = value[0] if isinstance(value, list) and value else value
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
        return default
    return (first("nozzle_temperature_initial_layer", "nozzle_temperature", default=210),
            first("hot_plate_temp_initial_layer", "hot_plate_temp", default=60))


def fake_orca_run(real_run, orca_command, events=None, warnings=()):
    """subprocess.run stand-in. Only a slice may reach Orca in the mock tier; the fit probe is modelled.

    ``warnings`` are the non-critical texts real Orca reports for the case (a case's
    ``[mock_slicer] warnings``). Like Orca's support check, they are written to result.json only
    when the slice ran with supports off.
    """
    def run(cmd, *args, **kwargs):
        if list(cmd[:len(orca_command)]) != list(orca_command):
            return real_run(cmd, *args, **kwargs)
        assert "--slice" in cmd, f"mock tier: Orca was asked to do more than slice: {cmd}"
        if events is not None:
            events.append(("slice", list(cmd)))
        out = Path(cmd[cmd.index("--outputdir") + 1])
        machine, process = (Path(p) for p in cmd[cmd.index("--load-settings") + 1].split(";")[:2])
        filaments = cmd[cmd.index("--load-filaments") + 1].split(";")
        process_cfg = json.loads(process.read_text())
        support = str(process_cfg.get("enable_support", "0"))
        nozzle = json.loads(machine.read_text()).get("nozzle_diameter", "0.4")
        nozzle = nozzle[0] if isinstance(nozzle, list) else nozzle
        tools = "".join(f"T{i}\n" for i in range(len(filaments))) if len(filaments) > 1 else ""
        # Like Orca, write the temperatures the first filament profile asks for: Print Handy puts
        # the nozzle-type-adjusted temperature there (+5 C for the AD5X's stainless nozzle), and
        # offline verification checks START_PRINT against exactly that.
        hotend, bed = _profile_temperatures(filaments[0])
        text = FAKE_GCODE.format(
            hotend=hotend, bed=bed, nozzle=nozzle, support=support, types=";".join(["PLA"] * len(filaments)), tools=tools,
            layer=process_cfg.get("layer_height", ""), first_layer=process_cfg.get("initial_layer_print_height", ""),
            infill=process_cfg.get("sparse_infill_density", ""), pattern=process_cfg.get("sparse_infill_pattern", ""),
            support_lines="; FEATURE: Support\nG1 X110 Y110 E2\n" if support == "1" else "")
        (out / "plate_1.gcode").write_text(text)
        result = {"error_string": "Success.", "return_code": 0, "sliced_plates": [{"id": 1}]}
        if warnings and support != "1":
            result["warnings"] = [{"class": "slicing_warning_non_critical", "plate_id": 1, "text": str(w)}
                                  for w in warnings]
        (out / "result.json").write_text(json.dumps(result, indent=1))
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
    # The fit-check timing keys on the step's __name__ (FIT_CHECK_STEPS, matched
    # in thread_heavy): the stand-in must carry the production name or the
    # probes it replaces are never timed in mock mode (P2-281).
    capacity.__name__ = "orca_plate_capacity"
    return capacity


# ----------------------------------------------------------------------------- the run

def _latest_plan_id(work):
    """The plan the setup flow of a print-next entry case wrote under the patched PLANS_DIR."""
    plans = sorted((Path(work) / "plans").glob("*.json"))
    if not plans:
        raise AssertionError("the setup flow wrote no print-next plan")
    return json.loads(plans[-1].read_text())["plan_id"]


def _print_next_followups(work):
    """The typed /print-next for the plate that is next once the setup flow's first
    plate is recorded complete — credited through print_records, the same write the
    listener makes when that print finishes, so resolve_next targets the next plate."""
    from print_handy.runtime import print_records
    plan_path = sorted((Path(work) / "plans").glob("*.json"))[-1]
    plan = json.loads(plan_path.read_text())
    first = plan["parts"][0]
    print_records.update_multipart_plan({"multipart_plan_id": plan["plan_id"], "success": True,
                                         "part_id": first["id"], "completed_quantity": 1})
    return [f"/print-next {plan['plan_id']}"]


def _stub_print_now(folder):
    stub = Path(folder) / "print-now-stub"
    stub.write_text('#!/bin/sh\necho "started $2"\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


BUILT = "built"  # default: printer state produced by the real create_spool and bind_slot flows (#594)


def mock_slicer_warnings(case):
    """The Orca warnings a case's ``[mock_slicer]`` table says real Orca reports for its model."""
    table = (getattr(case, "data", None) or {}).get("mock_slicer") or {}
    return [str(text) for text in table.get("warnings") or ()]


def is_post_slice_question(text):
    """The slicer support question: asked after slicing by design, from Orca's own warnings."""
    from print_handy.core import slice_warnings
    return text.startswith(slice_warnings.QUESTION_PREFIX)


def run_case(case, work, *, real_slicer=False, state_file=BUILT, transport="direct", audit_started=None, auto=True,
             overrides=None, explore=False, spools=None):
    """Run the case end to end and return what happened. `work` is a fresh directory.

    Everything the flow writes (archive, receipts, claims) goes under `work`.

    transport="direct" calls the handler hook directly, with the FakeAdapter.
    transport="standalone" posts the file to the standalone bot (print_handy.bot) on a fake discord.py
    client: real adapter, real buttons, clicks resolved through the core prompt registry.
    """
    if transport not in ("direct", "standalone"):
        raise ValueError(f"unknown transport {transport!r}")
    spoolman = None
    # A case's [printer] table varies the shared hardware record (nozzle, bed, slots, tower; #824).
    path_overrides = overrides
    overrides = printer_overrides.validate((getattr(case, "data", None) or {}).get("printer"))
    if overrides and state_file != BUILT:
        raise ValueError("[printer] overrides apply to the built default state only")
    if overrides.get("bed") and real_slicer:
        raise ValueError("a [printer] bed override is mock tier only: the Orca templates carry the real bed")
    if state_file == BUILT:
        # The slot map the journey slices from is made by production spool code, not handwritten JSON.
        from . import spool_fixture
        Path(work).mkdir(parents=True, exist_ok=True)
        (Path(work) / "built-state").mkdir(exist_ok=True)
        skeleton = FIXTURE_STATE
        if overrides:
            skeleton = Path(work) / "built-state" / "skeleton.json"
            skeleton.write_text(json.dumps(printer_overrides.apply_to_state(json.loads(FIXTURE_STATE.read_text()), overrides)))
        state_file, spoolman = spool_fixture.build_state(
            Path(work) / "built-state", skeleton,
            spools=spools or spool_fixture.SPOOLS[:overrides.get("slots", len(spool_fixture.SPOOLS))])
    spoolman_calls_built = len(spoolman.calls) if spoolman else 0
    entry = getattr(case, "entry", None) or {}
    from print_handy.core import background_slice, model_job, orca_adapter, prepared_intake, printer_state, source_colors
    from print_handy.runtime import _config, print_next, print_records

    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    stub = _stub_print_now(work)
    model_copy = None
    if case.model is not None:
        model_copy = work / case.model.name
        model_copy.write_bytes(case.model.read_bytes())
    events = TimedEvents()
    adapter = FakeAdapter(case.clicks, events, overrides=path_overrides, fallback=explore)
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
        # The source audit moved to jobs/audit.py (#1323); model_job still re-exports it.
        if (getattr(func, "__module__", "") in ("print_handy.core.model_job", "print_handy.jobs.audit")
                and getattr(func, "__name__", "") == "audit"):
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

    def offline_verify(out, adapter=None, state=None, printer_id=None):
        # verify_gcode asks Moonraker for the display mode. The offline check is the same minus that.
        # The stub takes verify_gcode's current arguments (adapter/state/printer_id)
        # and verifies against the state the caller resolved for the job's printer.
        return model_job.verify_gcode_offline(
            out, state if state is not None else printer_state.load(), adapter=adapter)

    def find_job(filename, *args):
        path = sorted(Path(model_job.ARCHIVE).rglob("JOB.json"))[-1]
        return json.loads(path.read_text()), {"gcode": str(filename), "plate": 1, "parts": [case.name],
                                              "slot": 1, "color": "white", "material": "PLA",
                                              "filament_g": None, "seconds": 3600}

    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        patch = lambda *a, **k: enter(mock.patch.object(*a, **k))  # noqa: E731
        enter(printer_overrides.patched(overrides))
        patch(printer_state, "STATE", str(state_file))
        patch(_config, "state_dir", lambda: work / "state")
        archive = (WORK / f"{case.name}-{uuid.uuid4().hex[:8]}") if real_slicer else work / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        patch(model_job, "ARCHIVE", archive)
        if entry.get("kind") == "print-next":
            # resolve_next checks the plan's job against print_next.ARCHIVE, a module constant
            # bound from config at import time; in the corpus it must be the same patched
            # archive model_job gets, or every plan reads as foreign.
            patch(print_next, "ARCHIVE", archive)
        patch(print_records, "PLANS_DIR", work / "plans")
        patch(model_job, "START_CLAIMS", work / "claims.json")
        patch(model_job, "CONTEXT", work / "context.json")
        patch(model_job, "BOUNDS_CACHE", work / "bounds-cache")
        patch(prepared_intake, "_heavy", thread_heavy)
        patch(background_slice.BackgroundSlice, "start", observe_slice_start)
        patch(model_job, "verify_gcode", offline_verify)
        # The already-verified shortcut in _verify_routed still asks the printer (#1373): the
        # mock tier has none, so a re-slice of identical bytes failed with "the printer could
        # not be reached". Offline, the live check is the same no-op the offline verify is.
        from print_handy.gcode import verify as _gverify
        patch(_gverify, "_run_live_printer_check", lambda *a, **k: None)
        # Multicolor summaries probe live ZMod purge settings. This offline corpus models
        # an unavailable reading explicitly, rather than attempting (and swallowing) I/O.
        patch(printer_state, "read_purge_mode", lambda st: None)
        # ZMod purge/plugin detection (#832) reads the printer at intake; offline it has
        # nothing to read, exactly like an unreachable printer (today's prime tower).
        from print_handy.core import purge
        patch(purge, "refresh_best_effort", lambda *a, **kw: {})
        # The first confirmation card's spool-remap note reads the ZMod version
        # the same fail-soft way (P2-242); model it unavailable too.
        patch(model_job, "_zmod_version", lambda st: None)
        patch(print_next, "start", start)
        patch(prepared_intake.print_summary, "find_job", find_job)
        patch(prepared_intake._external.load("moonraker_event_listener"), "generate_roast", lambda *a, **kw: "")
        network_attempts = []

        def reject_network(*args, **kwargs):
            network_attempts.append(getattr(args[0], "full_url", str(args[0])) if args else "unknown")
            raise AssertionError("corpus flow reached the network")

        # The print lifecycle reads cached spool data through the shared client too.
        # Serve that expected bookkeeping request from the same fake used to build the slots.
        from print_handy.core import spoolman_client
        if spoolman is not None:
            patch(spoolman_client, "request", lambda method, path, body=None, **kw: spoolman(method, path, body))
        enter(mock.patch("urllib.request.urlopen", side_effect=reject_network))
        if real_slicer:
            # Flatpak Orca cannot see /tmp, and the fit probe writes its scratch files to tempfile's dir.
            (WORK / "tmp").mkdir(parents=True, exist_ok=True)
            patch(tempfile, "tempdir", str(WORK / "tmp"))
        else:
            patch(orca_adapter.subprocess, "run", fake_orca_run(orca_adapter.subprocess.run, orca_adapter.ORCA_COMMAND, events,
                                                                     warnings=mock_slicer_warnings(case)))
            patch(orca_adapter, "orca_plate_capacity",
                  model_capacity(orca_adapter._multi_plate_capacity, orca_adapter.source_triangles,
                                 orca_adapter._source_plate_count))
        plugin = load_plugin()
        entry_kind = entry.get("kind")
        wall_s = None
        segment = None
        run_events = events
        if entry_kind == "typed":
            # A scripted chat conversation instead of a file drop (#1142): the typed-message
            # path through the dispatch hooks (direct) or the bot core (standalone).
            if transport == "standalone":
                adapter, outcome, wall_s = _run_standalone_typed(case, work, plugin, events, patch, auto=auto)
            else:
                from print_handy.core import prompts as _prompts
                _real_wait = _prompts.wait_for_response
                patch(_prompts, "wait_for_response",
                      lambda cid, timeout=None: _real_wait(cid, 10 if timeout is None else min(timeout, 10)))
                t0 = time.monotonic()
                outcome = None
                for text in entry.get("texts", []):
                    outcome = dispatch_text(plugin, adapter, text)
                wall_s = time.monotonic() - t0
        elif transport == "standalone":
            # A print-next entry case: the drop flow is the untimed setup, the typed
            # /print-next follow-up is the timed segment (its plan id exists only once
            # the setup has run, so the follow-up text is built lazily inside).
            followups = (lambda: _print_next_followups(work)) if entry_kind == "print-next" else ()
            adapter, outcome, segment = _run_standalone(
                case, work, model_copy, plugin, events, patch, auto=auto,
                followup_texts=followups, starts=starts)
            if segment is not None:
                run_events = segment.events
                wall_s = segment.wall_s
        else:
            from print_handy.core import prompts as _prompts
            _real_wait = _prompts.wait_for_response
            patch(_prompts, "wait_for_response",
                  lambda cid, timeout=None: _real_wait(cid, 10 if timeout is None else min(timeout, 10)))
            gateway = S(_is_user_authorized_for_source=lambda s: True, _session_key_for_source=lambda s: "corpus-session",
                        _delivery_adapter_for=lambda s: adapter)
            event = drop_event(model_copy, case.name)
            outcome = asyncio.run(plugin.model_link_dispatch(event, gateway))
            if entry_kind == "print-next":
                # The setup flow is untimed; only the typed /print-next segment is the case.
                seg_events = TimedEvents()
                adapter.events = seg_events
                run_events = seg_events
                q0, s0, sent0 = len(adapter.questions), len(starts), len(adapter.sent)
                t0 = time.monotonic()
                outcome = dispatch_text(plugin, adapter, _print_next_followups(work)[0])
                segment = S(wall_s=time.monotonic() - t0, starts=len(starts) - s0,
                            questions=adapter.questions[q0:], sends=adapter.sent[sent0:])
                wall_s = segment.wall_s
        state_root = plugin._state_dir()
        receipts = sorted((state_root / "model-link-intakes").glob("*.json"))
        receipt = json.loads(receipts[-1].read_text()) if receipts else {}
        job = json.loads(Path(receipt["job_json"]).read_text()) if receipt.get("job_json") else {}
        state = printer_state.load()
        if spoolman is not None:
            assert not network_attempts, f"corpus attempted unexpected network I/O: {network_attempts}"
    return S(outcome=outcome, receipt=receipt, job=job, state=state, adapter=adapter, starts=starts, events=run_events,
             slice_starts=slice_starts, state_root=state_root, receipts=receipts, archive=archive,
             gcodes=[Path(g) for g in receipt.get("gcodes", [])], audit_started=audit_started,
             spoolman=spoolman, spoolman_calls_built=spoolman_calls_built, state_file=state_file,
             real_slicer=real_slicer, wall_s=wall_s, segment=segment)


def _standalone_core(case, work, plugin, events, patch, *, auto=True):
    """The standalone bot (print_handy.bot) on a fake discord.py client, as _run_standalone sets it up."""
    import copy
    from print_handy.bot.router import Router
    from print_handy.bot.runtime import BotCore
    from print_handy.core import prepared_intake, prompts
    from print_handy.runtime import _config
    from . import fake_discord

    discord = fake_discord.FakeDiscord(case.clicks, events=events, auto=auto)
    cfg = copy.deepcopy(_config.cfg())
    cfg["bot"]["mode"] = "standalone"
    cfg["discord"].update(chat_id=str(discord.channel.id), allowed_user_ids=str(discord.operator.id),
                          approval_user_id="")
    cfg["paths"]["cache"] = Path(work) / "cache"
    cfg["llm"].update(chat_url="", model="")
    # A process global the bot sets; restore it so later tests see the default.
    patch(prepared_intake, "STOP_SENDER", None)
    # Buttons resolve in the core prompt registry; keep its cleanup semantics,
    # but apply a ten-second expiry so unanswered prompts fail closed fast.
    wait_for_response = prompts.wait_for_response

    def short_wait(cid, timeout=None):
        return wait_for_response(cid, 10 if timeout is None else min(timeout, 10))

    patch(prompts, "wait_for_response", short_wait)
    return discord, BotCore(discord.client, cfg, router=Router(plugin))


async def _handle(core, discord, message):
    outcome = await core.handle_message(message)
    if discord.pending:
        await asyncio.gather(*discord.pending)
    return outcome


def _run_standalone(case, work, model, plugin, events, patch, *, auto=True,
                    followup_texts=(), starts=None):
    """Drop the model in a fake Discord channel and let the standalone bot handle the message.

    With followup_texts (a print-next entry case), the drop flow is the untimed setup: the
    returned segment covers only the typed follow-ups, timed and event-logged separately
    (its TimedEvents starts when the segment does, so its phases are segment-relative)."""
    from . import fake_discord

    discord, core = _standalone_core(case, work, plugin, events, patch, auto=auto)
    message = discord.message(attachments=[fake_discord.FakeAttachment(model, f"{case.name}{Path(model).suffix.lower()}")])
    outcome = asyncio.run(_handle(core, discord, message))
    segment = None
    if callable(followup_texts):
        followup_texts = followup_texts()
    if followup_texts:
        q0, s0, sent0 = len(discord.questions), len(starts) if starts is not None else 0, len(discord.sent)
        seg_events = TimedEvents()
        discord.events = seg_events
        t0 = time.monotonic()
        for text in followup_texts:
            outcome = asyncio.run(_handle(core, discord, discord.message(content=text)))
        segment = S(wall_s=time.monotonic() - t0, events=seg_events,
                    starts=(len(starts) - s0) if starts is not None else None,
                    questions=discord.questions[q0:], sends=discord.sent[sent0:])
    return discord, outcome, segment


def _run_standalone_typed(case, work, plugin, events, patch, *, auto=True):
    """A typed-entry case on the standalone bot: scripted chat messages, no attachment."""
    discord, core = _standalone_core(case, work, plugin, events, patch, auto=auto)
    t0 = time.monotonic()
    outcome = None
    for text in case.entry.get("texts", []):
        outcome = asyncio.run(_handle(core, discord, discord.message(content=text)))
    return discord, outcome, time.monotonic() - t0


# ----------------------------------------------------------------------------- inspecting a run

def part_names(run):
    return [p["name"] for p in run.job["color_manifest"]["parts"]]


def colors_by_part(run):
    """{part name: color} from the saved color decisions; a single color applies to every part."""
    decisions = run.receipt["color_decisions"]
    parts = run.job["color_manifest"]["parts"]
    if decisions["mode"] == "single_color":
        return {p["name"]: decisions["selection"]["color"] for p in parts}
    # Only part choices name a part's color; filament choices (painted regions,
    # height-based color changes, #870) belong to no single part.
    return {next(p["name"] for p in parts if p["key"] == key.removeprefix("part:")): row["color"]
            for key, row in decisions["choices"].items() if key.startswith("part:")}


def slice_support_setting(run):
    """enable_support in the process profile the slice actually used (from the job's slice_config)."""
    values = set()
    for plate in [run.job] + list(run.job.get("derived_plates") or []):
        for config in plate.get("slice_config") or []:
            path = Path(config)
            if path.name == "process.json" and path.is_file():
                values.add(str(json.loads(path.read_text()).get("enable_support", "0")))
    return values


def orca_inputs(run):
    """[(project settings Orca read, the job's merged machine/process/filament profile)] for every 3MF the
    slicer was handed directly (orca_input.3mf: a derived plate's private copy, #868)."""
    import zipfile
    found = []
    for plate in [run.job] + list(run.job.get("derived_plates") or []):
        configs = [Path(c) for c in plate.get("slice_config") or []]
        if not configs or not (configs[0].parent / "orca_input.3mf").is_file():
            continue
        with zipfile.ZipFile(configs[0].parent / "orca_input.3mf") as archive:
            if "Metadata/project_settings.config" not in archive.namelist():
                continue
            project = json.loads(archive.read("Metadata/project_settings.config"))
        job = {}
        for path in configs[:3]:
            job.update(json.loads(path.read_text()))
        found.append((project, job))
    return found


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


def check_refusal(case, run):
    """Assert a pinned intake refusal (P2-177): the FAILED receipt, for the
    expected reason, with the harness's adapter checks, and proof that
    slicing and starting never happened.

    check_expectations cannot serve a refused case (it asserts a print
    started), so until now the runners accepted any FAILED receipt —
    including one from an unrelated intake regression that never exercised
    the pinned limit. `error_contains` in the case's [expect] table names
    the refusal text the case pins.
    """
    want = case.expect
    assert not getattr(run.adapter, "unanswered", None), "\n".join(run.adapter.unanswered)
    assert not getattr(run.adapter, "errors", None), run.adapter.errors
    assert run.receipt.get("status") == "FAILED", (
        f"expected an intake refusal, got receipt {run.receipt.get('status')!r}: "
        f"{run.receipt.get('error')}\n{_transcript(run)}")
    needle = want.get("error_contains")
    if needle:
        assert needle in str(run.receipt.get("error") or ""), (
            f"refusal reason {run.receipt.get('error')!r} does not name the pinned "
            f"refusal {needle!r}\n{_transcript(run)}")
    assert not run.slice_starts, "a refused case must never reach slicing"
    assert not run.starts, "a refused case must never start a print"


def _check_entry_expectations(case, run):
    """Assert an entry case's [expect] table (typed conversation / print-next segment)."""
    want = case.expect
    assert not getattr(run.adapter, "unanswered", None), "\n".join(run.adapter.unanswered)
    assert not getattr(run.adapter, "errors", None), run.adapter.errors
    if case.entry.get("kind") == "typed":
        sent = " \n".join(str(s) for s in run.adapter.sent).casefold()
        for text in want.get("sends_contain", []):
            assert text.casefold() in sent, f"never told the operator {text!r}\n{_transcript(run)}"
        asked = " || ".join(q["question"] for q in run.adapter.questions)
        for text in want.get("prompts", []):
            assert text.casefold() in asked.casefold(), f"never asked about {text!r}\n{_transcript(run)}"
        assert len(run.starts) == int(want.get("starts", 0)), run.starts
        return
    # print-next: the setup flow's receipt must show the first plate started, and the
    # timed segment must ask its prompt and start exactly the expected number of plates.
    assert run.receipt.get("status") == want.get("status", "STARTED"), (
        f"setup receipt status {run.receipt.get('status')!r}: {run.receipt.get('error')}\n{_transcript(run)}")
    seg = run.segment
    assert seg is not None, "the print-next segment did not run"
    assert seg.starts == int(want.get("segment_starts", 1)), f"segment starts {seg.starts}\n{_transcript(run)}"
    asked = " || ".join(q["question"] for q in seg.questions) + " " + " ".join(str(s) for s in seg.sends)
    for text in want.get("segment_prompts", []):
        assert text.casefold() in asked.casefold(), f"segment never asked about {text!r}\n{_transcript(run)}"


def check_expectations(case, run):
    """Assert the case's [expect] table. Returns nothing; raises AssertionError with the difference."""
    if getattr(case, "entry", None):
        _check_entry_expectations(case, run)
        return
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
    if "slice_error_contains" in want:
        assert want["slice_error_contains"] in str(receipt.get("slice_error") or ""), (
            f"slice error {receipt.get('slice_error')!r}\n{_transcript(run)}")
    if "says" in want:
        said = " ".join(run.adapter.sent).casefold()
        for text in want["says"]:
            assert text.casefold() in said, f"never told the operator {text!r}\n{_transcript(run)}"
    if any(key in want for key in ("orca_input", "orca_input_from_job", "orca_input_absent")):
        inputs = orca_inputs(run)
        assert inputs, "no 3MF was handed to the slicer directly (expected a derived plate, route (c))"
        for project, job in inputs:
            for key, value in (want.get("orca_input") or {}).items():
                assert project.get(key) == value, f"Orca read {key}={project.get(key)!r}, expected {value!r}"
            for key in want.get("orca_input_from_job") or ():
                assert project.get(key) == job.get(key), f"Orca read {key}={project.get(key)!r}, job has {job.get(key)!r}"
            for key in want.get("orca_input_absent") or ():
                assert key not in project, f"Orca still read the source's {key}={project[key]!r}"
    if "gcode_settings" in want and getattr(run, "real_slicer", False):
        for path in run.gcodes:
            header = dict(line[2:].split(" = ", 1) for line in path.read_text(errors="replace").splitlines()
                          if line.startswith("; ") and " = " in line)
            for key, value in want["gcode_settings"].items():
                assert header.get(key) == value, f"{path.name}: {key} = {header.get(key)!r}, expected {value!r}"
    if "clearance_codes" in want:
        report = run.job.get("support_clearance") or {}
        codes = sorted({f.get("code") for f in report.get("findings") or []})
        assert codes == sorted(want["clearance_codes"]), f"clearance codes {codes}\n{_transcript(run)}"
    if "buttons_absent" in want:
        offered = [label for q in run.adapter.questions for label in (q.get("choices") or [])]
        for text in want["buttons_absent"]:
            hit = [label for label in offered if text.casefold() in str(label).casefold()]
            assert not hit, f"offered {hit!r}, expected no {text!r} button\n{_transcript(run)}"
    if want.get("conversation_checks", True):
        conversation_problems = conversation_checks(run)
        assert not conversation_problems, "\n".join(conversation_problems) + "\n" + _transcript(run)
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


def conversation_checks(run):
    """Problems a person would notice in the conversation itself, for every case (#1373).

    The corpus used to judge a run by its receipt alone (STARTED, scale,
    colors), so a hard live intake that offered an option before its
    numbers existed, asked about the same short spool twice and printed
    with supports the operator had turned down still passed. These checks
    read the questions, the posts and the sliced artifact:

    - no question offers a "with supports" button while its line still
      reads "slicing...";
    - a spool shortage shown on the Ready question is not asked again at
      the start;
    - an operator's recorded supports decision is what the started G-code
      (or, without a config block, the slice profile) says.
    Returns a list of problem lines; empty when the conversation is sound.
    """
    problems = []
    for q in run.adapter.questions:
        text = str(q.get("question") or "")
        if "with supports" in text and "slicing..." in text and any(
                "with supports" in str(label) for label in q.get("choices") or []):
            problems.append("a 'with supports' button was offered while its estimate still read 'slicing...'")
            break
    asked = [str(q.get("question") or "") for q in run.adapter.questions]
    shown_short = any("short by about" in text and "Ready to slice" in text for text in asked)
    if shown_short and any("short of filament" in text for text in asked):
        problems.append("the spool shortage shown on the Ready question was asked again at the start")
    decided = (run.job.get("support_decision") or {})
    if decided.get("by") == "operator" and decided.get("supports") is not None:
        want = "1" if decided["supports"] else "0"
        from print_handy.core.edit_plan import gcode_config
        seen = set()
        for path in run.gcodes:
            value = gcode_config(Path(path).read_text(errors="replace")).get("enable_support")
            if value is not None:
                seen.add(value.strip())
        if not seen:
            seen = slice_support_setting(run)
        told = any("keep the supports their new pose needs" in str(q.get("question") or "")
                   for q in run.adapter.questions)
        if told and decided["supports"] is False and seen == {"0", "1"}:
            # A full-size split that turned parts onto supports keeps them on those plates,
            # and the Ready question said so (#1373, prepared_intake.split_turn_supports_note).
            seen = {want}
        if seen and seen != {want}:
            problems.append(f"the operator chose supports={decided['supports']} but the slice has "
                            f"enable_support={sorted(seen)}")
    return problems


STARTED_STATUSES = {"STARTED"}


def path_invariants(run):
    """Product rules every path through the intake must keep, whatever the operator pressed (#1373).

    The conversation checks plus what each pressed button promises:
    - no error was recorded and no question went unanswered;
    - a cancel ("Cancel", "Stop", "Do not print") means nothing started;
    - a start happened at most once, and a STARTED receipt means exactly once;
    - "without supports" G-code has supports off, "with supports" has them on;
    - nothing posted reads as an internal failure (a traceback or a raw exception name).
    Returns a list of problem lines; empty when the path is sound.
    """
    problems = list(conversation_checks(run))
    for exc in getattr(run.adapter, "raised", None) or []:
        problems.append(f"harness raised inside the intake (caught there): {type(exc).__name__}: {str(exc)[:200]}")
    if getattr(run.adapter, "errors", None):
        problems.append(f"errors: {run.adapter.errors}")
    picked = [str(q.get("picked") or "") for q in run.adapter.questions]
    status = run.receipt.get("status")
    if len(run.starts) > 1:
        problems.append(f"the start ran {len(run.starts)} times")
    if status in STARTED_STATUSES and len(run.starts) != 1:
        problems.append(f"receipt says {status} but the start ran {len(run.starts)} times")
    if any(_CANCEL_LABEL.search(label) for label in picked) and run.starts:
        problems.append(f"a cancel was pressed ({[p for p in picked if _CANCEL_LABEL.search(p)]}) "
                        "but the print still started")
    from print_handy.core.edit_plan import gcode_config
    configs = [gcode_config(Path(g).read_text(errors="replace")) for g in run.gcodes if Path(g).is_file()]
    supports = {str(c.get("enable_support") or "").strip() for c in configs} - {""}
    last_support_choice = next((label for label in reversed(picked)
                                if "with supports" in label or "without supports" in label), None)
    told = any("keep the supports their new pose needs" in str(q.get("question") or "")
               for q in run.adapter.questions)
    if last_support_choice and supports and run.starts:
        want = "0" if "without supports" in last_support_choice else "1"
        if told and want == "0" and supports <= {"0", "1"}:
            supports = {want}  # split-turned parts keep their supports, and the question said so
        if supports != {want}:
            problems.append(f"pressed {last_support_choice!r} but the started G-code has enable_support={sorted(supports)}")
    for text in run.adapter.sent:
        if re.search(r"Traceback \(most recent call last\)|\b(?:KeyError|TypeError|AttributeError|IndexError)\b", str(text)):
            problems.append(f"posted an internal error: {str(text)[:200]}")
            break
    return problems


def explore(case, work_root, *, max_runs=None, spools=None):
    """Walk the case's decision tree one changed answer at a time and check every path (#1373).

    The default journey runs first; then, for each question it asked, every other offered button
    is pressed in a fresh run (later questions keep their [[click]] rules, and a question no rule
    answers takes its first non-cancel button). Every run is held to path_invariants. Returns a
    list of (path description, problems) for the paths that broke a rule, and the number of
    runs. ``max_runs`` bounds the walk (None: every alternative).
    """
    work_root = Path(work_root)
    failures, runs = [], 0

    def one(overrides, label):
        nonlocal runs
        runs += 1
        work = work_root / f"path-{runs:03d}"
        try:
            run = run_case(case, work, overrides=overrides, explore=True, spools=spools)
        except (UnansweredPrompt, ExploreLoop) as exc:
            failures.append((label, [f"{type(exc).__name__}: {str(exc)[:300]}"]))
            return None
        problems = path_invariants(run)
        if problems:
            failures.append((label, problems + [_transcript(run)[-2500:]]))
        return run

    base = one({}, "default journey")
    if base is None:
        return failures, runs
    # The Ready question carries most decisions (supports, repairs, scale, reorient, print
    # anyway), so a bounded walk tries its buttons first, then the rest in order.
    order = sorted(range(len(base.adapter.questions)),
                   key=lambda i: (0 if "Ready to slice" in base.adapter.questions[i]["question"] else 1, i))
    for index in order:
        q = base.adapter.questions[index]
        for label in q["choices"]:
            if label == q["picked"]:
                continue
            if max_runs is not None and runs >= max_runs:
                return failures, runs
            one({index: label}, f"Q{index} {q['question'][:70]!r} -> {label!r}")
    return failures, runs


def _transcript(run):
    lines = [f"Q: {q['question']}\n   buttons {q['choices']}\n   clicked {q['picked']!r}" for q in run.adapter.questions]
    return "\n".join(lines + ["sent: " + repr(run.adapter.sent)])
