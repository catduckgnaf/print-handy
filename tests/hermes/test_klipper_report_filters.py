"""Stop and cancel diagnostics filters (2026-11-05 cancel post): offline fixtures only."""
from print_handy.hermes import klipper_report as kr

NOW = 1792070000.0

# The exact console chatter the 13:26 "Print cancelled" post showed as diagnostics.
ZMOD_PROMPT_STORE = [
    {"message": "// action:prompt_begin Select color", "time": NOW - 40},
    {"message": "// action:prompt_button Slot 3|RUN_ZCOLOR SLOT=3|primary", "time": NOW - 39},
    {"message": "RESPOND TYPE=command MSG=action:prompt_show", "time": NOW - 38},
    {"message": "CHANGE_ZCOLOR SLOT=4", "time": NOW - 37},
    {"message": "// Reset colors", "time": NOW - 36},
    {"message": "echo: Colors reset", "time": NOW - 35},
    {"message": "!! Move out of range: 240.000 10.000 0.200 [0.000]", "time": NOW - 30},
    {"message": "// Probe triggered at z=0.012", "time": NOW - 20},
]

# The post-restart startup config dump the same post showed as "klippy.log warnings".
STARTUP_DUMP = """partial line
Start printer at Thu Nov  5 13:27:02 2026 (1792073222.1 12.3)
===== Config file =====
[printer]
kinematics = cartesian
=======================
Loaded MCU 'mcu' 112 commands (v0.12.0)
MCU 'mcu' config: ADC_MAX=4095 BUS_PINS_i2c1=PB6,PB7 BUS_PINS_spi1=PA6,PA7,PA5 CLOCK_FREQ=72000000
MCU 'mcu' config: BUS_PINS_i2c2=PB10,PB11 RESERVE_PINS_crystal=PD0,PD1 STATS_SUMSQ_BASE=256
Configured MCU 'mcu' (1024 moves)
Starting heater checks for extruder
Starting heater checks for heater_bed
webhooks: registering remote method 'shutdown_machine' for connection id: 1
webhooks: registering remote method 'reboot_machine' for connection id: 1
Stats 23.1: gcodein=0 mcu: mcu_awake=0.001
"""

SHUTDOWN_SESSION = """partial line
Start printer at Thu Nov  5 08:43:10 2026 (1792060990.1 3.3)
MCU 'mcu' config: ADC_MAX=4095 BUS_PINS_i2c1=PB6,PB7
Starting heater checks for extruder
Stats 9000.1: gcodein=0 mcu: mcu_awake=0.004
Timer too close
MCU 'mcu' shutdown: Timer too close
Transition to shutdown state: MCU 'mcu' shutdown: Timer too close
Dumping gcode input 0 blocks
"""


def test_console_drops_zmod_prompt_traffic_and_keeps_real_errors():
    lines = kr.console_lines(ZMOD_PROMPT_STORE, NOW - 100)
    joined = "\n".join(lines)
    for noise in ("action:prompt", "RUN_ZCOLOR", "CHANGE_ZCOLOR", "Reset colors", "RESPOND", "Colors reset"):
        assert noise not in joined
    assert "Move out of range: 240.000 10.000 0.200 [0.000]" in lines
    assert "Probe triggered at z=0.012" in lines


def test_startup_config_dump_is_never_a_warning():
    assert kr.log_alarms(STARTUP_DUMP) == []
    assert kr.log_alarms("") == []


def test_warnings_come_from_before_the_restart():
    tail = SHUTDOWN_SESSION + STARTUP_DUMP.split("\n", 1)[1]
    alarms = kr.log_alarms(tail)
    assert "Transition to shutdown state: MCU 'mcu' shutdown: Timer too close" in alarms
    assert "Timer too close" in alarms
    assert not any("ADC_MAX" in a or "heater checks" in a or "registering" in a for a in alarms)


def test_cancel_post_says_no_warnings_instead_of_padding():
    status = {"webhooks": {"state": "ready"}, "print_stats": {"state": "cancelled", "print_duration": 600}}
    out = kr.build("f.gcode", status, ZMOD_PROMPT_STORE, STARTUP_DUMP, by_bot=False, now=NOW)
    assert "ADC_MAX" not in out and "RUN_ZCOLOR" not in out and "action:prompt" not in out
    assert kr.NO_WARNINGS in out
    assert "Move out of range" in out


def test_post_leads_with_the_captured_stop_reason():
    status = {"webhooks": {"state": "ready"}, "print_stats": {"state": "cancelled", "print_duration": 600}}
    reason = "MCU 'mcu' shutdown: Timer too close"
    out = kr.build("f.gcode", status, [], STARTUP_DUMP, by_bot=False, now=NOW, stop_reason=reason)
    assert out.splitlines()[1] == "**Stop reason:** " + reason
    assert "plain manual cancel" not in out and kr.NO_WARNINGS in out


def test_config_macro_declaration_does_not_override_real_stop_session():
    startup_with_macro = """partial line
Start printer at Thu Nov  5 13:27:02 2026 (1792073222.1 12.3)
===== Config file =====
[printer]
kinematics = cartesian
[gcode_macro CANCEL_PRINT]
description = Cancel print macro
rename_existing = CANCEL_PRINT_BASE
gcode =
  CANCEL_PRINT_BASE
=======================
Loaded MCU 'mcu' 112 commands (v0.12.0)
Starting heater checks for extruder
"""
    tail = SHUTDOWN_SESSION + startup_with_macro.split("\n", 1)[1]
    session = kr.stop_session(tail)
    assert any("Transition to shutdown state" in line for line in session)
    assert not any("CANCEL_PRINT_BASE" in line for line in session)
    alarms = kr.log_alarms(tail)
    assert "Transition to shutdown state: MCU 'mcu' shutdown: Timer too close" in alarms
