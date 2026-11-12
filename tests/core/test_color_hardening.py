"""Filament color handling, hardened: one hex reader, one color comparison, one fallback order.

Audit of main d3ada39 found color codes normalised in five places with a six-digit-only
regex, an exact string compare refusing a mapped launch over FFFFFF vs FAFAFA, a truthy
but invalid Spoolman color_hex hiding a valid multi_color_hexes, a genuine black AMS tray
read as empty, and a name-only color crashing the infinite-spool classifier. Each
numbered test below (bug1 ... bug9) pins one of those; the launch-level ones (bug 1) live
in tests/pipeline/test_launch_state.py next to the `mapped` fixture.
"""
import pytest

from print_handy.core import filament_color as fc
from print_handy.core import model_job
from print_handy.core import spool_lifecycle as sl
from print_handy.runtime import spool_sync
from print_handy.printers.bambulab import ams
from print_handy.printers.flashforge.zmod import multicolor_contract as contract
from print_handy.printers.flashforge.zmod import screen


# ------------------------------------------------------------- parse_hex

@pytest.mark.parametrize("text, expected", [
    ("0C6283", ("0C6283", None)),
    ("#0c6283", ("0C6283", None)),
    ("  0x0c6283 ", ("0C6283", None)),
    ("0X0C6283", ("0C6283", None)),
    ("#abc", ("AABBCC", None)),
    ("#fff", ("FFFFFF", None)),
    ("#ace", ("AACCEE", None)),
    ("0xace", ("AACCEE", None)),
    ("0C6283FF", ("0C6283", 255)),
    ("#0c628300", ("0C6283", 0)),
    ("0C6283,FFFFFF", ("0C6283", None)),
    ("nope, #00ff00", ("00FF00", None)),
])
def test_parse_hex_reads_every_form(text, expected):
    assert fc.parse_hex(text) == expected


@pytest.mark.parametrize("value", [None, "", "  ", "red", "matte white", "12345", "#GGGGGG", 0xFFFFFF, ["FFFFFF"], "1234567",
                                   "bad", "fed", "ACE", "bed", "fab", "add", "cab", "FFF"])
def test_parse_hex_gives_none_for_names_empties_and_junk(value):
    assert fc.parse_hex(value) is None


def test_normalize_hex_is_a_thin_wrapper_that_still_refuses_a_list():
    assert fc.normalize_hex("#abc") == "AABBCC"
    assert fc.normalize_hex("0C6283FF") == "0C6283"
    assert fc.normalize_hex("FF0000,00FF00") is None  # a list is not one color
    assert fc.first_hex("FF0000,00FF00") == "FF0000"
    assert fc.first_hex("bogus,00FF00") == "00FF00"


# ------------------------------------------------------------- same_color

@pytest.mark.parametrize("a, b", [
    ("FFFFFF", "FAFAFA"), ("FFFFFF", "EEEEEE"), ("FAFAFA", "EEEEEE"),
    ("0C6283", "0C6283FF"), ("#FFF", "FFFFFF"), ("#0c6283", "0X0C6283"),
])
def test_same_color_calibration_same(a, b):
    assert fc.same_color(a, b) is True
    assert fc.compare_colors(a, b) == "same"


@pytest.mark.parametrize("a, b", [
    ("0C6283", "067749"), ("FF0000", "00FF00"), ("FFFFFF", "000000"), ("FF0000", "D32F2F"),
])
def test_same_color_calibration_different(a, b):
    assert fc.same_color(a, b) is False
    assert fc.compare_colors(a, b) == "different"


def test_same_color_between_the_thresholds_is_close_not_same_or_different():
    assert fc.SAME_DELTA_E < fc.color_distance("000000", "161616") < fc.DIFFERENT_DELTA_E
    assert fc.compare_colors("000000", "161616") == "close"
    assert fc.same_color("000000", "161616") is None


@pytest.mark.parametrize("a, b", [(None, "FFFFFF"), ("FFFFFF", ""), ("red", "FFFFFF"), ("", None)])
def test_same_color_is_none_when_either_side_has_no_hex(a, b):
    assert fc.same_color(a, b) is None
    assert fc.compare_colors(a, b) == "unknown"
    assert fc.color_distance(a, b) is None


def test_thresholds_are_sane():
    assert 0 < fc.SAME_DELTA_E < fc.DIFFERENT_DELTA_E


