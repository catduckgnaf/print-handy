"""Offline safety and policy tests for the cold-pull control path."""
import time
from unittest.mock import MagicMock

from print_handy.hermes import print_control
from print_handy.hermes import maintenance


def state(status="standby", filament=True, target=0):
    return {
        "print_stats": {"state": status},
        "filament_switch_sensor head_switch_sensor": {"filament_detected": filament},
        "extruder": {"target": target},
    }


def test_coldpull_refuses_printing_and_paused_without_sending():
    for status in ("printing", "paused"):
        sender = MagicMock()
        ok, message = print_control.coldpull(220, 90, confirmed_at=time.time(),
            gcode_sender=sender, reader=lambda status=status: state(status))
        assert not ok and "Refused" in message
        sender.assert_not_called()


def test_coldpull_refuses_missing_head_filament():
    sender = MagicMock()
    ok, message = print_control.coldpull(220, 90, confirmed_at=time.time(),
        gcode_sender=sender, reader=lambda: state(filament=False))
    assert not ok and "Load filament on the screen" in message
    sender.assert_not_called()


def test_coldpull_sends_exact_t0_scripts_and_reads_heater_target():
    for temp, cold in ((220, 90), (245, 100), (220, 95)):
        sent = []
        reads = iter((state(), state(target=0)))
        ok, message = print_control.coldpull(temp, cold, confirmed_at=time.time(),
            gcode_sender=sent.append, reader=lambda: next(reads))
        assert ok and "target is 0" in message
        assert sent == [f"_COLDPULL_LOAD_MATERIAL T=0 TEMP={temp} COLD={cold}"]


def test_coldpull_refuses_if_heater_readback_is_not_off():
    sent = []
    reads = iter((state(), state(target=90)))
    ok, message = print_control.coldpull(220, 90, confirmed_at=time.time(),
        gcode_sender=sent.append, reader=lambda: next(reads))
    assert not ok and "not 0" in message
    assert sent == ["_COLDPULL_LOAD_MATERIAL T=0 TEMP=220 COLD=90"]


def test_result_policy_repeat_retry_and_two_dirty_nozzle_swap():
    assert maintenance.result_guidance("dirty", 0)["offer_repeat"] is True
    assert maintenance.result_guidance("dirty", 1)["suggest_nozzle_swap"] is True
    assert maintenance.result_guidance("snapped")["offer_cold_95"] is True
    assert maintenance.result_guidance("didnt_release")["offer_cold_95"] is True


def test_maintenance_and_standalone_use_same_workflow_source_tags():
    from pathlib import Path
    plugin = Path(__file__).resolve().parents[2] / "plugins/print-feedback/__init__.py"
    text = plugin.read_text()
    assert "_coldpull_workflow(adapter, source, metadata, source_tag='coldpull'," in text
    assert "live_key=gateway._session_key_for_source(source)" in text
    assert "source_tag='maintenance', initial_cold=cold" in text
    assert text.count("pc.coldpull, temp, cold, confirmed_at=confirmed_at") == 1


def test_selected_options_map_to_exact_scripts_before_send(monkeypatch):
    """PLA 220/90, PLA 220/95, and PETG 245/100 are fixed before any gcode send."""
    import asyncio
    import importlib.util
    import sys
    from pathlib import Path

    plugin_path = Path(__file__).resolve().parents[2] / "plugins/print-feedback/__init__.py"
    spec = importlib.util.spec_from_file_location("print_feedback_coldpull_map", plugin_path)
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)

    async def confirm(adapter, source, metadata, key, prompt, choices, timeout, live_key=None):
        return "Confirm cold pull" if key.endswith("-actuate") else None

    monkeypatch.setattr(plugin, "_confirm", confirm)
    monkeypatch.setattr(plugin, "_maintenance_hours_now", lambda: 0.0)
    monkeypatch.setattr("print_handy.hermes.maintenance.load", lambda *args, **kwargs: {"runs": []})
    monkeypatch.setattr("print_handy.hermes.maintenance.append_run", lambda record, **kwargs: record)

    cases = [
        ("PLA (trial) 220/90", None, 220, 90),
        ("Run PLA (trial) cold pull 220/95", 95, 220, 95),
        ("PETG 245/100", None, 245, 100),
    ]
    for selected, initial_cold, temp, cold in cases:
        sent = []

        class Adapter:
            async def send(self, **kwargs):
                return None

        def fake_coldpull(got_temp, got_cold, *, confirmed_at, gcode_sender=None, reader=None):
            sent.append(f"_COLDPULL_LOAD_MATERIAL T=0 TEMP={got_temp} COLD={got_cold}")
            return True, "Cold pull finished; heater target is 0."

        monkeypatch.setattr(plugin, "asyncio", asyncio)
        from print_handy.hermes import print_control as pc
        monkeypatch.setattr(pc, "coldpull", fake_coldpull)
        source = MagicMock()
        source.chat_id = "c"
        asyncio.run(plugin._coldpull_workflow(
            Adapter(), source, None, source_tag="coldpull",
            initial_cold=initial_cold, selected_material=selected,
        ))
        assert sent == [f"_COLDPULL_LOAD_MATERIAL T=0 TEMP={temp} COLD={cold}"]


