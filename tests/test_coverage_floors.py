"""``scripts/coverage_floors.py`` logic, on synthetic Cobertura fixtures.

These tests pin the SCRIPT's behavior -- above a floor passes, below a
floor fails and names the module, a missing module fails -- not the
live suite's coverage. The live floors are data in the script, set
below the levels the current suite actually achieves (see the
measuring note in the script's header).
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import coverage_floors  # noqa: E402

GUARD = "print_handy/core/slice_guard.py"


def xml_for(rates):
    classes = "".join(
        f'<class name="{Path(name).stem}" filename="{name}" line-rate="{rate}" branch-rate="0">'
        f'<methods/><lines/></class>'
        for name, rate in rates.items())
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<coverage line-rate="0" branch-rate="0" version="7" timestamp="0" '
            f'lines-covered="0" lines-valid="0" branches-covered="0" branches-valid="0">'
            f'<packages><package name="print_handy" line-rate="0" branch-rate="0">'
            f'<classes>{classes}</classes></package></packages></coverage>')


def rates_for(rates):
    import xml.etree.ElementTree as ET

    return coverage_floors.rates_from_xml(ET.fromstring(xml_for(rates)))


def all_at(rate):
    return {module: rate for module in coverage_floors.FLOORS}


def test_every_guard_module_at_or_above_its_floor_passes():
    rates = {module: floor for module, floor in coverage_floors.FLOORS.items()}
    assert coverage_floors.check_floors(rates_for(rates)) == []
    assert coverage_floors.check_floors(rates_for(all_at(1.0))) == []


def test_below_floor_fails_and_names_the_module_and_both_numbers():
    rates = all_at(1.0)
    rates[GUARD] = coverage_floors.FLOORS[GUARD] - 0.01
    problems = coverage_floors.check_floors(rates_for(rates))
    assert len(problems) == 1
    assert GUARD in problems[0] and "below the floor" in problems[0]


def test_a_missing_module_fails_even_when_everything_else_is_full():
    rates = all_at(1.0)
    del rates[GUARD]
    problems = coverage_floors.check_floors(rates_for(rates))
    assert len(problems) == 1
    assert GUARD in problems[0] and "missing" in problems[0]


def test_rates_are_read_from_cobertura_class_entries():
    rates = rates_for({GUARD: 0.5, "print_handy/other.py": 0.25})
    assert rates == {GUARD: 0.5, "print_handy/other.py": 0.25}


def test_the_floor_table_names_only_guard_modules():
    # The point of the tool is a small, named set -- a floor table that
    # quietly grows to the whole package would be a global percentage
    # in disguise.
    assert set(coverage_floors.FLOORS) == {
        "print_handy/core/slice_guard.py",
        "print_handy/core/launch_state.py",
        "print_handy/core/slice_refusal.py",
        "print_handy/core/slice_failure.py",
        "print_handy/diagnostics.py",
    }
    assert all(0 < floor < 1 for floor in coverage_floors.FLOORS.values())


def test_the_script_cli_exit_codes_and_output(tmp_path):
    good = tmp_path / "good.xml"
    good.write_text(xml_for(all_at(1.0)))
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "coverage_floors.py"),
                             str(good)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 problem(s)" in result.stdout

    rates = all_at(1.0)
    rates[GUARD] = 0.0
    bad = tmp_path / "bad.xml"
    bad.write_text(xml_for(rates))
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "coverage_floors.py"),
                             str(bad)], capture_output=True, text=True)
    assert result.returncode == 1
    assert GUARD in result.stdout
