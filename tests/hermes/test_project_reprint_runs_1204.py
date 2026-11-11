"""#1204 verify-first items (#1008 acceptance items 5 and 6). Offline: no printer, no chat.

(a) ``/print-again list`` groups a project's prints under the project and
    keeps standalone prints on the list.
(b) An entire-project reprint is a new logical run: a fresh
    ``project_run_id`` shared by its plates, new attempt ids, the original
    plan's ``completed_quantity`` never moves, and the armed run (like the
    queued plate claims) is consumed once and never reused.

The project fixture is the one the #1008 stage D tests use: an archived
3-plate job anchored at project 82, live plan counts (1, 1, 0).
"""
import json
import os
import time

import pytest

from print_handy.hermes import print_control, print_records
from print_handy.hermes.print_next import NextPlateError

from .test_print_again_project_scope import LABELS, PLAN_ID, _project


@pytest.fixture(autouse=True)
def _plain_clock(monkeypatch):
    # The list's timestamp format is platform specific; the grouping is not.
    monkeypatch.setattr(print_control, "_et", lambda ts: "when")


def _standalone(archive, title="Desk Stand", age=10):
    run = archive / "desk-stand--b"
    d = run / "sliced/orca/p0"
    d.mkdir(parents=True)
    g = d / "plate_1.gcode"
    g.write_text("; standalone\nG1 X1\n")
    rec = d / "plate_1.launch.json"
    rec.write_text(json.dumps({"file": "desk.gcode"}))
    t = time.time() - age
    os.utime(rec, (t, t))
    (run / "JOB.json").write_text(json.dumps({"title": title, "gcode": str(g)}))
    return run / "JOB.json"


def _target_for(archive, job, gcode):
    return {"job_json": str(job), "gcode": str(gcode), "title": "Rack Mount"}


# ------------------------------------------------------------- (a) list grouping

def test_print_again_list_groups_project_plates_and_keeps_standalone_prints(tmp_path):
    archive, job, gcodes, plans = _project(tmp_path)
    _standalone(archive)
    numbers = tmp_path / "print_numbers.json"
    rows = print_control.printed_jobs(archive, numbers)
    assert len(rows) == 4  # three plate launches and one standalone print
    text = print_control.printed_list_text(rows, plans_dir=plans)
    lines = text.splitlines()
    standalone = next(r for r in rows if r["title"] == "Desk Stand")
    # The standalone print keeps its exact one-line form at the top level.
    assert f"{standalone['index']}. Desk Stand (when)" in lines
    # The project is one entry with its children under it, each named by
    # its exact plate alias.
    head = lines.index("Project 82: Rack Mount, 3 plates, 2 of 3 finished")
    children = lines[head + 1:head + 4]
    assert all(line.startswith("  ") for line in children)
    for suffix, label in zip("ABC", LABELS):
        assert any(f"plate 82{suffix} ({label})" in line for line in children), (suffix, children)
    # Newest first: the standalone print (10 s old) is listed above the project.
    assert lines.index(f"{standalone['index']}. Desk Stand (when)") < head
    assert "/print-again 82B" in text


def test_print_again_list_without_projects_is_unchanged(tmp_path):
    archive = tmp_path / "archive"
    _standalone(archive)
    rows = print_control.printed_jobs(archive, tmp_path / "print_numbers.json")
    expected = ("Printed before, newest first:\n"
                f"{rows[0]['index']}. Desk Stand (when)"
                "\nReprint one with /print-again <number> or /print-again <name>.")
    assert print_control.printed_list_text(rows, plans_dir=tmp_path / "plans") == expected


def test_print_again_list_limit_counts_a_project_as_one_entry(tmp_path):
    archive, job, gcodes, plans = _project(tmp_path)
    _standalone(archive)
    rows = print_control.printed_jobs(archive, tmp_path / "print_numbers.json")
    # Two top-level entries; a limit of 2 must keep both, the standalone
    # print included, even though there are four print rows.
    text = print_control.printed_list_text(rows, limit=2, plans_dir=plans)
    assert "Desk Stand" in text and "Project 82" in text and "older" not in text


# ------------------------------------------------------------- (b) reprint runs

@pytest.fixture
def records_dirs(tmp_path, monkeypatch):
    prints = tmp_path / "vault" / "Prints"
    prints.mkdir(parents=True)
    monkeypatch.setattr(print_records, "PRINTS_DIR", prints)
    return prints


