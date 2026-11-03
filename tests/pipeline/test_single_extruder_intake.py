"""#263: a single-extruder printer with no slot map offers its configured spool as slot 1.

The hardware record of an offline Bambu Lab A1 has a spool (material, density, `spoolman_id: null`) and no
`filament_slots`. Discord intake used to stop before the color question with "requires one to four distinct
configured slots". Offline: the fake Discord adapter from test_prepared_intake answers buttons, slicing
is stubbed, and nothing contacts a printer, Discord or Spoolman.
"""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy import printers, settings
from print_handy.core import link_intake, model_job, prepared_intake
from .test_color_intake import state as ad5x_state
from .test_prepared_intake import AD5X_ROW, Adapter, env, run_preview  # noqa: F401  (pytest fixture)

FIXTURE = Path(__file__).parent / "fixtures" / "printer_hardware.json"
A1 = "bambulab.a1"


def use_model(monkeypatch, key):
    data = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": key})
    adapter = printers.get(key, data)
    monkeypatch.setattr(printers, "current", lambda config=None: adapter)
    return adapter


def a1_state(spoolman_id=None):
    """The issue's record: nozzle, spool and temperatures, no filament_slots, no Spoolman binding."""
    st = copy.deepcopy(json.loads(FIXTURE.read_text()))
    st.update(printer="Bambu Lab A1", moonraker_url="", bed_mm=[256, 256], max_print_height_mm=256)
    st.pop("filament_slots")
    st.pop("spoolman_url")
    st["nozzle"]["type"] = "stainless_steel"
    st["spool"]["spoolman_id"] = spoolman_id
    return st


def link_event(mid="a1"):
    return S(source=S(profile="3dprinting", chat_id="c1", thread_id=None, user_id="u"), message_id=mid,
             text="https://www.printables.com/model/1-ghost")


def run_a1_flow(env, picks, st):
    adapter = Adapter(picks)
    result = asyncio.run(prepared_intake.prepare_link(link_event(), adapter, st, env.root, timeout=1))
    receipt = json.loads(next(env.root.glob("*.json")).read_text())
    return adapter, result, receipt


def saved_job(env):
    return json.loads(next((env.tmp / "archive").rglob("JOB.json")).read_text())


# ----------------------------------------------------------------------------- the reported flow

def test_a1_without_spoolman_reaches_the_color_question_and_saves_one_color(env, monkeypatch):  # noqa: F811
    use_model(monkeypatch, A1)
    # "One color for everything", the only slot, then "Cancel print request":
    # the color decisions are already saved on the job when it is answered.
    adapter, result, receipt = run_a1_flow(env, [1, 0, 3], a1_state())
    assert not any("configured slots" in m for m in adapter.sent), adapter.sent
    color = next(q for q in adapter.questions if q["question"].startswith("Choose the color for"))
    assert color["choices"][:-1] == ["White PLA"]
    job = saved_job(env)
    assert job["filament_selection"] == {"slot": 1, "color": "white", "material": "PLA", "spoolman_id": None}
    assert job["color_decisions"]["mode"] == "single_color"
    assert receipt["status"] == "CANCELLED" and env.sliced == [] and env.starts == []


def test_a1_link_intake_offers_print_in_one_color(tmp_path, monkeypatch):
    use_model(monkeypatch, A1)
    adapter = Adapter([0, 0])
    asyncio.run(link_intake.collect_link(link_event(), adapter, a1_state(), tmp_path / "receipts", timeout=1))
    assert not any("configured slots" in m for m in adapter.sent), adapter.sent
    assert adapter.questions[0]["choices"][:2] == ["Print in one color", "Choose colors per part"]
    assert adapter.questions[1]["choices"][:-1] == ["White PLA"]
    receipt = json.loads(next((tmp_path / "receipts").glob("*.json")).read_text())
    assert receipt["status"] == "COLOR_CONFIRMED"
    assert receipt["filament_selection"] == {"slot": 1, "color": "white", "material": "PLA", "spoolman_id": None}


def test_a1_summary_names_the_configured_printer_not_the_ad5x(env, monkeypatch):  # noqa: F811
    use_model(monkeypatch, A1)
    log, _ = run_preview(env, monkeypatch, "A sturdy stand.", {**AD5X_ROW, "site_printer": "Bambu Lab A1"})
    assert log[0][1].endswith("The Bambu Lab A1 figure after slicing will differ somewhat.")
    assert "AD5X" not in log[0][1]


# ----------------------------------------------------------------------------- slot 1 comes from the spool

