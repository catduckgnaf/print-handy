"""Roasts on request in plain language (#1002): "roast my last print".

Request parsing goes through the shared fuzzy layer (typos), the print is
resolved the way 'print 84' and /print-again <name> resolve it, a fuzzy
near miss is a "Did you mean" list (never a roast of a guess), the print's
own record picks the status bank, and nothing calls a model.
"""
import json
import random

import pytest

from print_handy.runtime import print_control as pc
from print_handy.runtime import print_records
from print_handy.runtime import roast_chat


@pytest.mark.parametrize("text,query", [
    ("roast my last print", ""),
    ("Roast it!", ""),
    ("can you roast that one?", ""),
    ("please roast the last one", ""),
    ("raost my lasst pirnt", ""),       # typos, through fuzzy.correct_typos
    ("rosat print 12", "12"),
    ("roast print 84", "84"),
    ("roast #84", "84"),
    ("roast print number 7", "7"),
    ("roast my benchy", "benchy"),
    ("roast the Dragon print", "Dragon"),
])
def test_roast_requests_are_understood(text, query):
    assert roast_chat.roast_request(text) == query


@pytest.mark.parametrize("text", [
    "", "/print-again", "I love a good roast", "the roast was funny", "roasted marshmallows",
    "roast https://printables.com/model/1", "what does roast mean",
])
def test_other_chat_is_not_a_roast_request(text):
    assert roast_chat.roast_request(text) is None


@pytest.fixture()
def history(tmp_path, monkeypatch):
    prints = tmp_path / "Prints"
    prints.mkdir()
    monkeypatch.setattr(print_records, "PRINTS_DIR", prints)
    monkeypatch.setattr(pc, "_load_number_registry",
                        lambda numbers_path=None: {"numbers": {}, "launches": []})
    rows = []
    for index, title, status in ((3, "Benchy", "completed"), (2, "Dragon Egg", "failed"),
                                 (1, "Cable Clip", "completed")):
        job_dir = tmp_path / f"job{index}"
        job_dir.mkdir()
        (job_dir / "JOB.json").write_text(json.dumps({"title": title}))
        (prints / f"print-{index:04d}.json").write_text(json.dumps({
            "print_number": index, "title": title, "status": status,
            "print_duration_seconds": 5400, "filament_used_g": 40.0,
            "filament_material": "PLA", "completed_at": "2026-11-06T02:00:00Z"}))
        rows.append({"index": index, "title": title, "job_json": job_dir / "JOB.json",
                     "record": job_dir / "x.launch.json", "launched_at": float(index)})
    return rows


def test_last_print_by_number_and_by_name(history, tmp_path):
    state = tmp_path / "roast.json"
    reply = roast_chat.roast_reply("roast my last print", rows=history, state_path=state,
                                   rng=random.Random(1))
    assert reply.startswith("Print #3, Benchy: ")
    assert roast_chat.roast_reply("roast print 1", rows=history, state_path=state).startswith("Print #1, Cable Clip: ")
    assert roast_chat.roast_reply("roast the cable clip", rows=history, state_path=state).startswith("Print #1, ")


def test_a_failed_print_gets_its_status_bank(history, tmp_path):
    from print_handy.core import roast as engine
    reply = roast_chat.roast_reply("roast print 2", rows=history, state_path=tmp_path / "s.json",
                                   rng=random.Random(4))
    line = reply.split(": ", 1)[1]
    ctx = engine.build_context("Dragon Egg", duration_s=5400, material="PLA", grams=40,
                               print_number=2, finish_hour=2)
    corpus = engine.load_corpus()
    failed = {engine.render(t, ctx) for _id, t in corpus["banks"]["failed"]}
    failed |= {framed for text in (t for _id, t in corpus["banks"]["failed"])
               if (rendered := engine.render(text, ctx))
               for framed in engine.framing_options(rendered, "failed", ctx, corpus)}
    assert line in failed


