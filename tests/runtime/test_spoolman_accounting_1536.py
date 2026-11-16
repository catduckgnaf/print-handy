"""#1536: one accounting authority per printer, the extra-field fallback
for an outside Spoolman, and two-way sync planning.

- Item 3: when the printer is the accounting authority, Print Handy's
  end-of-print charge writes nothing to Spoolman and the ledger says why.
- Item 4: when an outside Spoolman refuses Print Handy's extra field, the
  nickname falls back to a local store while shelf and archive (native
  Spoolman fields) keep working against that same instance.
- Item 5: the sync plan converges loaded-spool assignments both ways and
  names conflicts instead of overwriting them.
"""
import json

import pytest

from print_handy import settings
from print_handy.runtime import multislot_usage, spool_sync


# --- Item 3: charge gating ---------------------------------------------------

class FakeSpoolman:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, body=None, **kw):
        self.calls.append((method, path, body))
        return {"id": int(path.split("/")[4])}


def _context(**extra):
    context = {
        "job_key": "dragon-two-color.gcode",
        "print_number": 41,
        "started_at": "2026-11-10T14:00:00Z",
        "tool_to_physical_slot": {"0": 2, "1": 3},
        "slicer_settings": {
            "filament_used_g_list": [6.8, 28.15],
            "mapped_materials": {
                "0": {"spoolman_id": 12, "material": "PLA"},
                "1": {"spoolman_id": 15, "material": "PLA"}}},
        "planned_filament_mm": 11627.68,
    }
    context.update(extra)
    return context


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    monkeypatch.setenv(multislot_usage.LEDGER_ENV, str(path))
    return path


def test_printer_authority_charges_nothing_and_the_ledger_says_why(ledger):
    fake = FakeSpoolman()
    entry = multislot_usage.charge_finished_print(
        _context(spool_accounting_authority="printer"), "complete", request=fake)
    assert fake.calls == []  # no PUT /use reached Spoolman at all
    assert entry["status"] == "skipped"
    assert entry["accounting_authority"] == "printer"
    assert "accounting authority" in entry["reason"]
    assert all(slot["status"] == "skipped" for slot in entry["slots"])
    assert "not charged" in multislot_usage.summary_line(entry)
    saved = json.loads(ledger.read_text())
    key = multislot_usage.charge_key(_context(spool_accounting_authority="printer"))
    assert saved[key]["status"] == "skipped"


def test_print_handy_authority_still_charges(ledger):
    fake = FakeSpoolman()
    entry = multislot_usage.charge_finished_print(
        _context(spool_accounting_authority="print_handy"), "complete", request=fake)
    assert entry["status"] == "charged"
    assert [(c[1], c[2]) for c in fake.calls] == [
        ("/api/v1/spool/12/use", {"use_weight": 6.8}),
        ("/api/v1/spool/15/use", {"use_weight": 28.15})]


def test_configured_printer_authority_gates_without_a_launch_stamp(ledger):
    data = settings.load(local=False, environ={}, user=False)
    data["spool"]["accounting"] = "printer"
    settings.set_override(data)
    try:
        fake = FakeSpoolman()
        entry = multislot_usage.charge_finished_print(_context(), "complete", request=fake)
    finally:
        settings.set_override(None)
    assert fake.calls == [] and entry["status"] == "skipped"


def test_single_slot_printer_authority_skips_the_reconciliation_write(ledger):
    data = settings.load(local=False, environ={}, user=False)
    data["spool"]["accounting"] = "printer"
    settings.set_override(data)
    try:
        fake = FakeSpoolman()
        context = {"job_key": "single-tool.gcode", "print_number": 42,
                   "tool_to_physical_slot": {"0": 2},
                   "slicer_settings": {"filament_used_g_list": [6.8], "mapped_materials":
                       {"0": {"spoolman_id": 12}}},
                   "spool_accounting_authority": "printer",
                   "spool_usage_baseline": {"spool_id": 12, "used_weight": 10.0,
                                             "recorded_at": 0}}
        entry = multislot_usage.guard_single_slot(
            context, "complete", actual_mm=10, active_spool_id=12, request=fake)
    finally:
        settings.set_override(None)
    assert fake.calls == []
    assert entry["status"] == "skipped"
    assert entry["reason"] == multislot_usage.PRINTER_AUTHORITY_REASON


# --- Item 4: extra-field fallback ---------------------------------------------

