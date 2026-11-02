"""Spoolman ph_slot as the source of truth and spool_sync.reconcile (#798 step 2).

Slot assignment lives in the Spoolman extra field ``ph_slot``
(``<printer_id>:<slot>``); ``location`` is display-only and
``printer_hardware.json`` is a cache reconcile rebuilds from Spoolman.
The fake below follows test_spool_sync.py's patterns: a callable
(method, path, body) Spoolman and a callable Moonraker.
"""
import copy
import json
from unittest import mock

from print_handy.hermes import spool_sync

PID = "ad5x"


# ---------------------------------------------------------------- fakes

class FakeSpoolman:
    """A stateful in-memory Spoolman: field defs, spools with extra values, a call log."""

    def __init__(self, spools=(), fields=()):
        self.spools = {int(s["id"]): copy.deepcopy(s) for s in spools}
        self.fields = [dict(f) for f in fields]
        self.calls = []
        self.down = False

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if self.down:
            raise OSError("spoolman down")
        if method == "GET" and path == "/api/v1/field/spool":
            return copy.deepcopy(self.fields)
        if method == "POST" and path == "/api/v1/field/spool/ph_slot":
            self.fields.append({"key": "ph_slot", **(body or {})})
            return {}
        if method == "GET" and path == "/api/v1/spool?allow_archived=true":
            return [copy.deepcopy(s) for s in self.spools.values()]
        if method == "GET" and path.startswith("/api/v1/spool/"):
            return copy.deepcopy(self.spools[int(path.rsplit("/", 1)[1])])
        if method == "PATCH" and path.startswith("/api/v1/spool/"):
            row = self.spools[int(path.rsplit("/", 1)[1])]
            for key, value in (body or {}).items():
                if key == "extra":
                    row.setdefault("extra", {}).update(value)
                else:
                    row[key] = value
            return {}
        if method == "DELETE" and path.startswith("/api/v1/spool/"):
            del self.spools[int(path.rsplit("/", 1)[1])]
            return {}
        return {}

    def patches(self, spool_id):
        return [c for c in self.calls if c[0] == "PATCH" and c[1] == f"/api/v1/spool/{spool_id}"]


def _spool(sid, name="Black", material="PLA", hex_code="161616", location="Shelf",
           archived=False, ph_slot=None, remaining=800):
    spool = {"id": sid, "location": location, "archived": archived,
             "remaining_weight": remaining,
             "filament": {"name": name, "material": material, "color_hex": hex_code}}
    if ph_slot is not None:
        spool["extra"] = {"ph_slot": json.dumps(ph_slot)}
    return spool


def _mr(active=None):
    """Moonraker fake: idle printer, a spool pointer, screen posts accepted."""
    active = active if active is not None else [None]

    def mr(method, path, body=None):
        if path.startswith("/printer/objects/query"):
            return {"result": {"status": {"print_stats": {"state": "standby"}}}}
        if path == "/server/spoolman/spool_id":
            if method == "POST":
                active[0] = (body or {}).get("spool_id")
                return {}
            return {"result": {"spool_id": active[0]}}
        return {"result": "ok"}

    return mr


def _state_file(tmp_path, slots):
    path = tmp_path / "printer_hardware.json"
    path.write_text(json.dumps({"spool": {}, "filament_slots": slots}))
    return path


def _record(sid, name="Black PLA", material="PLA", color="black", hex_code="161616"):
    return {"spoolman_id": sid, "name": name, "material": material, "color": color, "hex": hex_code}


# ---------------------------------------------------------------- ph_slot helpers