# ------------------------------------------------------------- fallback order

def test_resolve_fallback_order_slot_spoolman_multi_printer_name_default(capsys):
    full = dict(spoolman_hex="111111", spoolman_multi="222222,333333", printer_hex="444444", name="red")
    assert fc.resolve({"hex": "AAAAAA"}, **full) == ("AAAAAA", "slot")
    assert fc.resolve(None, **full) == ("111111", "spoolman")
    full["spoolman_hex"] = "garbage"
    assert fc.resolve(None, **full) == ("222222", "spoolman")
    full["spoolman_multi"] = "nope,333333"
    assert fc.resolve(None, **full) == ("333333", "spoolman")
    full["spoolman_multi"] = None
    assert fc.resolve(None, **full) == ("444444", "printer")
    full["printer_hex"] = "blank"
    assert fc.resolve(None, **full) == ("D32F2F", "name")
    assert capsys.readouterr().out == ""
    assert fc.resolve(None, name="zzz", warn_label="Slot 3") == (fc.NEUTRAL_DEFAULT, "default")
    assert "WARNING: Slot 3 has no color code" in capsys.readouterr().out


def test_resolve_reads_any_hex_form_at_every_step():
    assert fc.resolve({"hex": "#0c6283ff"}) == ("0C6283", "slot")
    assert fc.resolve(None, spoolman_hex="#abc") == ("AABBCC", "spoolman")
    assert fc.resolve(None, printer_hex="0x0C6283") == ("0C6283", "printer")


# ------------------------------------------------------------- bug 3: one reader everywhere

def test_bug3_slot_hex_reads_an_eight_digit_hex_instead_of_falling_to_the_name(capsys):
    assert fc.slot_hex(1, {"hex": "FF0000FF", "color": "red"}) == "#FF0000"
    assert fc.slot_hex(1, {"hex": "#0c6283", "color": "green"}) == "#0C6283"
    assert fc.slot_hex(1, "#abc") == "#AABBCC"
    assert capsys.readouterr().out == ""


def test_bug3_screen_command_and_screen_hex_accept_every_form():
    assert screen.screen_command(1, "PLA", "0C6283FF") == "CHANGE_ZCOLOR SLOT=1 TYPE=PLA HEX=0C6283"
    assert screen.screen_command(2, "PLA", "#abc") == "CHANGE_ZCOLOR SLOT=2 TYPE=PLA HEX=AABBCC"
    assert spool_sync.screen_hex("0x0c6283ff") == "0C6283"
    assert spool_sync.screen_hex("FF0000,00FF00") is None
    with pytest.raises(spool_sync.SpoolError, match="not a hex color"):
        screen.screen_command(1, "PLA", "teal")


def test_bug3_spool_new_accepts_an_eight_digit_hex():
    parsed = spool_sync.parse_new("Sunlu, PLA, Ocean, 1000, 0C6283FF")
    assert parsed["hex"] == "0C6283"
    assert spool_sync.parse_new("Sunlu, PLA, Ocean, 1000, #FFF")["hex"] == "FFFFFF"
    with pytest.raises(spool_sync.SpoolError, match="not a hex color"):
        spool_sync.parse_new("Sunlu, PLA, Ocean, 1000, ocean")


def test_bug3_lifecycle_hex_raises_only_for_an_explicit_invalid_value():
    assert sl.normalize_hex_color("0C6283FF") == "0C6283"
    assert sl.normalize_hex_color("#abc") == "AABBCC"
    assert sl.normalize_hex_color(None) is None
    with pytest.raises(ValueError):
        sl.normalize_hex_color("teal")
    with pytest.raises(ValueError):
        sl.normalize_hex_color("")


# ------------------------------------------------------------- bug 2: invalid color_hex hides multi list

def test_bug2_an_invalid_color_hex_does_not_hide_a_valid_multi_color_list():
    filament = {"name": "Rainbow PLA", "material": "PLA", "color_hex": "not-a-hex",
                "multi_color_hexes": "FF0000,00FF00"}
    rec = spool_sync._slot_record(1, {"id": 7, "filament": filament}, None, "test")
    assert (rec["hex"], rec["hex_source"]) == ("FF0000", "spoolman")
    assert rec["multi_color"] is True


