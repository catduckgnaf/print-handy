"""Readable conversion report, plate statistics, external intake and
U1 feasibility (#1525, tranches F-I).

One test module per tranche would spread these small surfaces thin;
each class below pins one tranche's contract.
"""
import json
import zipfile
from pathlib import Path

import pytest

from print_handy.core import (conversion_report as cr, external_intake as ei,
                              plate_statistics as ps, u1_feasibility as u1)
from print_handy.webui import settings_report

CONVERSION = {
    "source_format": "cura_project", "source_slicer": "UltiMaker Cura",
    "source_slicer_version": None, "destination_slicer_version": "2.5.0",
    "destination_profile_name": "AD5X 0.4 nozzle", "intent_status": "unknown",
    "governing_rules": ["cura-key-translation"], "conversion_notes": ["note one"],
    "unresolved_conflicts": [{"key": "wall_loops", "source_value": "9",
                              "target_value": "3"}],
    "settings": {
        "layer_height": {"project_value": "0.16", "selected_value": "0.16",
                         "selected_reason": "designer", "origin": "designer"},
        "wall_loops": {"project_value": "3", "selected_value": "2",
                       "selected_reason": "destination_profile",
                       "origin": "designer"},
        "brim_width": {"project_value": "6", "selected_value": None,
                       "selected_reason": None, "origin": "unknown"},
        "mystery": {"project_value": "1", "selected_value": "2",
                    "selected_reason": "unattributed", "origin": "unknown"}}}


class TestConversionReport:
    def test_classes_come_from_the_record(self):
        report = cr.build_report(CONVERSION, job_id="j1")
        classes = {row["setting"]: row["class"] for row in report["settings"]}
        assert classes == {"layer_height": "preserved", "wall_loops": "replaced",
                           "brim_width": "dropped", "mystery": "unknown"}

    def test_changed_foreign_project_translation_is_translated(self):
        assert cr.classify({"project_value": 0.2, "selected_value": 0.24,
                            "selected_reason": "translation",
                            "origin": "foreign_project"}) == "translated"

    def test_text_and_html_render_the_same_rows(self):
        report = cr.build_report(CONVERSION, job_id="j1")
        text = cr.render_text(report)
        html = cr.render_html(report)
        assert "wall_loops" in text and "wall_loops" in html
        assert "Unresolved: wall_loops" in text
        assert "<script>" not in html

    def test_webui_report_uses_the_same_provenance(self, tmp_path):
        archive = tmp_path / "archive"
        job = archive / "j1"
        job.mkdir(parents=True)
        (job / "JOB.json").write_text(json.dumps(
            {"provenance": {"conversion_provenance": CONVERSION}}))
        rendered = settings_report.conversion_report(archive, "j1")
        assert rendered is not None
        html_body, text_body = rendered
        assert "wall_loops" in html_body and "wall_loops" in text_body
        assert settings_report.conversion_report(archive, "nope") is None


class TestPlateStatistics:
    GCODE = ("; estimated printing time (normal mode) = 1h 2m\n"
             "; total filament used [g] = 12.5\n"
             "; filament used [g] = 10.0, 2.5\n"
             "; total layers: 42\n")

    def test_gcode_comment_path_with_unknowns_kept(self):
        artifact = ps.build_plate_statistics(
            gcode_text=self.GCODE,
            filaments=[{"source_extruder": 1, "material": "PLA", "color": "#FF0000"},
                       {"source_extruder": 2, "material": "PETG", "color": "#0000FF"},
                       {"source_extruder": 3, "material": "PLA", "color": "#00FF00"}],
            slot_map={1: 1, 2: 2})
        assert artifact["statistics_source"] == "gcode-comments"
        assert artifact["layer_count"] == 42
        rows = {r["logical_filament"]: r for r in artifact["filaments"]}
        assert rows[1]["estimated_grams"] == 10.0
        assert rows[3]["estimated_grams"] is None
        assert rows[3]["grams_status"] == "unknown"
        assert rows[3]["slot_status"] == "unknown"
        assert artifact["total_estimated_grams"] == 12.5

    def test_statistics_json_wins_when_it_names_per_filament_weights(self):
        artifact = ps.build_plate_statistics(
            gcode_text=self.GCODE,
            statistics_text=json.dumps(ORCA_STATISTICS),
            filaments=[{"source_extruder": 1}, {"source_extruder": 2}])
        assert artifact["statistics_source"] == "orca-statistics-json"
        assert artifact["filaments"][0]["estimated_grams"] == 7.0
        assert artifact["filaments"][1]["estimated_grams"] == 1.5

    def test_statistics_json_of_another_shape_is_not_trusted(self):
        # A guessed key, a future major version or another weight unit
        # proves nothing: the G-code comments stand instead.
        for record in ({"filament_used_g": [7.0, 1.5]},
                       dict(ORCA_STATISTICS, version=2),
                       dict(ORCA_STATISTICS, units={"weight": "kg"})):
            assert ps.parse_statistics_json(json.dumps(record)) is None, record

    def test_total_only_never_fabricates_per_filament(self):
        artifact = ps.build_plate_statistics(
            gcode_text="; total filament used [g] = 9.0\n",
            filaments=[{"source_extruder": 1}, {"source_extruder": 2}])
        assert all(r["estimated_grams"] is None for r in artifact["filaments"])
        assert artifact["total_estimated_grams"] == 9.0

    def test_spool_suggestions_are_exact_and_deterministic(self):
        spools = [{"id": 5, "material": "PLA", "color": "#FF0000"},
                  {"id": 3, "material": "PLA", "color": "#FF0000"},
                  {"id": 4, "material": "PLA", "color": "#FE0000"},
                  {"id": 2, "material": "PETG", "color": "#FF0000"}]
        got = ps.suggest_spools({"material": "PLA", "color": "#FF0000"}, spools)
        assert [row["spool_id"] for row in got] == [3, 5]  # never the near color


