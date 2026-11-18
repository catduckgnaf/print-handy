"""Chat cost handler tests (#1625): amounts, last job, clarification."""
from print_handy.bot.handlers import cost as cost_handler


def test_parse_ask_amounts_and_material():
    grams, hours, material = cost_handler._parse_ask(
        "/print-cost 50 g, 3 h, in PETG")
    assert grams == 50.0 and hours == 3.0 and material == "PETG"
    grams, hours, material = cost_handler._parse_ask("/print-cost")
    assert grams is None and hours is None and material is None


def test_answer_with_amounts_is_compact_breakdown():
    data = {"cost": {}, "paths": {}}
    text = cost_handler.cost_answer("how much will this cost, 50 g, 3 h", data=data)
    # Hobby mode (the default) leads with out of pocket and names the full cost.
    assert text.startswith("Out of pocket") and "the full cost is" in text
    assert "Priced from:" in text
    assert "never blocks a print" in text


def test_answer_without_job_clarifies(tmp_path):
    data = {"cost": {}, "paths": {"records_vault": str(tmp_path)}}
    text = cost_handler.cost_answer("how much did that print cost", data=data)
    assert "Cannot estimate yet" in text
    assert "50 g, 3 h" in text


def test_answer_from_frozen_record(tmp_path):
    from print_handy.core import cost, print_stats
    import json
    job = cost.JobCostInput(lines=[cost.MaterialLine(grams=50.0)], print_hours=2.0)
    result = cost.compute(job, cost.PrinterCostProfile(purchase_price=500.0),
                          cost.WorkspaceCost())
    prints = print_stats.prints_dir(tmp_path)
    prints.mkdir(parents=True)
    (prints / "print-0001.json").write_text(json.dumps(
        {"title": "Bracket", "status": "completed", "cost": cost.freeze(result)}))
    data = {"cost": {}, "paths": {"records_vault": str(tmp_path)}}
    text = cost_handler.cost_answer("how much did that print cost", data=data)
    assert "Bracket" in text
    assert f"{result.cost_per_build:.2f}" in text