def _load_coldpull_plugin():
    import importlib.util
    import sys
    from pathlib import Path

    plugin_path = Path(__file__).resolve().parents[2] / "plugins/print-feedback/__init__.py"
    spec = importlib.util.spec_from_file_location("print_feedback_coldpull_policy", plugin_path)
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    return plugin


def test_next_pla_run_offers_cold_95_and_sends_it(monkeypatch):
    """After Snapped or Didn't release, the next PLA run offers COLD=95 and sends it."""
    import asyncio
    import sys

    plugin = _load_coldpull_plugin()
    prompts = []

    async def confirm(adapter, source, metadata, key, prompt, choices, timeout, live_key=None):
        prompts.append((key, prompt, tuple(choices)))
        if key.endswith("-temp"):
            assert "COLD=95" in prompt
            return "Use COLD=95"
        if key.endswith("-actuate"):
            return "Confirm cold pull"
        if key.endswith("-tip"):
            return "Clean"
        return None

    monkeypatch.setattr(plugin, "_confirm", confirm)
    monkeypatch.setattr(plugin, "_maintenance_hours_now", lambda: 1.0)
    monkeypatch.setattr("print_handy.hermes.maintenance.load", lambda *args, **kwargs: {
        "runs": [{"cold_pull": {"result": "snapped", "next_cold": 95}}]})
    saved = []
    monkeypatch.setattr("print_handy.hermes.maintenance.append_run", lambda record, **kwargs: saved.append(record) or record)

    sent = []

    class Adapter:
        async def send(self, **kwargs):
            return None

    def fake_coldpull(got_temp, got_cold, *, confirmed_at, gcode_sender=None, reader=None):
        sent.append(f"_COLDPULL_LOAD_MATERIAL T=0 TEMP={got_temp} COLD={got_cold}")
        return True, "Cold pull finished; heater target is 0."

    from print_handy.hermes import print_control as pc
    monkeypatch.setattr(pc, "coldpull", fake_coldpull)
    source = MagicMock()
    source.chat_id = "c"
    _record, message = asyncio.run(plugin._coldpull_workflow(
        Adapter(), source, None, source_tag="coldpull",
        selected_material="PLA (trial) 220/90"))
    assert any(key.endswith("-temp") and "Use COLD=95" in choices for key, _prompt, choices in prompts)
    assert sent == ["_COLDPULL_LOAD_MATERIAL T=0 TEMP=220 COLD=95"]
    assert saved and saved[-1]["source"] == "coldpull"
    assert "heater target is 0" in message