# The shape OrcaSlicer's --export-statistics writes (plate_N.statistics.json,
# gcode_statistics_json in its GCodeStatistics.cpp), trimmed to the keys read.
# Filaments listed out of id order on purpose: weights follow the id.
ORCA_STATISTICS = {
    "version": 1,
    "generator": "OrcaSlicer 2.5.0",
    "units": {"time": "s", "length": "mm", "volume": "mm3", "weight": "g"},
    "summary": {"time": 812.0, "weight": 8.5, "filaments": [
        {"id": 2, "length": 500.1, "volume": 1203.0, "weight": 1.5},
        {"id": 1, "length": 2333.4, "volume": 5600.0, "weight": 7.0}]},
    "layers": [], "objects": [],
}


class TestExternalIntake:
    def test_scan_is_gated_stable_and_deduped(self, tmp_path):
        watched = tmp_path / "watch"
        watched.mkdir()
        (watched / "job.gcode").write_text("G1 X1\n")
        cards, state = ei.scan(watched)  # disabled by default
        assert cards == []
        cards, state = ei.scan(watched, enabled=True, state=state)
        assert cards == []  # first sighting is not stable yet
        cards, state = ei.scan(watched, enabled=True, state=state)
        assert len(cards) == 1 and cards[0]["status"] == "planned"
        cards, state = ei.scan(watched, enabled=True, state=state)
        assert cards == []  # deduped by hash + path

    def test_confirm_charges_once_and_never_twice(self, tmp_path):
        card = {"sha256": "abc", "status": "planned", "path": "x"}
        ledger = set()
        calls = []
        result = ei.confirm_mark_printed(
            card, printer_id="ad5x", print_id="p1", ledger=ledger,
            charge=lambda **kw: calls.append(kw) or "charged")
        assert result["accounting"] == "charged" and len(calls) == 1
        with pytest.raises(ei.IntakeRefused, match="already recorded"):
            ei.confirm_mark_printed(
                card, printer_id="ad5x", print_id="p1", ledger=ledger,
                charge=lambda **kw: calls.append(kw))
        assert len(calls) == 1

    def test_watcher_never_charges_on_appearance(self, tmp_path):
        watched = tmp_path / "watch"
        watched.mkdir()
        (watched / "a.3mf").write_bytes(b"junk")
        _cards, state = ei.scan(watched, enabled=True)
        cards, _state = ei.scan(watched, enabled=True, state=state)
        assert all(c["status"] == "planned" for c in cards)


class TestU1Feasibility:
    def test_over_tool_plate_is_reported_not_collapsed(self, tmp_path):
        from tests.core.test_plate_usage import write_source
        source = write_source(
            tmp_path / "five.3mf",
            [("A", 1, None), ("B", 2, None), ("C", 3, None),
             ("D", 4, None), ("E", 5, None)],
            [[0, 1, 2, 3, 4]])
        report = u1.feasibility(source)
        assert report["hardware_status"].startswith("unverified")
        assert report["plates"][0]["filament_count"] == 5
        assert any("5 logical filaments" in p for p in report["problems"])
        assert not report["feasible"]
        assert "not feasible" in u1.render_text(report)

    def test_fitting_plate_is_feasible_on_paper(self, tmp_path):
        from tests.core.test_plate_usage import write_source
        source = write_source(tmp_path / "two.3mf",
                              [("A", 1, None), ("B", 2, None)], [[0, 1]])
        report = u1.feasibility(source)
        assert report["feasible"], report["problems"]
