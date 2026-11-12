"""A filament shortage on any slot a print uses warns and can be accepted before upload.

Owner ruling (#1336): filament and material conditions are warn-and-accept,
never a hard block. A live run planned a two-slot print whose second slot's
spool had 0 g in Spoolman ("Slot 2 bronze PLA+: needs about 6.8 g; spool has
0 g, short by about 6.8 g"), and the print then uploaded and started without
the operator ever being asked: the per-slot need versus remaining check only
fed that display line, and no start path compared the G-code's per-slot grams
with the spools.

Regression test seen failing on base: on the base commit
``test_two_slot_print_with_one_short_slot_asks_before_upload`` uploaded and
started (no refusal), and the material_accept helpers it uses did not exist.
"""
import asyncio
import json
from unittest import mock

import pytest

from print_handy.core import material_accept, model_job, spoolman_client
from print_handy.launch import print_now

from .test_launch_state import PRINTER, make_job, upload_ok, uploads


def _two_slot_gcode(name):
    """An AD5X-style two-tool G-code header: T0 feeds 6.8 g, T1 feeds 27 g."""
    return (f"; {name}\n; filament_type = PLA+;PLA+\n; filament_colour = #B08D57;#000000\n"
            "; filament used [g] = 6.80, 27.00\n; total filament used [g] = 33.80\n"
            "START_PRINT EXTRUDER_TEMP=220 BED_TEMP=60\n")


SLOTS = {"2": {"material": "PLA+", "color": "bronze", "hex": "#B08D57", "spoolman_id": 19},
         "3": {"material": "PLA+", "color": "black", "hex": "#000000", "spoolman_id": 21}}
SPOOLMAN = {19: {"id": 19, "remaining_weight": 0}, 21: {"id": 21, "remaining_weight": 787.1}}


@pytest.fixture
def two_slot(tmp_path):
    """The mapped AD5X launch with T0 -> slot 2 (0 g in Spoolman) and T1 -> slot 3 (787.1 g)."""
    from print_handy.printers import flashforge
    from print_handy.printers.flashforge.zmod import screen as zmod_screen
    native = mock.Mock(spec=flashforge.zmod)
    native.normalize_mapping.side_effect = lambda m, st: {"0": 2, "1": 3}
    native.require_idle_settled.return_value = True
    native.screen_label_matches.side_effect = zmod_screen.label_matches
    adapter = mock.Mock(native=native)
    scripts = []

    def fake_mr(st, path, data=None, timeout=30):
        if path == "/printer/gcode/script":
            scripts.append(data["script"])
            return {"result": "ok"}
        if path.startswith("/server/history/list"):
            return {"result": {"count": 0, "jobs": []}}
        return {"result": {"status": {
            "webhooks": {"state": "ready"}, "print_stats": {"state": "standby", "filename": ""},
            "extruder": {"target": 0}, "heater_bed": {"target": 0},
            "zmod_color": {"slots": [{"ID": 2, "Material": "PLA", "HEX": "B08D57", "hasFilament": True},
                                     {"ID": 3, "Material": "PLA", "HEX": "000000", "hasFilament": True}]},
            "filament_switch_sensor head_switch_sensor": {"enabled": True}}}}

    st = {"moonraker_url": PRINTER, "spoolman_url": "http://spoolman.invalid",
          "filament_slots": json.loads(json.dumps(SLOTS))}
    with mock.patch.object(model_job.printers, "current", return_value=adapter), \
         mock.patch.object(model_job, "START_CLAIMS", tmp_path / "claims.json"), \
         mock.patch.object(model_job, "validate_saved_mesh_start"), \
         mock.patch.object(model_job, "_mr", side_effect=fake_mr), \
         mock.patch.object(model_job, "read_stock_detail", return_value={}), \
         mock.patch.object(model_job, "read_stock_filament_slots_from_detail", return_value={2: True, 3: True}), \
         mock.patch.object(model_job, "_sync_active_spool"), \
         mock.patch.object(model_job, "_announce_print_name"), \
         mock.patch.object(model_job, "_publish_job_card"), \
         mock.patch.object(model_job, "_assign_print_number", return_value=None), \
         mock.patch.object(model_job, "record_start", return_value="/vault/x.md"), \
         mock.patch.object(model_job, "_wait_printing", return_value="standby"), \
         mock.patch.object(model_job, "_preparation_evidence", return_value=False), \
         mock.patch.object(spoolman_client, "get_spool", side_effect=lambda sid, **k: SPOOLMAN.get(sid)), \
         mock.patch.object(model_job.subprocess, "run", side_effect=upload_ok) as run:
        yield mock.Mock(scripts=scripts, run=run, st=st, root=tmp_path)


def _launch(m, name="giveaway"):
    jp, g = make_job(m.root, name)
    g.write_text(_two_slot_gcode(name))
    job = json.loads(jp.read_text())
    return g, lambda: model_job.print_mapped_native(g, m.st, {"display_off": 0}, {"0": 2, "1": 3}, job, jp, 0, False)


