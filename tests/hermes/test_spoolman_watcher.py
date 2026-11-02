"""Spoolman-side new-spool watcher (#483).

The printer has a presence watcher for IFS slots; this is its Spoolman
twin: a spool created in Spoolman's UI (or by another tool) gets exactly
one Discord prompt. All Spoolman answers come from a fake lister behind
spoolman_client.list_spools' shape — no network, no printer.
"""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy.hermes import moonraker_event_listener as mel
from print_handy.hermes import print_alert_buttons as pab
from print_handy.hermes import spool_sync

HERE = Path(__file__).resolve().parent


def spool(sid, *, archived=False, brand="Sunlu", material="PLA+", color="Matte White"):
    return {"id": sid, "archived": archived, "remaining_weight": 1000.0, "location": "Shelf",
            "filament": {"name": color, "material": material, "vendor": {"name": brand}}}


class FakeLister:
    """list_spools' shape: a list of spool rows, or None when Spoolman is silent."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self):
        return None if self.rows is None else [dict(row) for row in self.rows]


def known_ids(path):
    return json.loads(Path(path).read_text())["known"]


# --------------------------------------------------------------------- watcher


def test_first_run_seeds_silently(tmp_path):
    state = tmp_path / "known.json"
    lister = FakeLister([spool(7), spool(9), spool(12, archived=True)])
    watcher = mel.SpoolmanNewSpoolWatcher(state_path=state, lister=lister)
    assert watcher.poll() == []
    assert known_ids(state) == [7, 9, 12]
    # And a second poll over the same inventory stays silent.
    assert watcher.poll() == []


def test_new_spool_prompts_exactly_once_even_across_restart(tmp_path):
    state = tmp_path / "known.json"
    lister = FakeLister([spool(7)])
    watcher = mel.SpoolmanNewSpoolWatcher(state_path=state, lister=lister)
    assert watcher.poll() == []
    lister.rows = [spool(7), spool(21, brand="eSun", material="PLA", color="Black")]
    fresh = watcher.poll()
    assert [row["id"] for row in fresh] == [21]
    assert known_ids(state) == [7, 21]
    watcher.delivered(21)
    # Not again on the next poll...
    assert watcher.poll() == []
    # ...and not again after a restart (a fresh watcher over the persisted set).
    restarted = mel.SpoolmanNewSpoolWatcher(state_path=state, lister=lister)
    assert restarted.poll() == []


def test_default_lister_asks_spoolman_for_archived_spools_too(tmp_path):
    paths = []
    with mock.patch("print_handy.core.spoolman_client.call",
                    lambda method, path, **kw: paths.append(path) or [spool(7)]):
        watcher = mel.SpoolmanNewSpoolWatcher(state_path=tmp_path / "known.json")
        watcher.poll()
    assert paths == ["/api/v1/spool?allow_archived=true"]


def test_archiving_then_unarchiving_a_known_spool_does_not_prompt(tmp_path):
    state = tmp_path / "known.json"
    lister = FakeLister([spool(7), spool(9)])
    watcher = mel.SpoolmanNewSpoolWatcher(state_path=state, lister=lister)
    assert watcher.poll() == []
    lister.rows = [spool(7), spool(9, archived=True)]
    assert watcher.poll() == []
    lister.rows = [spool(7), spool(9)]
    assert watcher.poll() == []
    assert known_ids(state) == [7, 9]


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_spools_print_handy_created_are_recorded_silently(tmp_path):
    state = tmp_path / "known.json"
    lister = FakeLister([spool(7)])
    watcher = mel.SpoolmanNewSpoolWatcher(state_path=state, lister=lister)
    assert watcher.poll() == []
    made_here = dict(spool(40), comment=spool_sync.CREATED_SPOOL_COMMENT)
    runout = dict(spool(41), comment=mel.RUNOUT_SPOOL_COMMENT)
    elsewhere = dict(spool(42), comment="bought at the fair")
    lister.rows = [spool(7), made_here, runout, elsewhere]
    assert [row["id"] for row in watcher.poll()] == [42]
    assert known_ids(state) == [7, 40, 41, 42]


def test_create_spool_stamps_the_comment_the_watcher_skips():
    calls = []

    def client(method, path, body):
        calls.append((method, path, body))
        if path == "/api/v1/filament" and method == "GET":
            return [{"id": 3, "material": "PLA", "name": "Black", "vendor": {"name": "eSun"}}]
        return {"id": 55}

    spool_sync.create_spool({"brand": "eSun", "material": "PLA", "color": "Black", "grams": 1000.0}, client=client)
    body = next(body for method, path, body in calls if method == "POST" and path == "/api/v1/spool")
    assert mel.created_by_print_handy({"comment": body["comment"]})


def test_undelivered_prompt_is_retried_until_delivered_even_across_restart(tmp_path):
    state = tmp_path / "known.json"
    clock = Clock()
    lister = FakeLister([spool(7)])
    watcher = mel.SpoolmanNewSpoolWatcher(state_path=state, lister=lister, clock=clock)
    assert watcher.poll() == []
    lister.rows = [spool(7), spool(21)]
    assert [row["id"] for row in watcher.poll()] == [21]  # first attempt; never delivered
    clock.now += mel.NEW_SPOOL_RETRY_S - 1
    assert watcher.poll() == []  # not hammered on every poll
    clock.now += 1
    restarted = mel.SpoolmanNewSpoolWatcher(state_path=state, lister=lister, clock=clock)
    assert [row["id"] for row in restarted.poll()] == [21]  # retried, and the restart kept it
    restarted.delivered(21)
    clock.now += mel.NEW_SPOOL_RETRY_S
    assert restarted.poll() == []


def test_pending_prompt_is_dropped_when_archived_deleted_or_too_old(tmp_path):
    state = tmp_path / "known.json"
    clock = Clock()
    lister = FakeLister([spool(7)])
    watcher = mel.SpoolmanNewSpoolWatcher(state_path=state, lister=lister, clock=clock)
    assert watcher.poll() == []
    lister.rows = [spool(7), spool(21), spool(22), spool(23)]
    assert [row["id"] for row in watcher.poll()] == [21, 22, 23]
    lister.rows = [spool(7), spool(21, archived=True), spool(23)]  # 21 archived, 22 deleted
    clock.now += mel.NEW_SPOOL_RETRY_S
    assert [row["id"] for row in watcher.poll()] == [23]
    clock.now += mel.NEW_SPOOL_GIVE_UP_S
    assert watcher.poll() == []  # 23 given up after a day
    assert json.loads(state.read_text())["pending"] == {}


def test_listener_retries_a_prompt_whose_delivery_failed(tmp_path):
    listener = mel.Listener()
    clock = Clock()
    lister = FakeLister([spool(7)])
    listener.spoolman_watcher = mel.SpoolmanNewSpoolWatcher(
        state_path=tmp_path / "known.json", lister=lister, clock=clock)
    attempts = []
    outcome = {"ok": False}
    with mock.patch.object(mel, "post_new_spool_prompt",
                           lambda row: attempts.append(row["id"]) or outcome["ok"]):
        asyncio.run(listener.poll_spoolman_new_spools())
        lister.rows = [spool(7), spool(21)]
        asyncio.run(listener.poll_spoolman_new_spools())
        assert attempts == [21]  # chat was down
        outcome["ok"] = True
        clock.now += mel.NEW_SPOOL_RETRY_S
        asyncio.run(listener.poll_spoolman_new_spools())
        assert attempts == [21, 21]  # retried and delivered
        clock.now += mel.NEW_SPOOL_RETRY_S
        asyncio.run(listener.poll_spoolman_new_spools())
        assert attempts == [21, 21]  # and never again


def test_archived_arrival_is_recorded_silently(tmp_path):
    state = tmp_path / "known.json"
    lister = FakeLister([spool(7)])
    watcher = mel.SpoolmanNewSpoolWatcher(state_path=state, lister=lister)
    assert watcher.poll() == []
    lister.rows = [spool(7), spool(30, archived=True)]
    assert watcher.poll() == []
    assert known_ids(state) == [7, 30]


def test_unreachable_spoolman_is_silent_and_changes_nothing(tmp_path):
    state = tmp_path / "known.json"
    lister = FakeLister(None)
    watcher = mel.SpoolmanNewSpoolWatcher(state_path=state, lister=lister)
    assert watcher.poll() == []
    assert not state.exists()  # an unanswered poll does not even seed
    lister.rows = [spool(7)]
    assert watcher.poll() == []
    before = state.read_bytes()
    lister.rows = None
    assert watcher.poll() == []
    assert state.read_bytes() == before


def test_deleted_ids_drop_out_of_the_known_set(tmp_path):
    state = tmp_path / "known.json"
    lister = FakeLister([spool(7), spool(9)])
    watcher = mel.SpoolmanNewSpoolWatcher(state_path=state, lister=lister)
    assert watcher.poll() == []
    lister.rows = [spool(7)]
    assert watcher.poll() == []
    assert known_ids(state) == [7]


def test_display_label_uses_the_one_picker_shape():
    assert mel.spool_display_label(spool(21)) == "Matte White · PLA+ · #21 · 1000 g left"
    assert mel.spool_display_label({"id": 5, "filament": {}}) == \
        "color unknown · material unknown · #5 · grams unknown"
    assert "#21" in mel.new_spool_prompt_text(spool(21))
    assert "/spool change <slot> 21" in mel.new_spool_plain_text(spool(21))


def test_listener_poll_posts_one_prompt_per_new_spool(tmp_path):
    listener = mel.Listener()
    lister = FakeLister([spool(7)])
    listener.spoolman_watcher = mel.SpoolmanNewSpoolWatcher(
        state_path=tmp_path / "known.json", lister=lister)
    posted = []
    with mock.patch.object(mel, "post_new_spool_prompt", lambda row: posted.append(row["id"]) or True):
        asyncio.run(listener.poll_spoolman_new_spools())
        assert posted == []  # first poll only seeds
        lister.rows = [spool(7), spool(21), spool(22, archived=True)]
        asyncio.run(listener.poll_spoolman_new_spools())
        assert posted == [21]
        asyncio.run(listener.poll_spoolman_new_spools())
        assert posted == [21]  # never twice


# --------------------------------------------------------------------- buttons


def test_post_new_spool_posts_three_buttons_and_records_the_spool(tmp_path):
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_open(request, timeout):
        seen["request"] = request
        return Response()

    with mock.patch.object(pab, "STATE", tmp_path / "alerts.json"), \
            mock.patch.object(pab.urllib.request, "urlopen", fake_open):
        assert pab.post_new_spool("token", "chan", "New spool", 21, "#21 Sunlu PLA+ Matte White")
        rows = [row for row in pab._load().values() if isinstance(row, dict)]
    body = json.loads(seen["request"].data)
    labels = [button["label"] for row in body["components"] for button in row["components"]]
    assert labels == ["Load into a slot", "Leave on the shelf", "Dismiss"]
    assert rows[0]["kind"] == "spoolman-new" and rows[0]["spool_id"] == 21


def test_new_spool_custom_ids_parse():
    assert pab.parse_new_spool_custom_id("pbnewspool:load:ab12") == ("load", "ab12")
    assert pab.parse_new_spool_custom_id("pbnewspool:dismiss:ab12") == ("dismiss", "ab12")
    assert pab.parse_new_spool_custom_id("pbnewspool:archive:ab12") == ("archive", "ab12")
    assert pab.parse_new_spool_custom_id("pbnewspool:confirm:ab12") == ("confirm", "ab12")
    assert pab.parse_new_spool_custom_id("pbalert:pause:ab12") is None
    assert pab.parse_new_spool_custom_id("pbnewspool:bogus:ab12") is None
    assert pab.parse_new_spool_custom_id("") is None


def test_new_spool_slot_options_name_current_contents():
    state = {"filament_slots": {"2": {"spoolman_id": 12, "name": "Red PETG"}}}
    options = pab.new_spool_slot_options(state)
    assert [option["value"] for option in options] == ["1", "2", "3", "4"]
    assert options[0]["description"] == "Now holds empty"
    assert options[1]["description"] == "Now holds #12 Red PETG"


# --------------------------------------------------------------------- clicks

def load_plugin():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "print_feedback_483", HERE.parents[1] / "plugins/print-feedback/__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Interaction:
    """The test_print_next.Interaction shape, plus a message to edit."""

    def __init__(self, custom_id, values=None):
        self.data = {"custom_id": custom_id, **({"values": values} if values else {})}
        self.user = S(id=1, bot=False, display_name="op")
        self.guild = None
        self.channel_id = "c"
        self.out = []
        interaction = self

        class Response:
            async def defer(self, **kw):
                pass  # the P2-191 ack: no visible message of its own

            async def send_message(self, content, ephemeral=False, **kw):
                interaction.out.append(("send", content))

            async def edit_message(self, content=None, view=None, **kw):
                interaction.out.append(("edit", content, view))

        self.response = Response()
        self.followup = S(send=self._followup)
        self.message = S(edit=self._edit)

    async def edit_original_response(self, content=None, view=None, **kw):
        self.out.append(("edit", content, view))

    async def _followup(self, content, view=None):
        self.out.append(("followup", content))

    async def _edit(self, **kw):
        self.out.append(("strip", kw))


@pytest.fixture
def click_harness(tmp_path, monkeypatch):
    class Button:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class Select:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class SelectOption:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class View:
        def __init__(self, timeout=None):
            self.items = []

        def add_item(self, item):
            self.items.append(item)

    fake = S(ui=S(View=View, Button=Button, Select=Select), SelectOption=SelectOption,
             ButtonStyle=S(success=3, secondary=2, primary=1, danger=4))
    monkeypatch.setitem(sys.modules, "discord", fake)
    monkeypatch.setattr(pab, "STATE", tmp_path / "alerts.json")
    handlers = {}
    plugin = load_plugin()
    plugin.alert_button_handlers(
        S(add_listener=lambda fn, name: handlers.update({name: fn})), S())
    return handlers["on_interaction"]


def test_shelf_and_dismiss_close_the_prompt(click_harness):
    on_interaction = click_harness
    aid = pab.record("spoolman-new", "", spool_id=21, label="#21 Sunlu PLA+ Matte White")
    click = Interaction(f"pbnewspool:shelf:{aid}")
    asyncio.run(on_interaction(click))
    assert click.out[0][0] == "edit" and "stays on the shelf" in click.out[0][1]
    # Single use: the record is claimed, so a second press gets the old-prompt reply.
    again = Interaction(f"pbnewspool:dismiss:{aid}")
    asyncio.run(on_interaction(again))
    assert "already answered" in again.out[0][1]


def test_load_path_walks_slot_disposition_confirm_and_binds(click_harness, monkeypatch):
    on_interaction = click_harness
    binds = []

    def fake_bind(slot, spool_id, **kw):
        binds.append((slot, spool_id, kw))
        return {"record": {"spoolman_id": spool_id}}

    monkeypatch.setattr(spool_sync, "load_state",
                        lambda *a, **kw: {"filament_slots": {"3": {"spoolman_id": 9, "name": "Old PLA"}}})
    monkeypatch.setattr(spool_sync, "bind_slot", fake_bind)
    monkeypatch.setattr(spool_sync, "bind_text", lambda result: "Slot 3 → spool #21. Bound.")

    aid = pab.record("spoolman-new", "", spool_id=21, label="#21 Sunlu PLA+ Matte White")
    load = Interaction(f"pbnewspool:load:{aid}")
    asyncio.run(on_interaction(load))
    kind, content, view = load.out[0]
    assert kind == "edit" and "Which slot" in content
    select = view.items[0]
    assert select.custom_id.startswith("pbnewspoolselect:")
    select_aid = select.custom_id.split(":", 1)[1]

    picked = Interaction(select.custom_id, ["3"])
    asyncio.run(on_interaction(picked))
    kind, content, view = picked.out[0]
    assert kind == "edit" and "What happens to spool #9" in content
    disp_aid = next(item.custom_id.rsplit(":", 1)[1] for item in view.items
                    if item.custom_id.startswith("pbnewspool:archive:"))

    archived = Interaction(f"pbnewspool:archive:{disp_aid}")
    asyncio.run(on_interaction(archived))
    kind, content, view = archived.out[0]
    assert kind == "edit" and "will be archived" in content
    confirm_aid = next(item.custom_id.rsplit(":", 1)[1] for item in view.items
                       if item.custom_id.startswith("pbnewspool:confirm:"))

    confirmed = Interaction(f"pbnewspool:confirm:{confirm_aid}")
    asyncio.run(on_interaction(confirmed))
    assert confirmed.out[0][0] == "edit" and "Bound." in confirmed.out[0][1]
    assert binds == [(3, 21, {"prior_disposition": "archive",
                              "expected_prior_spool_id": 9,
                              "note": "Bound from Discord new-spool prompt"})]


def test_load_into_empty_slot_skips_the_disposition_step(click_harness, monkeypatch):
    on_interaction = click_harness
    binds = []
    monkeypatch.setattr(spool_sync, "load_state", lambda *a, **kw: {"filament_slots": {}})
    monkeypatch.setattr(spool_sync, "bind_slot",
                        lambda slot, spool_id, **kw: binds.append((slot, spool_id, kw)) or {"record": {}})
    monkeypatch.setattr(spool_sync, "bind_text", lambda result: "Bound.")

    aid = pab.record("spoolman-new", "", spool_id=21, label="#21 Sunlu PLA+ Matte White")
    load = Interaction(f"pbnewspool:load:{aid}")
    asyncio.run(on_interaction(load))
    select = load.out[0][2].items[0]
    picked = Interaction(select.custom_id, ["1"])
    asyncio.run(on_interaction(picked))
    kind, content, view = picked.out[0]
    assert kind == "edit" and content.startswith("Load spool #21")
    confirm_aid = next(item.custom_id.rsplit(":", 1)[1] for item in view.items
                       if item.custom_id.startswith("pbnewspool:confirm:"))
    confirmed = Interaction(f"pbnewspool:confirm:{confirm_aid}")
    asyncio.run(on_interaction(confirmed))
    assert binds == [(1, 21, {"prior_disposition": "shelf",
                              "expected_prior_spool_id": None,
                              "note": "Bound from Discord new-spool prompt"})]
