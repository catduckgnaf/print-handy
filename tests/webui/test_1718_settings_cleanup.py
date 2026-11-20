"""#1718: settings, defaults, and cost cleanup (from the #1717 umbrella).

Pins the flipped defaults, the vision endpoint's removal from the Web
UI catalog, the new picker fields, the Spoolman section name, Restart
setup living under Printers only, Chat first in the navigation, the
chat page's Ask AI default, and the cost calculator's filled and named
defaults with Calculate at the top right.
"""
from print_handy import settings
from print_handy.webui import catalog, chat, cost_page, pages, shell
from print_handy.webui import config as config_api


def test_flipped_defaults_load():
    data = settings.load(local=False, environ={})
    assert data["spool"]["prompt_brand"] is True
    assert data["discord"]["approval_all_users"] is False
    assert data["timelapse"]["wrapped_video"] is True
    assert data["timelapse"]["wrapped_tts_engine"] == "piper"
    # The default music is the bundled track: the file stays empty.
    assert data["timelapse"]["wrapped_music_file"] == ""


def test_vision_endpoint_and_model_are_not_webui_fields():
    assert catalog.field_by_id("services.vision_chat_url") is None
    assert catalog.field_by_id("vision.model") is None
    note = catalog.GROUP_FILE_ONLY_NOTES["ai"]
    assert "vision_chat_url" in note and "vision.model" in note
    # No catalog text shown on the page names a proxy or an internal
    # model alias.
    for field in catalog.CATALOG:
        assert "CLIProxy" not in field.help and "gemini" not in field.help


def test_roasts_switch_lives_with_chat_platform_fields():
    field = catalog.field_by_id("vision.roast_enabled")
    assert field is not None and field.kind == "toggle"
    assert field.group == "platform"
    assert field.label == "Roasts"


def test_picker_fields_for_known_choice_sets():
    currency = catalog.field_by_id("cost.currency_code")
    assert currency.kind == "picker" and "USD" in currency.choices
    encoder = catalog.field_by_id("timelapse.encoder")
    assert encoder.kind == "picker"
    assert catalog.choices_for(encoder) == tuple(settings.TIMELAPSE_ENCODERS)
    capture = catalog.field_by_id("timelapse.capture_encoder")
    assert capture.kind == "picker"
    assert catalog.choices_for(capture) == tuple(settings.CAPTURE_ENCODERS)
    mode = catalog.field_by_id("timelapse.vertical_short_mode")
    assert mode.kind == "picker"
    assert catalog.choices_for(mode) == tuple(settings.VERTICAL_SHORT_MODES)
    assert catalog.field_by_id("timelapse.vertical_short").kind == "toggle"
    assert catalog.field_by_id("timelapse.vertical_short_overlay").kind == "toggle"


def test_spoolman_section_heading_and_hover_css():
    assert ("spools", "Spoolman") in pages.GROUPS
    assert ("spools", "Spools") not in pages.GROUPS
    # Hovering a pill tab must not paint it the page background (black).
    assert "nav.pills a:hover { background: var(--color-line);" in shell.CSS
    assert "nav.pills a:hover { background: var(--color-bg); }" not in shell.CSS
    # The layout is narrower than the old 64rem measure.
    assert "max-width: 56rem" in shell.CSS


def test_chat_is_first_in_the_navigation():
    assert shell.NAV[0] == ("/chat", "Chat")
    assert shell.NAV[-1] == ("/config", "Settings")


def _config_page(section):
    data = settings.load(local=False, environ={})
    fields = config_api.read_fields(environ={})
    return pages.render_config_page(fields, data=data, form_token="tok",
                                    section=section)


def test_restart_setup_only_under_printers():
    printers = _config_page("printers")
    assert '<div data-restart-setup>' in printers
    assert "Restart setup" in printers
    cost = _config_page("cost")
    assert '<div data-restart-setup hidden>' in cost
    # The tab script keeps the block hidden outside Printers.
    assert "restartSetup.hidden=name!=='printers'" in cost


def test_chat_page_ask_ai_default_and_no_print_help_suggestion():
    class Service:
        def busy(self):
            return False

        def open_prompt(self):
            return None

        def entries(self):
            return []

    page = chat.render_chat_page(Service(), form_token="tok", ask_ai_default=True)
    assert 'name="ask_ai" value="1" checked' in page
    assert "/print-help" not in page
    off = chat.render_chat_page(Service(), form_token="tok")
    assert 'name="ask_ai" value="1" checked' not in off
    assert "/print-help" not in off


def test_cost_page_fills_and_names_defaults_and_calculate_is_top_right():
    data = settings.load(local=False, environ={})
    values = cost_page.defaults_for(data)
    assert values["price_per_kg"] == "22"
    profile, _workspace = cost_page.cost_module.printer_profile_for(data)
    assert values["watts"] == f"{profile.watts:g}"
    assert values["failure_percent"] == "5"
    resin_values = cost_page.defaults_for(data, "resin")
    assert resin_values["technology"] == "resin"
    assert resin_values["failure_percent"] == "10"
    resin_page = cost_page.render_cost_page(data, form={"technology": ["resin"]})
    assert 'name="failure_percent" inputmode="decimal" value="10"' in resin_page
    assert 'name="technology"><option value="fdm">Filament (FDM)</option><option value="resin" selected>' in resin_page
    zero_form = {"technology": ["resin"], "failure_percent": ["0"]}
    assert 'name="failure_percent" inputmode="decimal" value="0"' in cost_page.render_cost_page(
        data, form=zero_form)
    job, _profile, _workspace = cost_page.job_from_form(zero_form, data)
    assert job.failure_rate == 0
    override_form = {"technology": ["resin"], "failure_percent": ["12"]}
    assert 'name="failure_percent" inputmode="decimal" value="12"' in cost_page.render_cost_page(
        data, form=override_form)
    job, _profile, _workspace = cost_page.job_from_form(override_form, data)
    assert job.failure_rate == 0.12
    notes = cost_page.default_notes(data)
    assert notes["price_per_kg"].startswith("Default: 22")
    page = cost_page.render_cost_page(data, form_token="tok")
    assert "Default: 22 per kilogram" in page or "Default: 22" in page
    assert 'name="price_per_kg" inputmode="decimal" value="22"' in page
    # Calculate cost is in the form's head row, before the first field.
    assert page.index("Calculate cost") < page.index('name="technology"')
