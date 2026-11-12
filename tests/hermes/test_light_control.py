"""Auto light-off (#516): command builders, light discovery, and the idle policy.

Everything here is the pure module (print_handy/runtime/light_control.py):
no Moonraker, no camera, no clock — times are injected. The listener
wiring that feeds it is exercised only through these contracts.
"""
import pytest

from print_handy.runtime import light_control as lc

T0 = 1_000_000.0
MIN = 60.0


# ---------------------------------------------------------------- builders

@pytest.mark.parametrize("value,on,off", [
    ("led chamber_led",
     "SET_LED LED=chamber_led RED=1 GREEN=1 BLUE=1 WHITE=1",
     "SET_LED LED=chamber_led RED=0 GREEN=0 BLUE=0 WHITE=0"),
    ("neopixel sb_leds",
     "SET_LED LED=sb_leds RED=1 GREEN=1 BLUE=1 WHITE=1",
     "SET_LED LED=sb_leds RED=0 GREEN=0 BLUE=0 WHITE=0"),
    ("output_pin caselight",
     "SET_PIN PIN=caselight VALUE=1",
     "SET_PIN PIN=caselight VALUE=0"),
])
def test_light_scripts_for_each_type(value, on, off):
    assert lc.light_scripts(value) == (on, off)


def test_light_scripts_type_is_case_insensitive():
    on, _off = lc.light_scripts("LED chamber_led")
    assert on.startswith("SET_LED LED=chamber_led")


@pytest.mark.parametrize("hostile", [
    "led chamber_led; M104 S0",            # a second command smuggled after a semicolon
    "led chamber_led\nM104 S0",            # a second line is a second command
    "led chamber_led RED=1",               # parameters are not a name
    "led chamber led",                     # whitespace inside the name
    "led",                                 # type without a name
    "chamber_led",                         # name without a type
    "fan chamber_led",                     # not a light type
    "led chamber_led extra tokens here",   # far beyond the type/name split
    "led chamber=led",                     # '=' is not a name character
    "led 'chamber_led'",                   # quotes are not name characters
    "",
    None,
    42,
])
def test_light_scripts_reject_hostile_names(hostile):
    with pytest.raises(ValueError):
        lc.light_scripts(hostile)


# --------------------------------------------------------------- discovery

OBJECTS = ["print_stats", "led chamber_led", "neopixel sb_leds",
           "output_pin caselight", "extruder", "filament_switch_sensor e0"]


def test_candidates_are_light_objects_only():
    assert lc.light_candidates(OBJECTS) == ["led chamber_led", "neopixel sb_leds",
                                            "output_pin caselight"]
    assert lc.light_candidates(None) == []
    assert lc.light_candidates("led chamber_led") == []


def test_configured_object_wins_over_discovery():
    target, candidates = lc.resolve_light_object("output_pin caselight", OBJECTS)
    assert target == "output_pin caselight"
    assert candidates == ["led chamber_led", "neopixel sb_leds", "output_pin caselight"]


def test_single_candidate_auto_resolves():
    target, candidates = lc.resolve_light_object("", ["print_stats", "led chamber_led"])
    assert target == "led chamber_led"
    assert candidates == ["led chamber_led"]


def test_multiple_candidates_stay_unresolved_with_the_list():
    target, candidates = lc.resolve_light_object("", OBJECTS)
    assert target is None
    assert candidates == ["led chamber_led", "neopixel sb_leds", "output_pin caselight"]


def test_zero_candidates_stay_unresolved():
    target, candidates = lc.resolve_light_object(None, ["print_stats", "extruder"])
    assert target is None
    assert candidates == []


def test_invalid_configured_object_raises():
    with pytest.raises(ValueError):
        lc.resolve_light_object("led chamber_led; M104 S0", OBJECTS)


# ------------------------------------------------------------------ policy

def policy(minutes=30, **kwargs):
    return lc.AutoLightPolicy(minutes, **kwargs)


def test_disabled_policy_is_a_complete_no_op():
    p = policy(0)
    assert p.enabled is False
    assert p.print_started(T0) is None
    p.print_finished(T0)
    assert p.should_check(T0 + 99999) is False
    assert p.note_verdict("empty_idle", T0 + 99999) is None
    # Negative and non-int minutes are also disabled at this layer
    # (settings refuses them earlier; the policy never trusts a caller).
    assert lc.AutoLightPolicy(-5).enabled is False
    assert lc.AutoLightPolicy("30").enabled is False


def test_print_start_turns_on_once_per_print():
    p = policy()
    assert p.print_started(T0) == "turn_on"
    assert p.print_started(T0 + 10) is None  # already printing: no repeat


def test_turn_off_needs_an_empty_streak_spanning_the_minutes():
    p = policy(30)
    p.print_started(T0)
    p.print_finished(T0 + 600)
    # Window age alone is not enough: the streak starts at the first
    # empty verdict, whenever that happens.
    assert p.note_verdict("empty_idle", T0 + 2400) is None       # streak opens at +40m
    assert p.note_verdict("empty_idle", T0 + 2400 + 29 * MIN) is None
    assert p.note_verdict("empty_idle", T0 + 2400 + 30 * MIN) == "turn_off"
    # Exactly once: the window is closed afterwards.
    assert p.note_verdict("empty_idle", T0 + 2400 + 31 * MIN) is None
    assert p.should_check(T0 + 2400 + 31 * MIN) is False


