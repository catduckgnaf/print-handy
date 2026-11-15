"""#1515: the Web UI's slot actions are in chat too.

Rename (the per-spool ``ph_nickname``), put back on shelf, archive and
unarchive. Offline: a fake Spoolman callable and a fake hardware record;
the chat flow runs through the same harness as the guided spool tests.
"""
import json
from unittest import mock

from print_handy.bot import commands
from print_handy.runtime import spool_sync

from tests.runtime.test_spool_guided import STATE, _run


class Spoolman:
    """Spoolman with one shelf spool (#7), one loaded spool (#12) and an archived one (#19)."""

    def __init__(self, fields=None):
        self.spools = {
            7: {"id": 7, "remaining_weight": 400.0, "archived": False, "extra": {},
                "filament": {"name": "Matte White", "material": "PLA+"}},
            12: {"id": 12, "remaining_weight": 1000.0, "archived": False, "extra": {},
                 "filament": {"name": "Red", "material": "PETG"}},
            19: {"id": 19, "remaining_weight": 50.0, "archived": True, "extra": {},
                 "filament": {"name": "Silk", "material": "PLA"}},
        }
        self.fields = list(fields or [])
        self.writes = []

    def __call__(self, method, path, body=None):
        if method == "GET" and path.startswith("/api/v1/spool/"):
            return json.loads(json.dumps(self.spools.get(int(path.rsplit("/", 1)[1]))))
        if method == "GET" and path == "/api/v1/field/spool":
            return [{"key": key} for key in self.fields]
        if method == "GET" and path == "/api/v1/spool?allow_archived=false":
            return [json.loads(json.dumps(s)) for s in self.spools.values() if not s["archived"]]
        self.writes.append((method, path, body))
        if method == "POST" and path.startswith("/api/v1/field/spool/"):
            self.fields.append(path.rsplit("/", 1)[1])
        if method == "PATCH" and path.startswith("/api/v1/spool/"):
            spool = self.spools[int(path.rsplit("/", 1)[1])]
            for key, value in body.items():
                if key == "extra":
                    spool["extra"].update({k: v for k, v in value.items() if v is not None})
                    for k, v in value.items():
                        if v is None:
                            spool["extra"].pop(k, None)
                else:
                    spool[key] = value
        return {}


def state():
    return json.loads(json.dumps(STATE))


def test_rename_sets_only_this_spools_nickname():
    fake = Spoolman()
    text = spool_sync.rename_spool_text(7, "  Lucy's   pink ", client=fake)
    assert "Spool #7 is now called \"Lucy's pink\"" in text
    assert ("POST", "/api/v1/field/spool/ph_nickname",
            {"name": "Print Handy nickname", "field_type": "text"}) in fake.writes
    patch = [w for w in fake.writes if w[0] == "PATCH"]
    assert patch == [("PATCH", "/api/v1/spool/7", {"extra": {"ph_nickname": json.dumps("Lucy's pink")}})]
    assert not any("/filament" in w[1] for w in fake.writes)  # the shared record is untouched


def test_an_empty_rename_clears_the_nickname_and_a_long_one_is_refused():
    fake = Spoolman(fields=["ph_nickname"])
    assert "cleared" in spool_sync.rename_spool_text(7, "", client=fake)
    assert fake.writes[-1] == ("PATCH", "/api/v1/spool/7", {"extra": {"ph_nickname": None}})
    fake.writes.clear()
    assert "60 characters" in spool_sync.rename_spool_text(7, "x" * 61, client=fake)
    assert fake.writes == []


def test_archive_refuses_a_loaded_spool_and_archives_a_shelf_spool():
    fake = Spoolman()
    with mock.patch.object(spool_sync, "load_state", return_value=state()):
        refused = spool_sync.set_archived_text(12, True, client=fake)
        done = spool_sync.set_archived_text(7, True, client=fake)
    assert "loaded in a slot" in refused and "Nothing changed" in refused
    assert fake.writes == [("PATCH", "/api/v1/spool/7", {"archived": True})]
    assert "is archived" in done and "/spool unarchive 7" in done


def test_unarchive_brings_a_spool_back_and_a_no_op_says_so():
    fake = Spoolman()
    assert "is back on the shelf" in spool_sync.set_archived_text(19, False, client=fake)
    assert fake.writes == [("PATCH", "/api/v1/spool/19", {"archived": False})]
    fake.writes.clear()
    assert "already not archived" in spool_sync.set_archived_text(7, False, client=fake)
    assert fake.writes == []


def test_chat_lists_show_a_spools_own_name():
    fake = Spoolman()
    fake.spools[7]["extra"]["ph_nickname"] = json.dumps("Lucy's pink")
    fake.spools[12]["extra"]["ph_nickname"] = json.dumps("Garage red")
    with mock.patch.object(spool_sync, "load_state", return_value=state()):
        listing = spool_sync.list_text(client=fake)
        shelf = spool_sync.shelf_spools(client=fake)
    assert "Slot 2: #12 Garage red" in listing
    assert "#7 Lucy's pink (" in listing
    assert [row["name"] for row in shelf] == ["Lucy's pink"]
    assert "/spool rename <spool#> <name>" in listing


def test_spool_command_offers_the_new_subcommands():
    words = {sub.word for c in commands.COMMANDS if c.name == "spool" for sub in c.subcommands}
    assert {"shelve", "rename", "archive", "unarchive"} <= words
    assert commands.as_text("spool", {"subcommand": "rename", "spool": 7, "name": "Pink"}) == "/spool rename 7 Pink"
    assert commands.as_text("spool", {"subcommand": "shelve", "slot": 2}) == "/spool shelve 2"


def test_chat_shelve_unbinds_the_slot():
    calls = []

    def unbind(slot, **kw):
        calls.append((slot, kw.get("note")))
        return {"slot": slot, "spool_id": 12}

    with mock.patch.object(spool_sync, "unbind_slot", side_effect=unbind):
        _out, adapter, _fake = _run("/spool shelve 2", [])
    assert calls == [(2, "Put back on the shelf from chat")]
    assert "Slot 2 no longer holds spool #12" in adapter.sent[-1]


def test_chat_rename_and_archive_route_to_spoolman():
    seen = []
    with mock.patch.object(spool_sync, "rename_spool_text",
                           side_effect=lambda n, name: seen.append(("rename", n, name)) or "renamed"), \
         mock.patch.object(spool_sync, "set_archived_text",
                           side_effect=lambda n, a, **kw: seen.append(("archive", n, a)) or "archived"):
        _run("/spool rename #7 Lucy's pink", [])
        _run("/spool archive 7", [])
        _run("/spool unarchive 19", [])
    assert seen == [("rename", 7, "Lucy's pink"), ("archive", 7, True), ("archive", 19, False)]


def test_bad_forms_change_nothing():
    _o, adapter, fake = _run("/spool rename pink", [])
    assert "`/spool rename <spool#> <name>`" in adapter.sent[-1] and fake.writes == []
    _o, adapter, fake = _run("/spool archive", [])
    assert "`/spool archive <spool#>`" in adapter.sent[-1] and fake.writes == []
