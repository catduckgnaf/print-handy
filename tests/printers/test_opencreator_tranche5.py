import pytest

from print_handy.printers.flashforge_opencreator.adapter import OpenCreatorAdapter
from print_handy.printers.flashforge_opencreator.start_contract import option_params


@pytest.mark.parametrize(
    ("option", "parameter"),
    [
        ("flow_calibration", "FLOW_CALIBRATION=1"),
        ("purge", "PURGE=0"),
        ("purge_line", "PURGE_LINE=0"),
        ("purge_line_all_tools", "PURGE_LINE_ALL_TOOLS=1"),
        ("bed_leveling", "BED_LEVELING=0"),
        ("host_pressure_advance", "HOST_PRESSURE_ADVANCE=1"),
    ],
)
def test_option_renders_as_uppercase_start_parameter(option, parameter):
    rendered = option_params({f"opencreator_{option}": parameter.endswith("=1")})

    assert rendered.startswith("C5_PRINT_START ")
    assert parameter in rendered
    assert "BUILDPLATE_CHECK=0" in rendered
    assert rendered == rendered.upper()


def test_adapter_appends_option_line_after_start_lines():
    adapter = OpenCreatorAdapter.__new__(OpenCreatorAdapter)
    adapter.model = None
    lines = adapter.start_plan(
        60,
        215,
        options={"opencreator_purge": False},
    )

    assert lines[-1] == "C5_PRINT_START PURGE=0 BUILDPLATE_CHECK=0"


def test_unknown_option_is_refused():
    with pytest.raises(ValueError, match="unknown OpenCreator option"):
        option_params({"opencreator_not_a_real_option": True})


def test_defaults_render_no_explicit_start_line():
    assert option_params() == ""
    assert option_params({}) == ""
