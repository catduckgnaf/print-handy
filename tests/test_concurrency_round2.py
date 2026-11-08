"""Concurrency round 2: the races round 1 did not stage.

(a) Two printers bind different spools against ONE Spoolman-shaped fake
    at the same moment — the ph_slot claims and each printer's own state
    file must both survive (no lost update).
(b) A print start (the /print-again path lands on the same start seam)
    issued while the printer is printing gets the documented refusal,
    asserted on state: nothing uploaded, nothing recorded, the job
    untouched.
(c) Two authorized users press the same approval prompt in one shared
    channel at the same moment — exactly one claim lands, at both the
    prompt-registry layer and the adapter layer.
(d) A record file vanishing mid-read (pruning) while Wrapped loads:
    the loader names it skipped and returns only whole records — never
    a torn one. Gap named: print_handy has no records-pruning function
    of its own (only timelapse/log/session pruning exists), so the
    prune is staged by deleting the file mid-load, which is the exact
    filesystem race a pruner would create.

Races are staged with barriers, not sleeps.
"""
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.bot.platform import BasePlatformAdapter, SendResult
from print_handy.bot.rest import split_media
from print_handy.core import print_stats, prompts
from print_handy.hermes import spool_sync


# --------------------------------------------------------------- (a) Spoolman race

class FakeSpoolman:
    """The stateful fake from tests/hermes/test_spool_reconcile.py, made
    thread-safe and given a rendezvous: the first per-spool GET for each
    target spool waits at a barrier, so both binds are provably inside
    Spoolman traffic at the same time."""

    def __init__(self, spools):
        self.lock = threading.Lock()
        self.spools = {s["id"]: dict(s) for s in spools}
        self.fields = [{"name": "ph_slot", "key": "ph_slot",
                        "field_type": "text", "default_value": '""'}]
        self.barrier = threading.Barrier(2)
        self._rendezvoused = set()

    def request(self, method, path, body=None):
        if method == "GET" and path.startswith("/api/v1/spool/"):
            spool_id = int(path.rsplit("/", 1)[1])
            with self.lock:
                first = spool_id in (11, 21) and spool_id not in self._rendezvoused
                if first:
                    self._rendezvoused.add(spool_id)
            if first:
                self.barrier.wait(timeout=15)
        with self.lock:
            if method == "GET" and path.startswith("/api/v1/field/"):
                return {"total": len(self.fields), "items": [dict(f) for f in self.fields]}
            if method == "POST" and path == "/api/v1/field/spool":
                field = {"name": body["name"], "key": body["key"],
                         "field_type": body.get("field_type", "text"),
                         "default_value": body.get("default_value", '""')}
                self.fields.append(field)
                return dict(field)
            if method == "GET" and path.startswith("/api/v1/spool?"):
                return {"total": len(self.spools),
                        "items": [dict(s) for s in self.spools.values()]}
            if method == "GET" and path.startswith("/api/v1/spool/"):
                row = self.spools.get(int(path.rsplit("/", 1)[1]))
                return dict(row) if row else None
            if method == "PATCH" and path.startswith("/api/v1/spool/"):
                row = self.spools[int(path.rsplit("/", 1)[1])]
                for key, value in (body or {}).items():
                    if key == "extra" and isinstance(value, dict):
                        row.setdefault("extra", {}).update(value)
                    else:
                        row[key] = value
                return dict(row)
            if method == "DELETE" and "/extra/" in path:
                return {}
        raise AssertionError(f"unexpected Spoolman call {method} {path}")

    def ph_slot(self, spool_id):
        return json.loads(self.spools[spool_id].get("extra", {}).get("ph_slot", '""'))


def _mr():
    def call(method, path, body=None, timeout=10):
        if path.startswith("/printer/objects/query"):
            return {"result": {"status": {"print_stats": {"state": "standby"}}}}
        return {"result": {}}
    return S(call=call)


def _spool(spool_id):
    return {"id": spool_id, "registered": "2026-01-01T00:00:00Z", "archived": False,
            "filament": {"id": 1, "name": "PLA", "material": "PLA", "density": 1.24,
                         "diameter": 1.75, "vendor": {"id": 1, "name": "Generic"}},
            "remaining_weight": 900.0, "used_weight": 100.0, "extra": {}}


