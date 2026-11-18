"""#1643: Print Handy publishes cost to Moonraker so HelixScreen reads the same number.

Covers the proposed ``print_handy_cost`` namespace record: the estimate
written when a file is sent, the frozen cost replacing it at completion
under the same key, per-tool lines with price sources, and the
fail-soft rule that a Moonraker write never reaches the send or
completion path. Non-Moonraker printers publish nothing: the send hook
lives only in the Moonraker launcher and the completion hook only in
the Moonraker announcements path, and publish() itself no-ops without
a Moonraker base URL.
"""
from datetime import datetime, timezone

import pytest

from print_handy.core import cost as cost_engine
from print_handy.core import cost_publish

BASE = "http://printer.invalid:7125"
NOW = datetime(2026, 11, 18, 3, 0, 0, tzinfo=timezone.utc)


def _estimate_result():
    """A real engine estimate: 50 g of PLA priced by its Spoolman spool, 3 h."""
    spool = {"id": 12, "price": 25.0, "initial_weight": 1000,
             "filament": {"name": "PLA Red", "material": "PLA", "price": 20.0,
                          "weight": 1000, "color_hex": "FF0000"}}
    line = cost_engine.MaterialLine(name="PLA Red", material="PLA", grams=50.0,
                                    spool=spool, spool_id=12)
    job = cost_engine.JobCostInput(lines=[line], print_hours=3.0)
    return cost_engine.compute(job, cost_engine.PrinterCostProfile(),
                               cost_engine.WorkspaceCost())


def _frozen_completed():
    result = _estimate_result()
    data = cost_engine.freeze(result)
    data["outcome"] = "completed"
    return data


class _FakePost:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, base_url, payload):
        self.calls.append((base_url, payload))
        if self.fail:
            raise RuntimeError("moonraker unreachable")
        return {"result": {}}


def test_record_shape_from_estimate():
    result = _estimate_result()
    record = cost_publish.build_record("benchy.gcode", cost_engine.freeze(result), now=NOW)
    assert record["schema"] == 1
    assert record["state"] == "estimate"
    assert record["outcome"] == "estimate"
    assert record["file"] == "benchy.gcode"
    assert record["out_of_pocket"] == result.out_of_pocket
    assert record["full_cost"] == result.cost_per_build
    assert record["cost_per_part"] == result.cost_per_part
    assert record["parts_per_build"] == 1
    assert record["currency"] == "USD"
    assert record["updated_at"] == "2026-11-18T03:00:00Z"
    keys = [entry["key"] for entry in record["lines"]]
    assert "material" in keys and "electricity" in keys and "consumables" in keys


def test_record_carries_per_tool_lines_and_price_sources():
    record = cost_publish.build_record("benchy.gcode", _frozen_completed(), now=NOW)
    assert record["state"] == "final"
    assert record["outcome"] == "completed"
    (tool,) = record["tools"]
    assert tool["name"] == "PLA Red"
    assert tool["material"] == "PLA"
    assert tool["grams"] == 50.0
    assert tool["amount"] == 1.25
    assert tool["spool_id"] == 12
    assert tool["price_source"] == "spool price (Spoolman spool 12)"
    assert tool["price_source_code"] == "spoolman_spool"


def test_price_source_codes_cover_the_engine_labels():
    assert cost_publish.price_source_code("spool price (Spoolman spool 3)") == "spoolman_spool"
    assert cost_publish.price_source_code("filament profile price (Spoolman)") == "spoolman_filament"
    assert cost_publish.price_source_code("bottle price by volume (Spoolman)") == "spoolman_bottle"
    assert cost_publish.price_source_code("slicer-embedded price") == "slicer"
    assert cost_publish.price_source_code("manual price") == "manual"
    assert cost_publish.price_source_code("workspace default cost per kilogram") == "default"
    assert cost_publish.price_source_code("something unlisted") == "unknown"


def test_publish_writes_namespace_with_literal_file_name_key():
    post = _FakePost()
    name = "sub folder/benchy v2.gcode"
    assert cost_publish.publish(BASE, name, cost_engine.freeze(_estimate_result()),
                                 post=post, now=NOW) is True
    (base, payload), = post.calls
    assert base == BASE
    assert payload["namespace"] == "print_handy_cost"
    # The array form keeps the whole name, dots and slashes included, as
    # one literal field (a string key would be split on "." by Moonraker).
    assert payload["key"] == [name]
    assert payload["value"]["state"] == "estimate"
    assert payload["value"]["file"] == name