def test_streak_spanning_exactly_the_minutes_fires():
    p = policy(30)
    p.print_finished(T0)
    assert p.note_verdict("empty_idle", T0) is None
    assert p.note_verdict("empty_idle", T0 + 30 * MIN) == "turn_off"


@pytest.mark.parametrize("verdict", ["ok", "air", "spaghetti", "detached", "uncertain", "safe", ""])
def test_any_non_empty_verdict_resets_the_streak(verdict):
    """Conservative by spec: uncertainty and darkness (which arrive as
    'uncertain') reset just like a part on the plate does."""
    p = policy(30)
    p.print_finished(T0)
    assert p.note_verdict("empty_idle", T0) is None
    assert p.note_verdict(verdict, T0 + 20 * MIN) is None
    assert p.note_verdict("empty_idle", T0 + 40 * MIN) is None   # new streak opens here
    assert p.note_verdict("empty_idle", T0 + 40 * MIN + 29 * MIN) is None
    assert p.note_verdict("empty_idle", T0 + 40 * MIN + 30 * MIN) == "turn_off"


def test_no_turn_off_while_printing():
    p = policy(30)
    p.print_started(T0)
    assert p.note_verdict("empty_idle", T0) is None
    assert p.note_verdict("empty_idle", T0 + 60 * MIN) is None
    assert p.should_check(T0 + 60 * MIN) is False


def test_new_print_cancels_the_window_and_turns_the_light_back_on():
    p = policy(30)
    p.print_finished(T0)
    assert p.note_verdict("empty_idle", T0) is None
    assert p.print_started(T0 + 10 * MIN) == "turn_on"
    assert p.should_check(T0 + 10 * MIN) is False
    assert p.note_verdict("empty_idle", T0 + 60 * MIN) is None


def test_verdicts_outside_a_window_are_ignored():
    p = policy(30)
    assert p.note_verdict("empty_idle", T0) is None
    assert p.empty_since is None


def test_check_cadence():
    p = policy(30, check_interval_s=300)
    assert p.should_check(T0) is False          # no window yet
    p.print_finished(T0)
    assert p.should_check(T0) is True           # first check is due at once
    p.note_check(T0)
    assert p.should_check(T0 + 299) is False
    assert p.should_check(T0 + 300) is True
    # A verdict also counts as a check for cadence purposes.
    p.note_verdict("ok", T0 + 300)
    assert p.should_check(T0 + 599) is False
    assert p.should_check(T0 + 600) is True


def test_default_check_interval_is_five_minutes():
    assert lc.AutoLightPolicy(30).check_interval_s == 300


# -------------------------------------------- Bambu lights_report reader

def test_bambu_light_report_finds_the_chamber_light():
    snap = {"print": {"gcode_state": "IDLE",
                      "lights_report": [{"node": "chamber_light", "mode": "on"},
                                        {"node": "chamber_light2", "mode": "off"}]},
            "info": None}
    assert lc.bambu_light_report(snap) is True
    # The bare print payload is accepted too.
    assert lc.bambu_light_report(snap["print"]) is True


def test_bambu_light_report_without_the_node_is_a_reported_absence():
    snap = {"print": {"lights_report": [{"node": "work_light", "mode": "on"}]}}
    assert lc.bambu_light_report(snap) is False
    assert lc.bambu_light_report({"print": {"lights_report": []}}) is False


def test_bambu_light_report_without_a_report_is_unknown():
    assert lc.bambu_light_report({"print": {"gcode_state": "IDLE"}}) is None
    assert lc.bambu_light_report({"print": {"lights_report": "chamber_light"}}) is None
    assert lc.bambu_light_report({}) is None
    assert lc.bambu_light_report(None) is None
    assert lc.bambu_light_report("junk") is None


# ------------------------------------------------ SDCP LightStatus reader

def test_sdcp_light_state_reads_second_light():
    raw = {"Status": {"CurrentStatus": [0], "LightStatus": {"SecondLight": 1}},
           "PrintInfo": {}}
    assert lc.sdcp_light_state(raw) is True
    raw["Status"]["LightStatus"]["SecondLight"] = 0
    assert lc.sdcp_light_state(raw) is False
    # Top-level nesting (firmware builds differ) reads too.
    assert lc.sdcp_light_state({"LightStatus": {"SecondLight": 1}}) is True


def test_sdcp_light_state_unknown_shapes():
    assert lc.sdcp_light_state({"Status": {"CurrentStatus": [0]}}) is None
    assert lc.sdcp_light_state({"Status": {"LightStatus": {"SecondLight": 2}}}) is None
    assert lc.sdcp_light_state({"Status": {"LightStatus": {"SecondLight": True}}}) is None
    assert lc.sdcp_light_state({"Status": {"LightStatus": "on"}}) is None
    assert lc.sdcp_light_state(None) is None
    assert lc.sdcp_light_state({}) is None