def test_unknown_number_and_near_miss_name_are_answered_not_guessed(history, tmp_path):
    state = tmp_path / "s.json"
    assert "There is no print #99" in roast_chat.roast_reply("roast print 99", rows=history, state_path=state)
    near = roast_chat.roast_reply("roast the dragn egg", rows=history, state_path=state)
    assert near.startswith("Nothing printed is called") and "2. Dragon Egg" in near
    assert "roast print <number>" in near
    assert roast_chat.roast_reply("roast it", rows=[], state_path=state).startswith("Nothing has been printed yet")


def test_no_model_is_ever_called(history, tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("no network or model call for a roast")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    from print_handy import ai_routes
    monkeypatch.setattr(ai_routes, "call", boom)
    assert roast_chat.roast_reply("roast my last print", rows=history, state_path=tmp_path / "s.json")


# --- #1002 Phase 2: history facts, color & dark humor chat roasts ---

def test_history_facts_calculation(tmp_path, monkeypatch):
    prints = tmp_path / "Prints"
    prints.mkdir()
    monkeypatch.setattr(print_records, "PRINTS_DIR", prints)

    # Write a sequence of historical records: 3 prints of Black, then 2 failures
    records = [
        {"print_number": 1, "title": "Baseplate", "status": "completed", "filament_color": "Black", "gcode_file": "base.gcode"},
        {"print_number": 2, "title": "Gears", "status": "failed", "filament_color": "Black", "gcode_file": "gear.gcode"},
        {"print_number": 3, "title": "Gears retry", "status": "failed", "filament_color": "Black", "gcode_file": "gear.gcode"},
    ]
    for r in records:
        (prints / f"print-{r['print_number']:04d}.json").write_text(json.dumps(r))

    facts = roast_chat.history_facts("Gears final", current_gcode="gear.gcode", current_color="Black", status="completed")
    assert facts.get("last_title") == "Gears retry"
    assert facts.get("prev_fails") == 2
    assert facts.get("fail_streak") == 2
    assert facts.get("color_streak") == 4
    assert facts.get("top_color") == "Black"


def test_roast_chat_color_and_dark_humor(history, tmp_path):
    from print_handy.core import roast as engine
    state = tmp_path / "state.json"
    # Overwrite row 3 record with Silk Gold color
    record_path = tmp_path / "Prints" / "print-0003.json"
    data = json.loads(record_path.read_text())
    data["filament_color"] = "Silk Gold"
    record_path.write_text(json.dumps(data))

    reply = roast_chat.roast_reply("roast my last print", rows=history, state_path=state, rng=random.Random(10))
    assert reply.startswith("Print #3, Benchy: ")
    line = reply.split(": ", 1)[1]
    assert line
    # Contract holds
    assert engine.contract_problem(line) is None


# --- #1002 Phase 3: conversational roast phrasings, status/defect & typo resolution ---

@pytest.mark.parametrize("text,expected", [
    ("give me a roast", ""),
    ("give my print a roast", ""),
    ("burn my last print", ""),
    ("burn my failed print", "failed"),
    ("roast this disaster", ""),
    ("raost my print", ""),
    ("roast my failed print", "failed"),
    ("roast the spaghetti", "spaghetti"),
    ("roast my spegatti", "spaghetti"),
])
def test_phase3_conversational_roast_phrasings(text, expected):
    assert roast_chat.roast_request(text) == expected


def test_phase3_resolve_status_defect_and_typo_targets(history):
    # Status and defect target resolution finds the newest matching failed print (#2, Dragon Egg)
    assert roast_chat.resolve("failed", history)["index"] == 2
    assert roast_chat.resolve("failed print", history)["index"] == 2
    assert roast_chat.resolve("spaghetti", history)["index"] == 2
    # Near-miss model typos surface the matching printed model in the pick-list, never guessing
    with pytest.raises(roast_chat.RoastLookupError, match="3. Benchy"):
        roast_chat.resolve("becnhy", history)
    with pytest.raises(roast_chat.RoastLookupError, match="2. Dragon Egg"):
        roast_chat.resolve("dragn", history)

