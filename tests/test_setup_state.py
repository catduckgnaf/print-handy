"""Shared setup-completion state (setup_state) and its surface fold-ins."""
import json
from pathlib import Path

import pytest

from print_handy import setup_state


def _data(tmp_path):
    return {"paths": {"hermes_home": tmp_path}}


def test_record_round_trip(tmp_path):
    record = setup_state.record_completion(_data(tmp_path), "chat",
                                           when="2026-11-09T12:00:00+00:00")
    assert record == {"done": True, "surface": "chat",
                      "when": "2026-11-09T12:00:00+00:00"}
    path = tmp_path / "state" / setup_state.COMPLETION_FILE
    assert json.loads(path.read_text()) == record
    assert (path.stat().st_mode & 0o777) == 0o600
    assert setup_state.is_complete(_data(tmp_path))
    assert setup_state.load_completion(_data(tmp_path)) == record
    assert "chat" in setup_state.describe(record)


def test_unknown_surface_refused(tmp_path):
    with pytest.raises(ValueError):
        setup_state.record_completion(_data(tmp_path), "carrier-pigeon")


def test_absent_record_is_not_complete(tmp_path):
    assert setup_state.load_completion(_data(tmp_path)) == {}
    assert not setup_state.is_complete(_data(tmp_path))


def test_walkthrough_done_migrates_on_read(tmp_path):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "webui_walkthrough.json").write_text(json.dumps({"done": True, "step": 5}))
    record = setup_state.load_completion(_data(tmp_path))
    assert record["done"] is True and record["surface"] == "webui"
    assert record["when"] is None and record["migrated"] is True
    assert setup_state.is_complete(_data(tmp_path))


def test_walkthrough_not_done_does_not_migrate(tmp_path):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "webui_walkthrough.json").write_text(json.dumps({"done": False}))
    assert setup_state.load_completion(_data(tmp_path)) == {}


def test_shared_record_wins_over_walkthrough_migration(tmp_path):
    setup_state.record_completion(_data(tmp_path), "wizard", when="2026-11-09T01:00:00+00:00")
    state = tmp_path / "state"
    (state / "webui_walkthrough.json").write_text(json.dumps({"done": True}))
    record = setup_state.load_completion(_data(tmp_path))
    assert record["surface"] == "wizard" and "migrated" not in record


def test_corrupt_record_reads_as_incomplete(tmp_path):
    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / setup_state.COMPLETION_FILE).write_text("{not json")
    assert setup_state.load_completion(_data(tmp_path)) == {}


def test_string_home_shape_is_accepted(tmp_path):
    data = {"paths": {"hermes_home": str(tmp_path)}}
    setup_state.record_completion(data, "webui", when="2026-11-09T02:00:00+00:00")
    assert setup_state.is_complete(data)


# -- surface fold-ins -------------------------------------------------------

def test_walkthrough_pending_ends_when_chat_completed(tmp_path):
    from print_handy.webui import walkthrough
    data = _data(tmp_path)
    assert walkthrough.pending(data)
    setup_state.record_completion(data, "chat")
    assert not walkthrough.pending(data)


def test_guided_apply_writes_shared_record(tmp_path):
    from print_handy.bot import guided
    store = guided.SetupStore(tmp_path / "state" / guided.STORE_NAME)
    key = "chan::user"
    guided.handle_text(key, "/print-handy setup", store=store, now=1000.0)
    answers = ["ad5x", "12345", "everyone", "skip", "skip", "skip"]
    # model key must be a real one; discover a moonraker model dynamically
    # by replaying: first answer may fail if the key is wrong, so use the
    # first sorted model key from the registry instead.
    from print_handy import printers
    answers[0] = sorted(printers.MODELS)[0]
    guided.handle_text(key, "/print-handy setup restart", store=store, now=1001.0)
    for index, answer in enumerate(answers):
        guided.handle_text(key, answer, store=store, now=1002.0 + index)
    reply = guided.handle_text(key, "apply", store=store, now=1010.0,
                               apply_config=lambda answers: "written.")
    assert reply.startswith("**Setup complete.**")
    record = setup_state.load_completion_in_state_dir(tmp_path / "state")
    assert record["done"] is True and record["surface"] == "chat"
    # A fresh setup command now reports the earlier completion.
    other = guided.handle_text("chan2::user2", "/print-handy setup",
                               store=store, now=1011.0)
    assert "already completed via chat" in other
    assert "setup restart" in other


def test_wizard_apply_records_completion(tmp_path, monkeypatch):
    from print_handy import setup_wizard
    plan = {"config": {}, "flatpaks": [], "actions": [], "notes": [],
            "secrets": {}, "data_home": str(tmp_path / "home"),
            "display": "x", "material": "PLA", "slicer": "orca",
            "timezone": "UTC", "printers": []}

    class W:
        def say(self, text=""):
            pass

        def confirm(self, key, prompt, default=True):
            return False

    class Result:
        returncode = 0

    setup_wizard.apply(plan, W(), repo=tmp_path, local=tmp_path / "local.yaml",
                       runner=lambda cmd: Result())
    record = setup_state.load_completion_in_state_dir(tmp_path / "home" / "state")
    assert record["done"] is True and record["surface"] == "wizard"