def _completion(ctx, started_at, success=True):
    return dict(ctx, started_at=started_at, completed_at="2026-11-10T12:00:00Z",
                success=success, status="complete" if success else "cancelled",
                gcode_file="plate_1.gcode")


def test_entire_project_reprint_is_a_new_run_that_never_moves_the_original(
        tmp_path, monkeypatch, records_dirs):
    archive, job, gcodes, plans = _project(tmp_path)
    numbers = tmp_path / "print_numbers.json"
    monkeypatch.setattr(print_records, "PLANS_DIR", plans)
    runs = tmp_path / "reprint_runs.json"
    monkeypatch.setattr(print_control, "REPRINT_RUNS", runs)
    target = _target_for(archive, job, gcodes[0])

    targets = print_control.project_scope_targets(target, "entire", archive, numbers, plans)
    run_ids = {t["project_run_id"] for t in targets}
    assert len(targets) == 3 and len(run_ids) == 1
    run_id = run_ids.pop()
    assert run_id.startswith("run-") and run_id != "original"
    # A second whole-project reprint is another run, never the same one.
    again = print_control.project_scope_targets(target, "entire", archive, numbers, plans)
    assert again[0]["project_run_id"] != run_id
    # The remaining scope finishes the original run and arms nothing.
    remaining = print_control.project_scope_targets(target, "remaining", archive, numbers, plans)
    assert {t["project_run_id"] for t in remaining} == {"original"}
    assert print_control.arm_reprint_run(remaining[0]) is None and not runs.exists()

    # Plate C's reprint start: the armed run is consumed by its start record.
    plate_c = targets[2]
    assert print_control.arm_reprint_run(plate_c) == run_id
    ctx = {"job_key": "plate-2.gcode", "title": "Rack Mount", "print_number": 90,
           "project_id": "project-intake-1", "project_number": 82,
           "project_run_id": "original", "project_plate_id": "plate3",
           "project_plate_suffix": "C", "multipart_plan_id": PLAN_ID, "part_id": "plate3",
           "slicer_settings": {"local_gcode": str(gcodes[2].resolve())},
           "started_at": "2026-11-10T11:00:00Z"}
    start_path = print_records.write_start_record(ctx)
    assert ctx["project_run_id"] == run_id  # carried on into the completion sidecar
    assert json.loads(open(start_path, encoding="utf-8").read())["project_run_id"] == run_id
    # Consumed once: a later start of the same G-code is not this run.
    assert print_control.take_reprint_run(gcodes[2]) is None

    # The reprint plate succeeds: the original plan's plate 3 does not move.
    print_records.update_multipart_plan(_completion(ctx, "2026-11-10T11:00:00Z"))
    live = json.loads((plans / f"{PLAN_ID}.json").read_text())
    plate3 = next(p for p in live["parts"] if p["id"] == "plate3")
    assert plate3["completed_quantity"] == 0 and plate3["status"] == "planned"
    assert not plate3.get("completed_attempt_ids")
    # Control: the same success on the original run is what would credit it.
    print_records.update_multipart_plan(_completion(dict(ctx, project_run_id="original"),
                                                    "2026-11-10T11:05:00Z"))
    live = json.loads((plans / f"{PLAN_ID}.json").read_text())
    assert next(p for p in live["parts"] if p["id"] == "plate3")["completed_quantity"] == 1

    # Every physical attempt of the run has its own attempt id and the run id.
    first = print_records._record_json(print_records.build_record_data(
        "plate-2.gcode", stats={"state": "complete"}, context=_completion(ctx, "2026-11-10T11:00:00Z")))
    second = print_records._record_json(print_records.build_record_data(
        "plate-2.gcode", stats={"state": "complete"}, context=_completion(ctx, "2026-11-10T13:00:00Z")))
    assert first["project_run_id"] == second["project_run_id"] == run_id
    assert first["print_attempt_id"] != second["print_attempt_id"]


def test_project_reprint_queue_never_reuses_a_consumed_claim(tmp_path, monkeypatch):
    archive, job, gcodes, plans = _project(tmp_path)
    numbers = tmp_path / "print_numbers.json"
    queue = tmp_path / "queue.json"
    targets = print_control.project_scope_targets(_target_for(archive, job, gcodes[0]),
                                                  "entire", archive, numbers, plans)
    run_id = targets[0]["project_run_id"]
    print_control.queue_project_sequence(targets, path=queue)  # plate A already started
    b = print_control.next_copy_target(path=queue, archive=archive)
    assert b["project_plate_ref"] == "82B" and b["project_run_id"] == run_id
    assert print_control.next_copy_target(path=queue, archive=archive)["copy"] == b["copy"]
    print_control.mark_copy_started(path=queue)
    c = print_control.next_copy_target(path=queue, archive=archive)
    # A new plate is a new one-start claim: plate B's consumed claim is not reused.
    assert c["project_plate_ref"] == "82C" and c["copy"] != b["copy"]
    assert c["project_run_id"] == run_id
    print_control.mark_copy_started(path=queue)
    with pytest.raises(NextPlateError):
        print_control.next_copy_target(path=queue, archive=archive)