def test_bug2_an_eight_digit_color_hex_is_a_valid_spoolman_hex():
    filament = {"name": "Ocean PLA", "material": "PLA", "color_hex": "0C6283FF"}
    rec = spool_sync._slot_record(1, {"id": 7, "filament": filament}, None, "test")
    assert (rec["hex"], rec["hex_source"]) == ("0C6283", "spoolman")


def test_bug2_a_short_color_hex_beats_the_name(capsys):
    filament = {"name": "Red PLA", "material": "PLA", "color_hex": "#abc"}
    rec = spool_sync._slot_record(1, {"id": 7, "filament": filament}, None, "test")
    assert (rec["hex"], rec["hex_source"]) == ("AABBCC", "spoolman")


def test_binder_says_when_it_fell_all_the_way_to_the_neutral_default(capsys):
    rec = spool_sync._slot_record(1, {"id": 9, "filament": {"name": "Zzyzx PLA", "material": "PLA"}}, None, "test")
    assert (rec["hex"], rec["hex_source"]) == (fc.NEUTRAL_DEFAULT, "default")
    assert "WARNING: Spool #9 has no color code" in capsys.readouterr().out


# ------------------------------------------------------------- bug 4: genuine black AMS tray

def test_bug4_a_black_tray_is_black_and_only_alpha_zero_is_empty():
    assert ams._colour("000000FF") == "000000"
    assert ams._colour("000000") == "000000"
    assert ams._colour("00000000") is None  # the empty-tray report
    assert ams._colour("12345600") is None
    assert ams._colour("00FF00FF") == "00FF00"
    assert ams._colour("#0f0") == "00FF00"
    assert ams._colour("") is None and ams._colour(None) is None and ams._colour("xyz") is None


# ------------------------------------------------------------- bug 6: lifecycle binding

def _bind(filament, *, hex_code=None, old=None):
    state = {"spoolman_url": "http://spoolman.test:7912", "filament_slots": {}}
    if old:
        state["filament_slots"]["2"] = old
    data = {"id": 31, "archived": False, "filament": {"material": "PLA", **filament}}
    out = sl.replace_slot_spool(state, 2, material="PLA", color="Ocean", spoolman_id=31, hex_code=hex_code,
                                spoolman_spool_data=data)
    return out["slot_data"]


def test_bug6_binding_uses_the_multi_color_list_and_records_the_source():
    slot = _bind({"name": "Rainbow PLA", "color_hex": None, "multi_color_hexes": "FF0000,00FF00"})
    assert (slot["hex"], slot["hex_source"]) == ("FF0000", "spoolman")


def test_bug6_binding_reads_an_eight_digit_spoolman_hex():
    slot = _bind({"name": "Ocean PLA", "color_hex": "0C6283FF"})
    assert (slot["hex"], slot["hex_source"]) == ("0C6283", "spoolman")


def test_bug6_a_typed_hex_is_the_slot_hex():
    slot = _bind({"name": "Ocean PLA", "color_hex": "111111"}, hex_code="#abc")
    assert (slot["hex"], slot["hex_source"]) == ("AABBCC", "slot")


def test_bug6_binding_with_no_hex_anywhere_uses_the_name_not_white():
    slot = _bind({"name": "Burnt Orange PLA"})
    assert (slot["hex"], slot["hex_source"]) == ("CC5500", "name")


def test_bug6_binding_with_nothing_to_go_on_is_loudly_neutral_not_white(capsys):
    state = {"spoolman_url": "http://spoolman.test:7912", "filament_slots": {}}
    data = {"id": 31, "archived": False, "filament": {"material": "PLA", "name": "Zzyzx PLA"}}
    out = sl.replace_slot_spool(state, 2, material="PLA", color="zzyzx", spoolman_id=31, spoolman_spool_data=data)
    slot = out["slot_data"]
    assert (slot["hex"], slot["hex_source"]) == (fc.NEUTRAL_DEFAULT, "default")
    assert "WARNING: Slot 2 has no color code" in capsys.readouterr().out


def test_bug6_rebinding_the_same_spool_keeps_its_earlier_real_hex_over_a_guess():
    old = {"spoolman_id": 31, "hex": "0C6283", "hex_source": "slot"}
    slot = _bind({"name": "Zzyzx PLA"}, old=old)
    assert (slot["hex"], slot["hex_source"]) == ("0C6283", "slot")


def test_bug6_a_different_spools_old_hex_is_never_inherited():
    old = {"spoolman_id": 99, "hex": "0C6283", "hex_source": "slot"}
    slot = _bind({"name": "Zzyzx PLA"}, old=old)
    assert slot["hex"] == fc.NEUTRAL_DEFAULT


