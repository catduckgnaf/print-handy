"""Display-only OpenCreator backup-lane summaries and sanity warnings."""

from print_handy.printers.flashforge_opencreator.backup import summarize_backup_lanes


def status(*, backup="lane1", backup_material="PLA", source_material="PLA"):
    return {
        "AFC": {"lanes": ["extruder0", "extruder1", "lane1", "extruder3"]},
        "AFC_lane extruder0": {"map": "T0", "extruder": "extruder0",
                                "runout_lane": backup, "material": source_material},
        "AFC_lane extruder1": {"map": "T1", "extruder": "extruder1"},
        "AFC_lane lane1": {"map": "T2", "extruder": "extruder2",
                            "load": bool(backup_material), "material": backup_material},
        "AFC_lane extruder3": {"map": "T3", "extruder": "extruder3"},
    }


def test_summary_uses_afc_tool_map_and_actual_backup_lane_name():
    result = summarize_backup_lanes(status(), [0], {0: "PLA"})
    assert result["rows"] == [{"tool": 0, "lane": "extruder0", "runout_lane": "lane1",
                               "material": "PLA", "backup_material": "PLA"}]
    assert result["warnings"] == []
    assert result["display_only"] and not result["hardware_verified"]


def test_missing_target_warns():
    result = summarize_backup_lanes(status(backup=""), [0], {0: "PLA"})
    assert any("no runout_lane" in warning for warning in result["warnings"])


def test_same_lane_warns():
    result = summarize_backup_lanes(status(backup="extruder0"), [0], {0: "PLA"})
    assert any("same lane" in warning for warning in result["warnings"])


def test_target_not_listed_by_afc_warns():
    result = summarize_backup_lanes(status(backup="not-a-lane"), [0], {0: "PLA"})
    assert any("not in AFC's lane list" in warning for warning in result["warnings"])


def test_empty_backup_lane_warns():
    result = summarize_backup_lanes(status(backup_material=""), [0], {0: "PLA"})
    assert any("no filament" in warning for warning in result["warnings"])


def test_unknown_backup_material_warns():
    result = summarize_backup_lanes(status(backup_material="MysteryBlend"), [0], {0: "PLA"})
    assert any("unknown material" in warning for warning in result["warnings"])


def test_unknown_file_material_warns():
    result = summarize_backup_lanes(status(), [0], {0: "MysteryBlend"})
    assert any("file material" in warning and "unknown" in warning
               for warning in result["warnings"])


def test_material_mismatch_warns_without_stopping_or_mutating_status():
    raw = status(backup_material="PETG")
    before = repr(raw)
    result = summarize_backup_lanes(raw, [0], {0: "PLA"})
    assert any("does not match" in warning for warning in result["warnings"])
    assert result["rows"][0]["runout_lane"] == "lane1"
    assert repr(raw) == before


def test_only_used_tools_are_summarized():
    result = summarize_backup_lanes(status(), [0], {0: "PLA"})
    assert [row["tool"] for row in result["rows"]] == [0]


def test_unresolved_tool_map_is_advisory_and_does_not_guess_a_lane():
    raw = {"AFC": {"lanes": ["lane1"]}, "AFC_lane lane1": {"runout_lane": "lane1"}}
    result = summarize_backup_lanes(raw, [0], {0: "PLA"})
    assert result["rows"][0]["lane"] is None
    assert result["warnings"]


def test_no_printer_commands_or_status_writes_are_part_of_result():
    result = summarize_backup_lanes(status(), [0], {0: "PLA"})
    assert set(result) == {"rows", "warnings", "display_only", "hardware_verified"}
