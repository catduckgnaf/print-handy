from datetime import datetime, timedelta, timezone
import json

from print_handy.hermes import maintenance
def test_checklist_step_persists_in_session_shape():
    session = {"steps": [None] * 4}
    maintenance.checklist_step(session, 2, "problem")
    assert session["steps"] == [None, None, "problem", None]


def test_pending_checklist_roundtrips_for_resume(tmp_path):
    path = tmp_path / "maintenance.json"
    state = maintenance.load(path)
    session = maintenance.begin_session("full", state, path=path)
    maintenance.checklist_step(session, 0, "done")
    maintenance.save_session(session, state, path=path)
    assert maintenance.load(path)["pending"]["steps"][0] == "done"


def test_append_run_persists_profile_and_vault(tmp_path):
    profile = tmp_path / "state.json"
    vault = tmp_path / "vault" / "maintenance.jsonl"
    maintenance.append_run({"kind": "full", "steps": ["done"]}, state_path=profile, vault_path=vault)
    assert maintenance.load(profile)["runs"][0]["kind"] == "full"
    lines = vault.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "full"


def test_hours_due_thresholds(monkeypatch):
    monkeypatch.setattr(maintenance, "QUICK_HOURS", 25)
    monkeypatch.setattr(maintenance, "FULL_HOURS", 75)
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    runs = [
        {"kind": "full", "hours_total": 0, "at": now.isoformat()},
        {"kind": "quick", "hours_total": 10, "at": now.isoformat()},
    ]
    result = maintenance.due(35, runs, now=now)
    assert result["quick_due"] is True
    assert result["full_due"] is False
    assert result["quick_hours_since"] == 25


def test_reminders_suppressed_when_not_idle():
    assert maintenance.reminder_text({"runs": []}, 100, idle=False) is None


def test_full_clean_due_after_days(monkeypatch):
    monkeypatch.setattr(maintenance, 'FULL_DAYS', 10)
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    old = datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()
    status = maintenance.due(1, [{"kind": "full", "hours_total": 0, "at": old}], now=now)
    assert status["full_due"] is True


def test_stored_full_clean_status_without_history():
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    old = datetime(2026, 10, 10, tzinfo=timezone.utc).isoformat()
    text = maintenance.status_text(
        12.0,
        [{"kind": "full", "hours_total": 12.0, "at": old}],
        now=now,
    )
    assert text == "Maintenance: last full clean 2 days / 0.0 h ago."


def test_listener_history_totals_are_hours_not_seconds(monkeypatch):
    # Moonraker's /server/history/totals reports total_time/print_time in
    # SECONDS; maintenance records and thresholds are in HOURS, so the
    # listener's totals must be converted before reminder_text sees them.
    from print_handy.hermes import moonraker_event_listener as mel

    monkeypatch.setattr(mel.print_control, "_call",
                        lambda path: {"result": {"total_time": 7200.0, "print_time": 7000.0}})
    assert mel._maintenance_history_totals() == 2.0
    monkeypatch.setattr(mel.print_control, "_call",
                        lambda path: {"result": {"print_time": 1800.0}})
    assert mel._maintenance_history_totals() == 0.5
    # A run recorded at 2.0 total hours with 0.5 h printed since must not
    # read as thousands of hours and retrigger the reminder at once.
    state = {"runs": [{"kind": "full", "hours_total": 2.0,
                       # Recent on purpose: a fixed date ages past full_days and
                       # makes the reminder due by the calendar, not the hours.
                       "at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}]}
    assert maintenance.reminder_text(state, mel._maintenance_history_totals(), idle=True) is None