# ------------------------------------------------------------- bug 7: configured vs selected color

def _selection(configured_color, chosen_color):
    state = {"filament_slots": {"1": {"spoolman_id": 7, "color": configured_color, "material": "PLA"}}}
    return model_job.resolve_filament_selection(
        state, {"slot": 1, "spoolman_id": 7, "color": chosen_color, "material": "PLA"})


@pytest.mark.parametrize("configured, chosen", [
    ("#FFF", "FFFFFF"), ("FFFFFF", "#fff"), ("FFFFFF", "FAFAFA"), ("0C6283FF", "0C6283"), ("Coffee", " coffee "),
])
def test_bug7_equivalent_colors_match_the_configured_slot(configured, chosen):
    assert _selection(configured, chosen)["slot"] == 1


@pytest.mark.parametrize("configured, chosen", [("067749", "0C6283"), ("#FFF", "000000"), ("Coffee", "Blue")])
def test_bug7_different_colors_still_do_not_match(configured, chosen):
    with pytest.raises(ValueError, match="color does not match"):
        _selection(configured, chosen)


# ------------------------------------------------------------- bug 8: name coverage

@pytest.mark.parametrize("name", [
    "burnt orange", "peacock blue", "rose red", "olive", "teal", "navy", "maroon", "beige", "ivory", "silver",
    "gold", "copper", "bronze", "lavender", "mint", "sky blue", "coral", "salmon", "khaki", "charcoal",
    "transparent", "clear", "natural", "Burnt Orange PLA", "Matte Salmon",
])
def test_bug8_common_filament_color_names_are_known(name):
    guess = fc.derive(name, "PLA")
    assert guess is not None and guess.hex != fc.NEUTRAL_DEFAULT, name
    assert not guess.multi


def _lightness(rgb):
    return sum(int(rgb[i:i + 2], 16) for i in (0, 2, 4))


def test_bug8_dark_and_light_adjust_the_base_color():
    base = fc.derive("Cyan").hex
    dark, light = fc.derive("Dark Cyan").hex, fc.derive("Light Cyan").hex
    assert dark != base != light
    assert _lightness(dark) < _lightness(base) < _lightness(light)
    assert _lightness(fc.derive("dark olive").hex) < _lightness(fc.derive("olive").hex)
    assert _lightness(fc.derive("light coral").hex) > _lightness(fc.derive("coral").hex)
    assert fc.derive("Dark Cyan PLA", "PLA").multi_hexes == ()  # one color, not two


def test_bug8_listed_compounds_keep_their_own_swatch_and_modifier_alone_is_unknown():
    assert fc.derive("dark blue").hex == "0D47A1"
    assert fc.derive("dark") is None
    assert fc.derive("zzyzx") is None
    assert fc.resolve(None, name="zzyzx") == (fc.NEUTRAL_DEFAULT, "default")


# ------------------------------------------------------------- bug 9: infinite spool classifier

def test_bug9_eight_digit_and_short_hex_colors_group_with_their_six_digit_twins():
    slots = {'1': {'hex': '067749FF', 'material': 'PLA'}, '2': {'hex': '#067749', 'material': 'PLA'},
             '3': {'hex': '#0a0', 'material': 'PLA'}}
    assert contract.infinite_spool_groups(slots) == [{'color': '#067749', 'material': 'PLA', 'slots': [1, 2]}]


def test_bug9_name_only_slots_group_by_the_color_the_name_means():
    slots = {'1': {'color': 'Black', 'material': 'PLA'}, '2': {'color': 'black', 'material': 'PLA'},
             '3': {'color': 'white', 'material': 'PLA'}}
    assert contract.infinite_spool_groups(slots) == [{'color': '#161616', 'material': 'PLA', 'slots': [1, 2]}]


def test_bug9_a_slot_with_no_readable_color_still_cannot_be_grouped():
    with pytest.raises(RuntimeError, match='no documented color'):
        contract.infinite_spool_groups({'1': {'color': 'zzyzx', 'material': 'PLA'}, '2': {'hex': '#FFFFFF', 'material': 'PLA'}})
    with pytest.raises(RuntimeError, match='no documented material'):
        contract.infinite_spool_groups({'1': {'hex': '#FFFFFF'}})