def test_single_extruder_slot_one_is_the_configured_spool(monkeypatch):
    use_model(monkeypatch, A1)
    assert model_job.configured_slots(a1_state()) == {"1": {"color": "white", "material": "PLA",
                                                             "spoolman_id": None}}
    # A recorded slot map is used as is; nothing is derived over it.
    recorded = {**a1_state(), "filament_slots": {"1": {"color": "black", "material": "PETG", "spoolman_id": 4}}}
    assert model_job.configured_slots(recorded) == recorded["filament_slots"]
    # A spool with no color name is offered by its material; no color is invented for it.
    unnamed = a1_state()
    unnamed["spool"].pop("name")
    assert model_job.configured_slots(unnamed)["1"]["color"] == "loaded"
    assert model_job.build_slot_labels([model_job.resolve_filament_selection(unnamed, {"slot": 1})]) == ["Loaded PLA"]
    # No spool material: nothing to offer, so intake still stops before any color question.
    bare = a1_state()
    bare["spool"].pop("material")
    assert model_job.configured_slots(bare) == {}


def test_multi_slot_printers_still_require_their_configured_slots(env, monkeypatch):  # noqa: F811
    st = {**ad5x_state(), "filament_slots": {}}
    assert printers.current().multi_slot
    assert model_job.configured_slots(st) == {}
    adapter, result, receipt = run_a1_flow(env, [1, 0, 1], st)
    assert any("requires one to four distinct configured slots" in m for m in adapter.sent)
    assert receipt["status"] != "COLOR_CONFIRMED" and env.sliced == [] and env.starts == []
    # Stock Flashforge is not multi_slot, but it has four IFS bays: no slot is derived for it either.
    use_model(monkeypatch, "flashforge_stock.ad5x")
    assert model_job.configured_slots(st) == {}


# ----------------------------------------------------------------------------- Spoolman ID rules

def test_unbound_spool_needs_no_spoolman_id_on_a_single_extruder(monkeypatch):
    use_model(monkeypatch, A1)
    picked = model_job.resolve_filament_selection(a1_state(), {"slot": 1})
    assert picked == {"slot": 1, "color": "white", "material": "PLA", "spoolman_id": None}
    # Spoolman being configured (Docker always sets services.spoolman_url) does not change that.
    st = {**a1_state(), "spoolman_url": "http://127.0.0.1:8000"}
    assert model_job.resolve_filament_selection(st, {"slot": 1})["spoolman_id"] is None


def test_bound_spool_keeps_the_existing_spoolman_id_rules(monkeypatch):
    use_model(monkeypatch, A1)
    st = {**a1_state(spoolman_id=7), "spoolman_url": "http://spoolman.test:7912"}
    assert model_job.resolve_filament_selection(st, {"slot": 1})["spoolman_id"] == 7
    with pytest.raises(ValueError, match="spool mismatch"):
        model_job.resolve_filament_selection(st, {"slot": 1, "spoolman_id": 8})
    with pytest.raises(ValueError, match="positive Spoolman spool ID"):
        model_job.resolve_filament_selection(a1_state(), {"slot": 1, "spoolman_id": 0})


def test_multi_slot_still_requires_a_spoolman_id(monkeypatch):
    unmapped = {**ad5x_state(), "filament_slots": {"1": {"color": "white", "material": "PLA"}}}
    with pytest.raises(ValueError, match="requires an explicit Spoolman spool ID"):
        model_job.resolve_filament_selection(unmapped, {"slot": 1})
    with pytest.raises(ValueError, match="positive Spoolman spool ID"):
        model_job.validate_filament_selection({"slot": 1, "color": "white", "material": "PLA"})
    use_model(monkeypatch, "flashforge_stock.ad5x")
    with pytest.raises(ValueError, match="requires an explicit Spoolman spool ID"):
        model_job.resolve_filament_selection(unmapped, {"slot": 1})


# ----------------------------------------------------------------------------- guards still refuse

def test_material_mismatch_and_unknown_slots_are_still_refused(monkeypatch):
    use_model(monkeypatch, A1)
    with pytest.raises(ValueError, match="slot 1 material does not match"):
        model_job.resolve_filament_selection(a1_state(), {"slot": 1, "material": "PETG"})
    with pytest.raises(ValueError, match="slot 1 color does not match"):
        model_job.resolve_filament_selection(a1_state(), {"slot": 1, "color": "black"})
    # Only slot 1 is derived; a second slot on a single extruder has no record and no spool.
    with pytest.raises(ValueError):
        model_job.resolve_filament_selection(a1_state(), {"slot": 2, "color": "white", "material": "PLA"})
