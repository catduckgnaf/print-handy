"""A multi-select's ticks come back in the page's order; the same set is no change."""
from print_handy.webui import pages
from print_handy.webui.config import read_fields


def test_the_same_ticks_in_another_order_is_no_change(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("printer:\n  model: klipper.generic\n")
    fields = read_fields(local=local, environ={})
    stored = next(fv for fv in fields if fv.field.id == "firmware.notice_firmwares").value
    assert len(stored) > 1
    form = {"firmware.notice_firmwares": list(reversed(stored))}
    changes, _submitted, errors = pages.parse_form(form, fields, only={"firmware.notice_firmwares"})
    assert errors == {} and changes == {}
    form = {"firmware.notice_firmwares": list(stored[:-1])}
    changes, _submitted, _errors = pages.parse_form(form, fields, only={"firmware.notice_firmwares"})
    assert [f.id for f in changes] == ["firmware.notice_firmwares"]