def test_format_and_parse_ph_slot():
    assert spool_sync.format_ph_slot("ad5x", 2) == "ad5x:2"
    # Spoolman JSON-encodes extra text values; a raw string is tolerated too.
    assert spool_sync.parse_ph_slot('"ad5x:2"') == ("ad5x", 2)
    assert spool_sync.parse_ph_slot("ad5x:2") == ("ad5x", 2)
    assert spool_sync.parse_ph_slot(None) is None
    assert spool_sync.parse_ph_slot("") is None
    assert spool_sync.parse_ph_slot("Shelf") is None
    assert spool_sync.parse_ph_slot("ad5x:9") is None  # not a slot
    assert spool_sync.spool_ph_slot(_spool(5, ph_slot="ad5x:3")) == ("ad5x", 3)
    assert spool_sync.spool_ph_slot(_spool(5)) is None
    assert spool_sync.spool_ph_slot({"extra": {"ph_slot": "weird"}}) is None
    assert spool_sync.spool_ph_slot_text({"extra": {"ph_slot": "weird"}}) == "weird"


def test_ensure_ph_slot_field_creates_a_missing_field_once():
    fake = FakeSpoolman()
    assert spool_sync.ensure_ph_slot_field(client=fake) is True
    assert fake.fields == [{"key": "ph_slot", "name": "Print Handy slot", "field_type": "text"}]
    calls = list(fake.calls)
    assert spool_sync.ensure_ph_slot_field(client=fake) is True
    assert not any(c[0] == "POST" for c in fake.calls[len(calls):])  # already defined: no re-create


def test_ensure_ph_slot_field_never_raises_into_callers():
    def down(method, path, body=None):
        raise OSError("down")

    assert spool_sync.ensure_ph_slot_field(client=down) is False
    # A client that answers every GET with a spool dict does not speak the
    # field endpoints: no creation is attempted and nothing raises.
    spool_only = lambda method, path, body=None: {"id": 7}
    assert spool_sync.ensure_ph_slot_field(client=spool_only) is False
    assert spool_sync.ensure_ph_slot_field(client=mock.MagicMock()) is False
    assert spool_sync.ensure_ph_slot_field(client=object()) is False


# ---------------------------------------------------------------- bind_slot writes

def _bind(tmp_path, fake, disposition="shelf", prior_ph_slot="ad5x:2"):
    state = _state_file(tmp_path, {"2": _record(3, "Pink PLA", "PLA", "pink", "F95D73")})
    return spool_sync.bind_slot(2, 7, client=fake, mr=_mr(), state_path=state,
                                push_screen_update=False, prior_disposition=disposition,
                                printer_id=PID)


def _bind_fake(prior_ph_slot="ad5x:2"):
    return FakeSpoolman([
        _spool(3, "Pink", "PLA", "F95D73", location="IFS Slot 2", ph_slot=prior_ph_slot),
        _spool(7, "Coffee", "PLA+", "6F4E37"),
    ])


def test_bind_slot_writes_ph_slot_and_shelves_the_displaced_spool(tmp_path):
    fake = _bind_fake()
    _bind(tmp_path, fake)
    assert fake.spools[7]["extra"]["ph_slot"] == '"ad5x:2"'
    assert fake.spools[7]["location"] == "IFS Slot 2"
    assert fake.spools[3]["extra"]["ph_slot"] is None
    assert fake.spools[3]["location"] == "Shelf"


def test_bind_slot_archive_clears_the_displaced_spools_claim(tmp_path):
    fake = _bind_fake()
    _bind(tmp_path, fake, disposition="archive")
    assert fake.spools[3]["archived"] is True
    assert fake.spools[3]["extra"]["ph_slot"] is None
    assert fake.spools[7]["extra"]["ph_slot"] == '"ad5x:2"'


def test_bind_slot_depleted_clears_the_displaced_spools_claim(tmp_path):
    fake = _bind_fake()
    _bind(tmp_path, fake, disposition="depleted")
    assert fake.spools[3]["archived"] is True
    assert fake.spools[3]["location"] == "depleted"
    assert fake.spools[3]["extra"]["ph_slot"] is None


def test_bind_slot_delete_removes_the_claim_with_the_record(tmp_path):
    fake = _bind_fake()
    _bind(tmp_path, fake, disposition="delete")
    assert 3 not in fake.spools
    assert fake.spools[7]["extra"]["ph_slot"] == '"ad5x:2"'


