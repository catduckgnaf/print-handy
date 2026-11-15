"""Forge-X calibration routing (#1506 tranche 2b), offline.

Forge-X keeps firmware_family "none" in the registry, so calibration
recognizes it by vendor. Its macros come from the ff5m 1.4.2 beta (read for
behavior only): AUTO_FULL_BED_LEVEL for the mesh (never a bare
BED_MESH_CALIBRATE), raw PID_CALIBRATE, FORGE_SHAPER_CALIBRATE for the input
shaper (it applies its recommendation in memory, so Discard restarts it
away), BED_LEVEL_SCREWS_TUNE and the Z offset as guides. The mesh is
measured only on Feather, Guppy or Headless; the Stock screen owns
MESH_DATA, and an unreadable screen mode is reported, never guessed.
"""
import pytest

from print_handy import calibration as cal

from test_calibration import (NO_SLEEP, ZMOD_SETTINGS, FakeTransport,  # noqa: E402
                              entry)

FORGEX = entry("flashforge_forgex.ad5m")
FORGEX_PRO = entry("flashforge_forgex.ad5m_pro")

FORGEX_OBJECTS = [
    "print_stats", "bed_mesh", "input_shaper", "configfile", "mod_params",
    "gcode_macro AUTO_FULL_BED_LEVEL", "gcode_macro FORGE_SHAPER_CALIBRATE",
    "gcode_macro BED_LEVEL_SCREWS_TUNE", "extruder", "heater_bed",
]
FORGEX_SHAPER_LINES = [
    "X: recommended mzv at 52.3 Hz",
    "Y: recommended ei at 41.8 Hz",
    "Recommended shaper settings applied. SAVE_CONFIG saves them and restarts Klipper.",
]


class ForgeXTransport(FakeTransport):
    """FakeTransport plus Forge-X's mod_params object in the facts query."""

    def __init__(self, display="FEATHER", load_zoffset=1, **kwargs):
        kwargs.setdefault("objects", FORGEX_OBJECTS)
        kwargs.setdefault("variables", {})
        super().__init__(**kwargs)
        self.mod_params = ({"variables": {"display": display, "load_zoffset": load_zoffset}}
                           if display is not None else {})

    def __call__(self, method, path, body=None):
        result = super().__call__(method, path, body)
        if path == cal.FACTS_PATH:
            result["result"]["status"]["mod_params"] = self.mod_params
        return result


def facts_for(**kwargs):
    transport = ForgeXTransport(**kwargs)
    return transport, cal.fetch_facts(transport)


# ------------------------------------------------------------ eligibility and menu

@pytest.mark.parametrize("model", [FORGEX, FORGEX_PRO])
def test_forgex_is_eligible_although_its_family_is_none(model):
    assert model.firmware_family == "none"
    assert cal.block_reason(model) is None


def test_stock_flashforge_stays_refused():
    stock = entry("flashforge_stock.ad5x")
    assert cal.block_reason(stock) is not None


def test_menu_offers_the_forgex_macros_on_feather():
    _t, facts = facts_for()
    options = {o.key: o for o in cal.available_options(FORGEX, facts)}
    assert set(options) == {"mesh", "screws", "pid_extruder", "pid_bed", "shaper", "zoffset"}
    assert options["mesh"].mode == "run" and "AUTO_FULL_BED_LEVEL" in options["mesh"].note
    assert options["shaper"].mode == "run" and "FORGE_SHAPER_CALIBRATE" in options["shaper"].note
    assert options["screws"].mode == "guide" and options["zoffset"].mode == "guide"
    assert "belts" not in options


@pytest.mark.parametrize("display", ["STOCK", None, "SOMETHING"])
def test_mesh_is_report_only_on_stock_or_an_unreadable_screen(display):
    _t, facts = facts_for(display=display)
    mesh = cal.option_for(cal.available_options(FORGEX, facts), "mesh")
    assert mesh.mode == "report"


def test_a_missing_macro_is_not_offered():
    objects = [o for o in FORGEX_OBJECTS if o != "gcode_macro FORGE_SHAPER_CALIBRATE"]
    _t, facts = facts_for(objects=objects)
    assert cal.option_for(cal.available_options(FORGEX, facts), "shaper") is None


# ------------------------------------------------------------ mesh

