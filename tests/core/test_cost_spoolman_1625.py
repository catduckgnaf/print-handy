"""Spoolman precedence, freeze and aggregate tests (#1625)."""
from print_handy.core import cost, print_stats


def _line(**kwargs):
    return cost.MaterialLine(**kwargs)


def test_precedence_spool_then_filament_then_slicer_then_default():
    workspace = cost.WorkspaceCost(default_filament_cost_per_kg=22.0)
    spool = {"id": 3, "price": 30.0, "initial_weight": 1000.0,
             "filament": {"price": 25.0, "weight": 1000.0}}
    per_g, source = cost.resolve_price(_line(spool=spool), workspace)
    assert per_g == 0.030 and "spool" in source

    per_g, source = cost.resolve_price(
        _line(filament={"price": 25.0, "weight": 1000.0}), workspace)
    assert per_g == 0.025 and "filament profile" in source

    per_g, source = cost.resolve_price(
        _line(slicer_price_per_kg=20.0), workspace)
    assert per_g == 0.020 and "slicer-embedded" in source

    per_g, source = cost.resolve_price(_line(), workspace)
    assert per_g == 0.022 and "default" in source


def test_net_weight_excludes_tare():
    # The filament record's weight is net; spool_weight (tare) never
    # enters the price math even when it rides along on the record.
    filament = {"price": 25.0, "weight": 1000.0, "spool_weight": 250.0}
    per_g, _source = cost.resolve_price(
        _line(filament=filament), cost.WorkspaceCost())
    assert per_g == 0.025


def test_freeze_round_trip_and_history_immutability():
    job = cost.JobCostInput(lines=[cost.MaterialLine(grams=50.0)], print_hours=2.0)
    result = cost.compute(job, cost.PrinterCostProfile(purchase_price=500.0),
                          cost.WorkspaceCost())
    frozen = cost.freeze(result)
    record = {"title": "Bracket", "printer": "AD5X", "cost": frozen}
    # A later price edit changes a fresh computation, never the record.
    dearer = cost.compute(
        cost.JobCostInput(lines=[cost.MaterialLine(
            grams=50.0, manual_price_per_kg=99.0)], print_hours=2.0),
        cost.PrinterCostProfile(purchase_price=500.0), cost.WorkspaceCost())
    assert dearer.cost_per_build != frozen["cost_per_build"]
    restored = cost.CostResult.from_dict(record["cost"])
    assert restored.cost_per_build == result.cost_per_build
    assert record["cost"] == frozen


def test_single_decrement_guard():
    assert cost.should_decrement_spoolman("auto", moonraker_already_reported=True) is False
    assert cost.should_decrement_spoolman("printer", moonraker_already_reported=False) is False
    assert cost.should_decrement_spoolman("auto", moonraker_already_reported=False) is True
    assert cost.should_decrement_spoolman("print_handy", moonraker_already_reported=False) is True


def _priced_record(title, build, currency="USD", printer="AD5X", material="PLA",
                   grams=50.0, hours=2.0, failure=0.10, spool_id=3):
    return {"title": title, "printer": printer, "status": "completed",
            "started_at": "2026-03-01T10:00:00Z", "cost": {
        "cost_per_build": build, "cost_per_part": build, "currency_code": currency,
        "failure_amount": failure,
        "material_lines": [{"name": material, "material": material,
                            "amount": 1.0, "spool_id": spool_id}],
        "totals_detail": {"total_grams": grams, "print_hours": hours}}}


def test_aggregates_from_frozen_records_only():
    records = [_priced_record("A", 5.00), _priced_record("B", 7.50),
               {"title": "unpriced"}]
    agg = cost.aggregate_costs(records)
    assert agg["has_priced"] is True
    assert agg["priced_count"] == 2 and agg["unpriced_count"] == 1
    assert agg["total_spend"] == 12.50
    assert agg["by_printer"] == {"AD5X": 12.50}
    assert agg["by_material"] == {"PLA": 2.0}
    assert agg["most_expensive"]["title"] == "B"
    assert agg["cost_per_gram"] == 0.13  # 12.50 / 100 g, cent-rounded
    # Failure cost is what failed prints cost (#1632 review); both of
    # these completed, so there is none, and the allowance they carried
    # is reported separately.
    assert agg["failure_total"] == 0.0 and agg["failed_count"] == 0
    assert agg["failure_allowance_total"] == 0.20


def test_aggregates_never_mix_currencies():
    records = [_priced_record("A", 5.00, currency="USD"),
               _priced_record("B", 9.00, currency="EUR"),
               _priced_record("C", 1.00, currency="EUR")]
    agg = cost.aggregate_costs(records)
    assert agg["currency_code"] == "EUR"
    assert agg["total_spend"] == 10.00
    assert sorted(agg["currencies"]) == ["EUR", "USD"]


def test_aggregates_plain_note_when_no_priced_jobs():
    agg = cost.aggregate_costs([{"title": "x"}])
    assert agg["has_priced"] is False
    assert "No priced jobs" in agg["note"]


def test_print_stats_payload_carries_costs():
    records = [_priced_record("A", 5.00), {"title": "unpriced"}]
    payload = print_stats.aggregate(records)
    assert payload["costs"]["total_spend"] == 5.00
    wrapped = print_stats.build_wrapped(records, 2026)
    script = "\n".join(wrapped["script"])
    assert "recorded print costs" in script


def test_print_stats_no_cost_line_without_priced_jobs():
    wrapped = print_stats.build_wrapped([{"title": "x", "status": "completed",
                                          "started_at": "2026-03-01T10:00:00Z"}], 2026)
    assert "recorded print costs" not in "\n".join(wrapped["script"])
    assert wrapped["stats"]["costs"]["has_priced"] is False