def test_an_unstarted_reprint_is_disarmed_and_a_stale_arm_is_ignored(tmp_path, monkeypatch):
    archive, job, gcodes, plans = _project(tmp_path)
    runs = tmp_path / "reprint_runs.json"
    target = _target_for(archive, job, gcodes[1])
    run_id = print_control.arm_reprint_run(target, path=runs)
    assert run_id and target["project_run_id"] == run_id
    print_control.disarm_reprint_run(target, path=runs)
    assert print_control.take_reprint_run(gcodes[1], path=runs) is None
    print_control.arm_reprint_run(target, path=runs)
    late = time.time() + print_control.REPRINT_RUN_TTL_S + 1
    assert print_control.take_reprint_run(gcodes[1], path=runs, now=late) is None
    # A standalone job is not a project: nothing is armed for it.
    plain = _standalone(archive)
    assert print_control.arm_reprint_run(
        {"job_json": str(plain), "gcode": str(gcodes[0])}, path=runs) is None


# ------------------------------------------- the shared next-plate question

def _plate_hook(tmp_path, picks):
    """/print-next ghost on a plan with two startable plates, answered by ``picks``."""
    import asyncio
    from types import SimpleNamespace as S
    from unittest import mock

    from print_handy.hermes import print_next

    from .test_print_next import load_plugin, make_job, make_plan

    folder, rows = make_job(tmp_path)
    plans = make_plan(tmp_path, folder, rows)
    plan = json.loads((plans / 'ghost.json').read_text())
    plan['parts'][0].update(status='planned', completed_quantity=0,
                            job_json=str(folder / 'JOB.json'), gcode_path=rows[0]['gcode'])
    (plans / 'ghost.json').write_text(json.dumps(plan))
    mod = load_plugin()

    class Transport:
        def __init__(self):
            self.questions, self.sent = [], []

        async def send_clarify(self, **kwargs):
            self.questions.append(kwargs)
            import tools.clarify_gateway as cm
            pick = picks[len(self.questions) - 1]
            cm.resolve_gateway_clarify(kwargs['clarify_id'],
                                       kwargs['choices'][pick] if pick is not None else cm.CANCELLED)
            return S(success=True)

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.sent.append(content)
            return S(success=True)

    adapter = Transport()
    src = S(platform=S(value='discord'), profile='3dprinting', user_id='42', chat_id='c', thread_id=None)
    gateway = S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter,
                _session_key_for_source=lambda s: 'session-key')
    started = []

    async def fake_start(target):
        started.append(target['part_id'])
        return True, 'START REQUEST ACCEPTED'
    real_resolve, real_load = print_next.resolve_next, print_next.load_plan
    with mock.patch.object(print_next, 'load_plan', lambda pid: real_load(pid, plans)), \
            mock.patch.object(print_next, 'start', fake_start), \
            mock.patch.object(print_next, 'resolve_next',
                              lambda plan, part_id=None: real_resolve(plan, tmp_path / 'archive', part_id)):
        asyncio.run(mod.print_next_dispatch(S(source=src, text='/print-next ghost', internal=False), gateway))
    return adapter, started


def test_print_next_without_a_plate_asks_which_plate_then_confirms_it(tmp_path):
    from print_handy.hermes import print_next

    adapter, started = _plate_hook(tmp_path, [1, 0])
    plate_question, confirm = adapter.questions
    assert 'Which plate is next?' in plate_question['question']
    assert plate_question['choices'][-1] == print_next.PLATE_LATER_LABEL
    # The pick is a choice, not an approval: the picked plate (the second,
    # not plan order's first) still gets its own bed-clear confirmation.
    assert confirm['choices'] == [print_next.START_LABEL, print_next.CANCEL_LABEL]
    assert 'Tail' in confirm['question']
    assert started == ['tail']


def test_print_next_later_starts_nothing(tmp_path):
    adapter, started = _plate_hook(tmp_path, [2])
    assert len(adapter.questions) == 1 and started == []
    assert 'Nothing uploaded or started' in adapter.sent[-1]