def test_two_slot_print_with_one_short_slot_asks_before_upload(two_slot):
    _, launch = _launch(two_slot)
    with pytest.raises(SystemExit) as out:
        launch()
    detail = str(out.value.code)
    assert detail.startswith("REFUSED: " + material_accept.PREFIX) and "Nothing uploaded or started" in detail
    assert uploads(two_slot.run) == [] and two_slot.scripts == []
    warnings = material_accept.pending(detail)
    assert [w["kind"] for w in warnings] == ["filament"]  # slot 3 has plenty: only slot 2 is named
    text = warnings[0]["text"]
    assert "Slot 2 (bronze PLA+ spool #19) needs about 6.8 g" in text
    assert "Spoolman says the spool has 0 g left" in text and "short by about 6.8 g" in text
    assert "weight may not be set in Spoolman" in text  # a 0 g Spoolman reading says so
    # The chat question: Print anyway or Cancel, never a Reslice that cannot help.
    assert material_accept.choices(warnings) == [material_accept.PRINT_ANYWAY_LABEL, material_accept.CANCEL_LABEL]
    assert material_accept.print_label(warnings) == material_accept.PRINT_ANYWAY_LABEL
    question = material_accept.question(warnings)
    assert "short of filament" in question and "Slot 2" in question and "run out" in question


def test_accepting_the_shortage_uploads_and_starts_once(two_slot, capsys):
    g, launch = _launch(two_slot)
    with pytest.raises(SystemExit) as out:
        launch()
    keys = [w["key"] for w in material_accept.pending(str(out.value.code))]
    sha = model_job.file_sha256(g)
    with material_accept.accepting(keys, gcode_sha256=sha, expected_sha256=sha):
        launch()
    assert len(uploads(two_slot.run)) == 1 and len(two_slot.scripts) == 1
    assert "FILAMENT WARNING ACCEPTED: Slot 2" in capsys.readouterr().out


def test_both_slots_short_are_named_in_one_question(two_slot):
    two_slot.st["filament_slots"]["3"]["spoolman_id"] = 99  # not in Spoolman: the slot record's grams
    two_slot.st["filament_slots"]["3"]["remaining_grams"] = 10
    _, launch = _launch(two_slot)
    with pytest.raises(SystemExit) as out:
        launch()
    texts = [w["text"] for w in material_accept.pending(str(out.value.code))]
    assert len(texts) == 2 and texts[0].startswith("Slot 2") and texts[1].startswith("Slot 3")
    assert "the slot record says the spool has 10 g left, short by about 17 g" in texts[1]


def test_enough_filament_or_unknown_weight_does_not_ask(two_slot):
    SPOOLMAN_FULL = {19: {"id": 19, "remaining_weight": 500}, 21: {"id": 21}}  # 21: weight never set, no record
    _, launch = _launch(two_slot)
    with mock.patch.object(spoolman_client, "get_spool", side_effect=lambda sid, **k: SPOOLMAN_FULL.get(sid)):
        launch()
    assert len(uploads(two_slot.run)) == 1


def test_single_slot_and_single_spool_need_the_whole_print():
    text = _two_slot_gcode("one")
    assert print_now.filament_need_by_slot(text, slot=2) == {2: 33.8}
    assert print_now.filament_need_by_slot(text) == {None: 33.8}
    assert print_now.filament_need_by_slot(text, {"0": 2, "1": 2}) == {2: pytest.approx(33.8)}
    assert print_now.filament_need_by_slot("G1 X1\n", slot=2) == {}
    st = {"spool": {"spoolman_id": 7, "name": "Galaxy", "remaining_grams": 0}}
    with pytest.raises(SystemExit) as out:
        print_now.check_filament_amount(st, {None: 33.8})
    [warning] = material_accept.pending(str(out.value.code))
    assert warning["text"].startswith("The loaded spool (Galaxy spool #7) needs about 33.8 g")
    assert "(0 g recorded for this slot)" in warning["text"]


def test_mixed_material_and_shortage_keeps_the_reslice_choice():
    material = {"key": "material|PETG|PLA", "kind": "material", "parts": ["PETG", "PLA"], "text": "x"}
    short = {"key": "filament|2|19", "kind": "filament", "parts": ["2", "19"], "text": "y"}
    assert material_accept.choices([material, short])[1] == material_accept.PRINT_AS_SLICED_LABEL
    assert material_accept.print_label([material, short]) == material_accept.PRINT_AS_SLICED_LABEL
    assert "Load more filament" in material_accept.reslice_message([short])


def test_chat_start_offer_accepts_a_shortage_with_print_anyway(monkeypatch):
    """The /print-next and /print-again offer: Print anyway restarts with the shortage accepted."""
    from print_handy.bot.handlers import print_next as handler
    refusal = ("REFUSED: " + material_accept.PREFIX + "Slot 2 (bronze PLA+ spool #19) needs about 6.8 g but "
               "Spoolman says the spool has 0 g left, short by about 6.8 g [accept filament|2|19]. "
               "Nothing uploaded or started.")
    starts = []

    class FakePrintNext:
        @staticmethod
        async def start(target):
            starts.append(target)
            return (True, "started") if target.get("accept_material") else (False, refusal)

    asked = []

    async def confirm(adapter, source, metadata, kind, question, choices, timeout, **k):
        asked.append(choices)
        return material_accept.PRINT_ANYWAY_LABEL

    monkeypatch.setattr(handler, "_confirm", confirm)
    ok, detail, _ = asyncio.run(handler._start_with_offers(None, None, None, FakePrintNext, {"gcode": "g"}))
    assert ok and asked == [[material_accept.PRINT_ANYWAY_LABEL, material_accept.CANCEL_LABEL]]
    assert starts[-1]["accept_material"] == ["filament|2|19"]
