"""#1817 E keeps the bare `sv06` model name working for configs written before a
second vendor registered an sv06 (review of #1834)."""
from print_handy import printers, settings


def test_bare_sv06_still_resolves_to_sovol():
    assert printers.resolve_key("sv06") == "sovol.sv06"


def test_a_config_with_the_bare_sv06_model_loads_as_sovol():
    data = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "sv06"})
    assert data["printer"]["model"] == "sovol.sv06"