def test_two_printers_binding_against_one_spoolman_lose_nothing(tmp_path):
    fake = FakeSpoolman([_spool(11), _spool(21)])
    client = fake.request  # spool_sync calls its client: client(method, path, body)
    states = {"printer-a": tmp_path / "a" / "hardware.json",
              "printer-b": tmp_path / "b" / "hardware.json"}
    for path in states.values():
        path.parent.mkdir(parents=True)
        path.write_text("{}")  # bind_slot's state reader requires the file to exist
    errors = []

    def bind(printer_id, spool_id):
        try:
            spool_sync.bind_slot(1, spool_id, client=client, mr=_mr(),
                                 state_path=states[printer_id], printer_id=printer_id)
        except Exception as exc:  # collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=bind, args=("printer-a", 11)),
               threading.Thread(target=bind, args=("printer-b", 21))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a bind deadlocked"
    assert errors == []
    # Both claims landed on the shared Spoolman — neither overwrote the other.
    assert fake.ph_slot(11) == "printer-a:1"
    assert fake.ph_slot(21) == "printer-b:1"
    # And each printer's own state file kept its own spool.
    for printer_id, spool_id in (("printer-a", 11), ("printer-b", 21)):
        state = json.loads(states[printer_id].read_text())
        assert state["filament_slots"]["1"]["spoolman_id"] == spool_id


# ------------------------------------------------- (b) start while printing

def test_start_while_printing_is_refused_and_touches_nothing(tmp_path, monkeypatch):
    from print_handy.core import model_job as job

    folder = tmp_path / "job"
    (folder / "out").mkdir(parents=True)
    gcode = folder / "out" / "x.gcode"
    gcode.write_text("G1 X0\n")
    job_doc = {"title": "Race Widget", "gcode": str(gcode)}
    (folder / "JOB.json").write_text(json.dumps(job_doc))

    uploads, starts = [], []
    monkeypatch.setattr(job, "START_CLAIMS", S(
        claim=lambda *a, **k: S(release=lambda: None),
        release=lambda *a, **k: None,
        record=lambda *a, **k: starts.append(a)))
    monkeypatch.setattr(job.printer_state, "load",
                        lambda *a, **k: {"moonraker_url": "http://m"})
    monkeypatch.setattr(job, "_printer_phase",
                        lambda cfg, printer=None: ("printing", "orig.gcode", "Printing"))
    monkeypatch.setattr(job, "verify_gcode", lambda *a, **k: None)
    monkeypatch.setattr(job, "_verify_filament_readiness", lambda *a, **k: None)
    monkeypatch.setattr(job, "_mr", lambda cfg: S(call=lambda *a, **k: {"result": {}}))
    monkeypatch.setattr(job, "_wait_printing", lambda *a, **k: None)
    monkeypatch.setattr(job, "record_start", lambda *a, **k: starts.append(a))
    monkeypatch.setattr(job, "live_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(job, "mesh_readiness", lambda *a, **k: {"action": "skip"})
    monkeypatch.setattr(job, "screen_z_offset", lambda *a, **k: None)
    monkeypatch.setattr(job, "_announce_print_name", lambda *a, **k: None)
    monkeypatch.setattr(job, "_publish_job_card", lambda *a, **k: None)
    monkeypatch.setattr(job.subprocess, "run",
                        lambda *a, **k: uploads.append(a) or S(returncode=0, stdout="", stderr=""))

    with pytest.raises(SystemExit, match=r"REFUSED: printer is busy \(printing, orig\.gcode\)"):
        job.print_now(folder / "JOB.json")
    # State-level proof of the refusal: no upload ran, no start was
    # recorded or claimed, and the job document is byte-identical.
    assert uploads == [] and starts == []
    assert json.loads((folder / "JOB.json").read_text()) == job_doc


# ------------------------------------------------- (c) two users, one prompt

class _PressAdapter(BasePlatformAdapter):
    name = "fake"
    message_limit = 2000
    supports_buttons = True

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        body, _files = split_media(content)
        return SendResult(True, message_id="1")

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        self._record_prompt(clarify_id, {"choices": list(choices), "question": question})
        return SendResult(True, message_id=str(clarify_id))

    async def close_prompt(self, clarify_id, note):
        self._prompts.pop(str(clarify_id), None)


def _press_concurrently(fn, args_list):
    barrier = threading.Barrier(len(args_list))
    results, errors = [], []

    def worker(args):
        try:
            barrier.wait(timeout=15)
            results.append(fn(*args))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(a,)) for a in args_list]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == [] and not any(t.is_alive() for t in threads)
    return results


