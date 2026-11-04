"""#789: Web UI spool management — API, Spools page, and the spool_sync
unassign/swap primitives beside bind_slot.

Fake Spoolman and fake Moonraker only: nothing here opens a socket to
a printer or to Spoolman. The settings/routing patterns follow
tests/test_webui_controls.py; the fake Spoolman follows
tests/hermes/test_spool_reconcile.py.
"""
import copy
import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from print_handy import settings
from print_handy.hermes import purchase_intake, spool_sync
from print_handy.webui import server, spools

TOKEN = "spool-test-token"
PRINTER = "flashforge-ad5x"


def load_settings(tmp, extra="", environ=None):
    local = Path(tmp) / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {tmp}/hermes\n{extra}")
    return settings.load(local=local, environ={} if environ is None else environ)


def enabled(tmp, extra=""):
    return load_settings(tmp, "webui:\n  controls_enabled: true\n" + extra)


# ---------------------------------------------------------------------- fakes

class FakeSpoolman:
    """A stateful in-memory Spoolman: spools, filaments, vendors, field defs."""

    def __init__(self):
        self.fields = [{"key": "ph_slot", "name": "Print Handy slot",
                        "field_type": "text"}]
        self.vendors = {1: {"id": 1, "name": "Sunlu"}, 2: {"id": 2, "name": "eSun"}}
        self.filaments = {
            31: {"id": 31, "name": "Pink", "material": "PLA", "color_hex": "F95D73",
                 "vendor": {"id": 1, "name": "Sunlu"}, "vendor_id": 1},
            37: {"id": 37, "name": "Coffee", "material": "PLA+", "color_hex": "6F4E37",
                 "vendor": {"id": 2, "name": "eSun"}, "vendor_id": 2},
            39: {"id": 39, "name": "Black", "material": "PLA", "color_hex": "161616",
                 "vendor": {"id": 1, "name": "Sunlu"}, "vendor_id": 1},
            41: {"id": 41, "name": "Gray", "material": "PETG", "color_hex": "808080",
                 "vendor": {"id": 1, "name": "Sunlu"}, "vendor_id": 1},
        }
        self.spools = {
            3: self._spool(3, 31, location="IFS Slot 2", remaining=500, initial=1000,
                           ph_slot=f"{PRINTER}:2"),
            7: self._spool(7, 37, location="Shelf", remaining=800, initial=1000),
            9: self._spool(9, 39, location="Shelf", remaining=250, initial=None),
            11: self._spool(11, 41, location="Shelf", remaining=100, initial=1000,
                            archived=True),
        }
        self.calls = []
        self._next_filament = 100
        self._next_vendor = 100
        self._next_spool = 100

    @staticmethod
    def _spool(sid, filament_id, *, location, remaining, initial, archived=False,
               ph_slot=None):
        row = {"id": sid, "filament_id": filament_id, "location": location,
               "remaining_weight": remaining, "archived": archived, "extra": {}}
        if initial is not None:
            row["initial_weight"] = initial
        if ph_slot is not None:
            row["extra"]["ph_slot"] = json.dumps(ph_slot)
        return row

    def _with_filament(self, row):
        row = copy.deepcopy(row)
        filament = self.filaments.get(row["filament_id"])
        row["filament"] = copy.deepcopy(filament) if filament else {}
        return row

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if method == "GET" and path == "/api/v1/field/spool":
            return copy.deepcopy(self.fields)
        if method == "POST" and path.startswith("/api/v1/field/spool/"):
            self.fields.append({"key": path.rsplit("/", 1)[1], **(body or {})})
            return {}
        if method == "GET" and path.startswith("/api/v1/spool?"):
            archived_ok = "allow_archived=true" in path
            return [self._with_filament(row) for row in self.spools.values()
                    if archived_ok or not row["archived"]]
        if method == "GET" and path == "/api/v1/spool":
            return [self._with_filament(row) for row in self.spools.values()
                    if not row["archived"]]
        if method == "GET" and path.startswith("/api/v1/spool/"):
            row = self.spools.get(int(path.rsplit("/", 1)[1]))
            return self._with_filament(row) if row else {}
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
        if method == "GET" and path == "/api/v1/filament":
            return [copy.deepcopy(f) for f in self.filaments.values()]
        if method == "POST" and path == "/api/v1/filament":
            fid = self._next_filament
            self._next_filament += 1
            row = {"id": fid, **copy.deepcopy(body or {})}
            vendor = self.vendors.get(row.get("vendor_id"))
            row["vendor"] = copy.deepcopy(vendor) if vendor else None
            self.filaments[fid] = row
            return copy.deepcopy(row)
        if method == "PATCH" and path.startswith("/api/v1/filament/"):
            row = self.filaments[int(path.rsplit("/", 1)[1])]
            row.update(copy.deepcopy(body or {}))
            if "vendor_id" in row:
                vendor = self.vendors.get(row["vendor_id"])
                row["vendor"] = copy.deepcopy(vendor) if vendor else None
            return {}
        if method == "GET" and path == "/api/v1/vendor":
            return [copy.deepcopy(v) for v in self.vendors.values()]
        if method == "POST" and path == "/api/v1/vendor":
            vid = self._next_vendor
            self._next_vendor += 1
            self.vendors[vid] = {"id": vid, "name": (body or {})["name"]}
            return copy.deepcopy(self.vendors[vid])
        if method == "POST" and path == "/api/v1/spool":
            sid = self._next_spool
            self._next_spool += 1
            body = body or {}
            row = {"id": sid, "filament_id": body["filament_id"],
                   "location": body.get("location", "Shelf"),
                   "remaining_weight": body.get("remaining_weight"),
                   "initial_weight": body.get("initial_weight"),
                   "archived": False, "extra": {}}
            self.spools[sid] = row
            return {"id": sid}
        return {}

    def patches(self, kind, row_id):
        return [c for c in self.calls
                if c[0] == "PATCH" and c[1] == f"/api/v1/{kind}/{row_id}"]


