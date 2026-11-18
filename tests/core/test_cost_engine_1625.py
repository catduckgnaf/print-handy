"""Formula tests for the cost engine (#1625).

The two worked examples adapt the reference research cases (FDM 50 g /
3 h and resin 30 mL / 4 h) to this model: waste and failure are
separate, failure divides repeatable lines by (1 - rate), and every
line is rounded to cents before summing.
"""
import pytest

from print_handy.core import cost


def _fdm_profile(**overrides):
    profile = cost.PrinterCostProfile(
        name="Test FDM", purchase_price=799.0, lifetime_hours=5000.0,
        watts=150.0)
    for key, value in overrides.items():
        setattr(profile, key, value)
    return profile


def _workspace(**overrides):
    workspace = cost.WorkspaceCost(electricity_rate_per_kwh=0.23,
                                   labor_rate_per_hour=20.0)
    for key, value in overrides.items():
        setattr(workspace, key, value)
    return workspace


def test_fdm_worked_example():
    # 50 g at 19.99/kg = 0.9995, waste 3 percent -> material 1.03.
    # Electricity 150 W x 3 h at 0.23 = 0.10; depreciation 3 x 799/5000
    # = 0.48; maintenance 3 x 0.05 = 0.15; consumables 0.10.
    # Repeatable 1.86, failure 5 percent -> 1.96 (failure 0.10).
    # Labor 15 min at 20/h = 5.00 once. Total 6.96.
    job = cost.JobCostInput(
        lines=[cost.MaterialLine(grams=50.0, manual_price_per_kg=19.99)],
        print_hours=3.0, prep_minutes=5.0, post_minutes_per_part=10.0)
    result = cost.compute(job, _fdm_profile(), _workspace())
    amounts = {entry["key"]: entry["amount"] for entry in result.lines}
    assert amounts["material"] == 1.03
    assert amounts["electricity"] == 0.10
    assert amounts["depreciation"] == 0.48
    assert amounts["maintenance"] == 0.15
    assert amounts["labor"] == 5.00
    assert amounts["failure"] == 0.10
    assert result.cost_per_build == 6.96
    assert result.cost_per_part == 6.96


def test_resin_worked_example():
    # 30 mL at 35/L = 1.05, waste 10 percent -> 1.16 (rounded).
    # Electricity: 60 W x 4 h plus 60 W wash/cure x 0.25 h at 0.23 = 0.06.
    # Depreciation 4 x 320/4000 = 0.32; maintenance 0.20; consumables
    # film 0.12 + IPA 0.23 + PPE 0.12 + screen 0.12 = 0.59.
    # Repeatable 2.33, failure 10 percent -> 2.59. Labor 5.00. Total 7.59.
    profile = cost.PrinterCostProfile(
        name="Test resin", technology=cost.RESIN, purchase_price=320.0,
        lifetime_hours=4000.0, watts=60.0)
    job = cost.JobCostInput(
        lines=[cost.MaterialLine(volume_ml=30.0,
                                 spool={"id": 9, "price": 35.0, "volume_ml": 1000.0})],
        print_hours=4.0, prep_minutes=5.0, post_minutes_per_part=10.0)
    # The bottle is priced by volume (35 per liter), so 30 mL is 1.05
    # before the waste allowance.
    result = cost.compute(job, profile, _workspace())
    amounts = {entry["key"]: entry["amount"] for entry in result.lines}
    assert amounts["material"] == 1.16
    assert amounts["consumables"] == 0.59
    assert result.totals_detail["consumable_parts"] == {
        "film": 0.12, "ipa": 0.23, "ppe": 0.12, "screen_wear": 0.12}
    assert result.cost_per_build == 7.59


def test_resin_bottle_priced_by_weight_uses_density():
    # 1 kg bottle at 38.50 with density 1.10 -> 909.09 mL, so 0.04235/mL.
    line = cost.MaterialLine(
        volume_ml=100.0,
        spool={"id": 7, "price": 38.50, "initial_weight": 1000.0},
        density=1.10)
    per_ml, source = cost.resolve_price(line, cost.WorkspaceCost(), cost.RESIN)
    assert per_ml == pytest.approx(0.04235, abs=1e-5)
    assert "spool" in source


def test_failure_divides_repeatable_only():
    # At 20 percent failure, repeatable 100 becomes 125 under
    # divide-by-(1-f); times-(1+f) would give 120 and is not used.
    # Labor and extras stay counted once either way.
    job = cost.JobCostInput(
        lines=[cost.MaterialLine(grams=1000.0, manual_price_per_kg=100.0)],
        print_hours=0.0, prep_minutes=60.0, post_minutes_per_part=0.0,
        consumables_per_print=0.0, waste_fraction=0.0, failure_rate=0.20)
    profile = cost.PrinterCostProfile(purchase_price=0.0, watts=0.0,
                                      maintenance_per_hour=0.0)
    result = cost.compute(job, profile, cost.WorkspaceCost(labor_rate_per_hour=10.0))
    # Material 100.00 repeatable -> 125.00; labor 10.00 once.
    assert result.failure_amount == 25.00
    assert result.cost_per_build == 135.00


