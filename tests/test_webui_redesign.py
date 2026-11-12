"""Web UI redesign (#875): Printers page with Add a printer, Settings tabs,
search and Restart setup, the Spools printer switcher, Layers controls."""
from print_handy import settings
from print_handy.webui import layers, pages, printers_page, spools, walkthrough

ROSTER = """\
printers:
  - id: workshop-ad5x
    name: Workshop AD5X
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
"""


def _write(tmp_path, text=ROSTER):
    path = tmp_path / "local.yaml"
    path.write_text(text + f"paths:\n  state_home: {tmp_path}/h\n")
    return path


def test_add_printer_appends_through_the_roster_writer(tmp_path):
    path = _write(tmp_path)
    environ = {"PRINT_HANDY_CONFIG": str(path)}
    roster = settings.printers(settings.load(local=path, environ=environ))
    ok, message = printers_page.add(
        {"name": ["Garage AD5X"], "model": ["flashforge.ad5x"],
         "address": ["http://192.168.1.62:7125"]}, roster, environ=environ)
    assert ok, message
    after = settings.printers(settings.load(local=path, environ=environ))
    assert [entry["id"] for entry in after][0] == "workshop-ad5x"
    added = after[-1]
    assert added["name"] == "Garage AD5X"
    assert added["moonraker_url"] == "http://192.168.1.62:7125"
    assert "Restart" in message
    assert list(tmp_path.glob("local.yaml.*"))  # the backup is kept


def test_add_printer_refuses_bad_input_without_writing(tmp_path):
    path = _write(tmp_path)
    before = path.read_text()
    environ = {"PRINT_HANDY_CONFIG": str(path)}
    for form in ({"model": ["flashforge.ad5x"]}, {"name": ["X"], "model": ["nope"]},
                 {"name": ["Lab A1"], "model": ["bambulab.a1"], "address": ["192.168.1.9"]}):
        ok, _message = printers_page.add(form, [], environ=environ)
        assert not ok
    assert path.read_text() == before


def test_bambu_entry_names_the_secret_variable_not_the_secret():
    entry, error = printers_page.build_entry(
        {"name": ["Lab A1"], "model": ["bambulab.a1"], "address": ["192.168.1.9"],
         "serial": ["00M00A000000000"]}, [])
    assert error is None and entry is not None
    assert entry["lan_access_code_env"] == "BAMBU_LAN_PASSWORD_LAB_A1"
    assert "lan_access_code" not in entry


def test_printers_page_renders_cards_and_the_add_form():
    html = printers_page.render([{"id": "workshop-ad5x", "name": "Workshop AD5X",
                                  "model": "flashforge.ad5x"}], form_token="t")
    assert 'href="/spools/workshop-ad5x"' in html
    assert 'action="/printers"' in html and 'name="model"' in html


def test_config_page_has_tabs_search_and_restart_setup(tmp_path):
    path = _write(tmp_path)
    data = settings.load(local=path, environ={"PRINT_HANDY_CONFIG": str(path)})
    from print_handy.webui import config as config_api
    fields = config_api.read_fields(environ={"PRINT_HANDY_CONFIG": str(path)})
    html = pages.render_config_page(fields, data=data, form_token="t")
    assert 'class="pills config-tabs"' in html and 'href="/config?section=' in html
    assert 'id="field-search"' in html
    assert 'name="action" value="restart"' in html


def test_restart_setup_starts_again_without_a_redirect_loop(tmp_path):
    path = _write(tmp_path)
    data = settings.load(local=path, environ={"PRINT_HANDY_CONFIG": str(path)})
    walkthrough.save_state(data, {"step": 4, "done": True})
    assert not walkthrough.pending(data)
    walkthrough.restart(data)
    assert walkthrough.pending(data)
    assert not walkthrough.offer_once(data)  # already offered: banner only


def test_spools_page_switcher_only_with_more_than_one_printer():
    view = {"id": "a", "name": "A", "slots": [], "shelf": [], "spools": [], "conflicts": []}
    one = spools.render_spools_page(view, form_token="t", printers=[{"id": "a"}])
    assert "switcher" not in one
    two = spools.render_spools_page(view, form_token="t",
                                    printers=[{"id": "a"}, {"id": "b", "name": "B"}])
    assert 'class="switcher"' in two and 'href="/spools/b"' in two


def test_layers_controls_are_always_visible():
    from tests.test_webui_layers import PLATE
    parsed = layers.parse_gcode(PLATE)
    page = layers.render_plate(parsed, 0, title="x")
    assert 'type="range"' in page and "data-zoom" in page