def test_standalone_log_uses_coldpull_source(monkeypatch):
    """A standalone pull is written to the maintenance log with source coldpull."""
    import asyncio
    import sys

    plugin = _load_coldpull_plugin()

    async def confirm(adapter, source, metadata, key, prompt, choices, timeout, live_key=None):
        if key.endswith("-actuate"):
            return "Confirm cold pull"
        if key.endswith("-tip"):
            return "Clean"
        return None

    monkeypatch.setattr(plugin, "_confirm", confirm)
    monkeypatch.setattr(plugin, "_maintenance_hours_now", lambda: 2.0)
    monkeypatch.setattr("print_handy.hermes.maintenance.load", lambda *args, **kwargs: {"runs": []})
    saved = []
    monkeypatch.setattr("print_handy.hermes.maintenance.append_run", lambda record, **kwargs: saved.append(dict(record)) or record)

    class Adapter:
        async def send(self, **kwargs):
            return None

    def fake_coldpull(got_temp, got_cold, *, confirmed_at, gcode_sender=None, reader=None):
        return True, "Cold pull finished; heater target is 0."

    from print_handy.hermes import print_control as pc
    monkeypatch.setattr(pc, "coldpull", fake_coldpull)
    source = MagicMock()
    source.chat_id = "c"
    record, _message = asyncio.run(plugin._coldpull_workflow(
        Adapter(), source, None, source_tag="coldpull",
        selected_material="PETG 245/100"))
    assert record["source"] == "coldpull"
    assert record["kind"] == "coldpull"
    assert saved[-1]["source"] == "coldpull"
    assert saved[-1]["cold_pull"]["temp"] == 245
    assert saved[-1]["cold_pull"]["cold"] == 100


def test_two_dirty_results_suggest_nozzle_swap(monkeypatch):
    """A second dirty result in a row suggests a nozzle swap and does not offer another repeat."""
    import asyncio
    import sys

    plugin = _load_coldpull_plugin()

    async def confirm(adapter, source, metadata, key, prompt, choices, timeout, live_key=None):
        if key.endswith("-repeat"):
            return "Run one repeat"
        if key.endswith("-actuate"):
            return "Confirm cold pull"
        if key.endswith("-tip"):
            return "Dirty"
        return None

    monkeypatch.setattr(plugin, "_confirm", confirm)
    monkeypatch.setattr(plugin, "_maintenance_hours_now", lambda: 3.0)
    monkeypatch.setattr("print_handy.hermes.maintenance.load", lambda *args, **kwargs: {
        "runs": [{"cold_pull": {"result": "dirty"}}]})
    saved = []
    monkeypatch.setattr("print_handy.hermes.maintenance.append_run", lambda record, **kwargs: saved.append(record) or record)

    class Adapter:
        async def send(self, **kwargs):
            return None

    def fake_coldpull(got_temp, got_cold, *, confirmed_at, gcode_sender=None, reader=None):
        return True, "Cold pull finished; heater target is 0."

    from print_handy.hermes import print_control as pc
    monkeypatch.setattr(pc, "coldpull", fake_coldpull)
    source = MagicMock()
    source.chat_id = "c"
    _record, message = asyncio.run(plugin._coldpull_workflow(
        Adapter(), source, None, source_tag="coldpull",
        selected_material="PLA (trial) 220/90"))
    assert "nozzle swap" in message
    assert "can be repeated" not in message
    assert saved[-1]["cold_pull"]["suggest_nozzle_swap"] is True
    assert saved[-1]["cold_pull"]["offer_repeat"] is False
    assert saved[-1]["source"] == "coldpull"


def test_cold_pull_prompts_use_their_configured_timeouts(monkeypatch):
    """The go-ahead uses the safety window; the tip result uses the outcome window.

    The recorded seconds come from the configured accessors rather than a literal.
    """
    import asyncio

    plugin = _load_coldpull_plugin()
    recorded = []

    async def confirm(adapter, source, metadata, key, prompt, choices, timeout, live_key=None):
        recorded.append((key, timeout))
        if key.endswith("-actuate"):
            return "Confirm cold pull"
        if key.endswith("-tip"):
            return "Clean"
        return None

    monkeypatch.setattr(plugin, "_confirm", confirm)
    monkeypatch.setattr(plugin, "_maintenance_hours_now", lambda: 0.0)
    monkeypatch.setattr("print_handy.hermes.maintenance.load", lambda *args, **kwargs: {"runs": []})
    monkeypatch.setattr("print_handy.hermes.maintenance.append_run", lambda record, **kwargs: record)

    class Adapter:
        async def send(self, **kwargs):
            return None

    def fake_coldpull(got_temp, got_cold, *, confirmed_at, gcode_sender=None, reader=None):
        return True, "Cold pull finished; heater target is 0."

    from print_handy.hermes import _config
    from print_handy.hermes import print_control as pc
    monkeypatch.setattr(pc, "coldpull", fake_coldpull)
    source = MagicMock()
    source.chat_id = "c"
    asyncio.run(plugin._coldpull_workflow(
        Adapter(), source, None, source_tag="coldpull",
        selected_material="PLA (trial) 220/90"))
    by_key = {key.rsplit("-", 1)[-1]: timeout for key, timeout in recorded}
    assert by_key["actuate"] == _config.safety_confirm_timeout_s()
    assert by_key["tip"] == _config.cold_pull_outcome_s()
    assert by_key["actuate"] != _config.question_timeout_s()