def test_frozen_cost_replaces_estimate_under_the_same_key():
    post = _FakePost()
    assert cost_publish.publish(BASE, "benchy.gcode", cost_engine.freeze(_estimate_result()),
                                 post=post, now=NOW)
    assert cost_publish.publish(BASE, "benchy.gcode", _frozen_completed(), post=post, now=NOW)
    assert len(post.calls) == 2
    keys = [(payload["namespace"], payload["key"]) for _base, payload in post.calls]
    assert keys[0] == keys[1] == ("print_handy_cost", ["benchy.gcode"])
    assert post.calls[0][1]["value"]["state"] == "estimate"
    final = post.calls[1][1]["value"]
    assert final["state"] == "final"
    assert final["outcome"] == "completed"
    assert final["full_cost"] == _frozen_completed()["cost_per_build"]


def test_publish_failure_is_swallowed():
    post = _FakePost(fail=True)
    assert cost_publish.publish(BASE, "benchy.gcode", _frozen_completed(),
                                 post=post, now=NOW) is False


def test_publish_noops_without_moonraker_or_cost():
    post = _FakePost()
    # No base URL: a printer that is not Moonraker-backed publishes nothing.
    assert cost_publish.publish("", "benchy.gcode", _frozen_completed(), post=post) is False
    assert cost_publish.publish(None, "benchy.gcode", _frozen_completed(), post=post) is False
    # No cost record, and no file name: nothing to publish.
    assert cost_publish.publish(BASE, "benchy.gcode", None, post=post) is False
    assert cost_publish.publish(BASE, "", _frozen_completed(), post=post) is False
    assert post.calls == []


def test_send_hook_publishes_the_estimate(monkeypatch):
    from print_handy.launch import print_now

    captured = {}

    def fake_publish(base_url, filename, cost, **kwargs):
        captured.update(base=base_url, filename=filename, cost=cost)
        return True

    monkeypatch.setattr(cost_engine, "estimate_for_job",
                        lambda job, data, spool_lookup=None: _estimate_result())
    monkeypatch.setattr(cost_publish, "publish", fake_publish)
    monkeypatch.setattr(print_now.settings, "current", lambda: {})

    st = {"moonraker_url": BASE}
    assert print_now._publish_cost_estimate(st, {"title": "Benchy"}, "benchy.gcode") is True
    assert captured["base"] == BASE
    assert captured["filename"] == "benchy.gcode"
    assert captured["cost"]["outcome"] == "estimate"
    assert captured["cost"]["cost_per_build"] == _estimate_result().cost_per_build