def test_mesh_runs_auto_full_bed_level_into_a_scratch_profile_then_saves_auto():
    transport, facts = facts_for()
    prepared = cal.prepare_mesh(transport, FORGEX, facts)
    assert prepared.family == "forgex" and prepared.profile == "auto"
    assert prepared.script == "AUTO_FULL_BED_LEVEL PROFILE=print_handy EXTRUDER_TEMP=220 BED_TEMP=60"
    assert "BED_MESH_CALIBRATE" not in prepared.script and "SAVE=" not in prepared.script
    assert "Forge-X's own leveling" in cal.mesh_prompt_text(prepared)
    result = cal.execute_mesh(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert transport.scripts == [prepared.script, "TURN_OFF_HEATERS"]
    cal.accept_mesh(transport, result)
    assert transport.scripts[2:] == ["BED_MESH_PROFILE LOAD=print_handy",
                                     "BED_MESH_PROFILE SAVE=auto",
                                     "BED_MESH_PROFILE REMOVE=print_handy", "SAVE_CONFIG"]


def test_mesh_discard_never_saves():
    transport, facts = facts_for()
    prepared = cal.prepare_mesh(transport, FORGEX, facts)
    result = cal.execute_mesh(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    cal.discard_mesh(transport, result)
    assert "SAVE_CONFIG" not in transport.scripts


@pytest.mark.parametrize("display,word", [("STOCK", "Stock screen"), (None, "could not read")])
def test_mesh_run_is_refused_on_stock_or_unknown_before_anything_is_sent(display, word):
    transport, facts = facts_for(display=display)
    with pytest.raises(cal.CalibrationRefusal, match=word):
        cal.prepare_mesh(transport, FORGEX, facts)
    assert transport.posts == []


def test_stock_screen_mesh_report_names_mesh_data():
    _t, facts = facts_for(display="STOCK")
    text = cal.mesh_report_text(FORGEX, facts)
    assert "MESH_DATA" in text and "nothing was sent" in text


def test_mesh_without_the_macro_is_refused_not_replaced_by_bed_mesh_calibrate():
    objects = [o for o in FORGEX_OBJECTS if o != "gcode_macro AUTO_FULL_BED_LEVEL"]
    transport, facts = facts_for(objects=objects)
    with pytest.raises(cal.CalibrationRefusal, match="AUTO_FULL_BED_LEVEL"):
        cal.prepare_mesh(transport, FORGEX, facts)
    assert transport.posts == []


# ------------------------------------------------------------ input shaper

def _shaper_transport():
    return ForgeXTransport(store_appends={"FORGE_SHAPER_CALIBRATE": FORGEX_SHAPER_LINES})


def test_shaper_parses_forgex_recommendations_and_accepts_with_one_save():
    transport = _shaper_transport()
    facts = cal.fetch_facts(transport)
    prepared = cal.prepare_shaper(transport, FORGEX, facts)
    assert prepared.script == "FORGE_SHAPER_CALIBRATE"
    assert "applies its recommendation in memory" in cal.shaper_prompt_text(prepared)
    result = cal.execute_shaper(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    assert (result.after.type_x, result.after.freq_x) == ("mzv", 52.3)
    assert (result.after.type_y, result.after.freq_y) == ("ei", 41.8)
    assert "FIRMWARE_RESTART" in cal.shaper_result_text(result)
    cal.accept_shaper(transport, result)
    assert transport.scripts == ["FORGE_SHAPER_CALIBRATE", "SAVE_CONFIG"]


def test_shaper_discard_restarts_the_applied_values_away():
    transport = _shaper_transport()
    prepared = cal.prepare_shaper(transport, FORGEX, cal.fetch_facts(transport))
    result = cal.execute_shaper(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    text = cal.discard_shaper(transport, result)
    assert transport.scripts == ["FORGE_SHAPER_CALIBRATE", "FIRMWARE_RESTART"]
    assert "Nothing was saved" in text


def test_zmod_shaper_discard_still_sends_nothing():
    zmod = entry("flashforge.ad5x")
    result = cal.ShaperResult(prepared=cal.PreparedShaper(family="zmod", before=cal.ShaperValues()),
                              after=cal.ShaperValues())
    transport = FakeTransport()
    cal.discard_shaper(transport, result)
    assert transport.scripts == []
    assert zmod.firmware_family == "zmod"


# ------------------------------------------------------------ PID, screws, Z offset

def test_pid_uses_raw_pid_calibrate_never_the_auto_saving_macros():
    transport = ForgeXTransport(settings_map=ZMOD_SETTINGS, store_appends={
        "PID_CALIBRATE HEATER=extruder TARGET=220":
            ["// PID parameters: pid_Kp=23.100 pid_Ki=1.600 pid_Kd=110.000"]})
    prepared = cal.prepare_pid(transport, FORGEX, "extruder", 220)
    result = cal.execute_pid(transport, prepared, poll_interval_s=0, sleep=NO_SLEEP)
    cal.discard_pid(transport, result)
    assert transport.scripts == ["PID_CALIBRATE HEATER=extruder TARGET=220", "FIRMWARE_RESTART"]
    assert not any(banned in s for s in transport.scripts for banned in cal.NEVER_SEND)


def test_screws_are_a_forgex_guide_and_never_driven():
    transport, facts = facts_for()
    assert "BED_LEVEL_SCREWS_PROBE" in cal.screws_guide(FORGEX)
    with pytest.raises(cal.CalibrationRefusal, match="Forge-X"):
        cal.prepare_screws(transport, FORGEX, facts)
    assert transport.posts == []


@pytest.mark.parametrize("armed,word", [(1, "on: the saved offset"), (0, "off or unreadable")])
def test_zoffset_guide_reads_load_zoffset_and_never_suggests_a_start_param(armed, word):
    transport, facts = facts_for(load_zoffset=armed)
    text = cal.zoffset_report(transport, FORGEX, facts)
    assert word in text and "SET_MOD PARAM=load_zoffset VALUE=1" in text
    assert "cancel the print" in text
    assert transport.posts == []
