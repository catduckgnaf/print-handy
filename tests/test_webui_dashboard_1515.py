"""#1515: the dashboard's nozzle and bed panels, the current-vs-target
temperature graph, and the chat prefill the nozzle swap button uses.

Offline: settings come from temp YAML, live reads and Spoolman are the
same injected fakes the #1445 dashboard tests use, and the hardware
record is injected through ``hardware_for``.
"""
import tempfile

from print_handy.webui import chat, dashboard, server

from tests.test_webui_dashboard import PRINTING, SNAP, load, spools_fake, status_fake

RECORD = {"nozzle": {"diameter_mm": 0.6, "type": "hardened_steel", "installed": "2026-11-10"},
          "z_offset_mm": -0.125, "z_offset_verified": True}


def render(tmp, record, extra="", live=PRINTING, history=None):
    data = load(tmp, extra)
    dash = dashboard.build_dashboard(
        data, status_for=status_fake(live), spools_for=spools_fake(),
        history=history or dashboard.TempHistory(), hardware_for=lambda _pid: record)
    return dash, server.render_status_html(SNAP, dashboard=dash)


def test_nozzle_panel_shows_the_fitted_nozzle_offset_and_swap_link():
    with tempfile.TemporaryDirectory() as tmp:
        dash, page = render(tmp, RECORD)
    facts = dash["cards"][0]["hardware"]
    assert facts["diameter_mm"] == 0.6 and facts["type"] == "hardened_steel"
    assert facts["temp_offset_c"] == 5.0  # nozzles.toml default for hardened steel
    for needle in ("0.6 mm · hardened steel", "fitted 2026-11-10", "Z offset -0.125 mm",
                   "Nozzle temperature offset +5°C", 'href="/chat?text=%2Fnozzle%20change"'):
        assert needle in page, needle
    assert "not verified" not in page


def test_bed_panel_lists_material_defaults_with_the_nozzle_offset_added():
    with tempfile.TemporaryDirectory() as tmp:
        dash, page = render(tmp, RECORD)
    rows = {row["family"]: row for row in dash["cards"][0]["hardware"]["materials"]}
    assert rows["PLA"]["base_nozzle_c"] == 210 and rows["PLA"]["nozzle_c"] == 215
    assert rows["PLA"]["bed_c"] == 60
    assert "<td>PETG</td><td>245°C</td><td>85°C</td>" in page


def test_configured_offset_override_wins_over_the_data_file():
    with tempfile.TemporaryDirectory() as tmp:
        dash, page = render(tmp, RECORD, "nozzle_temp_offsets:\n  hardened_steel: 10\n")
    assert dash["cards"][0]["hardware"]["temp_offset_c"] == 10.0
    assert "<td>PLA</td><td>220°C</td>" in page


def test_after_a_nozzle_change_the_panel_says_what_is_unverified_and_due():
    record = {"nozzle": {"diameter_mm": 0.4, "type": "brass", "installed": "2026-11-15"},
              "z_offset_mm": None, "z_offset_verified": False, "bed_mesh_stale": True,
              "calibration_pending": ["z_offset", "bed_mesh", "pressure_advance_flow"]}
    with tempfile.TemporaryDirectory() as tmp:
        _dash, page = render(tmp, record)
    for needle in ("Z offset: not recorded",
                   "Calibration due: Z offset, bed mesh, pressure advance and flow",
                   "Bed mesh: stale since the last nozzle change"):
        assert needle in page, needle


def test_no_hardware_record_falls_back_to_settings_and_says_so():
    with tempfile.TemporaryDirectory() as tmp:
        dash, page = render(tmp, None)
    facts = dash["cards"][0]["hardware"]
    assert facts["source"] == "settings" and facts["diameter_mm"] == 0.4
    assert "from settings: no nozzle change recorded yet" in page


def test_an_exploding_hardware_read_never_breaks_the_card():
    def boom(_pid):
        raise RuntimeError("record exploded")
    with tempfile.TemporaryDirectory() as tmp:
        data = load(tmp)
        dash = dashboard.build_dashboard(
            data, status_for=status_fake(PRINTING), spools_for=spools_fake(),
            history=dashboard.TempHistory(), hardware_for=boom)
        page = server.render_status_html(SNAP, dashboard=dash)
    assert "The hardware record could not be read." in page
    assert "benchy.gcode" in page


def test_history_keeps_targets_and_the_graph_draws_dashed_target_lines():
    clock = [1000.0]
    history = dashboard.TempHistory(clock=lambda: clock[0])
    history.sample("flashforge-ad5x", 150.0, 50.0, 210, 60)
    clock[0] += 600
    with tempfile.TemporaryDirectory() as tmp:
        dash, page = render(tmp, RECORD, history=history)
    samples = dash["cards"][0]["history"]
    assert samples[0] == (1000.0, 150.0, 50.0, 210.0, 60.0)
    for needle in ('<polyline class="nozzle target"', '<polyline class="bed target"',
                   '<polyline class="nozzle"', "Nozzle 210°C → 210°C", "Bed 60°C → 60°C",
                   "Last 10 min", "Solid: current; dashed: target."):
        assert needle in page, needle


def test_an_off_heater_draws_no_target_line():
    clock = [0.0]
    history = dashboard.TempHistory(clock=lambda: clock[0])
    history.sample("p", 23.0, 21.0, 0, 0)
    clock[0] += 60
    history.sample("p", 23.5, 21.0, 0, 0)
    card = {"history": history.samples("p"), "live": {"hotend_c": 23.5, "bed_c": 21.0}}
    svg = dashboard._temp_chart(card)
    assert ' target"' not in svg  # no "nozzle target" / "bed target" polyline
    assert "→" not in svg


def test_chat_prefill_accepts_only_known_commands():
    assert chat.prefill_text("/nozzle  change") == "/nozzle change"
    assert chat.prefill_text("/print-cancel") == ""
    assert chat.prefill_text('"><script>') == ""