def fake_mr(active=None):
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


def record(sid, name, material="PLA", color="pink", hex_code="F95D73", grams=500):
    return {"spoolman_id": sid, "name": name, "material": material, "color": color,
            "hex": hex_code, "remaining_grams": grams}


def state_file(tmp, slots=None):
    path = Path(tmp) / "printer_hardware.json"
    path.write_text(json.dumps({
        "spool": {}, "ph_slot_migrated": True,
        "filament_slots": slots if slots is not None else {
            "2": record(3, "Pink PLA", color="pink", hex_code="F95D73", grams=500)},
    }))
    return path


class SpoolCase(unittest.TestCase):
    """Base: a tmp state file, a fake Spoolman, screen pushes stubbed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.data = enabled(self.tmp)
        self.fake = FakeSpoolman()
        self.state = state_file(self.tmp)
        push = mock.patch.object(spool_sync, "push_screen", lambda *a, **k: "pushed")
        adapter = mock.patch.object(spool_sync, "_printer_adapter", lambda pid: None)
        push.start()
        adapter.start()
        self.addCleanup(push.stop)
        self.addCleanup(adapter.stop)

    def deps(self):
        return {"client": self.fake, "mr": fake_mr(), "state_path": self.state}

    def cache_slots(self):
        return json.loads(self.state.read_text())["filament_slots"]


# ---------------------------------------------------------------------- GET

class ViewTests(SpoolCase):
    def test_get_shape_slots_shelf_and_percent(self):
        code, body = spools.spools_view(self.data, PRINTER, client=self.fake,
                                        state_path=self.state)
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual([row["slot"] for row in body["slots"]], [1, 2, 3, 4])
        slot2 = body["slots"][1]["spool"]
        self.assertEqual(slot2["id"], 3)
        self.assertEqual(slot2["material"], "PLA")
        self.assertEqual(slot2["color"], "Pink")
        self.assertEqual(slot2["remaining_weight"], 500.0)
        self.assertEqual(slot2["remaining_percent"], 50.0)
        self.assertIsNone(body["slots"][0]["spool"])
        # Shelf: live, unassigned spools only — not the loaded #3, not
        # the archived #11.
        self.assertEqual([s["id"] for s in body["shelf"]], [7, 9])
        shelf9 = next(s for s in body["shelf"] if s["id"] == 9)
        self.assertIsNone(shelf9["remaining_percent"])  # no starting weight recorded
        self.assertEqual(body["conflicts"], [])

    def test_get_names_a_duplicate_claim_as_a_conflict(self):
        self.fake.spools[9]["extra"]["ph_slot"] = json.dumps(f"{PRINTER}:2")
        code, body = spools.spools_view(self.data, PRINTER, client=self.fake,
                                        state_path=self.state)
        self.assertEqual(code, 200)
        self.assertTrue(any("both claim" in c for c in body["conflicts"]))
        self.assertIsNone(body["slots"][1]["spool"])

    def test_get_unknown_printer_is_404(self):
        code, body = spools.spools_view(self.data, "nope", client=self.fake,
                                        state_path=self.state)
        self.assertEqual(code, 404)
        self.assertIn("nope", body["reason"])

    def test_get_spoolman_down_reason_names_no_exception(self):
        # #840: the operator-facing reason is a plain sentence naming
        # Spoolman — no exception class or repr (that goes to the log).
        def down(method, path, body=None):
            raise urllib.error.URLError(OSError(111, "Connection refused"))

        code, body = spools.spools_view(self.data, PRINTER, client=down,
                                        state_path=self.state)
        self.assertEqual(code, 502)
        self.assertIn("Spoolman did not answer", body["reason"])
        self.assertNotIn("URLError", body["reason"])
        self.assertNotIn("Errno", body["reason"])


# ---------------------------------------------------------------------- guard

class GuardTests(SpoolCase):
    def test_controls_off_is_403_and_writes_nothing(self):
        data = load_settings(self.tmp)
        before = list(self.fake.calls)
        for action, body in (("assign", {"slot": 1, "spool_id": 7}),
                             ("swap", {"slot_a": 1, "slot_b": 2}),
                             ("unassign", {"slot": 2}),
                             ("add", {"material": "PLA", "color": "White", "grams": 1000}),
                             ("edit", {"spool_id": 7, "remaining_weight": 10}),
                             ("sync", {})):
            code, out = spools.spool_action(data, PRINTER, action, body, **self.deps())
            self.assertEqual(code, 403, (action, out))
            self.assertIn("controls_enabled", out["reason"])
        writes = [c for c in self.fake.calls[len(before):] if c[0] in ("POST", "PATCH", "DELETE")]
        self.assertEqual(writes, [])

    def test_every_post_route_refuses_an_unknown_printer(self):
        for action, body in (("assign", {"slot": 1, "spool_id": 7}),
                             ("swap", {"slot_a": 1, "slot_b": 2}),
                             ("unassign", {"slot": 2}),
                             ("add", {"material": "PLA", "color": "White", "grams": 1000}),
                             ("edit", {"spool_id": 7, "remaining_weight": 10}),
                             ("sync", {}),
                             ("purchase/create", {"lines": [{"material": "PLA",
                                                             "color": "Black",
                                                             "grams": 1000,
                                                             "quantity": 1}]})):
            code, out = spools.spool_action(self.data, "ghost", action, body,
                                            **self.deps(), ai_configured=lambda: True)
            self.assertEqual(code, 404, (action, out))
            self.assertIn("ghost", out["reason"])

    def test_unknown_action_is_404(self):
        code, body = spools.spool_action(self.data, PRINTER, "explode", {}, **self.deps())
        self.assertEqual(code, 404)
        self.assertIn("assign", body["reason"])


# ---------------------------------------------------------------------- assign

class AssignTests(SpoolCase):
    def test_assign_happy_path_writes_ph_slot_and_cache(self):
        code, body = spools.assign(self.data, PRINTER, {"slot": 1, "spool_id": 7},
                                   **self.deps())
        self.assertEqual(code, 200, body)
        spool = self.fake.spools[7]
        self.assertEqual(spool["location"], "IFS Slot 1")
        self.assertEqual(json.loads(spool["extra"]["ph_slot"]), f"{PRINTER}:1")
        self.assertEqual(self.cache_slots()["1"]["spoolman_id"], 7)

    def test_assign_replacing_shelves_the_prior_spool(self):
        code, body = spools.assign(self.data, PRINTER, {"slot": 2, "spool_id": 9},
                                   **self.deps())
        self.assertEqual(code, 200, body)
        prior = self.fake.spools[3]
        self.assertEqual(prior["location"], "Shelf")
        self.assertIsNone(prior["extra"].get("ph_slot"))
        self.assertEqual(self.cache_slots()["2"]["spoolman_id"], 9)

    def test_assign_unknown_spool_is_a_named_refusal(self):
        code, body = spools.assign(self.data, PRINTER, {"slot": 1, "spool_id": 999},
                                   **self.deps())
        self.assertEqual(code, 409)
        self.assertIn("Spoolman has no spool #999", body["reason"])
        self.assertNotIn("1", self.cache_slots())

    def test_assign_archived_spool_is_a_named_refusal(self):
        code, body = spools.assign(self.data, PRINTER, {"slot": 1, "spool_id": 11},
                                   **self.deps())
        self.assertEqual(code, 409)
        self.assertIn("archived", body["reason"])

    def test_assign_bad_bodies_are_400(self):
        for body in ({"spool_id": 7}, {"slot": 9, "spool_id": 7},
                     {"slot": 1}, {"slot": "1", "spool_id": 7}, None):
            code, out = spools.assign(self.data, PRINTER, body, **self.deps())
            self.assertEqual(code, 400, body)


# ---------------------------------------------------------------------- swap

class SwapTests(SpoolCase):
    def test_swap_exchanges_both_sides(self):
        spools.assign(self.data, PRINTER, {"slot": 1, "spool_id": 7}, **self.deps())
        code, body = spools.swap(self.data, PRINTER, {"slot_a": 1, "slot_b": 2},
                                 **self.deps())
        self.assertEqual(code, 200, body)
        self.assertEqual(body["spool_in_a"], 3)
        self.assertEqual(body["spool_in_b"], 7)
        self.assertEqual(json.loads(self.fake.spools[3]["extra"]["ph_slot"]), f"{PRINTER}:1")
        self.assertEqual(json.loads(self.fake.spools[7]["extra"]["ph_slot"]), f"{PRINTER}:2")
        slots = self.cache_slots()
        self.assertEqual(slots["1"]["spoolman_id"], 3)
        self.assertEqual(slots["2"]["spoolman_id"], 7)

    def test_swap_both_empty_and_same_slot_refuse(self):
        code, body = spools.swap(self.data, PRINTER, {"slot_a": 3, "slot_b": 4},
                                 **self.deps())
        self.assertEqual(code, 409)
        self.assertIn("both empty", body["reason"])
        code, body = spools.swap(self.data, PRINTER, {"slot_a": 2, "slot_b": 2},
                                 **self.deps())
        self.assertEqual(code, 409)
        self.assertIn("itself", body["reason"])


# ---------------------------------------------------------------------- unassign

class UnassignTests(SpoolCase):
    def test_unassign_shelves_spool_and_removes_cache_record(self):
        code, body = spools.unassign(self.data, PRINTER, {"slot": 2}, **self.deps())
        self.assertEqual(code, 200, body)
        self.assertEqual(body["spool_id"], 3)
        spool = self.fake.spools[3]
        self.assertEqual(spool["location"], "Shelf")
        self.assertIsNone(spool["extra"].get("ph_slot"))
        self.assertNotIn("2", self.cache_slots())
        code, view = spools.spools_view(self.data, PRINTER, client=self.fake,
                                        state_path=self.state)
        self.assertIsNone(view["slots"][1]["spool"])
        self.assertIn(3, [s["id"] for s in view["shelf"]])

    def test_unassign_empty_slot_refuses(self):
        code, body = spools.unassign(self.data, PRINTER, {"slot": 3}, **self.deps())
        self.assertEqual(code, 409)
        self.assertIn("no spool assigned", body["reason"])


# ---------------------------------------------------------------------- add

class AddTests(SpoolCase):
    def test_add_creates_a_shelf_spool_reusing_a_known_filament(self):
        code, body = spools.add(self.data, PRINTER,
                                {"brand": "Sunlu", "material": "PLA", "color": "Pink",
                                 "grams": 1000}, **self.deps())
        self.assertEqual(code, 200, body)
        self.assertTrue(body["reused_filament"])
        created = self.fake.spools[body["spool_id"]]
        self.assertEqual(created["location"], "Shelf")
        self.assertEqual(created["filament_id"], 31)

    def test_add_with_a_new_filament_type(self):
        code, body = spools.add(self.data, PRINTER,
                                {"brand": "Sunlu", "material": "PLA", "color": "White",
                                 "grams": 500}, **self.deps())
        self.assertEqual(code, 200, body)
        self.assertFalse(body["reused_filament"])

    def test_add_bad_bodies_are_400_and_create_nothing(self):
        before = len(self.fake.spools)
        for body in ({"material": "", "color": "White", "grams": 1000},
                     {"material": "PLA", "color": "White", "grams": 0},
                     {"material": "PLA", "color": "White"},
                     {"material": "PLA", "color": "White", "grams": 1000,
                      "hex": "not-a-color"}):
            code, _ = spools.add(self.data, PRINTER, body, **self.deps())
            self.assertEqual(code, 400, body)
        self.assertEqual(len(self.fake.spools), before)


# ---------------------------------------------------------------------- edit

class EditTests(SpoolCase):
    def test_edit_writes_spool_and_filament_fields(self):
        code, body = spools.edit(self.data, PRINTER,
                                 {"spool_id": 7, "remaining_weight": 321,
                                  "color_hex": "112233", "brand": "NewBrand"},
                                 **self.deps())
        self.assertEqual(code, 200, body)
        self.assertEqual(self.fake.spools[7]["remaining_weight"], 321.0)
        filament = self.fake.filaments[37]
        self.assertEqual(filament["color_hex"], "112233")
        self.assertEqual(filament["vendor"]["name"], "NewBrand")
        self.assertEqual(body["spool"]["remaining_weight"], 321.0)
        self.assertIn("filament record", body["detail"])

    def test_edit_name_and_material(self):
        code, _ = spools.edit(self.data, PRINTER,
                              {"spool_id": 7, "name": "Mocha", "material": "PLA"},
                              **self.deps())
        self.assertEqual(code, 200)
        self.assertEqual(self.fake.filaments[37]["name"], "Mocha")
        self.assertEqual(self.fake.filaments[37]["material"], "PLA")

    def test_edit_archive_and_unarchive(self):
        code, _ = spools.edit(self.data, PRINTER, {"spool_id": 7, "archived": True},
                              **self.deps())
        self.assertEqual(code, 200)
        self.assertTrue(self.fake.spools[7]["archived"])
        code, _ = spools.edit(self.data, PRINTER, {"spool_id": 7, "archived": False},
                              **self.deps())
        self.assertEqual(code, 200)
        self.assertFalse(self.fake.spools[7]["archived"])

    def test_edit_transport_failure_reason_names_no_exception(self):
        # #840: _call's 502 wording drops the exception class/repr too.
        def down(method, path, body=None):
            raise RuntimeError("kaput")

        code, body = spools.edit(self.data, PRINTER,
                                 {"spool_id": 7, "remaining_weight": 1}, client=down)
        self.assertEqual(code, 502)
        self.assertIn("did not complete", body["reason"])
        self.assertNotIn("RuntimeError", body["reason"])
        self.assertNotIn("kaput", body["reason"])

    def test_edit_refusals_are_named(self):
        code, body = spools.edit(self.data, PRINTER,
                                 {"spool_id": 999, "remaining_weight": 1}, **self.deps())
        self.assertEqual(code, 404)
        self.assertIn("no spool #999", body["reason"])
        code, body = spools.edit(self.data, PRINTER, {"spool_id": 7}, **self.deps())
        self.assertEqual(code, 400)
        self.assertIn("at least one field", body["reason"])
        code, body = spools.edit(self.data, PRINTER,
                                 {"spool_id": 7, "remaining_weight": -1}, **self.deps())
        self.assertEqual(code, 400)


# ---------------------------------------------------------------------- sync

class SyncTests(SpoolCase):
    def test_sync_reconciles_a_spoolman_side_claim(self):
        # A claim made in Spoolman directly: spool #7 now claims slot 3.
        self.fake.spools[7]["extra"]["ph_slot"] = json.dumps(f"{PRINTER}:3")
        self.fake.spools[7]["location"] = "IFS Slot 3"
        code, body = spools.sync(self.data, PRINTER, {}, **self.deps())
        self.assertEqual(code, 200, body)
        self.assertEqual(body["assignments"].get(3), 7)
        self.assertEqual(body["assignments"].get(2), 3)
        self.assertEqual(self.cache_slots()["3"]["spoolman_id"], 7)


# ---------------------------------------------------------------------- purchase

LINES = [{"brand": "Sunlu", "material": "PLA", "color": "Black", "hex": None,
          "grams": 1000.0, "quantity": 1, "price": None, "nozzle_temp": None,
          "bed_temp": None, "purchase_url": "https://shop.example/pla"}]


class PurchaseTests(SpoolCase):
    def test_propose_without_an_ai_route_refuses_naming_why(self):
        called = []
        code, body = spools.propose_purchase(
            self.data, PRINTER, {"url": "https://shop.example/pla"},
            ai_configured=lambda: False,
            propose=lambda url: called.append(url) or LINES)
        self.assertEqual(code, 403)
        self.assertIn("AI route", body["reason"])
        self.assertEqual(called, [])

    def test_propose_shows_lines_and_creates_nothing(self):
        before = len(self.fake.spools)
        code, body = spools.propose_purchase(
            self.data, PRINTER, {"url": "https://shop.example/pla"},
            ai_configured=lambda: True, propose=lambda url: LINES)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["lines"], LINES)
        self.assertIn("Nothing is created yet", body["detail"])
        self.assertEqual(len(self.fake.spools), before)

    def test_propose_blocked_page_is_a_named_refusal(self):
        def blocked(url):
            raise purchase_intake.PurchaseError("blocked")

        code, body = spools.propose_purchase(
            self.data, PRINTER, {"url": "https://shop.example/pla"},
            ai_configured=lambda: True, propose=blocked)
        self.assertEqual(code, 400)
        self.assertIn("screenshot", body["reason"])

    def test_create_after_confirm_creates_on_the_shelf(self):
        code, body = spools.create_purchase_spools(
            self.data, PRINTER, {"lines": LINES}, client=self.fake)
        self.assertEqual(code, 200, body)
        self.assertEqual(len(body["created"]), 1)
        created = self.fake.spools[body["created"][0]["spool_id"]]
        self.assertEqual(created["location"], "Shelf")

    def test_create_with_no_lines_is_400(self):
        code, body = spools.create_purchase_spools(
            self.data, PRINTER, {"lines": []}, client=self.fake)
        self.assertEqual(code, 400)


# ------------------------------------------------------- spool_sync primitives

class PrimitiveTests(SpoolCase):
    def test_unbind_slot_clears_claim_location_and_cache(self):
        result = spool_sync.unbind_slot(2, client=self.fake, mr=fake_mr(),
                                        state_path=self.state, printer_id=PRINTER)
        self.assertEqual(result["spool_id"], 3)
        self.assertEqual(self.fake.spools[3]["location"], "Shelf")
        self.assertIsNone(self.fake.spools[3]["extra"].get("ph_slot"))
        self.assertNotIn("2", self.cache_slots())

    def test_unbind_slot_names_a_cache_spoolman_conflict(self):
        self.fake.spools[9]["extra"]["ph_slot"] = json.dumps(f"{PRINTER}:2")
        with self.assertRaises(spool_sync.SpoolError) as raised:
            spool_sync.unbind_slot(2, client=self.fake, mr=fake_mr(),
                                   state_path=self.state, printer_id=PRINTER)
        self.assertIn("#3", str(raised.exception))
        self.assertIn("#9", str(raised.exception))
        self.assertIn("2", self.cache_slots())  # nothing changed

    def test_swap_slots_with_an_empty_side_is_a_move(self):
        result = spool_sync.swap_slots(2, 4, client=self.fake, mr=fake_mr(),
                                       state_path=self.state, printer_id=PRINTER)
        self.assertIsNone(result["spool_in_a"])
        self.assertEqual(result["spool_in_b"], 3)
        self.assertEqual(json.loads(self.fake.spools[3]["extra"]["ph_slot"]),
                         f"{PRINTER}:4")
        slots = self.cache_slots()
        self.assertNotIn("2", slots)
        self.assertEqual(slots["4"]["spoolman_id"], 3)

    def test_swap_slots_refuses_when_spoolman_claims_disagree_with_the_cache(self):
        # Spoolman now assigns #9 to slot 2 while the cache still says #3:
        # swapping the cached ids would move a spool nobody picked and
        # clear #9's legitimate claim, so the swap refuses by name.
        self.fake.spools[9]["extra"]["ph_slot"] = json.dumps(f"{PRINTER}:2")
        with self.assertRaises(spool_sync.SpoolError) as raised:
            spool_sync.swap_slots(2, 4, client=self.fake, mr=fake_mr(),
                                  state_path=self.state, printer_id=PRINTER)
        self.assertIn("#3", str(raised.exception))
        self.assertIn("#9", str(raised.exception))
        self.assertEqual(json.loads(self.fake.spools[9]["extra"]["ph_slot"]), f"{PRINTER}:2")
        self.assertEqual(self.cache_slots()["2"]["spoolman_id"], 3)  # nothing changed

    def test_swap_slots_uses_a_claim_the_cache_has_not_caught_up_with(self):
        self.state = state_file(self.tmp, slots={})
        result = spool_sync.swap_slots(2, 4, client=self.fake, mr=fake_mr(),
                                       state_path=self.state, printer_id=PRINTER)
        self.assertEqual(result["spool_in_b"], 3)
        self.assertEqual(json.loads(self.fake.spools[3]["extra"]["ph_slot"]),
                         f"{PRINTER}:4")

    def test_unbind_slot_clears_the_active_spool_reference_and_charging(self):
        raw = json.loads(self.state.read_text())
        raw["spool"] = {"spoolman_id": 3, "name": "Pink PLA"}
        self.state.write_text(json.dumps(raw))
        active = [3]
        result = spool_sync.unbind_slot(2, client=self.fake, mr=fake_mr(active),
                                        state_path=self.state, printer_id=PRINTER)
        self.assertEqual(result["spool_id"], 3)
        saved = json.loads(self.state.read_text())
        self.assertEqual(saved["spool"], {})  # no longer classified as loaded
        self.assertIsNone(active[0])  # Moonraker charges no one now
        self.assertIn("charging stopped", result["usage"])


# ---------------------------------------------------------------------- routes

class RouteTests(unittest.TestCase):
    def start(self, tmp, extra="", environ=None, spools_view_func=None,
              spools_action_func=None):
        environ = {"WEBUI_ACCESS_TOKEN": TOKEN} if environ is None else environ
        data = load_settings(tmp, "webui:\n  enabled: true\n  port: 8765\n" + extra,
                             environ=environ)
        httpd = server.make_server(data, environ=environ, port=0,
                                   snapshot_func=lambda: {},
                                   spools_view_func=spools_view_func,
                                   spools_action_func=spools_action_func)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return httpd

    def request(self, port, method, path, body=b"", headers=None):
        code, _headers, raw = self.request_full(port, method, path, body, headers)
        return code, raw

    def request_full(self, port, method, path, body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode()
        finally:
            conn.close()

    def test_api_auth_json_and_dispatch(self):
        seen = []

        def fake_view(printer_id):
            return 200, {"ok": True, "id": printer_id, "slots": [], "shelf": [],
                         "spools": [], "conflicts": []}

        def fake_action(printer_id, action, body):
            seen.append((printer_id, action, body))
            return 200, {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            httpd = self.start(tmp, spools_view_func=fake_view,
                               spools_action_func=fake_action)
            port = httpd.server_address[1]
            path = f"/api/printers/{PRINTER}/spools"
            self.assertEqual(self.request(port, "GET", path)[0], 401)
            code, raw = self.request(port, "GET", path,
                                     headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(raw)["id"], PRINTER)
            post = f"{path}/assign"
            self.assertEqual(self.request(port, "POST", post, b"{}",
                                          headers={"Content-Type": "application/json"})[0],
                             401)
            self.assertEqual(self.request(
                port, "POST", post, b"slot=1", headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    server.TOKEN_HEADER: TOKEN})[0], 415)
            self.assertEqual(self.request(
                port, "POST", f"{path}/explode", b"{}", headers={
                    "Content-Type": "application/json",
                    server.TOKEN_HEADER: TOKEN})[0], 404)
            code, _ = self.request(port, "POST", post, b'{"slot": 1, "spool_id": 7}',
                                   headers={"Content-Type": "application/json",
                                            server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
            code, _ = self.request(port, "POST", f"{path}/purchase/propose",
                                   b'{"url": "https://shop.example/x"}',
                                   headers={"Content-Type": "application/json",
                                            server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
        self.assertEqual(seen, [(PRINTER, "assign", {"slot": 1, "spool_id": 7}),
                                (PRINTER, "purchase/propose",
                                 {"url": "https://shop.example/x"})])

    def test_real_wiring_unknown_printer_and_controls_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd = self.start(tmp)
            port = httpd.server_address[1]
            code, raw = self.request(port, "GET", "/api/printers/ghost/spools",
                                     headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 404)
            self.assertIn("ghost", json.loads(raw)["reason"])
            code, raw = self.request(
                port, "POST", f"/api/printers/{PRINTER}/spools/assign",
                b'{"slot": 1, "spool_id": 7}',
                headers={"Content-Type": "application/json",
                         server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 403)
            self.assertIn("controls_enabled", json.loads(raw)["reason"])

    def test_spools_pages_render_and_post_with_the_form_token(self):
        seen = []
        view = {"ok": True, "id": PRINTER, "name": "AD5X", "model": "flashforge_ad5x",
                "slots": [{"slot": 1, "spool": None},
                          {"slot": 2, "spool": {"id": 3, "name": "Pink PLA",
                                                "color": "Pink", "material": "PLA",
                                                "brand": "Sunlu", "color_hex": "F95D73",
                                                "remaining_weight": 500.0,
                                                "initial_weight": 1000.0,
                                                "remaining_percent": 50.0,
                                                "archived": False, "location": "IFS Slot 2"}},
                          {"slot": 3, "spool": None}, {"slot": 4, "spool": None}],
                "shelf": [{"id": 7, "name": "Coffee PLA+", "color": "Coffee",
                           "material": "PLA+", "brand": "eSun", "color_hex": "6F4E37",
                           "remaining_weight": 800.0, "initial_weight": 1000.0,
                           "remaining_percent": 80.0, "archived": False,
                           "location": "Shelf"}],
                "spools": [], "conflicts": []}
        view["spools"] = [view["slots"][1]["spool"], view["shelf"][0]]

        def fake_action(printer_id, action, body):
            seen.append((printer_id, action, body))
            return 200, {"ok": True, "detail": "Slot 1 → spool #7."}

        with tempfile.TemporaryDirectory() as tmp:
            httpd = self.start(tmp, spools_view_func=lambda pid: (200, view),
                               spools_action_func=fake_action)
            port = httpd.server_address[1]
            code, raw = self.request(port, "GET", "/spools",
                                     headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
            self.assertIn(PRINTER, raw)
            code, raw = self.request(port, "GET", f"/spools/{PRINTER}",
                                     headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
            self.assertIn("Pink", raw)
            self.assertIn('name="spool_id"', raw)  # pickers, no raw-id typing
            # The purchase section is present either as the link form (an
            # AI route is configured in this environment) or as the plain
            # no-AI refusal; the refusal wording itself is pinned at the
            # function level in PurchaseTests.
            self.assertIn("Add spools from a purchase link", raw)
            form = urllib.parse.urlencode(
                {"spool_action": "assign", "slot": "1", "spool_id": "7"}).encode()
            code, _ = self.request(port, "POST", f"/spools/{PRINTER}", form,
                                   headers={"Content-Type":
                                            "application/x-www-form-urlencoded"})
            self.assertEqual(code, 403)  # no form token, no header
            form = urllib.parse.urlencode(
                {"_csrf": httpd.form_token, "spool_action": "assign",
                 "slot": "1", "spool_id": "7"}).encode()
            # #840: success is Post/Redirect/Get — the POST itself is a
            # 303 to the page with the action's detail as the notice, so
            # a reload never repeats the action.
            code, headers, _ = self.request_full(
                port, "POST", f"/spools/{PRINTER}", form,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 303)
            location = headers["Location"]
            self.assertTrue(location.startswith(f"/spools/{PRINTER}?done="), location)
            code, raw = self.request(port, "GET", location,
                                     headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
            self.assertIn("Slot 1", raw)  # the detail round-trips as the notice
        self.assertEqual(seen, [(PRINTER, "assign", {"slot": 1, "spool_id": 7})])

    # ---------------------------------------------------------- #840 pages

    def test_spools_page_spoolman_down_is_a_themed_html_error(self):
        def down_view(printer_id):
            return 502, {"ok": False,
                         "reason": "Spoolman did not answer, so the spool view "
                                   "cannot be built"}

        with tempfile.TemporaryDirectory() as tmp:
            httpd = self.start(tmp, spools_view_func=down_view)
            port = httpd.server_address[1]
            code, headers, raw = self.request_full(
                port, "GET", f"/spools/{PRINTER}",
                headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 502)  # the status code is unchanged
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn('<header class="site">', raw)
            self.assertIn("Spoolman did not answer", raw)
            self.assertIn('href="/config"', raw)  # the Configuration hint
            self.assertNotIn('"ok": false', raw)
            self.assertNotIn("URLError", raw)

    def test_spools_page_unknown_printer_is_an_html_404(self):
        def missing_view(printer_id):
            return 404, {"ok": False,
                         "reason": f"no printer with id {printer_id!r} is configured"}

        with tempfile.TemporaryDirectory() as tmp:
            httpd = self.start(tmp, spools_view_func=missing_view)
            port = httpd.server_address[1]
            code, headers, raw = self.request_full(
                port, "GET", "/spools/nope", headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 404)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn('<header class="site">', raw)
            self.assertIn("Pick a printer", raw)
            self.assertNotIn('"ok": false', raw)
            # A malformed id (extra path segment) is the same HTML 404.
            code, _, raw = self.request_full(
                port, "GET", "/spools/a/b", headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 404)
            self.assertIn("Pick a printer", raw)

    def test_api_spools_route_stays_json_when_spoolman_is_down(self):
        def down_view(printer_id):
            return 502, {"ok": False,
                         "reason": "Spoolman did not answer, so the spool view "
                                   "cannot be built"}

        with tempfile.TemporaryDirectory() as tmp:
            httpd = self.start(tmp, spools_view_func=down_view)
            port = httpd.server_address[1]
            code, headers, raw = self.request_full(
                port, "GET", f"/api/printers/{PRINTER}/spools",
                headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 502)
            self.assertIn("application/json", headers["Content-Type"])
            self.assertFalse(json.loads(raw)["ok"])

    def test_spools_post_stale_form_token_is_an_html_refusal(self):
        view = {"ok": True, "id": PRINTER, "name": "AD5X", "slots": [],
                "shelf": [], "spools": [], "conflicts": []}
        with tempfile.TemporaryDirectory() as tmp:
            httpd = self.start(tmp, spools_view_func=lambda pid: (200, view),
                               spools_action_func=lambda pid, a, b: (200, {"ok": True}))
            port = httpd.server_address[1]
            form = urllib.parse.urlencode(
                {"spool_action": "sync"}).encode()  # no form token at all
            code, headers, raw = self.request_full(
                port, "POST", f"/spools/{PRINTER}", form,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 403)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn('<header class="site">', raw)
            self.assertIn("out of date (Print Handy restarted)", raw)
            self.assertNotIn('"ok": false', raw)

    def test_spools_post_refusal_keeps_the_direct_render(self):
        view = {"ok": True, "id": PRINTER, "name": "AD5X",
                "slots": [{"slot": n, "spool": None} for n in (1, 2, 3, 4)],
                "shelf": [], "spools": [], "conflicts": []}

        def refusing_action(printer_id, action, body):
            return 409, {"ok": False, "reason": "slot 1 has no spool assigned"}

        with tempfile.TemporaryDirectory() as tmp:
            httpd = self.start(tmp, spools_view_func=lambda pid: (200, view),
                               spools_action_func=refusing_action)
            port = httpd.server_address[1]
            form = urllib.parse.urlencode(
                {"_csrf": httpd.form_token, "spool_action": "unassign",
                 "slot": "1"}).encode()
            code, headers, raw = self.request_full(
                port, "POST", f"/spools/{PRINTER}", form,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 409)  # no redirect: the refusal stays on screen
            self.assertNotIn("Location", headers)
            self.assertIn("slot 1 has no spool assigned", raw)

    def test_spools_purchase_propose_keeps_the_direct_render(self):
        view = {"ok": True, "id": PRINTER, "name": "AD5X",
                "slots": [{"slot": n, "spool": None} for n in (1, 2, 3, 4)],
                "shelf": [], "spools": [], "conflicts": []}

        def propose_action(printer_id, action, body):
            return 200, {"ok": True, "detail": "proposal", "lines": LINES}

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(spools, "purchase_available", lambda: True):
            httpd = self.start(tmp, spools_view_func=lambda pid: (200, view),
                               spools_action_func=propose_action)
            port = httpd.server_address[1]
            form = urllib.parse.urlencode(
                {"_csrf": httpd.form_token, "spool_action": "purchase_propose",
                 "url": "https://shop.example/pla"}).encode()
            code, headers, raw = self.request_full(
                port, "POST", f"/spools/{PRINTER}", form,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 200)  # proposed lines render on the same page
            self.assertNotIn("Location", headers)
            self.assertIn("Create these spools", raw)
            self.assertIn('data-busy="Creating', raw)

    def test_spools_page_carries_the_shared_submit_script_and_button_attrs(self):
        view = {"ok": True, "id": PRINTER, "name": "AD5X",
                "slots": [{"slot": 1, "spool": {"id": 3, "name": "Pink PLA",
                                                "color": "Pink", "material": "PLA",
                                                "brand": "Sunlu", "color_hex": "F95D73",
                                                "remaining_weight": 500.0,
                                                "initial_weight": 1000.0,
                                                "remaining_percent": 50.0,
                                                "archived": False,
                                                "location": "IFS Slot 1"}},
                          {"slot": 2, "spool": None}, {"slot": 3, "spool": None},
                          {"slot": 4, "spool": None}],
                "shelf": [{"id": 7, "name": "Coffee PLA+", "color": "Coffee",
                           "material": "PLA+", "brand": "eSun", "color_hex": "6F4E37",
                           "remaining_weight": 800.0, "initial_weight": 1000.0,
                           "remaining_percent": 80.0, "archived": False,
                           "location": "Shelf"}],
                "spools": [], "conflicts": []}
        view["spools"] = [view["slots"][0]["spool"], view["shelf"][0]]
        with tempfile.TemporaryDirectory() as tmp:
            httpd = self.start(tmp, spools_view_func=lambda pid: (200, view))
            port = httpd.server_address[1]
            code, raw = self.request(port, "GET", f"/spools/{PRINTER}",
                                     headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 200)
            self.assertIn("<script>", raw)  # the shell's one shared submit script
            self.assertIn("dataset.confirm", raw)
            self.assertIn('data-busy="Syncing', raw)      # Sync
            self.assertIn("data-confirm", raw)            # Unassign / Swap forms
            self.assertIn('class="table-scroll"', raw)    # the shelf/loaded wrap

    def test_unknown_browser_get_is_an_html_refusal_api_stays_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            httpd = self.start(tmp)
            port = httpd.server_address[1]
            code, headers, raw = self.request_full(
                port, "GET", "/nope",
                headers={server.TOKEN_HEADER: TOKEN, "Accept": "text/html"})
            self.assertEqual(code, 404)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn('<header class="site">', raw)
            self.assertNotIn('"ok": false', raw)
            code, headers, raw = self.request_full(
                port, "GET", "/nope", headers={server.TOKEN_HEADER: TOKEN})
            self.assertEqual(code, 404)
            self.assertIn("application/json", headers["Content-Type"])
            self.assertFalse(json.loads(raw)["ok"])


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------ material and value pickers

class PickerFormTests(unittest.TestCase):
    """Material is picked as primary type then subtype; a brand or color
    outside the pickers needs an explicit yes before it reaches Spoolman."""

    VIEW = {"id": PRINTER, "slots": [], "shelf": [], "conflicts": [],
            "spools": [{"id": 7, "name": "Black PLA", "color": "Black", "material": "PLA",
                        "brand": "Sunlu", "archived": False, "remaining_weight": 500.0,
                        "initial_weight": 1000.0, "remaining_percent": 50.0,
                        "color_hex": None, "location": None},
                       {"id": 8, "name": "x", "color": "Teal", "material": "PLA",
                        "brand": "Unknown", "archived": False, "remaining_weight": None,
                        "initial_weight": None, "remaining_percent": None,
                        "color_hex": None, "location": None}]}

    @staticmethod
    def _form(**fields):
        return {k: [v] for k, v in fields.items()}

    def test_add_form_groups_materials_and_offers_known_values(self):
        html = spools.render_spools_page(self.VIEW, form_token="t")
        self.assertIn('<optgroup label="PLA">', html)
        self.assertIn('<option value="PLA+">PLA+</option>', html)
        self.assertIn('<option value="Silk PLA">Silk PLA</option>', html)
        self.assertIn('<option value="Sunlu">Sunlu</option>', html)
        self.assertNotIn('<option value="Unknown">', html)  # placeholder brand hidden
        self.assertIn('<option value="Teal">Teal</option>', html)
        self.assertNotIn('<input name="material"', html)  # no free-text material

    def test_picked_values_pass_straight_through(self):
        _, body = spools.form_body("add", self._form(material="PLA+", brand="Sunlu",
                                                  color="Black", grams="1000"))
        self.assertEqual((body["material"], body["brand"], body["color"]), ("PLA+", "Sunlu", "Black"))

    def test_other_value_needs_the_explicit_yes(self):
        with self.assertRaises(spools.SpoolsError) as refused:
            spools.form_body("add", self._form(material="PLA", brand="__other__",
                                               brand_new="Polymaker", color="Black", grams="1000"))
        self.assertIn("Add new values", refused.exception.reason)
        _, body = spools.form_body("add", self._form(material="PLA", brand="__other__",
                                                  brand_new="Polymaker", confirm_new="true",
                                                  color="Black", grams="1000"))
        self.assertEqual(body["brand"], "Polymaker")

    def test_unknown_material_is_refused_by_the_api(self):
        with mock.patch.object(spools, "_resolve_write", return_value={"id": PRINTER}):
            code, body = spools.add({}, PRINTER, {"material": "Unobtainium", "color": "Black",
                                                  "grams": 1000})
        self.assertEqual(code, 400)
        self.assertIn("not a known material", body["reason"])


class CanonicalVendorTests(SpoolCase):
    """Review of #879: an existing vendor is matched by canonical brand name."""

    def test_existing_bambu_vendor_is_reused_for_bambu_lab(self):
        from print_handy.hermes import spool_sync
        self.fake.vendors[3] = {"id": 3, "name": "Bambu"}
        before = len(self.fake.vendors)
        spool_sync.create_spool({"brand": "Bambu Lab", "material": "PLA", "color": "Cobalt Teal",
                                 "grams": 1000}, client=self.fake)
        self.assertEqual(len(self.fake.vendors), before)
        self.assertFalse([c for c in self.fake.calls if c[:2] == ("POST", "/api/v1/vendor")])
