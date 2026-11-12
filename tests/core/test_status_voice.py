"""Status voice corpus and delivery guarantees, no live printer required."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import random

import pytest

from print_handy.core import status_voice
from print_handy.core.quantity_selection import parse_quantity

STATUS_VOICE_LIVE = True  # tests/conftest.py: this module tests the real selector


def test_shipped_corpus_is_exactly_one_thousand_unique_lines_in_25_stages():
    stages = status_voice.load_bank()
    rows = [row for stage_rows in stages.values() for row in stage_rows]
    assert len(stages) == 25
    assert len(rows) == 1000
    assert all(len(stage_rows) == 40 for stage_rows in stages.values())
    assert len({row["id"] for row in rows}) == 1000
    assert len({row["text"].casefold() for row in rows}) == 1000
    assert all(row["family"] for row in rows)


@pytest.mark.parametrize(
    ("count", "phrase"),
    [
        (2, "Two prints at the same time man."),
        (10, "There are 10 types of people"),
        (11, "These go to eleven."),
    ],
)
def test_easter_egg_is_guaranteed_first_time_and_not_random_later(tmp_path, count, phrase):
    state = tmp_path / "voice.json"
    first = status_voice.quip(
        "quantity_selected", copies=count, identity="discord:user-a",
        event_key="job-first", state_path=state, rng=random.Random(1))
    assert phrase in first
    repeat = status_voice.quip(
        "quantity_selected", copies=count, identity="discord:user-a",
        event_key="job-first", state_path=state, rng=random.Random(100))
    assert repeat == first  # delivery replay does not redraw or eat another joke
    second_job = status_voice.quip(
        "quantity_selected", copies=count, identity="discord:user-a",
        event_key="job-second", state_path=state, rng=random.Random(1))
    assert phrase not in second_job
    another_user = status_voice.quip(
        "quantity_selected", copies=count, identity="discord:user-b",
        event_key="job-first", state_path=state, rng=random.Random(1))
    assert phrase in another_user


def test_specials_only_match_their_exact_verified_quantity(tmp_path):
    state = tmp_path / "voice.json"
    text = status_voice.quip(
        "quantity_selected", copies=9, identity="discord:u",
        event_key="nine", state_path=state)
    assert text
    assert "go to eleven" not in text
    assert "10 types" not in text
    assert "Two prints" not in text
    # Validation at the quantity-choice boundary rejects out-of-capacity
    # selections before the status voice hook can run.
    with pytest.raises(ValueError, match="will not fit"):
        parse_quantity("11", 10)


def test_first_time_specials_are_independent_per_quantity(tmp_path):
    state = tmp_path / "voice.json"
    for number, phrase in [(2, "Two prints"), (10, "10 types"), (11, "eleven")]:
        line = status_voice.quip(
            "quantity_selected", copies=number, identity="discord:same-user",
            event_key=f"job-{number}", state_path=state)
        assert phrase in line


def test_no_stale_fact_claims_and_missing_facts_cannot_render(tmp_path):
    path = tmp_path / "voice.json"
    assert status_voice.quip("quantity_selected", identity="discord:u", state_path=path) == ""
    assert status_voice.quip("quantity_selected", copies=-1, state_path=path) == ""
    assert status_voice.quip("quantity_selected", copies="11", state_path=path) == ""
    assert status_voice.quip("invented_stage", state_path=path) == ""
    line = status_voice.quip("bed_fit", state_path=path, event_key="fit-1")
    assert line
    assert "{copies}" not in line
    assert "fits up to" not in line  # not knowable until fit probe completes


def test_shuffle_bag_yields_variety_before_repeats(tmp_path):
    path = tmp_path / "voice.json"
    result = [
        status_voice.quip("plate_capacity", state_path=path, identity="discord:u",
                          event_key=f"capacity-{i}", rng=random.Random(i))
        for i in range(40)
    ]
    assert len(result) == 40
    assert len(set(result)) == 40


def test_bad_state_is_recoverable_and_replay_is_idempotent(tmp_path):
    path = tmp_path / "voice.json"
    path.write_text("{corrupt", encoding="utf-8")
    a = status_voice.quip("bed_fit", event_key="job1", state_path=path,
                          identity="discord:u", rng=random.Random(3))
    b = status_voice.quip("bed_fit", event_key="job1", state_path=path,
                          identity="discord:u", rng=random.Random(9))
    assert a and a == b
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved["history"]) == 1


def test_thread_concurrency_only_one_first_eleven(tmp_path):
    path = tmp_path / "voice.json"

    def get_line(i):
        return status_voice.quip(
            "quantity_selected", copies=11, identity="discord:shared",
            event_key=f"attempt-{i}", state_path=path, rng=random.Random(i))

    with ThreadPoolExecutor(max_workers=12) as pool:
        lines = list(pool.map(get_line, range(20)))
    assert sum("These go to eleven." in line for line in lines) == 1
    assert all(lines)


def test_authoritative_progress_remains_outside_corpus():
    # These are optional jokes, never safety-critical prompts or commands.
    stages = status_voice.load_bank()
    all_text = " ".join(row["text"].lower() for rows in stages.values() for row in rows)
    assert "press stop to cancel" not in all_text
    assert "click to print now" not in all_text
    assert "click approve to start the print" not in all_text


def test_voiced_appends_after_the_facts_and_keeps_the_stop_instruction_last(tmp_path):
    path = tmp_path / "voice.json"
    line = status_voice.voiced("Slicing.", "slice", event_key="a", state_path=path, rng=random.Random(1))
    assert line.startswith("Slicing. ") and len(line) > len("Slicing. ")
    joke = line[len("Slicing. "):]
    assert joke in {row["text"] for row in status_voice.load_bank()["slice"]}
    # Replay: the same key gives the same line, and a Stop tail stays last.
    stop = status_voice.voiced("Slicing. Press Stop to cancel.", "slice", event_key="a", state_path=path)
    assert stop == f"Slicing. {joke} Press Stop to cancel."
    multi = status_voice.voiced("Print complete.\nPrinter: A1", "slice", separator="\n",
                                event_key="a", state_path=path)
    assert multi == f"Print complete.\nPrinter: A1\n{joke}"


@pytest.mark.parametrize("text", ["Slicing. Press Stop to cancel.", "Checking the model.", "", None])
def test_voiced_failure_leaves_text_byte_identical(tmp_path, monkeypatch, text):
    assert status_voice.voiced(text, "no_such_stage", state_path=tmp_path / "v.json") == text
    monkeypatch.setattr(status_voice, "quip", lambda *a, **k: "")
    assert status_voice.voiced(text, "slice") == text

    def broken(*a, **k):
        raise RuntimeError("selector down")
    monkeypatch.setattr(status_voice, "quip", broken)
    assert status_voice.voiced(text, "slice") == text
