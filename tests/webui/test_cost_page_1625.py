"""Web UI cost settings and calculator tests (#1625)."""
from print_handy import settings
from print_handy.core import cost
from print_handy.webui import catalog, cost_page


def test_settings_defaults_carry_cost_section():
    data = settings.load()
    workspace = cost.WorkspaceCost.from_settings(data)
    assert workspace.currency_code == "USD"
    assert workspace.electricity_rate_per_kwh == 0.19
    assert workspace.failure_rate_fdm == 0.05
    assert workspace.failure_rate_resin == 0.10
    assert workspace.pricing_mode == "margin"
    assert workspace.default_margin == 0.40
    assert workspace.default_filament_cost_per_kg == 22.0
    assert data["cost_printers"] == {}


def test_settings_validate_rejects_bad_pricing_mode():
    data = settings.load()
    data["cost"]["pricing_mode"] = "sideways"
    try:
        settings.validate(data)
    except settings.SettingsError as exc:
        assert "pricing_mode" in str(exc)
    else:
        raise AssertionError("validate accepted a bad pricing_mode")


def test_catalog_covers_every_cost_field():
    ids = {field.id for field in catalog.CATALOG}
    expected = {f"cost.{key}" for key in cost.WorkspaceCost().__dict__}
    # WorkspaceCost attribute names map to settings keys one to one,
    # except the per-kWh and per-kg spellings.
    expected = {name.replace("electricity_rate_per_kwh", "electricity_rate_per_kwh")
                for name in expected}
    assert expected <= ids


def test_no_dead_options_engine_reads_catalog_fields():
    # Every catalog cost field is a key the engine's settings reader
    # consumes (WorkspaceCost.from_settings reads the whole section).
    data = {"cost": {"electricity_rate_per_kwh": 0.31, "labor_rate_per_hour": 12.0,
                     "tax_rate": 0.2, "marketplace_fee_rate": 0.1,
                     "overhead_percent": 0.05, "flat_fee_per_job": 1.5,
                     "default_filament_cost_per_kg": 30.0, "currency_code": "EUR",
                     "pricing_mode": "markup", "tax_mode": "included",
                     "failure_rate_fdm": 0.07, "failure_rate_resin": 0.12,
                     "default_margin": 0.5}}
    workspace = cost.WorkspaceCost.from_settings(data)
    assert workspace.electricity_rate_per_kwh == 0.31
    assert workspace.currency_code == "EUR"
    assert workspace.pricing_mode == "markup"
    assert workspace.tax_mode == "included"
    assert workspace.failure_rate_resin == 0.12


def test_calculator_page_computes_same_numbers_as_engine():
    data = settings.load()
    form = {"technology": ["fdm"], "amount": ["50"], "hours": ["3"],
            "parts": ["1"], "price_per_kg": ["20"], "prep_minutes": ["5"],
            "post_minutes": ["10"], "pack_minutes": ["0"], "hardware": ["0"],
            "packaging": ["0"]}
    job, profile, workspace = cost_page.job_from_form(form, data)
    result = cost.compute(job, profile, workspace)
    page = cost_page.render_cost_page(data, form=form, result=result)
    assert f"{result.cost_per_build:.2f}" in page
    assert "Cost per build" in page
    assert "AD5X" in page and "estimate" in page


def test_calculator_page_simple_and_advanced_fields():
    page = cost_page.render_cost_page(settings.load())
    assert 'name="amount"' in page
    assert 'name="hours"' in page
    assert "data-advanced" in page  # advanced inputs are on the page


def test_history_rows_only_for_priced_records():
    rows = cost_page.history_cost_rows([
        {"title": "A", "cost": {"cost_per_build": 3.5, "currency_code": "USD"}},
        {"title": "B"}])
    assert len(rows) == 1 and "3.50" in rows[0]
