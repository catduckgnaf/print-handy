"""Focused offline tests for the shared HelixScreen print-cost contract."""
from email.message import Message
from urllib.error import HTTPError

from print_handy.core import cost as cost_engine
from print_handy.core import cost_publish

BASE = "http://moonraker.invalid"
NOW = 1731970100


def estimate():
    spool = {"id": 12, "price": 25.0, "initial_weight": 1000,
             "filament": {"name": "PLA Red", "material": "PLA", "price": 20.0, "weight": 1000}}
    line = cost_engine.MaterialLine(name="PLA Red", material="PLA", grams=50,
                                    spool=spool, spool_id=12)
    result = cost_engine.compute(cost_engine.JobCostInput(lines=[line], print_hours=3),
                                 cost_engine.PrinterCostProfile(), cost_engine.WorkspaceCost())
    return cost_engine.freeze(result)


def actual():
    result = estimate()
    result["outcome"] = "completed"
    return result


def test_contract_and_encoded_key_and_only_filament_plus_electricity():
    data = estimate()
    record = cost_publish.build_record("parts/benchy.gcode", 1731970000.5, data, at=NOW)
    assert record == {
        "version": 1, "source": "print-handy", "filename": "parts/benchy.gcode",
        "file_modified": 1731970000.5, "currency": "USD",
        "estimate": {
            "total": record["estimate"]["filament"] + record["estimate"]["electricity"],
            "filament": data["lines"][0]["amount"],
            "electricity": data["lines"][1]["amount"], "at": NOW,
            "tools": [{"tool": -1, "grams": 50.0,
                       "cost": data["material_lines"][0]["amount"], "price_source": "spoolman"}],
        },
    }
    assert cost_publish.record_key("parts/benchy.gcode") == "print_cost.parts%2Fbenchy%2Egcode"
    assert cost_publish.record_key("x% .gcode") == "print_cost.x%25%20%2E gcode".replace("%2E ", "%2E")


def test_actual_preserves_estimate_only_for_same_file_revision():
    estimate_record = cost_publish.build_record("parts/benchy.gcode", 10.0, estimate(), at=20)
    record = cost_publish.build_record("parts/benchy.gcode", 10.0, actual(), at=30,
                                       existing=estimate_record)
    assert record["estimate"]["at"] == 20
    assert record["actual"]["at"] == 30
    changed = cost_publish.build_record("parts/benchy.gcode", 12.0, actual(), at=40,
                                        existing=record)
    assert "estimate" not in changed
    assert "actual" in changed


def test_contract_is_fail_soft_without_verified_metadata_or_valid_cost():
    assert cost_publish.build_record("x.gcode", None, estimate(), at=1) is None
    assert cost_publish.build_record("x.gcode", 1, {"currency_code": "USD"}, at=1) is None
    bad_currency = estimate()
    bad_currency["currency_code"] = "usd"
    assert cost_publish.build_record("x.gcode", 1, bad_currency, at=1) is None


def test_completion_publishes_snapshot_with_real_finish_time(monkeypatch):
    import threading
    from print_handy.runtime import moonraker_discord_announcements as announcements

    class ImmediateThread:
        def __init__(self, *, target, **_kwargs):
            self.target = target

        def start(self):
            self.target()

    calls = []
    monkeypatch.setattr(threading, "Thread", ImmediateThread)
    monkeypatch.setattr(cost_publish, "_read", lambda *_args: None)
    monkeypatch.setattr(cost_publish, "_post", lambda _base, payload: calls.append(payload))
    data = {"cost": actual(), "started_at": "1970-01-01T00:01:40Z",
            "completed_at": "1970-01-01T00:02:00Z"}
    announcements._publish_frozen_cost("parts/benchy.gcode", data, {"modified": 99.5})
    announcements._publish_frozen_cost("parts/benchy.gcode", data, {"modified": 110})
    assert len(calls) == 1
    assert calls[0]["value"]["file_modified"] == 99.5
    assert calls[0]["value"]["actual"]["at"] == 120


def test_publish_wire_envelope():
    wire = []
    assert cost_publish.publish(BASE, "parts/benchy.gcode", 99.5, estimate(),
                                post=lambda base, payload: wire.append(payload), at=100)
    payload = wire[0]
    assert payload["namespace"] == "helix-screen"
    assert payload["key"] == "print_cost.parts%2Fbenchy%2Egcode"
    assert payload["value"]["file_modified"] == 99.5


def test_first_write_accepts_missing_database_item(monkeypatch):
    def missing(base, path, **_kwargs):
        raise HTTPError(base + path, 404, "Key not found", Message(), None)

    monkeypatch.setattr(cost_publish._transport, "moonraker_json", missing)
    assert cost_publish._read(BASE, "parts/benchy.gcode") is None


def test_price_source_only_uses_supported_values():
    line = estimate()["material_lines"][0]
    record = cost_publish.build_record("x.gcode", 1, estimate(), at=2)
    assert record["estimate"]["tools"][0]["price_source"] == "spoolman"
    line["price_source"] = "unrecognized provenance"
    assert cost_publish._price_source(line["price_source"]) == "setting"