def test_margin_and_markup_differ_and_are_labeled():
    job = cost.JobCostInput(
        lines=[cost.MaterialLine(grams=100.0, manual_price_per_kg=100.0)],
        waste_fraction=0.0, failure_rate=0.0, consumables_per_print=0.0)
    profile = cost.PrinterCostProfile(purchase_price=0.0, watts=0.0,
                                      maintenance_per_hour=0.0)
    margin = cost.compute(job, profile, cost.WorkspaceCost(
        pricing_mode="margin", default_margin=0.40, labor_rate_per_hour=0.0))
    markup = cost.compute(job, profile, cost.WorkspaceCost(
        pricing_mode="markup", default_margin=0.40, labor_rate_per_hour=0.0))
    assert margin.cost_per_build == markup.cost_per_build == 10.00
    assert margin.selling["default_price"] == 16.67   # 10 / (1 - 0.40)
    assert markup.selling["default_price"] == 14.00   # 10 x (1 + 0.40)
    assert "Margin" in margin.selling["mode_label"]
    assert "Markup" in markup.selling["mode_label"]


def test_per_build_vs_per_part_batch_math():
    # Prep is per build, post is per part: 4 parts share one prep.
    job = cost.JobCostInput(
        lines=[cost.MaterialLine(grams=40.0, manual_price_per_kg=25.0)],
        print_hours=1.0, parts_per_build=4, prep_minutes=30.0,
        post_minutes_per_part=10.0, waste_fraction=0.0, failure_rate=0.0,
        consumables_per_print=0.0)
    profile = cost.PrinterCostProfile(purchase_price=0.0, watts=0.0,
                                      maintenance_per_hour=0.0)
    result = cost.compute(job, profile, cost.WorkspaceCost(labor_rate_per_hour=60.0))
    # Labor minutes: 30 + 10 x 4 = 70 -> 70.00 at 60/h. Material 1.00.
    assert result.cost_per_build == 71.00
    assert result.cost_per_part == 17.75


def test_cent_rounding_sum_invariant():
    job = cost.JobCostInput(
        lines=[cost.MaterialLine(grams=33.3, manual_price_per_kg=19.99),
               cost.MaterialLine(grams=7.7, manual_price_per_kg=24.49)],
        print_hours=1.37, prep_minutes=7.0, post_minutes_per_part=3.0,
        parts_per_build=3)
    result = cost.compute(job, _fdm_profile(), _workspace())
    assert round(sum(entry["amount"] for entry in result.lines), 2) == result.cost_per_build


def test_provenance_on_every_result():
    job = cost.JobCostInput(lines=[cost.MaterialLine(grams=10.0)],
                            print_hours=1.0, grams_source="slicer G-code")
    result = cost.compute(job, _fdm_profile(), _workspace())
    prov = result.provenance
    assert prov["grams_source"] == "slicer G-code"
    assert prov["price_sources"]
    assert prov["printer_profile"] == "Test FDM"
    assert prov["currency_code"] == "USD"
    assert prov["lifetime_hours"] == 5000.0
    assert prov["timestamp"]


def test_multi_material_priced_per_spool():
    spools = [
        cost.MaterialLine(name="A", grams=10.0,
                          spool={"id": 1, "price": 20.0, "initial_weight": 1000.0}),
        cost.MaterialLine(name="B", grams=10.0,
                          spool={"id": 2, "price": 40.0, "initial_weight": 1000.0}),
    ]
    job = cost.JobCostInput(lines=spools, print_hours=0.0, waste_fraction=0.0,
                            failure_rate=0.0, consumables_per_print=0.0,
                            prep_minutes=0.0, post_minutes_per_part=0.0)
    profile = cost.PrinterCostProfile(purchase_price=0.0, watts=0.0,
                                      maintenance_per_hour=0.0)
    result = cost.compute(job, profile, cost.WorkspaceCost(labor_rate_per_hour=0.0))
    amounts = [entry["amount"] for entry in result.material_lines]
    assert amounts == [0.20, 0.40]
    assert result.cost_per_build == 0.60


def test_hobby_preset_labor_zero_still_shows_line():
    job = cost.JobCostInput(lines=[cost.MaterialLine(grams=10.0)],
                            print_hours=1.0, prep_minutes=30.0)
    result = cost.compute(job, _fdm_profile(),
                          _workspace(labor_rate_per_hour=0.0))
    amounts = {entry["key"]: entry["amount"] for entry in result.lines}
    assert amounts["labor"] == 0.0


def test_ad5x_preset_labeled_estimate():
    assert cost.WATTAGE_PRESETS["flashforge_ad5x"]["watts"] == 130.0
    assert cost.WATTAGE_PRESETS["flashforge_ad5x"]["basis"] == "estimate"
    assert cost.WATTAGE_PRESETS["flashforge_ad5m"]["basis"] == "measured"