def test_send_hook_publishes_nothing_when_unpriceable(monkeypatch):
    from print_handy.launch import print_now

    calls = []
    monkeypatch.setattr(cost_engine, "estimate_for_job",
                        lambda job, data, spool_lookup=None: None)
    monkeypatch.setattr(cost_publish, "publish", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(print_now.settings, "current", lambda: {})

    assert print_now._publish_cost_estimate({"moonraker_url": BASE}, {}, "x.gcode") is None
    assert calls == []


def test_send_hook_failure_never_reaches_the_launch(monkeypatch):
    from print_handy.launch import print_now

    def boom(job, data, spool_lookup=None):
        raise RuntimeError("pricing exploded")

    monkeypatch.setattr(cost_engine, "estimate_for_job", boom)
    monkeypatch.setattr(print_now.settings, "current", lambda: {})
    assert print_now._publish_cost_estimate({"moonraker_url": BASE}, {}, "x.gcode") is None


def _record_completion(monkeypatch, data):
    from print_handy.runtime import moonraker_discord_announcements as announcer

    monkeypatch.setattr(announcer, "load_context", lambda job_key: {})
    monkeypatch.setattr(announcer, "get_file_metadata", lambda filename: {})
    monkeypatch.setattr(announcer, "get_active_spool", lambda: {})
    monkeypatch.setattr(announcer, "build_record_data", lambda *a, **k: data)
    monkeypatch.setattr(announcer, "write_completed_record", lambda record: {})
    return announcer


def test_completion_hook_publishes_the_frozen_cost(monkeypatch):
    frozen = _frozen_completed()
    data = {"cost": frozen, "completed_at": "2026-11-18T03:00:00Z",
            "gcode_file": "benchy.gcode"}
    announcer = _record_completion(monkeypatch, data)

    captured = {}
    monkeypatch.setattr(cost_publish, "publish",
                        lambda base, filename, cost, **k: captured.update(
                            base=base, filename=filename, cost=cost) or True)
    monkeypatch.setattr(announcer, "BASE", BASE)

    result = announcer.record_completion("benchy.gcode", "benchy.gcode",
                                         {"state": "complete", "start_time": 1700000000},
                                         completed_at="2026-11-18T03:00:00Z")
    assert result["completed_at"] == "2026-11-18T03:00:00Z"
    _join_publish_threads()
    assert captured["base"] == BASE
    assert captured["filename"] == "benchy.gcode"
    assert captured["cost"] is frozen


def test_completion_hook_without_cost_publishes_nothing(monkeypatch):
    data = {"cost": None, "completed_at": "2026-11-18T03:00:00Z",
            "gcode_file": "benchy.gcode"}
    announcer = _record_completion(monkeypatch, data)

    post = _FakePost()
    monkeypatch.setattr(announcer, "BASE", BASE)
    result = announcer.record_completion("benchy.gcode", "benchy.gcode",
                                         {"state": "complete", "start_time": 1700000000},
                                         completed_at="2026-11-18T03:00:00Z")
    assert result["completed_at"] == "2026-11-18T03:00:00Z"
    # publish() no-ops on a None cost before any transport is touched;
    # the real publish with the fake transport proves no call goes out.
    assert cost_publish.publish(BASE, "benchy.gcode", None, post=post) is False
    assert post.calls == []


def test_completion_publish_failure_never_reaches_the_completion(monkeypatch):
    from print_handy.runtime import moonraker_discord_announcements as announcer

    def boom(*a, **k):
        raise RuntimeError("database write exploded")

    monkeypatch.setattr(cost_publish, "publish", boom)
    # The helper swallows anything the publish layer does.
    assert announcer._publish_frozen_cost("benchy.gcode", {"cost": _frozen_completed()}) is None


def _join_publish_threads(timeout=5):
    import threading
    for worker in threading.enumerate():
        if worker.name.startswith("cost-publish-"):
            worker.join(timeout)


def test_a_hanging_moonraker_never_delays_the_send(monkeypatch):
    # duckcatgnaf on #1652: the estimate publish ran inline before submission,
    # so a slow Moonraker held the send for up to the POST timeout.
    import threading
    import time
    from print_handy.launch import print_now

    release, posted = threading.Event(), threading.Event()

    def hanging_post(base_url, payload):
        release.wait(10)
        posted.set()

    monkeypatch.setattr(cost_engine, "estimate_for_job",
                        lambda job, data, spool_lookup=None: _estimate_result())
    monkeypatch.setattr(cost_publish, "_post", hanging_post)
    monkeypatch.setattr(print_now.settings, "current", lambda: {})
    started = time.monotonic()
    worker = print_now._publish_cost_estimate_in_background({"moonraker_url": BASE}, {"title": "Benchy"},
                                                            "benchy.gcode")
    assert time.monotonic() - started < 1.0  # the send continues at once
    assert worker.daemon and not posted.is_set()
    release.set()
    worker.join(5)
    assert posted.is_set()  # the estimate still goes out


def test_the_send_paths_publish_in_the_background():
    import inspect
    from print_handy.launch import print_now
    source = inspect.getsource(print_now)
    assert source.count("        _publish_cost_estimate_in_background(st, job, remote)  # never delays") == 2
    assert "        _publish_cost_estimate(st, job, remote)\n" not in source


def test_a_hanging_moonraker_never_delays_the_completion(monkeypatch):
    import threading
    import time

    data = {"cost": _frozen_completed(), "completed_at": "2026-11-18T03:00:00Z",
            "gcode_file": "benchy.gcode"}
    announcer = _record_completion(monkeypatch, data)
    release, posted = threading.Event(), threading.Event()

    def hanging_post(base_url, payload):
        release.wait(10)
        posted.set()

    monkeypatch.setattr(cost_publish, "_post", hanging_post)
    monkeypatch.setattr(announcer, "BASE", BASE)
    started = time.monotonic()
    result = announcer.record_completion("benchy.gcode", "benchy.gcode",
                                         {"state": "complete", "start_time": 1700000000},
                                         completed_at="2026-11-18T03:00:00Z")
    assert time.monotonic() - started < 1.0 and result["completed_at"] == "2026-11-18T03:00:00Z"
    release.set()
    _join_publish_threads()
    assert posted.is_set()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