def test_two_users_pressing_one_approval_prompt_claim_once():
    prompts.register("race-cid", "shared-session", "Bed clear?",
                     ["Yes, print", "No"], approval=True)
    adapter = _PressAdapter(["user-a", "user-b"], approver=["user-a", "user-b"])
    asyncio.run(adapter.send_clarify("chan", "Bed clear?", ["Yes, print", "No"],
                                     "race-cid", "shared-session"))

    def press(user_id):
        return asyncio.run(adapter.answer_choice("race-cid", 0, user_id=user_id))

    results = _press_concurrently(press, [("user-a",), ("user-b",)])
    # Exactly one press resolves the prompt; the other is refused as
    # already answered, and the answer is delivered exactly once.
    assert sorted(r is None for r in results) == [False, True]
    assert "Yes, print" in results
    assert prompts.wait_for_response("race-cid", timeout=1) == "Yes, print"
    assert not prompts.is_open("race-cid")


def test_two_answers_at_the_registry_layer_claim_once():
    prompts.register("race-cid-2", "shared-session", "Proceed?", ["Go", "Stop"])

    results = _press_concurrently(
        lambda answer: prompts.resolve_gateway_clarify("race-cid-2", answer),
        [("Go",), ("Go",)])
    assert sorted(results) == [False, True]
    assert prompts.wait_for_response("race-cid-2", timeout=1) == "Go"


# ------------------------------------------- (d) prune during a Wrapped read

def _record(number, when):
    return {"print_number": number, "title": "Wrapped Widget", "status": "completed",
            "success": True, "started_at": when, "completed_at": when,
            "print_duration_seconds": 60.0, "filament_used_g": 5.0,
            "filament_material": "PLA", "printer": "AD5X"}


def test_a_record_deleted_mid_load_is_named_skipped_never_torn(tmp_path, monkeypatch):
    prints = tmp_path / "Prints"
    prints.mkdir()
    (prints / "print-0001.json").write_text(json.dumps(_record(1, "2026-03-01T10:00:00Z")))
    (prints / "print-0002.json").write_text(json.dumps(_record(2, "2026-03-02T10:00:00Z")))
    (prints / "print-0003.json").write_text(json.dumps(_record(3, "2026-03-03T10:00:00Z")))

    real_loads = json.loads
    pruned = []

    def prune_on_first_parse(text, *args, **kwargs):
        if not pruned:
            pruned.append(True)
            (prints / "print-0002.json").unlink()  # the pruner strikes mid-read
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(print_stats.json, "loads", prune_on_first_parse)
    records, skipped = print_stats.load_records_from_dir(prints)
    assert pruned, "the prune never fired — the race was not staged"
    assert skipped == ["print-0002.json"]
    # Every returned record is whole, and the aggregate over the snapshot
    # is self-consistent: totals count exactly the records returned.
    assert all(set(r) >= {"print_number", "status", "started_at"} for r in records)
    stats = print_stats.aggregate(records)
    assert stats["total_prints"] == len(records) == 2


def test_a_torn_record_file_is_named_skipped_and_contributes_nothing(tmp_path):
    prints = tmp_path / "Prints"
    prints.mkdir()
    (prints / "print-0001.json").write_text(json.dumps(_record(1, "2026-03-01T10:00:00Z")))
    torn = json.dumps(_record(2, "2026-03-02T10:00:00Z"))
    (prints / "print-0002.json").write_text(torn[: len(torn) // 2])  # half-written
    records, skipped = print_stats.load_records_from_dir(prints)
    assert skipped == ["print-0002.json"]
    assert [r["print_number"] for r in records] == [1]
    assert print_stats.aggregate(records)["total_prints"] == 1