def test_full_maintenance_with_cold_pull_records_full_run(monkeypatch):
    """A full maintenance that runs a cold pull must still save its own
    kind=full run (status/reminders look that kind up), without duplicating
    the cold-pull record the shared workflow already persisted (P2-7)."""
    import asyncio

    plugin = _load_coldpull_plugin()
    store = {"runs": []}

    from print_handy.hermes import maintenance as maint

    def fake_load(*args, **kwargs):
        return {"runs": list(store["runs"]), **({"pending": store["pending"]} if "pending" in store else {})}

    def fake_save(data, *args, **kwargs):
        store["runs"] = list(data.get("runs", store["runs"]))
        if "pending" in data:
            store["pending"] = data["pending"]
        else:
            store.pop("pending", None)

    def fake_append(record, **kwargs):
        entry = dict(record)
        store["runs"].append(entry)
        return entry

    def fake_begin(kind, state, **kwargs):
        session = {"kind": kind, "steps": [None] * 4, "problems": [], "cold_pull": None}
        state["pending"] = session
        return session

    monkeypatch.setattr(maint, "load", fake_load)
    monkeypatch.setattr(maint, "save", fake_save)
    monkeypatch.setattr(maint, "append_run", fake_append)
    monkeypatch.setattr(maint, "begin_session", fake_begin)
    monkeypatch.setattr(maint, "save_session", lambda session, state, **kw: state.__setitem__("pending", session))

    from print_handy.hermes import print_control as pc
    monkeypatch.setattr(pc, "status", lambda **kw: {"state": "standby"})
    monkeypatch.setattr(plugin, "_maintenance_hours_now", lambda *a, **k: 12.0)
    monkeypatch.setattr(plugin, "_routed_printer_id", lambda source, gateway=None: None)
    monkeypatch.setattr(plugin, "_live_key_for", lambda source, gateway, chosen=None: None)

    async def fake_decide(source, gateway, adapter, metadata, *, question=None):
        return None, None

    monkeypatch.setattr(plugin, "_decide_printer", fake_decide)

    async def fake_confirm(adapter, source, metadata, key, prompt, choices, timeout, live_key=None):
        if key == "maintenance-coldpull":
            return choices[0]  # run the PLA cold pull
        return "Done"

    monkeypatch.setattr(plugin, "_confirm", fake_confirm)

    async def fake_workflow(adapter, source, metadata, **kwargs):
        record = {"kind": "coldpull", "source": "maintenance",
                  "cold_pull": {"result": "clean"}}
        maint.append_run(record)  # the shared workflow persists its own record
        return record, "Cold pull finished."

    monkeypatch.setattr(plugin, "_coldpull_workflow", fake_workflow)

    class Adapter:
        async def send(self, **kwargs):
            return None

    source = MagicMock()
    source.chat_id = "c"
    source.thread_id = None
    event = MagicMock()
    event.text = "/print-maintenance full"
    monkeypatch.setattr(plugin, "_control_preamble",
                        lambda e, g, cmd, with_args=False: (source, Adapter()))

    result = asyncio.run(plugin.maintenance_dispatch(event, MagicMock()))
    assert result == {"action": "skip", "reason": "maintenance handled"}
    kinds = [r.get("kind") for r in store["runs"]]
    assert kinds.count("coldpull") == 1, kinds
    assert kinds.count("full") == 1, kinds

