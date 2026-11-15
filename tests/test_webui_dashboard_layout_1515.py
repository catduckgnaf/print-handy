"""#1515 visual cleanup: the dashboard reads as status, system detail folds away.

Offline, with the #1445 dashboard fakes.
"""
import tempfile

from print_handy.webui import dashboard, server

from tests.test_webui_dashboard import PRINTING, SNAP, load, spools_fake, status_fake


def render(extra="webui:\n  controls_enabled: true\n", snap=SNAP, history=None):
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp, extra)
        dash = dashboard.build_dashboard(
            data, status_for=status_fake(PRINTING), spools_for=spools_fake(),
            history=history or dashboard.TempHistory(), hardware_for=lambda _p: None)
        return server.render_status_html(snap, dashboard=dash)


def test_card_puts_status_and_filament_before_temperature_and_hardware():
    page = render()
    order = [page.index(n) for n in ("benchy.gcode", "<h3>Filament</h3>",
                                     "<h3>Temperature</h3>", "<h3>Nozzle</h3>")]
    assert order == sorted(order)


def test_each_heater_gets_its_own_plot_row():
    clock = [0.0]
    history = dashboard.TempHistory(clock=lambda: clock[0])
    history.sample("flashforge-ad5x", 30.0, 25.0, 210, 60)
    clock[0] += 120
    page = render(history=history)
    assert page.count('<div class="temp-row">') == 2
    assert 'aria-label="Nozzle temperature history"' in page
    assert 'aria-label="Bed temperature history"' in page


def test_estop_is_the_strong_red_button():
    page = render()
    assert 'class="danger" data-estop="flashforge-ad5x"' in page
    assert ">Emergency stop</button>" in page


def test_system_details_fold_closed_when_all_is_well_and_open_on_a_warning():
    page = render()
    assert '<details class="card sys-details">' in page and "<summary>System details</summary>" in page
    warn = dict(SNAP, doctor=[{"label": "settings", "status": "warn", "detail": "check"}])
    assert '<details class="card sys-details" open>' in render(snap=warn)


def test_material_defaults_sit_behind_a_fold():
    page = render()
    assert '<details class="hw-more"><summary>Material temperature defaults</summary>' in page
