"""Spoolman coverage expansion against a real server (follow-up to
test_spoolman_real.py, which carries the first five round trips).

Everything here runs against the same throwaway Spoolman the CI
spoolman job starts (pinned 0.27.1); skipped unless
PRINT_HANDY_SPOOLMAN_TEST_URL points at one. Never aim it at the home
server. Brands and colors are unique per test so tests cannot satisfy
each other's dedupe assertions, and every assertion is about state read
back from the server, not about what a fake was told to return.
"""
import json
import os
import socket

import pytest

from print_handy.core import printer_state, spoolman_client
from print_handy.runtime import spool_sync

URL = os.environ.get("PRINT_HANDY_SPOOLMAN_TEST_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="set PRINT_HANDY_SPOOLMAN_TEST_URL to a throwaway Spoolman")


def client(method, path, body=None):
    return spool_sync._http(URL, method, path, body)


def printer(method, path, body=None):
    """Moonraker stand-in: idle printer, no active usage spool."""
    return {"result": {"spool_id": None, "status": {"print_stats": {"state": "standby"}}}}


def new_spool(text):
    return spool_sync.create_spool(spool_sync.parse_new(text), client=client)


def hardware_record(tmp_path, name="printer_hardware.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"filament_slots": {}}))
    return path


def refused_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


# ------------------------------------------------------------ spoolman_client against the real server

def test_client_health_and_get_spool_round_trip():
    sid = new_spool("Clientbrand, PLA, Client Teal, 1000, 008080")["spool_id"]
    assert spoolman_client.health(base_url=URL) == (True, "HTTP 200")
    spool = spoolman_client.get_spool(sid, base_url=URL)
    assert spool["id"] == sid
    assert spool["filament"]["name"] == "Client Teal"
    assert spool["remaining_weight"] == pytest.approx(1000)


def test_client_missing_spool_is_none_but_request_names_404():
    missing = 999_999_001
    assert spoolman_client.get_spool(missing, base_url=URL) is None
    with pytest.raises(spoolman_client.SpoolmanUnavailable) as caught:
        spoolman_client.request("GET", f"/api/v1/spool/{missing}", base_url=URL)
    # #839: a 404 (no such record) is distinguishable from an outage.
    assert caught.value.status == 404


def test_client_list_spools_archived_filter():
    sid = new_spool("Filterbrand, PLA, Filter Pink, 1000, FFC0CB")["spool_id"]
    client("PATCH", f"/api/v1/spool/{sid}", {"archived": True})
    live = spoolman_client.list_spools(base_url=URL)
    assert all(row["id"] != sid for row in live)
    everything = spoolman_client.list_spools(base_url=URL, allow_archived=True)
    assert any(row["id"] == sid for row in everything)


def test_client_request_pagination_and_patch_round_trip():
    first = new_spool("Pagebrand, PLA, Page One, 1000")["spool_id"]
    second = new_spool("Pagebrand, PLA, Page Two, 1000")["spool_id"]
    page = spoolman_client.request("GET", "/api/v1/spool?limit=1", base_url=URL)
    assert len(page) == 1
    spoolman_client.request("PATCH", f"/api/v1/spool/{first}",
                            {"comment": "expansion was here"}, base_url=URL)
    assert spoolman_client.get_spool(first, base_url=URL)["comment"] == "expansion was here"
    assert spoolman_client.get_spool(second, base_url=URL)["comment"] != "expansion was here"


def test_client_usage_update_moves_remaining_weight():
    sid = new_spool("Usebrand, PLA, Use Gray, 1000, 808080")["spool_id"]
    spoolman_client.request("PATCH", f"/api/v1/spool/{sid}", {"used_weight": 250}, base_url=URL)
    spool = spoolman_client.get_spool(sid, base_url=URL)
    assert spool["used_weight"] == pytest.approx(250)
    assert spool["remaining_weight"] == pytest.approx(750)
    # used_weight is an absolute, not a delta: setting it again replaces it.
    spoolman_client.request("PATCH", f"/api/v1/spool/{sid}", {"used_weight": 400}, base_url=URL)
    assert spoolman_client.get_spool(sid, base_url=URL)["remaining_weight"] == pytest.approx(600)


# ------------------------------------------------------------ create_spool against the real server

def test_vendor_is_created_once_per_brand():
    new_spool("Oncebrand, PLA, Once Red, 1000, FF0000")
    new_spool("Oncebrand, PETG, Once Blue, 1000, 0000FF")
    vendors = [v for v in client("GET", "/api/v1/vendor") if v["name"] == "Oncebrand"]
    assert len(vendors) == 1
    filaments = [f for f in client("GET", "/api/v1/filament")
                 if (f.get("vendor") or {}).get("id") == vendors[0]["id"]]
    assert {f["material"] for f in filaments} == {"PLA", "PETG"}


def test_canonical_brand_spellings_share_one_vendor():
    new_spool("Bambu, PLA, Alias Green, 1000, 00FF00")
    new_spool("BAMBU LAB, PLA, Alias Lime, 1000, 32CD32")
    vendors = [v for v in client("GET", "/api/v1/vendor") if v["name"] == "Bambu Lab"]
    assert len(vendors) == 1


def test_price_lot_and_purchase_url_are_recorded():
    spec = spool_sync.parse_new("Shopbrand, PLA, Shop Yellow, 1000, FFFF00")
    spec.update({"price": 19.99, "lot": "LOT-42", "purchase_url": "https://example.invalid/shop/1"})
    created = spool_sync.create_spool(spec, client=client)
    spool = client("GET", f"/api/v1/spool/{created['spool_id']}")
    assert spool["price"] == pytest.approx(19.99)
    assert spool["lot_nr"] == "LOT-42"
    assert spool["comment"].startswith(spool_sync.CREATED_SPOOL_COMMENT)
    assert "purchased: https://example.invalid/shop/1" in spool["comment"]


def test_label_temperatures_land_on_a_new_filament_only():
    spec = spool_sync.parse_new("Tempbrand, PLA, Temp Orange, 1000, FFA500")
    spec.update({"nozzle_temp": 235, "bed_temp": 65})
    created = spool_sync.create_spool(spec, client=client)
    filament = client("GET", f"/api/v1/filament/{created['filament_id']}")
    assert filament["settings_extruder_temp"] == 235
    assert filament["settings_bed_temp"] == 65
    # A reused filament type keeps the temperatures it already has; a
    # second spool of the same type does not rewrite them (#514).
    spec2 = spool_sync.parse_new("Tempbrand, PLA, Temp Orange, 1000, FFA500")
    spec2.update({"nozzle_temp": 199, "bed_temp": 40})
    again = spool_sync.create_spool(spec2, client=client)
    assert again["reused_filament"] and again["filament_id"] == created["filament_id"]
    filament = client("GET", f"/api/v1/filament/{created['filament_id']}")
    assert filament["settings_extruder_temp"] == 235
    assert filament["settings_bed_temp"] == 65


def test_unknown_material_is_refused_and_creates_nothing():
    before = len(client("GET", "/api/v1/filament"))
    with pytest.raises(spool_sync.SpoolError, match="unknown material"):
        spool_sync.create_spool(spool_sync.parse_new("Nowbrand, Unobtainium, Red, 1000"), client=client)
    assert len(client("GET", "/api/v1/filament")) == before


def test_fresh_spool_like_copies_type_price_and_weight():
    spec = spool_sync.parse_new("Copybrand, PETG, Copy Black, 800, 111111")
    spec["price"] = 24.5
    original = spool_sync.create_spool(spec, client=client)
    fresh = spool_sync.fresh_spool_like(original["spool_id"], client=client)
    assert fresh["filament_id"] == original["filament_id"]
    assert fresh["grams"] == pytest.approx(800)
    spool = client("GET", f"/api/v1/spool/{fresh['spool_id']}")
    assert spool["location"] == spool_sync.SHELF
    assert spool["price"] == pytest.approx(24.5)
    assert spool["comment"] == spool_sync.REFILL_SPOOL_COMMENT
    # The old spool is left alone.
    assert client("GET", f"/api/v1/spool/{original['spool_id']}")["remaining_weight"] == pytest.approx(800)


def test_extra_field_value_round_trips_through_the_server():
    try:
        client("POST", "/api/v1/field/spool/ph_test_note",
               {"name": "PH test note", "field_type": "text"})
    except Exception:
        pass  # the field already exists from an earlier run
    sid = new_spool("Extrabrand, PLA, Extra White, 1000, FFFFFF")["spool_id"]
    client("PATCH", f"/api/v1/spool/{sid}", {"extra": {"ph_test_note": json.dumps("hello shelf")}})
    record = client("GET", f"/api/v1/spool/{sid}")
    assert spool_sync._decode_extra_value(record["extra"]["ph_test_note"]) == "hello shelf"


# ------------------------------------------------------------ filament / shelf reads against the real server

def test_filament_types_dedupes_case_variants():
    new_spool("Casebrand, PLA, Case Purple, 1000, 800080")
    new_spool("CASEBRAND, PLA, CASE PURPLE, 1000, 800080")
    matches = [t for t in spool_sync.filament_types(client=client)
               if t["material"] == "PLA" and t["color"].casefold() == "case purple"]
    assert len(matches) == 1
    assert matches[0]["hex"] == "800080"


def test_next_spool_id_counts_archived_spools():
    sid = new_spool("Nextbrand, PLA, Next Cyan, 1000, 00FFFF")["spool_id"]
    assert spool_sync.next_spool_id(client=client) == sid + 1
    client("PATCH", f"/api/v1/spool/{sid}", {"archived": True})
    assert spool_sync.next_spool_id(client=client) == sid + 1


def test_shelf_spools_excludes_loaded_and_archived(tmp_path):
    state = hardware_record(tmp_path)
    loaded = new_spool("Shelfbrand, PLA, Shelf A, 1000")["spool_id"]
    waiting = new_spool("Shelfbrand, PLA, Shelf B, 900")["spool_id"]
    archived = new_spool("Shelfbrand, PLA, Shelf C, 900")["spool_id"]
    client("PATCH", f"/api/v1/spool/{archived}", {"archived": True})
    spool_sync.bind_slot(1, loaded, client=client, mr=printer, state_path=state,
                         push_screen_update=False)
    shelf_ids = {s["spool_id"] for s in spool_sync.shelf_spools(client=client, state_path=state)}
    assert waiting in shelf_ids and loaded not in shelf_ids and archived not in shelf_ids
    entry = next(s for s in spool_sync.shelf_spools(client=client, state_path=state)
                 if s["spool_id"] == waiting)
    assert entry["name"] == "Shelf B" and entry["remaining_grams"] == pytest.approx(900)


# ------------------------------------------------------------ bind / unbind / swap against the real server

def test_bind_same_spool_twice_is_idempotent(tmp_path):
    state = hardware_record(tmp_path)
    sid = new_spool("Twicebrand, PLA, Twice Gold, 1000, FFD700")["spool_id"]
    kwargs = dict(client=client, mr=printer, state_path=state,
                  push_screen_update=False, printer_id="test-printer")
    spool_sync.bind_slot(2, sid, **kwargs)
    again = spool_sync.bind_slot(2, sid, **kwargs)
    assert again["prior_spool_id"] == sid
    record = client("GET", f"/api/v1/spool/{sid}")
    assert record["location"] == "IFS Slot 2"
    assert spool_sync.spool_ph_slot(record) == ("test-printer", 2)
    saved = json.loads(state.read_text())
    assert saved["filament_slots"]["2"]["spoolman_id"] == sid


def test_bind_refuses_a_spool_mapped_to_another_slot(tmp_path):
    state = hardware_record(tmp_path)
    sid = new_spool("Mapbrand, PLA, Mapped Magenta, 1000, FF00FF")["spool_id"]
    spool_sync.bind_slot(1, sid, client=client, mr=printer, state_path=state,
                         push_screen_update=False)
    with pytest.raises(spool_sync.SpoolError, match="already mapped to slot 1"):
        spool_sync.bind_slot(3, sid, client=client, mr=printer, state_path=state,
                             push_screen_update=False)


def test_bind_clears_a_stale_claim_on_the_slot(tmp_path):
    state = hardware_record(tmp_path)
    stale = new_spool("Stalebrand, PLA, Stale One, 1000")["spool_id"]
    winner = new_spool("Stalebrand, PLA, Stale Two, 1000")["spool_id"]
    assert spool_sync._set_ph_slot(client, stale, spool_sync.format_ph_slot("test-printer", 4))
    spool_sync.bind_slot(4, winner, client=client, mr=printer, state_path=state,
                         push_screen_update=False, printer_id="test-printer")
    assert spool_sync.spool_ph_slot(client("GET", f"/api/v1/spool/{stale}")) is None
    assert spool_sync.spool_ph_slot(client("GET", f"/api/v1/spool/{winner}")) == ("test-printer", 4)


def test_unbind_returns_the_spool_to_the_shelf(tmp_path):
    state = hardware_record(tmp_path)
    sid = new_spool("Unbindbrand, PLA, Unbind Olive, 1000, 808000")["spool_id"]
    spool_sync.bind_slot(3, sid, client=client, mr=printer, state_path=state,
                         push_screen_update=False, printer_id="test-printer")
    result = spool_sync.unbind_slot(3, client=client, mr=printer, state_path=state,
                                    printer_id="test-printer")
    assert result["spool_id"] == sid
    record = client("GET", f"/api/v1/spool/{sid}")
    assert record["location"] == spool_sync.SHELF
    assert spool_sync.spool_ph_slot(record) is None
    assert "3" not in json.loads(state.read_text())["filament_slots"]
    with pytest.raises(spool_sync.SpoolError, match="has no spool assigned"):
        spool_sync.unbind_slot(3, client=client, mr=printer, state_path=state,
                               printer_id="test-printer")


def test_swap_slots_moves_both_claims(tmp_path):
    state = hardware_record(tmp_path)
    a = new_spool("Swapbrand, PLA, Swap A, 1000")["spool_id"]
    b = new_spool("Swapbrand, PETG, Swap B, 1000")["spool_id"]
    kwargs = dict(client=client, mr=printer, state_path=state,
                  push_screen_update=False, printer_id="test-printer")
    spool_sync.bind_slot(1, a, **kwargs)
    spool_sync.bind_slot(2, b, **kwargs)
    spool_sync.swap_slots(1, 2, client=client, mr=printer, state_path=state,
                          printer_id="test-printer")
    assert spool_sync.spool_ph_slot(client("GET", f"/api/v1/spool/{a}")) == ("test-printer", 2)
    assert spool_sync.spool_ph_slot(client("GET", f"/api/v1/spool/{b}")) == ("test-printer", 1)
    assert client("GET", f"/api/v1/spool/{a}")["location"] == "IFS Slot 2"
    saved = json.loads(state.read_text())
    assert saved["filament_slots"]["1"]["spoolman_id"] == b
    assert saved["filament_slots"]["2"]["spoolman_id"] == a


def test_swap_with_an_empty_slot_is_a_move(tmp_path):
    state = hardware_record(tmp_path)
    a = new_spool("Movebrand, PLA, Move A, 1000")["spool_id"]
    spool_sync.bind_slot(1, a, client=client, mr=printer, state_path=state,
                         push_screen_update=False, printer_id="test-printer")
    spool_sync.swap_slots(1, 3, client=client, mr=printer, state_path=state,
                          printer_id="test-printer")
    assert spool_sync.spool_ph_slot(client("GET", f"/api/v1/spool/{a}")) == ("test-printer", 3)
    saved = json.loads(state.read_text())
    assert "1" not in saved["filament_slots"]
    assert saved["filament_slots"]["3"]["spoolman_id"] == a


def test_bind_delete_disposition_removes_the_replaced_spool(tmp_path):
    state = hardware_record(tmp_path)
    old = new_spool("Delbrand, PLA, Del Old, 1000")["spool_id"]
    new = new_spool("Delbrand, PLA, Del New, 1000")["spool_id"]
    kwargs = dict(client=client, mr=printer, state_path=state,
                  push_screen_update=False, printer_id="test-printer")
    spool_sync.bind_slot(1, old, **kwargs)
    spool_sync.bind_slot(1, new, prior_disposition="delete", **kwargs)
    # The record is gone, not archived: the client reads it as no such spool.
    assert spoolman_client.get_spool(old, base_url=URL) is None
    with pytest.raises(spoolman_client.SpoolmanUnavailable) as caught:
        spoolman_client.request("GET", f"/api/v1/spool/{old}", base_url=URL)
    assert caught.value.status == 404


def test_bind_archive_and_depleted_dispositions_retire_the_replaced_spool(tmp_path):
    state = hardware_record(tmp_path)
    kwargs = dict(client=client, mr=printer, state_path=state,
                  push_screen_update=False, printer_id="test-printer")
    old = new_spool("Retirebrand, PLA, Retire Arch, 1000")["spool_id"]
    new = new_spool("Retirebrand, PLA, Retire Next, 1000")["spool_id"]
    spool_sync.bind_slot(2, old, **kwargs)
    spool_sync.bind_slot(2, new, prior_disposition="archive", **kwargs)
    record = client("GET", f"/api/v1/spool/{old}")
    assert record["archived"] is True
    assert spool_sync.spool_ph_slot(record) is None

    older = new_spool("Retirebrand, PLA, Retire Depl, 500")["spool_id"]
    newest = new_spool("Retirebrand, PLA, Retire Last, 1000")["spool_id"]
    spool_sync.bind_slot(3, older, **kwargs)
    spool_sync.bind_slot(3, newest, prior_disposition="depleted", **kwargs)
    record = client("GET", f"/api/v1/spool/{older}")
    assert record["archived"] is True
    assert record["remaining_weight"] == pytest.approx(0)
    assert record["location"] == "depleted"
    assert spool_sync.spool_ph_slot(record) is None


# ------------------------------------------------------------ cross-printer and reconcile against the real server

def _printer_record(tmp_path, printer_id):
    folder = tmp_path / "printers" / printer_id
    folder.mkdir(parents=True)
    path = folder / "printer_hardware.json"
    path.write_text(json.dumps({"filament_slots": {}}))
    return path


def test_one_spool_cannot_load_on_two_printers(tmp_path):
    state_a = _printer_record(tmp_path, "printer-a")
    state_b = _printer_record(tmp_path, "printer-b")
    sid = new_spool("Dualbrand, PLA, Dual Denim, 1000, 224466")["spool_id"]
    spool_sync.bind_slot(1, sid, client=client, mr=printer, state_path=state_a,
                         push_screen_update=False, printer_id="printer-a")
    with pytest.raises(spool_sync.SpoolError, match="already loaded on printer-a"):
        spool_sync.bind_slot(2, sid, client=client, mr=printer, state_path=state_b,
                             push_screen_update=False, printer_id="printer-b")
    # Printer B's reconcile must not adopt printer A's claimed spool.
    result = spool_sync.reconcile("printer-b", client=client, mr=printer, state_path=state_b)
    assert sid not in result["assignments"].values()
    assert spool_sync.spool_ph_slot(client("GET", f"/api/v1/spool/{sid}")) == ("printer-a", 1)


def test_reconcile_drops_an_archived_spools_assignment(tmp_path):
    state = hardware_record(tmp_path)
    sid = new_spool("Archbrand, PLA, Arch Apricot, 1000, FBCEB1")["spool_id"]
    spool_sync.bind_slot(2, sid, client=client, mr=printer, state_path=state,
                         push_screen_update=False, printer_id="test-printer")
    client("PATCH", f"/api/v1/spool/{sid}", {"archived": True})
    result = spool_sync.reconcile("test-printer", client=client, mr=printer, state_path=state)
    assert 2 not in result["assignments"]
    assert any(f"#{sid}" in c and "archived" in c for c in result["conflicts"])
    assert spool_sync.spool_ph_slot(client("GET", f"/api/v1/spool/{sid}")) is None
    assert "2" not in json.loads(state.read_text())["filament_slots"]


def test_reconcile_names_two_spools_claiming_one_slot(tmp_path):
    state = hardware_record(tmp_path)
    a = new_spool("Clashbrand, PLA, Clash A, 1000")["spool_id"]
    b = new_spool("Clashbrand, PLA, Clash B, 1000")["spool_id"]
    claim = spool_sync.format_ph_slot("test-printer", 1)
    assert spool_sync._set_ph_slot(client, a, claim)
    assert spool_sync._set_ph_slot(client, b, claim)
    result = spool_sync.reconcile("test-printer", client=client, mr=printer, state_path=state)
    assert 1 not in result["assignments"]
    joined = "\n".join(result["conflicts"])
    assert f"#{a}" in joined and f"#{b}" in joined and "both claim" in joined


def test_reconcile_drops_a_deleted_spools_assignment(tmp_path):
    state = hardware_record(tmp_path)
    sid = new_spool("Gonebrand, PLA, Gone Garnet, 1000, 9C0044")["spool_id"]
    spool_sync.bind_slot(4, sid, client=client, mr=printer, state_path=state,
                         push_screen_update=False, printer_id="test-printer")
    client("DELETE", f"/api/v1/spool/{sid}")
    result = spool_sync.reconcile("test-printer", client=client, mr=printer, state_path=state)
    assert 4 not in result["assignments"]
    assert "4" not in json.loads(state.read_text())["filament_slots"]


def test_rebind_after_the_bound_spool_is_archived(tmp_path):
    state = hardware_record(tmp_path)
    old = new_spool("Rebindbrand, PLA, Rebind Old, 1000")["spool_id"]
    new = new_spool("Rebindbrand, PLA, Rebind New, 1000")["spool_id"]
    kwargs = dict(client=client, mr=printer, state_path=state,
                  push_screen_update=False, printer_id="test-printer")
    spool_sync.bind_slot(1, old, **kwargs)
    client("PATCH", f"/api/v1/spool/{old}", {"archived": True})
    spool_sync.reconcile("test-printer", client=client, mr=printer, state_path=state)
    result = spool_sync.bind_slot(1, new, **kwargs)
    assert result["spool_id"] == new
    assert spool_sync.spool_ph_slot(client("GET", f"/api/v1/spool/{new}")) == ("test-printer", 1)


# ------------------------------------------------------------ failure wording through the real wrapper

def test_wrapper_names_a_missing_spool_instead_of_an_outage(monkeypatch):
    monkeypatch.setattr(spool_sync, "SPOOLMAN", URL)
    with pytest.raises(spool_sync.SpoolError, match="has no spool #999999001"):
        spool_sync.spoolman("GET", "/api/v1/spool/999999001")


def test_wrapper_outage_wording_differs_for_reads_and_writes(monkeypatch):
    monkeypatch.setattr(spool_sync, "SPOOLMAN", refused_url())
    with pytest.raises(spool_sync.SpoolError, match="did not answer") as read:
        spool_sync.spoolman("GET", "/api/v1/spool")
    assert "Nothing changed" in str(read.value)
    with pytest.raises(spool_sync.SpoolError, match="may have partly") as write:
        spool_sync.spoolman("PATCH", "/api/v1/spool/1", {"location": "Shelf"})
    assert "Nothing changed" not in str(write.value)


def test_create_spool_propagates_a_dead_server_error():
    def dead(method, path, body=None):
        raise RuntimeError("server went away")
    with pytest.raises(RuntimeError, match="server went away"):
        spool_sync.create_spool(spool_sync.parse_new("Deadbrand, PLA, Dead Red, 1000"), client=dead)


# ------------------------------------------------------------ temperatures resolved from real records

def test_spoolman_temperatures_win_from_a_real_record():
    spec = spool_sync.parse_new("Resbrand, PLA, Res Ruby, 1000, E0115F")
    spec.update({"nozzle_temp": 238, "bed_temp": 68})
    created = spool_sync.create_spool(spec, client=client)
    spool = spoolman_client.get_spool(created["spool_id"], base_url=URL)
    # Even for a variant material label, recorded Spoolman temperatures
    # are explicit data and are never adjusted (#512 standing rule).
    family, targets, sources = printer_state.resolve_material_temperatures(
        "PLA Silk", spool["filament"])
    assert targets == {"nozzle": 238, "bed": 68}
    assert sources == {"nozzle": "Spoolman filament", "bed": "Spoolman filament"}


def test_missing_temperatures_fall_back_to_material_defaults_from_a_real_record():
    created = new_spool("Plainbrand, PLA, Plain Beige, 1000, F5F5DC")
    spool = spoolman_client.get_spool(created["spool_id"], base_url=URL)
    family, targets, sources = printer_state.resolve_material_temperatures(
        "PLA", spool["filament"])
    _, expected, _ = printer_state.resolve_material_temperatures("PLA", {})
    assert targets == expected
    assert sources["nozzle"] == "material default"
