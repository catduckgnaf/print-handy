"""#1632 review: the fixes to how the cost engine is fed and aggregated.

Each test here failed on the PR head ce15b253 that the review covered.
"""
from print_handy.core import cost
from print_handy.runtime import print_records

AD5X_ROSTER = {"printers": [{"id": "flashforge-ad5x", "name": "Garage AD5X",
                             "model": "flashforge.ad5x", "default": True}]}


def _fdm(grams=50.0, hours=3.0, outcome="estimate", material="PLA"):
    return cost.JobCostInput(lines=[cost.MaterialLine(material=material, grams=grams)],
                             print_hours=hours, outcome=outcome)


def test_a_rostered_model_uses_its_shipped_preset():
    profile, _ = cost.printer_profile_for(AD5X_ROSTER, printer_id="flashforge-ad5x")
    assert profile.watts == 130.0 and profile.watts_basis == "estimate"
    assert "AD5X" in profile.name


def test_a_record_with_no_printer_id_resolves_the_default_printer():
    # Completion records on a one-printer install carry printer_id None and
    # the Moonraker host name, not the roster name.
    profile, _ = cost.printer_profile_for(AD5X_ROSTER, printer_id=None,
                                          printer_name="Klipper 3D Printer")
    assert profile.watts == 130.0


def test_cost_printers_layers_over_the_model_preset():
    data = dict(AD5X_ROSTER, cost_printers={"flashforge-ad5x": {"purchase_price": 399}})
    profile, _ = cost.printer_profile_for(data, printer_name="Klipper 3D Printer")
    assert profile.purchase_price == 399 and profile.watts == 130.0


def test_an_unmapped_model_falls_back_to_generic():
    data = {"printers": [{"id": "x", "model": "klipper.generic"}]}
    profile, _ = cost.printer_profile_for(data, printer_id="x")
    assert profile.name == "Generic FDM" and profile.watts == 120.0


def test_a_failed_print_carries_no_failure_allowance_and_no_post_labor():
    ws = cost.WorkspaceCost()
    profile = cost.PrinterCostProfile()
    done = cost.compute(_fdm(20, 1, "completed"), profile, ws)
    failed = cost.compute(_fdm(20, 1, "failed"), profile, ws)
    assert done.failure_amount > 0
    assert failed.failure_amount == 0 and failed.outcome == "failed"
    labor = {e["key"]: e["amount"] for e in failed.lines}["labor"]
    assert labor == round(5 / 60 * 20, 2)  # prep only


def test_out_of_pocket_excludes_labor_and_machine_wear():
    ws = cost.WorkspaceCost()
    result = cost.compute(_fdm(12, 0.25), cost.PrinterCostProfile(), ws)
    by_key = {e["key"]: e["amount"] for e in result.lines}
    assert result.out_of_pocket < result.cost_per_build
    expected = round((by_key["material"] + by_key["electricity"] + by_key["consumables"])
                     / (1 - ws.failure_rate_fdm), 2)
    assert result.out_of_pocket == expected
    assert by_key["labor"] == 5.0  # still on the full cost, not out of pocket


def test_hobby_chat_leads_with_out_of_pocket_and_seller_with_full_cost():
    profile = cost.PrinterCostProfile()
    hobby = cost.compute(_fdm(), profile, cost.WorkspaceCost())
    seller = cost.compute(_fdm(), profile, cost.WorkspaceCost(cost_mode="seller"))
    assert cost.format_chat_summary(hobby).startswith("Out of pocket")
    assert "Labor" not in cost.format_chat_summary(hobby)
    assert cost.format_chat_summary(seller).startswith("Cost:")
    assert "in filament and power" in cost.ready_line(hobby)


def test_failure_cost_is_what_failed_prints_cost():
    ws, profile = cost.WorkspaceCost(), cost.PrinterCostProfile()
    ok = cost.freeze(cost.compute(_fdm(50, 2, "completed"), profile, ws))
    bad = cost.freeze(cost.compute(_fdm(20, 1, "failed"), profile, ws))
    agg = cost.aggregate_costs([{"title": "ok", "status": "completed", "cost": ok},
                                {"title": "bad", "status": "failed", "cost": bad}])
    assert agg["failed_count"] == 1
    assert agg["failure_total"] == bad["out_of_pocket"]
    assert agg["failure_allowance_total"] == ok["failure_amount"]


def test_mixed_currency_counts_only_the_headline_currency():
    def rec(amount, code):
        return {"cost": {"cost_per_build": amount, "currency_code": code}}
    agg = cost.aggregate_costs([rec(5, "USD"), rec(5, "USD"), rec(9, "EUR")])
    assert agg["currency_code"] == "USD" and agg["total_spend"] == 10.0
    assert agg["priced_count"] == 2 and agg["other_currencies"] == {"EUR": 1}


def test_partial_coverage_says_how_many_were_priced():
    agg = cost.aggregate_costs([{"cost": {"cost_per_build": 1.0}}, {"title": "old"},
                                {"title": "older"}])
    assert agg["note"].startswith("Priced 1 of 3 prints")


def test_completion_freezes_per_slot_lines_at_each_spool_rate(monkeypatch):
    spools = {7: {"id": 7, "price": 20.0, "initial_weight": 1000,
                  "filament": {"material": "PLA", "name": "White", "color_hex": "FFFFFF"}},
              9: {"id": 9, "price": 40.0, "initial_weight": 1000,
                  "filament": {"material": "PLA", "name": "Silk", "color_hex": "CD7F32"}}}
    monkeypatch.setattr(print_records, "_spool_record", lambda sid: spools.get(sid))
    monkeypatch.setattr(print_records, "_cost_settings", lambda: AD5X_ROSTER)
    context = {"tool_to_physical_slot": {"0": 1, "1": 2},
               "slicer_settings": {"filament_used_g_list": [10.0, 30.0],
                                   "mapped_materials": {"0": {"spoolman_id": 7},
                                                        "1": {"spoolman_id": 9}}}}
    frozen = print_records._frozen_cost(context, {}, {"print_duration": 3600}, {},
                                        status="completed")
    amounts = {line["spool_id"]: line["amount"] for line in frozen["material_lines"]}
    assert amounts == {7: 0.2, 9: 1.2}
    assert {line["color"] for line in frozen["material_lines"]} == {"#FFFFFF", "#CD7F32"}
    assert frozen["provenance"]["printer_profile"].startswith("Flashforge AD5X")


def test_completion_freeze_failure_is_logged_not_silent(monkeypatch, caplog):
    def boom():
        raise RuntimeError("settings unreadable")
    monkeypatch.setattr(print_records, "_cost_settings", boom)
    with caplog.at_level("WARNING", logger=print_records.__name__):
        assert print_records._frozen_cost({}, {}, {"print_duration": 60}, {},
                                          status="completed") is None
    assert "cost freeze skipped" in caplog.text