def test_bind_slot_keep_clears_the_claim_but_leaves_the_record_alone(tmp_path):
    fake = _bind_fake()
    _bind(tmp_path, fake, disposition="keep")
    assert fake.spools[3]["extra"]["ph_slot"] is None
    assert fake.spools[3]["location"] == "IFS Slot 2"  # the caller handled its fate
    assert fake.spools[3]["archived"] is False


def test_bind_slot_without_a_screen_update_still_writes_ph_slot(tmp_path):
    # The insertion-button path passes push_screen_update=False; the Spoolman
    # assignment is still written (#798: every bind path goes through here).
    fake = _bind_fake()
    result = _bind(tmp_path, fake)
    assert result["screen"] == "not requested"
    assert fake.spools[7]["extra"]["ph_slot"] == '"ad5x:2"'


def test_bind_slot_clears_a_stale_claim_by_another_spool(tmp_path):
    fake = FakeSpoolman([
        _spool(7, "Coffee", "PLA+", "6F4E37"),
        _spool(9, "Stale", "PLA", "111111", ph_slot="ad5x:2"),
    ])
    state = _state_file(tmp_path, {})
    spool_sync.bind_slot(2, 7, client=fake, mr=_mr(), state_path=state,
                         push_screen_update=False, printer_id=PID)
    assert fake.spools[9]["extra"]["ph_slot"] is None
    assert fake.spools[7]["extra"]["ph_slot"] == '"ad5x:2"'


# ---------------------------------------------------------------- reconcile

def test_reconcile_migrates_the_local_cache_on_the_first_run(tmp_path):
    fake = FakeSpoolman([_spool(11), _spool(12, "White", "PLA", "FFFFFF")])
    state = _state_file(tmp_path, {"1": _record(11), "2": _record(12, "White PLA", "PLA", "white", "FFFFFF")})
    result = spool_sync.reconcile(PID, client=fake, mr=_mr(), state_path=state,
                                  pending_path=tmp_path / "pending.json")
    assert result["ok"] is True and result["migrated"] is True
    assert result["assignments"] == {1: 11, 2: 12}
    assert fake.spools[11]["extra"]["ph_slot"] == '"ad5x:1"'
    assert fake.spools[12]["extra"]["ph_slot"] == '"ad5x:2"'
    # The field definition was created as part of the run.
    assert any(f.get("key") == "ph_slot" for f in fake.fields)
    assert "migrated" in spool_sync.reconcile_text(result)


def test_reconcile_rebuilds_the_cache_from_spoolman(tmp_path):
    fake = FakeSpoolman([
        _spool(11, ph_slot="ad5x:1"),
        _spool(12, "White", "PLA", "FFFFFF", ph_slot="ad5x:2"),
        _spool(13, "Other printer", "PLA", "222222", ph_slot="other:1"),
    ])
    state = _state_file(tmp_path, {"1": _record(99, "Gone PLA")})
    result = spool_sync.reconcile(PID, client=fake, mr=_mr(), state_path=state,
                                  pending_path=tmp_path / "pending.json")
    assert result["ok"] is True and result["migrated"] is False
    assert result["assignments"] == {1: 11, 2: 12}
    saved = json.loads(state.read_text())["filament_slots"]
    assert saved["1"]["spoolman_id"] == 11 and saved["1"]["name"] == "Black PLA"
    assert saved["2"]["spoolman_id"] == 12
    assert any("Slot 1: pushed" in line for line in result["screens"])


def test_reconcile_names_two_spools_claiming_one_slot(tmp_path):
    fake = FakeSpoolman([
        _spool(11, ph_slot="ad5x:1"),
        _spool(13, "Green", "PLA", "00FF00", ph_slot="ad5x:1"),
    ])
    state = _state_file(tmp_path, {"1": _record(11)})
    result = spool_sync.reconcile(PID, client=fake, mr=_mr(), state_path=state,
                                  pending_path=tmp_path / "pending.json")
    assert result["assignments"] == {}
    conflict = "\n".join(result["conflicts"])
    assert "#11" in conflict and "#13" in conflict and "both claim" in conflict
    # Neither was picked: the local record for the slot is left as it was.
    assert json.loads(state.read_text())["filament_slots"]["1"]["spoolman_id"] == 11
    assert "both claim" in spool_sync.reconcile_text(result)