class FieldRefusingSpoolman:
    """An outside Spoolman that will not let Print Handy create fields."""

    def __init__(self):
        self.spools = {
            7: {"id": 7, "remaining_weight": 400.0, "archived": False,
                "location": "", "extra": {},
                "filament": {"name": "Matte White", "material": "PLA"}}}
        self.writes = []

    def __call__(self, method, path, body=None):
        if method == "GET" and path == "/api/v1/field/spool":
            return []
        if method == "POST" and path.startswith("/api/v1/field/spool/"):
            raise RuntimeError("field creation is not allowed here")
        if method == "GET" and path.startswith("/api/v1/spool/"):
            return json.loads(json.dumps(
                self.spools.get(int(path.rsplit("/", 1)[1]))))
        if method == "GET" and path.startswith("/api/v1/spool?"):
            return [json.loads(json.dumps(s)) for s in self.spools.values()]
        self.writes.append((method, path, body))
        if method == "PATCH" and path.startswith("/api/v1/spool/"):
            self.spools[int(path.rsplit("/", 1)[1])].update(body or {})
        return {}


@pytest.fixture
def local_extras(tmp_path, monkeypatch):
    path = tmp_path / "spool_local_extras.json"
    monkeypatch.setenv(spool_sync.LOCAL_EXTRAS_ENV, str(path))
    return path


def test_rename_falls_back_to_the_local_store_when_fields_are_refused(local_extras):
    fake = FieldRefusingSpoolman()
    result = spool_sync.rename_spool(7, "Garage white", client=fake)
    assert result == {"spool_id": 7, "nickname": "Garage white", "stored": "local"}
    assert not any(w[0] == "PATCH" for w in fake.writes)  # nothing written there
    # The nickname reads back through the same production reader the
    # picker and lists use.
    assert spool_sync.spool_nickname(fake.spools[7]) == "Garage white"
    text = spool_sync.rename_spool_text(7, "Shelf white", client=fake)
    assert "stored on this install only" in text
    assert spool_sync.spool_nickname(fake.spools[7]) == "Shelf white"
    # Clearing the nickname clears the local entry too.
    spool_sync.rename_spool(7, "", client=fake)
    assert not spool_sync.spool_nickname(fake.spools[7])
    assert json.loads(local_extras.read_text()) == {}


def test_shelf_and_archive_use_native_fields_on_the_same_instance(
        local_extras, monkeypatch, tmp_path):
    fake = FieldRefusingSpoolman()
    state_path = tmp_path / "printer_hardware.json"
    state_path.write_text(json.dumps({"filament_slots": {}, "spool": {}}))
    result = spool_sync.set_archived(7, True, client=fake, state_path=state_path)
    assert result["archived"] is True
    assert ("PATCH", "/api/v1/spool/7", {"archived": True}) in fake.writes

    # Putting a loaded spool back on the shelf is a location write, a
    # native Spoolman field, so it works on the same refusing instance.
    state_path.write_text(json.dumps({
        "filament_slots": {"1": {"spoolman_id": 7, "name": "Matte White"}},
        "spool": {}}))
    monkeypatch.setattr(spool_sync, "write_lane_side", lambda *a, **k: "skipped")
    removed = spool_sync.unbind_slot(
        1, client=fake, mr=lambda *a, **k: None,
        state_path=state_path, printer_id="bench-printer")
    assert removed["spool_id"] == 7
    assert any(w[0] == "PATCH" and (w[2] or {}).get("location") == spool_sync.SHELF
               for w in fake.writes)


# --- Item 5: sync both ways ----------------------------------------------------

PID = "bench-printer"


def _row(spool_id, slot=None, remaining=500.0, archived=False):
    row = {"id": spool_id, "remaining_weight": remaining, "archived": archived,
           "extra": {}}
    if slot is not None:
        row["extra"]["ph_slot"] = json.dumps(spool_sync.format_ph_slot(PID, slot))
    return row


def test_sync_plan_takes_spoolmans_claim_when_only_spoolman_knows():
    plan = spool_sync.external_spool_sync_plan({}, [_row(12, slot=2)], printer_id=PID)
    assert plan["slot_changes"] == [
        {"slot": 2, "spool_id": 12, "source": "spoolman"}]
    assert plan["conflicts"] == []


def test_sync_plan_pushes_the_local_claim_when_only_print_handy_knows():
    plan = spool_sync.external_spool_sync_plan({1: 9}, [_row(9)], printer_id=PID)
    assert plan["slot_changes"] == [
        {"slot": 1, "spool_id": 9, "source": "local"}]


