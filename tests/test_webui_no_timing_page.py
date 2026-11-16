"""The Web UI has no Timing page (catduck, 2026-11-16).

The timing report itself stays: doctor, the diagnostics bundle and
``python3 -m print_handy.jobs.timing_report`` still print it. Only the
page, its route and its navigation entry are gone.
"""
import importlib.util

from print_handy.webui import routes, server, shell


def test_timing_is_not_in_the_web_ui_navigation():
    assert "/timing" not in {path for path, _label in shell.NAV}
    assert "Timing" not in {label for _path, label in shell.NAV}


def test_timing_has_no_route_of_its_own():
    route, _capture = routes.resolve("GET", "/timing")
    # Only the catch-all answers it now, like any unknown path.
    assert route is not None and route.handler != "_timing_route"
    assert not hasattr(server._Handler, "_timing_route")
    assert importlib.util.find_spec("print_handy.webui.timing") is None


def test_the_timing_report_stays_for_doctor_and_diagnostics():
    from print_handy.jobs import timing_report
    assert callable(timing_report.build_report) and callable(timing_report.render_text)