def test_reconcile_names_and_clears_an_archived_spools_claim(tmp_path):
    fake = FakeSpoolman([
        _spool(11, ph_slot="ad5x:1"),
        _spool(14, "Used Up", "PLA", "333333", archived=True, ph_slot="ad5x:2"),
    ])
    state = _state_file(tmp_path, {})
    result = spool_sync.reconcile(PID, client=fake, mr=_mr(), state_path=state,
                                  pending_path=tmp_path / "pending.json")
    assert result["assignments"] == {1: 11}
    conflict = "\n".join(result["conflicts"])
    assert "#14" in conflict and "archived" in conflict
    assert fake.spools[14]["extra"]["ph_slot"] is None
    assert "2" not in json.loads(state.read_text())["filament_slots"]


def test_reconcile_names_a_local_spool_spoolman_no_longer_has(tmp_path):
    fake = FakeSpoolman([_spool(11, ph_slot="ad5x:1")])
    state = _state_file(tmp_path, {"3": _record(99, "Deleted PLA")})
    result = spool_sync.reconcile(PID, client=fake, mr=_mr(), state_path=state,
                                  pending_path=tmp_path / "pending.json")
    assert any("#99" in c and "no longer" in c for c in result["conflicts"])
    assert "3" not in json.loads(state.read_text())["filament_slots"]


def test_reconcile_names_an_identity_cross_check_mismatch(tmp_path):
    fake = FakeSpoolman([_spool(11, ph_slot="ad5x:1")])
    state = _state_file(tmp_path, {"1": _record(11, "Pink PETG", "PETG", "pink", "F95D73")})
    result = spool_sync.reconcile(PID, client=fake, mr=_mr(), state_path=state,
                                  pending_path=tmp_path / "pending.json")
    assert result["assignments"] == {1: 11}
    assert any("PETG" in c for c in result["conflicts"])
    # Spoolman still wins the rebuild.
    assert json.loads(state.read_text())["filament_slots"]["1"]["material"] == "PLA"


def test_reconcile_is_idempotent(tmp_path):
    fake = FakeSpoolman([
        _spool(11, ph_slot="ad5x:1"),
        _spool(12, "White", "PLA", "FFFFFF", ph_slot="ad5x:2"),
    ])
    state = _state_file(tmp_path, {})
    kwargs = dict(client=fake, mr=_mr(), state_path=state, pending_path=tmp_path / "pending.json")
    first = spool_sync.reconcile(PID, **kwargs)
    assert first["ok"] is True
    after_first = state.read_text()
    fake.calls.clear()
    second = spool_sync.reconcile(PID, **kwargs)
    assert second["ok"] is True and second["changed"] is False
    assert second["assignments"] == first["assignments"]
    assert not any(c[0] == "PATCH" for c in fake.calls)
    assert state.read_text() == after_first


def test_reconcile_with_spoolman_down_leaves_the_cache_untouched(tmp_path):
    fake = FakeSpoolman([_spool(11, ph_slot="ad5x:1")])
    fake.down = True
    state = _state_file(tmp_path, {"1": _record(11)})
    before = state.read_text()
    result = spool_sync.reconcile(PID, client=fake, mr=_mr(), state_path=state,
                                  pending_path=tmp_path / "pending.json")
    assert result["ok"] is False
    assert "Spoolman did not answer" in spool_sync.reconcile_text(result)
    assert state.read_text() == before


def test_reconcile_derives_the_default_printer_from_config():
    fake = FakeSpoolman()
    fake.down = True
    result = spool_sync.reconcile(client=fake, mr=_mr())
    assert result["printer_id"] == spool_sync._default_printer_id()
    assert result["printer_id"] is not None