def test_sync_plan_reports_a_weight_difference_without_changing_either_side():
    local = {2: {"spoolman_id": 12, "remaining_grams": 480.0}}
    plan = spool_sync.external_spool_sync_plan(local, [_row(12, slot=2)], printer_id=PID)
    assert plan["slot_changes"] == []
    assert plan["weight_updates"] == [{
        "slot": 2, "spool_id": 12,
        "local_remaining_g": 480.0, "spoolman_remaining_g": 500.0}]


def test_sync_plan_names_a_conflict_and_changes_nothing_for_that_slot():
    plan = spool_sync.external_spool_sync_plan(
        {2: 9}, [_row(12, slot=2)], printer_id=PID)
    assert plan["slot_changes"] == []
    assert len(plan["conflicts"]) == 1
    conflict = plan["conflicts"][0]
    assert conflict["slot"] == 2 and conflict["local_spool_id"] == 9
    assert conflict["spoolman_spool_ids"] == [12]
    assert "#9" in conflict["reason"] and "#12" in conflict["reason"]


def test_sync_plan_ignores_other_printers_and_archived_rows():
    other = {"id": 5, "remaining_weight": 10.0, "archived": False,
             "extra": {"ph_slot": json.dumps(
                 spool_sync.format_ph_slot("someone-else", 1))}}
    plan = spool_sync.external_spool_sync_plan(
        {}, [other, _row(6, slot=3, archived=True)], printer_id=PID)
    assert plan == {"slot_changes": [], "weight_updates": [], "conflicts": []}


class SyncSpoolman:
    """A Spoolman that accepts extra fields, with merge-on-PATCH extras."""

    def __init__(self):
        self.spools = {
            7: {"id": 7, "remaining_weight": 400.0, "archived": False,
                "extra": {}, "filament": {"name": "Matte White", "material": "PLA"}}}
        self.fields = []
        self.writes = []

    def __call__(self, method, path, body=None):
        if method == "GET" and path == "/api/v1/field/spool":
            return [{"key": key} for key in self.fields]
        if method == "GET" and path.startswith("/api/v1/spool?"):
            rows = [json.loads(json.dumps(s)) for s in self.spools.values()]
            return rows if "allow_archived=true" in path else \
                [row for row in rows if not row["archived"]]
        if method == "GET" and path.startswith("/api/v1/spool/"):
            return json.loads(json.dumps(
                self.spools.get(int(path.rsplit("/", 1)[1]))))
        self.writes.append((method, path, body))
        if method == "POST" and path.startswith("/api/v1/field/spool/"):
            self.fields.append(path.rsplit("/", 1)[1])
        if method == "PATCH" and path.startswith("/api/v1/spool/"):
            spool = self.spools[int(path.rsplit("/", 1)[1])]
            for key, value in (body or {}).items():
                if key == "extra":
                    for extra_key, extra_value in value.items():
                        if extra_value is None:
                            spool["extra"].pop(extra_key, None)
                        else:
                            spool["extra"][extra_key] = extra_value
                else:
                    spool[key] = value
        return {}


def test_sync_external_spoolman_applies_local_claims_and_reconciles(tmp_path):
    fake = SyncSpoolman()
    state = {"printer_id": PID, "updated_at": "2026-11-15T00:00:00Z",
             "filament_slots": {"1": {"spoolman_id": 7, "remaining_grams": 400.0}}}
    state_path = tmp_path / "printer_hardware.json"
    state_path.write_text(json.dumps(state))
    result = spool_sync.sync_external_spoolman(
        PID, client=fake, mr=lambda *a, **k: None,
        state_path=state_path, pending_path=tmp_path / "pending.json")
    assert result["ok"] is True
    assert {"slot": 1, "spool_id": 7, "source": "local"} in result["plan"]["slot_changes"]
    patch = [w for w in fake.writes
             if w[0] == "PATCH" and w[2] and "extra" in w[2]]
    assert patch and json.loads(patch[0][2]["extra"]["ph_slot"]).endswith(":1")


def test_sync_external_spoolman_is_fail_soft_when_the_instance_is_down(tmp_path):
    def down(method, path, body=None):
        raise RuntimeError("connection refused")

    result = spool_sync.sync_external_spoolman(
        PID, client=down, state_path=tmp_path / "missing.json")
    assert result["ok"] is False and "did not answer" in result["error"]
