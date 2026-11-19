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


# --- #1632 review: "this" is the file in hand, "this month" is a month total ---

def _vault_with(tmp_path, records):
    import json
    prints = tmp_path / "vault" / "Prints"
    prints.mkdir(parents=True)
    for n, record in enumerate(records, 1):
        (prints / f"print-{n:04d}.json").write_text(json.dumps(record))
    return {"cost": {}, "paths": {"records_vault": str(tmp_path / "vault")}}


def _frozen(amount, outcome="completed"):
    from print_handy.core import cost
    job = cost.JobCostInput(lines=[cost.MaterialLine(material="PLA", grams=amount)],
                            print_hours=1.0, outcome=outcome)
    return cost.freeze(cost.compute(job, cost.PrinterCostProfile(), cost.WorkspaceCost()))


def test_will_this_cost_prices_the_newest_unprinted_sliced_job(tmp_path):
    import json
    data = _vault_with(tmp_path, [{"title": "Old print", "status": "completed",
                                   "job_key": "old_plate_1_aa.gcode", "cost": _frozen(50)}])
    archive = tmp_path / "archive" / "duck"
    archive.mkdir(parents=True)
    gcode = archive / "plate_1.gcode"
    gcode.write_text("; total filament used [g] = 12.5\n"
                     "; estimated printing time (normal mode) = 14m 0s\n")
    (archive / "JOB.json").write_text(json.dumps({"title": "Duck", "gcode": str(gcode)}))
    text = cost_handler.cost_answer("how much will this cost", data=data,
                                    archive=tmp_path / "archive", spool_lookup=lambda _sid: None)
    assert "Duck (estimate before printing)" in text
    assert "Old print" not in text


def test_will_this_cost_with_nothing_sliced_says_so(tmp_path):
    data = _vault_with(tmp_path, [{"title": "Old print", "status": "completed",
                                   "cost": _frozen(50)}])
    (tmp_path / "archive").mkdir()
    text = cost_handler.cost_answer("how much will this cost", data=data,
                                    archive=tmp_path / "archive")
    assert text.startswith("Cannot estimate this one yet")
    assert "Old print" not in text


def test_this_month_answers_the_month_total(tmp_path):
    from datetime import datetime, timezone
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    data = _vault_with(tmp_path, [
        {"title": "A", "status": "completed", "started_at": f"{month}-02T10:00:00Z", "cost": _frozen(50)},
        {"title": "B", "status": "failed", "started_at": f"{month}-03T10:00:00Z",
         "cost": _frozen(20, "failed")},
        {"title": "Last year", "status": "completed", "started_at": "2001-01-01T00:00:00Z",
         "cost": _frozen(500)}])
    text = cost_handler.cost_answer("/print-cost this month", data=data)
    assert "out of pocket" in text and "2 priced print(s)" in text
    assert "Failed or cancelled prints cost" in text


def test_a_printed_job_is_matched_by_its_gcode_hash_not_its_plate_name(tmp_path):
    import json
    sha = "c1edfa05" * 8
    data = _vault_with(tmp_path, [{"title": "Duck", "status": "completed",
                                   "job_key": f"Duck_plate_1_{sha}.gcode.3mf", "cost": _frozen(5)}])
    archive = tmp_path / "archive"
    for name, digest in (("duck", sha), ("frog", "ab" * 32)):
        folder = archive / name
        folder.mkdir(parents=True)
        gcode = folder / "plate_1.gcode"
        gcode.write_text("; total filament used [g] = 3\n; estimated printing time (normal mode) = 5m 0s\n")
        (folder / "JOB.json").write_text(json.dumps(
            {"title": name.title(), "gcode": str(gcode), "gcode_full_verified_sha256": digest}))
    text = cost_handler.cost_answer("how much will this cost", data=data, archive=archive,
                                    spool_lookup=lambda _sid: None)
    assert "Frog (estimate before printing)" in text
