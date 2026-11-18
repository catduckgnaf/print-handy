"""Pasta Wrapped is a surprise: it keeps counting all year and opens on December 1."""
import json
from datetime import datetime, timezone

from print_handy.webui import data as data_module
from print_handy.webui import shell

ON = {"timelapse": {"wrapped_enabled": True, "data_enabled": True, "wrapped_video": True},
      "discord": {"timezone": "UTC"}}


def at(year, month, day):
    return datetime(year, month, day, 12, tzinfo=timezone.utc)


def _vault(tmp_path, year):
    vault = tmp_path / "vault"
    (vault / "Prints").mkdir(parents=True)
    record = {"print_number": 1, "title": "Benchy", "status": "failed",
              "started_at": f"{year}-03-04T10:00:00Z", "print_duration_seconds": 3600,
              "filament_used_g": 12.0}
    (vault / "Prints" / "print-1.json").write_text(json.dumps(record))
    return vault


def test_the_year_opens_on_december_first_and_past_years_stay_open():
    assert not data_module.wrapped_revealed(ON, 2026, now=at(2026, 11, 30))
    assert data_module.wrapped_revealed(ON, 2026, now=at(2026, 12, 1))
    assert data_module.wrapped_revealed(ON, 2025, now=at(2026, 6, 1))
    assert not data_module.wrapped_revealed(ON, 2027, now=at(2026, 12, 25))


def test_the_wrapped_tab_shows_in_december_and_january_only():
    def targets(now):
        return [target for target, _label in data_module.nav_for(ON, now=now)]
    assert "/wrapped" in targets(at(2026, 12, 1))
    assert "/wrapped" in targets(at(2027, 1, 31))
    assert "/wrapped" not in targets(at(2026, 6, 15))
    assert "/wrapped" not in targets(at(2026, 11, 30))
    assert "/data" in targets(at(2026, 6, 15))


def test_the_current_year_is_sealed_until_it_opens(tmp_path):
    vault = _vault(tmp_path, 2026)
    code, payload = data_module.wrapped_view(ON, records_vault=vault, now=at(2026, 6, 15))
    assert code == 200 and payload["sealed"] is True and payload["year"] == 2026
    assert "headline" not in payload and "highlights" not in payload
    html = data_module.render_wrapped_sealed_page(ON, payload)
    assert "December 1" in html and "Benchy" not in html
    code, payload = data_module.wrapped_view(ON, records_vault=vault, now=at(2026, 12, 2))
    assert code == 200 and not payload.get("sealed")
    assert payload["headline"]["total_prints"] == 1


def test_a_sealed_year_cannot_be_peeked_at_through_the_video_or_share(tmp_path):
    vault = _vault(tmp_path, 2026)
    june = at(2026, 6, 15)
    for code, payload in (
            data_module.make_video(ON, 2026, records_vault=vault, now=june),
            data_module.export_share(ON, 2026, records_vault=vault, now=june)):
        assert code == 403 and "December 1" in payload["reason"]


def test_last_years_wrapped_is_still_there_after_the_season(tmp_path):
    vault = _vault(tmp_path, 2025)
    code, payload = data_module.wrapped_view(ON, records_vault=vault, now=at(2026, 6, 15))
    assert code == 200 and not payload.get("sealed") and payload["year"] == 2025
    assert 2026 not in payload["years_available"]


def test_the_header_groups_pages_under_their_parent_tab():
    nav = (("/", "Status"), ("/designs", "Designs"), ("/sliced", "Sliced files"),
           ("/workflow", "Workflow"), ("/printers", "Printers"), ("/config", "Settings"))
    html = shell.page("Sliced files", "", path="/sliced", nav=nav)
    top = html.split('aria-label="Web UI pages"')[1].split("</nav>")[0]
    assert 'href="/sliced"' not in top and 'href="/workflow"' not in top
    assert '<a href="/designs" class="active">' in top
    sub = html.split('aria-label="Designs pages"')[1].split("</nav>")[0]
    for target in ("/designs", "/sliced", "/workflow"):
        assert f'href="{target}"' in sub
    assert 'href="/sliced" class="active" aria-current="page"' in sub
    plain = shell.page("Status", "", path="/", nav=nav)
    assert "Designs pages" not in plain


def test_a_child_whose_parent_is_off_still_has_a_tab():
    nav = (("/", "Status"), ("/wrapped", "Wrapped"), ("/config", "Settings"))
    top = shell.page("x", "", path="/", nav=nav).split('aria-label="Web UI pages"')[1]
    assert 'href="/wrapped"' in top.split("</nav>")[0]
