#!/usr/bin/env python3
"""Tests for four-slot spool lifecycle, read-only reconciliation, and accounting receipts."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy.core import spool_lifecycle as sl


class SpoolLifecycleReplacementTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "spoolman_url": "http://spoolman.test:7912",
            "filament_slots": {
                "1": {"slot": 1, "material": "PLA", "color": "white", "hex": "FFFFFF", "spoolman_id": 10, "physical_color_verified": True, "physical_loading_verified": True},
                "2": {"slot": 2, "material": "PLA", "color": "pink", "hex": "FF69B4", "spoolman_id": 11, "physical_color_verified": True, "physical_loading_verified": True},
                "3": {"slot": 3, "material": "PLA", "color": "black", "hex": "000000", "spoolman_id": 8, "physical_color_verified": True, "physical_loading_verified": True},
                "4": {"slot": 4, "material": "PLA", "color": "green", "hex": "067749", "spoolman_id": 12, "physical_color_verified": True, "physical_loading_verified": True},
            },
            "spool": {"spoolman_id": 8, "name": "Black PLA", "material": "PLA", "diameter_mm": 1.75, "density": 1.24},
        }
        self.spoolman_db = {
            8: {"id": 8, "name": "Black PLA", "archived": False, "filament": {"material": "PLA", "name": "Black PLA", "color_hex": "000000"}},
            10: {"id": 10, "name": "White PLA", "archived": False, "filament": {"material": "PLA", "name": "White PLA", "color_hex": "FFFFFF"}},
            11: {"id": 11, "name": "Pink PLA", "archived": False, "filament": {"material": "PLA", "name": "Pink PLA", "color_hex": "FF69B4"}},
            12: {"id": 12, "name": "Green PLA", "archived": False, "filament": {"material": "PLA", "name": "Green PLA", "color_hex": "067749"}},
            15: {"id": 15, "name": "Orange PETG", "archived": False, "filament": {"material": "PETG", "name": "Orange PETG", "color_hex": "FFA500"}},
            20: {"id": 20, "name": "Old Grey PLA", "archived": True, "filament": {"material": "PLA", "name": "Old Grey PLA", "color_hex": "808080"}},
        }

    def test_replaces_slot_3_with_unknown_quantity(self):
        """Slot replacement identifies slot, accepts unknown quantity, maps non-archived spool."""
        st = copy.deepcopy(self.state)
        res = sl.replace_slot_spool(
            st,
            slot=3,
            material="PLA",
            color="black",
            spoolman_id=8,
            quantity_kind="unknown",
            spoolman_client=self.spoolman_db,
        )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["slot"], 3)
        self.assertIsNone(res["remaining_grams"])
        self.assertEqual(res["quantity_status"], "unknown")
        self.assertFalse(res["historical_mutation"])
        self.assertTrue(res["four_slot_boundary_enforced"])

        slot3 = st["filament_slots"]["3"]
        self.assertEqual(slot3["slot"], 3)
        self.assertEqual(slot3["material"], "PLA")
        self.assertEqual(slot3["color"], "black")
        self.assertEqual(slot3["hex"], "000000")
        self.assertEqual(slot3["spoolman_id"], 8)
        self.assertIsNone(slot3["remaining_grams"])
        self.assertEqual(slot3["quantity_status"], "unknown")
        self.assertEqual(slot3["weight_confidence"], "unknown")

        # Slots 1, 2, and 4 are untouched
        self.assertEqual(st["filament_slots"]["1"]["spoolman_id"], 10)
        self.assertEqual(st["filament_slots"]["2"]["spoolman_id"], 11)
        self.assertEqual(st["filament_slots"]["4"]["spoolman_id"], 12)

    def test_replaces_slot_with_grams_and_full_options(self):
        """Supports user estimate in grams or full assumed with nominal grams."""
        st = copy.deepcopy(self.state)
        res_grams = sl.replace_slot_spool(
            st,
            slot=1,
            material="PLA",
            color="white",
            spoolman_id=10,
            quantity_kind="grams",
            remaining_grams=450.5,
            spoolman_client=self.spoolman_db,
        )
        self.assertEqual(res_grams["remaining_grams"], 450.5)
        self.assertEqual(res_grams["quantity_status"], "user_estimate")
        self.assertEqual(st["filament_slots"]["1"]["weight_confidence"], "user_estimate")

        res_full_nominal = sl.replace_slot_spool(
            st,
            slot=2,
            material="PLA",
            color="pink",
            spoolman_id=11,
            quantity_kind="full",
            remaining_grams=1000.0,
            spoolman_client=self.spoolman_db,
        )
        self.assertEqual(res_full_nominal["remaining_grams"], 1000.0)
        self.assertEqual(res_full_nominal["quantity_status"], "full_assumed")

    def test_replaces_slot_with_full_omitted_grams_retains_unknown_weight(self):
        """Full spool with omitted grams retains weight as unknown and does not invent 1000g."""
        st = copy.deepcopy(self.state)
        res_full_omitted = sl.replace_slot_spool(
            st,
            slot=2,
            material="PLA",
            color="pink",
            spoolman_id=11,
            quantity_kind="full",
            spoolman_client=self.spoolman_db,
        )
        self.assertIsNone(res_full_omitted["remaining_grams"])
        self.assertEqual(res_full_omitted["quantity_status"], "unknown")
        self.assertEqual(st["filament_slots"]["2"]["weight_confidence"], "unknown")

    def test_rejects_out_of_bounds_slots(self):
        """Four-slot boundary strictly rejects slots not in 1..4."""
        st = copy.deepcopy(self.state)
        for bad_slot in [0, 5, -1, 10, "abc"]:
            with self.assertRaises(ValueError):
                sl.replace_slot_spool(
                    st,
                    slot=bad_slot,
                    material="PLA",
                    color="white",
                    spoolman_id=10,
                    spoolman_client=self.spoolman_db,
                )

    def test_refuses_archived_spoolman_spool(self):
        """Cannot map an archived Spoolman spool to an active IFS slot."""
        st = copy.deepcopy(self.state)
        with self.assertRaisesRegex(RuntimeError, "is archived"):
            sl.replace_slot_spool(
                st,
                slot=2,
                material="PLA",
                color="grey",
                spoolman_id=20,
                spoolman_client=self.spoolman_db,
            )

    def test_refuses_material_mismatch_with_spoolman(self):
        """Cannot map a spool whose Spoolman material differs from requested material."""
        st = copy.deepcopy(self.state)
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            sl.replace_slot_spool(
                st,
                slot=1,
                material="PLA",
                color="orange",
                spoolman_id=15,  # PETG
                spoolman_client=self.spoolman_db,
            )

    def test_unknown_quantity_rejects_numeric_grams(self):
        """If quantity_kind is unknown, passing numeric grams raises error."""
        st = copy.deepcopy(self.state)
        with self.assertRaisesRegex(ValueError, "must not specify numeric grams"):
            sl.replace_slot_spool(
                st,
                slot=1,
                material="PLA",
                color="white",
                spoolman_id=10,
                quantity_kind="unknown",
                remaining_grams=250.0,
                spoolman_client=self.spoolman_db,
            )


class SpoolLifecycleReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "spoolman_url": "http://spoolman.test:7912",
            "filament_slots": {
                "1": {"slot": 1, "material": "PLA", "color": "white", "hex": "FFFFFF", "spoolman_id": 10, "physical_loading_verified": True},
                "2": {"slot": 2, "material": "PLA", "color": "pink", "hex": "FF69B4", "spoolman_id": 11, "physical_loading_verified": True},
                "3": {"slot": 3, "material": "PLA", "color": "black", "hex": "000000", "spoolman_id": 8, "physical_loading_verified": True},
                "4": {"slot": 4, "material": "PLA", "color": "green", "hex": "067749", "spoolman_id": 12, "physical_loading_verified": True},
            },
            "spool": {"spoolman_id": 8, "name": "Black PLA", "material": "PLA", "diameter_mm": 1.75, "density": 1.24},
        }
        self.spoolman_db = {
            8: {"id": 8, "name": "Black PLA", "location": "IFS Slot 3", "archived": False, "filament": {"material": "PLA"}},
            10: {"id": 10, "name": "White PLA", "location": "IFS Slot 1", "archived": False, "filament": {"material": "PLA"}},
            11: {"id": 11, "name": "Pink PLA", "location": "IFS Slot 2", "archived": False, "filament": {"material": "PLA"}},
            12: {"id": 12, "name": "Green PLA", "location": "IFS Slot 4", "archived": False, "filament": {"material": "PLA"}},
        }

    def test_reconcile_clean_state_reports_no_drift(self):
        """When hardware state, native detail, Moonraker, and Spoolman match, drift count is 0."""
        detail = {
            "hasMatlStation": True,
            "matlStationInfo": {
                "slotInfos": [
                    {"slotId": 1, "hasFilament": True},
                    {"slotId": 2, "hasFilament": True},
                    {"slotId": 3, "hasFilament": True},
                    {"slotId": 4, "hasFilament": True},
                ]
            }
        }
        report = sl.reconcile_four_slot_state(
            self.state,
            detail_data=detail,
            moonraker_spool_id=8,
            spoolman_spools=self.spoolman_db,
        )
        self.assertFalse(report["drift_detected"])
        self.assertEqual(report["drift_count"], 0)
        self.assertFalse(report["mutation_performed"])
        self.assertTrue(report["read_only"])

    def test_reconcile_detects_occupancy_drift_without_mutation(self):
        """Native detail reports slot 4 empty when config expects loaded."""
        detail = {
            "hasMatlStation": True,
            "matlStationInfo": {
                "slotInfos": [
                    {"slotId": 1, "hasFilament": True},
                    {"slotId": 2, "hasFilament": True},
                    {"slotId": 3, "hasFilament": True},
                    {"slotId": 4, "hasFilament": False},  # Empty
                ]
            }
        }
        report = sl.reconcile_four_slot_state(
            self.state,
            detail_data=detail,
            moonraker_spool_id=8,
            spoolman_spools=self.spoolman_db,
        )
        self.assertTrue(report["drift_detected"])
        self.assertFalse(report["mutation_performed"])
        categories = [d["category"] for d in report["drifts"]]
        self.assertIn("occupancy_drift", categories)
        drift_slot4 = [d for d in report["drifts"] if d["slot"] == 4][0]
        self.assertEqual(drift_slot4["sources"]["native_detail"], False)

    def test_reconcile_detects_material_and_color_drift(self):
        """zmod_color has differing material or color."""
        zmod = {
            "slots": [
                {"ID": 1, "hasFilament": True, "Material": "PETG", "Color": "white"},  # Material drift
                {"ID": 2, "hasFilament": True, "Material": "PLA", "Color": "blue"},   # Color drift
                {"ID": 3, "hasFilament": True, "Material": "PLA", "Color": "black"},
                {"ID": 4, "hasFilament": True, "Material": "PLA", "Color": "green"},
            ]
        }
        report = sl.reconcile_four_slot_state(
            self.state,
            zmod_color_data=zmod,
            moonraker_spool_id=8,
            spoolman_spools=self.spoolman_db,
        )
        self.assertTrue(report["drift_detected"])
        categories = [d["category"] for d in report["drifts"]]
        self.assertIn("material_drift", categories)
        self.assertIn("color_drift", categories)

    def test_reconcile_detects_active_spool_pointer_drift(self):
        """Moonraker active spool is 12 (green) while hardware state is 8 (black)."""
        report = sl.reconcile_four_slot_state(
            self.state,
            moonraker_spool_id=12,
            spoolman_spools=self.spoolman_db,
        )
        self.assertTrue(report["drift_detected"])
        categories = [d["category"] for d in report["drifts"]]
        self.assertIn("active_spool_pointer_drift", categories)

    def test_reconcile_detects_archived_or_missing_spoolman_spool(self):
        """Slot maps to an archived or non-existent Spoolman spool."""
        db = copy.deepcopy(self.spoolman_db)
        db[12]["archived"] = True  # Slot 4 archived
        del db[11]  # Slot 2 missing

        report = sl.reconcile_four_slot_state(
            self.state,
            spoolman_spools=db,
        )
        self.assertTrue(report["drift_detected"])
        categories = [d["category"] for d in report["drifts"]]
        self.assertIn("spoolman_archived_drift", categories)
        self.assertIn("spoolman_missing", categories)

    def test_reconcile_detects_out_of_range_and_unmapped_active_spool(self):
        """Detects legacy slot 5 definition and active spool unmapped to any 1-4 physical slot."""
        st = copy.deepcopy(self.state)
        st["filament_slots"]["5"] = {"slot": 5, "material": "PETG", "color": "blue", "spoolman_id": 99}
        report = sl.reconcile_four_slot_state(
            st,
            moonraker_spool_id=99,
            spoolman_spools=self.spoolman_db,
        )
        self.assertTrue(report["drift_detected"])
        categories = [d["category"] for d in report["drifts"]]
        self.assertIn("out_of_range_slot_definition", categories)
        self.assertIn("active_spool_unmapped_slot", categories)
        self.assertFalse(report["mutation_performed"])


class JobAccountingReceiptTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "filament_slots": {
                "1": {"slot": 1, "material": "PLA", "color": "white", "hex": "FFFFFF", "spoolman_id": 10, "name": "White PLA", "remaining_grams": None, "quantity_status": "unknown"},
                "2": {"slot": 2, "material": "PLA", "color": "pink", "hex": "FF69B4", "spoolman_id": 11, "name": "Pink PLA", "remaining_grams": 500.0, "quantity_status": "user_estimate", "weight_confidence": "user_estimate"},
                "3": {"slot": 3, "material": "PLA", "color": "black", "hex": "000000", "spoolman_id": 8, "name": "Black PLA", "remaining_grams": None, "quantity_status": "unknown"},
                "4": {"slot": 4, "material": "PLA", "color": "green", "hex": "067749", "spoolman_id": 12, "name": "Green PLA", "remaining_grams": 950.0, "quantity_status": "known_estimate", "weight_confidence": "spoolman_recorded"},
            },
            "spool": {"spoolman_id": 8, "name": "Black PLA", "material": "PLA", "diameter_mm": 1.75, "density": 1.24},
        }

    def test_single_color_receipt_with_unknown_spool_weight(self):
        """Job using slot 3 (black, unknown starting weight) leaves remaining weight unknown."""
        sample_gcode = (
            "; HEADER_BLOCK_START\n"
            "; generated by OrcaSlicer 2.4.2\n"
            "; total filament used [g] = 14.85\n"
            "; total filament used [mm] = 4983.21\n"
            "; estimated printing time (normal mode) = 28m 15s\n"
            "; HEADER_BLOCK_END\n"
            "G90\n"
        )
        manifest = {
            "title": "Cable Buckle",
            "filament_selection": {"slot": 3},
        }
        receipt = sl.generate_job_accounting_receipt(
            job_manifest=manifest,
            gcode_text=sample_gcode,
            state=self.state,
        )
        self.assertTrue(receipt["receipt_id"].startswith("receipt-"))
        self.assertEqual(receipt["total_estimated_filament_g"], 14.85)
        self.assertEqual(receipt["estimated_duration"], "28m 15s")
        self.assertFalse(receipt["spoolman_mutation"])
        self.assertFalse(receipt["historical_mutation"])

        self.assertEqual(len(receipt["slots"]), 1)
        slot3_entry = receipt["slots"][0]
        self.assertEqual(slot3_entry["ifs_slot"], 3)
        self.assertEqual(slot3_entry["spoolman_id"], 8)
        self.assertEqual(slot3_entry["estimated_used_g"], 14.85)
        self.assertIsNone(slot3_entry["starting_spool_weight_g"])
        self.assertIsNone(slot3_entry["estimated_remaining_weight_g"])
        self.assertEqual(slot3_entry["quantity_status"], "unknown")
        self.assertEqual(slot3_entry["weight_confidence"], "unknown")

    def test_multi_tool_receipt_with_known_and_unknown_weights(self):
        """Multi-tool job uses tool map and computes remaining only for slots with known weights."""
        sample_gcode = (
            "; HEADER_BLOCK_START\n"
            "; total filament used [g] = 35.50\n"
            "; filament used [g] = 20.00, 15.50\n"
            "; hermes_tool_slots = {\"0\": 2, \"1\": 4}\n"
            "; estimated printing time (normal mode) = 1h 10m\n"
            "; HEADER_BLOCK_END\n"
            "G90\n"
        )
        receipt = sl.generate_job_accounting_receipt(
            gcode_text=sample_gcode,
            state=self.state,
        )
        self.assertEqual(len(receipt["slots"]), 2)

        # Slot 2 (Tool 0): starting 500.0 g, used 20.00 g -> remaining 480.0 g
        s2 = receipt["slots"][0]
        self.assertEqual(s2["ifs_slot"], 2)
        self.assertEqual(s2["estimated_used_g"], 20.00)
        self.assertEqual(s2["starting_spool_weight_g"], 500.0)
        self.assertEqual(s2["estimated_remaining_weight_g"], 480.0)
        self.assertEqual(s2["quantity_status"], "known_estimate")

        # Slot 4 (Tool 1): starting 950.0 g, used 15.50 g -> remaining 934.5 g
        s4 = receipt["slots"][1]
        self.assertEqual(s4["ifs_slot"], 4)
        self.assertEqual(s4["estimated_used_g"], 15.50)
        self.assertEqual(s4["starting_spool_weight_g"], 950.0)
        self.assertEqual(s4["estimated_remaining_weight_g"], 934.5)

    def test_receipt_from_moonraker_history(self):
        """Actual filament used in mm from Moonraker history converted to grams."""
        sample_gcode = "; total filament used [g] = 10.0\n"
        history_job = {"filament_used": 3350.0}  # 3.35m
        receipt = sl.generate_job_accounting_receipt(
            gcode_text=sample_gcode,
            state=self.state,
            moonraker_history_job=history_job,
        )
        self.assertEqual(receipt["total_estimated_filament_g"], 10.0)
        # 3350 mm * pi * (1.75/2)^2 = 8057.4 mm3 * 1.24 / 1000 = ~9.99 g
        self.assertAlmostEqual(receipt["total_actual_filament_g"], 9.99, places=1)

    def test_receipt_with_unmapped_tool_does_not_fabricate_slot(self):
        """When tool mapping is unknown, receipt reflects unknown status without fabricating slot 1."""
        empty_state = {"filament_slots": {}, "spool": {}}
        sample_gcode = "; total filament used [g] = 12.0\n"
        receipt = sl.generate_job_accounting_receipt(
            gcode_text=sample_gcode,
            state=empty_state,
        )
        self.assertEqual(receipt["tool_mapping_status"], "unknown")
        self.assertEqual(len(receipt["slots"]), 1)
        entry = receipt["slots"][0]
        self.assertIsNone(entry["ifs_slot"])
        self.assertEqual(entry["mapping_status"], "unknown")
        self.assertIsNone(entry["starting_spool_weight_g"])


class SpoolLifecycleCLITests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "spoolman_url": "http://spoolman.test:7912",
            "filament_slots": {
                "1": {"slot": 1, "material": "PLA", "color": "white", "hex": "FFFFFF", "spoolman_id": 10},
                "2": {"slot": 2, "material": "PLA", "color": "pink", "hex": "FF69B4", "spoolman_id": 11},
                "3": {"slot": 3, "material": "PLA", "color": "black", "hex": "000000", "spoolman_id": 8},
                "4": {"slot": 4, "material": "PLA", "color": "green", "hex": "067749", "spoolman_id": 12},
            },
            "spool": {"spoolman_id": 8, "name": "Black PLA", "material": "PLA", "diameter_mm": 1.75, "density": 1.24},
        }

    def test_cli_replace(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "state.json"
            state_file.write_text(json.dumps(self.state))
            spool_data = {"id": 8, "name": "Black PLA", "archived": False, "filament": {"material": "PLA", "name": "Black PLA", "color_hex": "000000"}}

            with mock.patch("sys.argv", ["spool_lifecycle.py", "replace", "--slot", "3", "--material", "PLA", "--color", "black", "--spoolman-id", "8", "--quantity", "unknown"]), \
                 mock.patch("print_handy.core.printer_state.load", return_value=copy.deepcopy(self.state)), \
                 mock.patch("print_handy.core.spool_lifecycle.fetch_spoolman_spool", return_value=spool_data):
                with mock.patch("sys.stdout") as mock_stdout:
                    sl.main()
                    self.assertTrue(mock_stdout.write.called)

    def test_cli_reconcile(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "state.json"
            state_file.write_text(json.dumps(self.state))
            with mock.patch("sys.argv", ["spool_lifecycle.py", "reconcile", "--state-file", str(state_file), "--offline"]):
                with mock.patch("sys.stdout") as mock_stdout:
                    sl.main()
                    self.assertTrue(mock_stdout.write.called)

    def test_cli_reconcile_offline_with_source_files(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "state.json"
            state_file.write_text(json.dumps(self.state))
            detail_file = Path(td) / "detail.json"
            detail_file.write_text(json.dumps({"hasMatlStation": True, "matlStationInfo": {"slotInfos": []}}))
            spoolman_file = Path(td) / "spoolman.json"
            spoolman_file.write_text(json.dumps([{"id": 8, "name": "Black PLA", "archived": False}]))

            with mock.patch("sys.argv", [
                "spool_lifecycle.py",
                "reconcile",
                "--state-file", str(state_file),
                "--offline",
                "--detail-file", str(detail_file),
                "--spoolman-file", str(spoolman_file),
                "--moonraker-spool-id", "8",
            ]):
                with mock.patch("sys.stdout") as mock_stdout:
                    sl.main()
                    self.assertTrue(mock_stdout.write.called)

    def test_cli_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            gcode_file = Path(td) / "test.gcode"
            gcode_file.write_text("; total filament used [g] = 5.2\n")
            with mock.patch("sys.argv", ["spool_lifecycle.py", "receipt", "--gcode", str(gcode_file)]), \
                 mock.patch("print_handy.core.printer_state.load", return_value=copy.deepcopy(self.state)):
                with mock.patch("sys.stdout") as mock_stdout:
                    sl.main()
                    self.assertTrue(mock_stdout.write.called)


class ParseSlicerMetadataBambuTests(unittest.TestCase):
    """#444: real Bambu Studio spellings, pinned against real OrcaSlicer output.

    BAMBU_GCODE's header block is copied line for line from an actual Bambu
    Studio 2.8.2.61 A1 slice (20 mm cube, Generic PLA): Bambu writes its usage
    totals only there, with colon separators, and no per-filament used lines
    anywhere in the file. ORCA_BBL_GCODE is the same cube sliced by
    OrcaSlicer 2.5.0-alpha with the BBL A1 profiles.
    """

    BAMBU_GCODE = (
        "; HEADER_BLOCK_START\n"
        "; BambuStudio 02.08.02.61\n"
        "; model printing time: 16m 18s; total estimated time: 22m 34s\n"
        "; total layer number: 100\n"
        "; total filament length [mm] : 1263.89\n"
        "; total filament volume [cm^3] : 3040.00\n"
        "; total filament weight [g] : 3.77\n"
        "; filament_density: 1.24\n"
        "; filament_diameter: 1.75\n"
        "; max_z_height: 20.00\n"
        "; filament: 1\n"
        "; HEADER_BLOCK_END\n"
        "G1 X1\n"
    )

    ORCA_GCODE = (
        "; HEADER_BLOCK_START\n"
        "; generated by OrcaSlicer 2.4.2\n"
        "; total filament used [g] = 14.85\n"
        "; total filament used [mm] = 4983.21\n"
        "; estimated printing time (normal mode) = 28m 15s\n"
        "; HEADER_BLOCK_END\n"
        "G90\n"
    )

    ORCA_BBL_GCODE = (
        "; HEADER_BLOCK_START\n"
        "; generated by OrcaSlicer 2.5.0-alpha on 2026-10-27 at 18:30:16\n"
        "; model printing time: 16m 23s; total estimated time: 22m 40s\n"
        "; estimated first layer printing time (normal mode) = 6m 16s\n"
        "; total layer number: 100\n"
        "; HEADER_BLOCK_END\n"
        "G1 X1\n"
        "; filament used [mm] = 1256.74\n"
        "; filament used [cm3] = 3.02\n"
        "; filament used [g] = 3.75\n"
    )

    def test_bambu_header_block_totals_parse(self):
        self.assertEqual(sl.parse_slicer_metadata(self.BAMBU_GCODE), {
            "total_filament_used_g": 3.77,
            "total_filament_used_mm": 1263.89,
            # The header labels 3040.00 as [cm^3] but the number is mm3
            # (length x cross-section; 3.77 g / 1.24 density = 3.04 cm3).
            "total_filament_used_cm3": 3.04,
            # Bambu writes no per-filament used lines; the list stays empty.
            "filament_used_g_list": [],
            "estimated_time_str": "22m 34s",
            "tool_mapping": None,
            "tool_spool_ids": {},
        })

    def test_bambu_receipt_uses_header_totals(self):
        state = {
            "filament_slots": {
                "3": {"slot": 3, "material": "PLA", "color": "black", "hex": "000000", "spoolman_id": 8, "name": "Black PLA", "remaining_grams": None, "quantity_status": "unknown"},
            },
            "spool": {"spoolman_id": 8, "name": "Black PLA", "material": "PLA", "diameter_mm": 1.75, "density": 1.24},
        }
        receipt = sl.generate_job_accounting_receipt(
            job_manifest={"title": "Cube", "filament_selection": {"slot": 3}},
            gcode_text=self.BAMBU_GCODE,
            state=state,
        )
        self.assertEqual(receipt["total_estimated_filament_g"], 3.77)
        self.assertEqual(receipt["estimated_duration"], "22m 34s")
        self.assertEqual(receipt["slots"][0]["estimated_used_g"], 3.77)

    def test_orca_metadata_byte_identical(self):
        """OrcaSlicer files parse exactly as before the Bambu fallbacks existed."""
        self.assertEqual(sl.parse_slicer_metadata(self.ORCA_GCODE), {
            "total_filament_used_g": 14.85,
            "total_filament_used_mm": 4983.21,
            "total_filament_used_cm3": None,
            "filament_used_g_list": [],
            "estimated_time_str": "28m 15s",
            "tool_mapping": None,
            "tool_spool_ids": {},
        })
        # BBL-profile Orca output shares the header totals line with Bambu;
        # none of the Bambu fallbacks may fire on it.
        self.assertEqual(sl.parse_slicer_metadata(self.ORCA_BBL_GCODE), {
            "total_filament_used_g": None,
            "total_filament_used_mm": 1256.74,
            "total_filament_used_cm3": 3.02,
            "filament_used_g_list": [3.75],
            "estimated_time_str": None,
            "tool_mapping": None,
            "tool_spool_ids": {},
        })

    def test_bambu_absent_values_stay_unknown(self):
        """A Bambu file without the totals lines keeps None, never zero."""
        meta = sl.parse_slicer_metadata(
            "; HEADER_BLOCK_START\n; BambuStudio 02.08.02.61\n"
            "; filament_density: 1.24\n; HEADER_BLOCK_END\nG1 X1\n")
        self.assertIsNone(meta["total_filament_used_g"])
        self.assertIsNone(meta["total_filament_used_mm"])
        self.assertIsNone(meta["total_filament_used_cm3"])
        self.assertIsNone(meta["estimated_time_str"])
        self.assertEqual(meta["filament_used_g_list"], [])


if __name__ == "__main__":
    unittest.main()
