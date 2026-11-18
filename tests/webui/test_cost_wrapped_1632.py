"""#1632 review: recorded costs reach the Data tab, Wrapped page, video and script."""
from print_handy.core import cost, print_stats
from print_handy.webui import data as data_module
from print_handy.webui import wrapped_video


def _record(title, grams, status="completed", color="White"):
    job = cost.JobCostInput(lines=[cost.MaterialLine(material="PLA", grams=grams, color=color)],
                            print_hours=1.0,
                            outcome="failed" if status == "failed" else "completed")
    return {"title": title, "status": status, "printer": "Garage AD5X",
            "started_at": "2026-05-01T10:00:00Z",
            "cost": cost.freeze(cost.compute(job, cost.PrinterCostProfile(), cost.WorkspaceCost()))}


RECORDS = [_record("Duck", 12), _record("Vase", 200, color="Bronze"),
           _record("Spaghetti", 30, status="failed"), {"title": "Old print", "status": "completed",
                                                       "started_at": "2026-04-01T10:00:00Z"}]


def test_the_data_tab_renders_recorded_costs():
    html = data_module._cost_section(cost.aggregate_costs(RECORDS))
    assert "<h2>Print costs</h2>" in html and "Out of pocket" in html
    assert "Most expensive print" in html and "Vase" in html
    assert "By color" in html and "Bronze" in html
    assert "Priced 3 of 4 prints" in html


def test_the_data_tab_says_plainly_when_nothing_is_priced():
    html = data_module._cost_section(cost.aggregate_costs([{"title": "x"}]))
    assert "No priced jobs" in html and 'href="/cost"' in html


def test_the_wrapped_video_gets_a_cost_slide():
    stats = {"costs": cost.aggregate_costs(RECORDS)}
    slides = wrapped_video.build_slides({"year": 2026, "stats": stats, "headline": {},
                                         "highlights": {}, "script": []})
    cost_slides = [s for s in slides if s.get("eyebrow") == "WHAT IT COST"]
    assert len(cost_slides) == 1 and "spent" in cost_slides[0]["hero"]


def test_the_wrapped_script_leads_with_out_of_pocket_and_coverage():
    wrapped = print_stats.build_wrapped(RECORDS, 2026)
    lines = wrapped["script"]
    money = [line for line in lines if "spent" in line]
    assert money and "(3 of 4 prints had a recorded cost)" in money[0]
    assert any(line.startswith("Your failures cost you") for line in lines)
    assert wrapped["stats"]["costs"]["failed_count"] == 1


def test_the_cost_page_lists_recorded_costs_newest_first():
    from print_handy.webui import cost_page
    rows = cost_page.history_cost_rows([
        {"title": "Old", "completed_at": "2026-01-01", "cost": {"cost_per_build": 2, "out_of_pocket": 1}},
        {"title": "New", "completed_at": "2026-06-01", "status": "failed",
         "cost": {"cost_per_build": 6, "out_of_pocket": 0.5}},
        {"title": "Unpriced"}])
    assert len(rows) == 2 and "New" in rows[0] and "failed" in rows[0]
    assert "USD 0.50" in rows[0] and "USD 6.00" in rows[0]
    html = cost_page.render_cost_page({}, history="<h2>Recorded print costs</h2>")
    assert "<h2>Recorded print costs</h2>" in html
