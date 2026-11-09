"""Source inventory and filament estimates from small synthetic files."""
import tempfile
import unittest
import zipfile
from pathlib import Path

from print_handy.core import makerworld_profiles as mp
from print_handy.core import source_colors as sc

SLICE_INFO = """<?xml version="1.0"?>
<config>
  <plate><filament id="1" used_g="40.0" used_m="13.0"/><filament id="2" used_g="5.5" used_m="1.8"/></plate>
  <plate><filament id="1" used_g="60.0" used_m="20.0"/></plate>
</config>"""


class FilamentStatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_filament_on_several_plates_adds_up(self):
        path = self.dir / "multi.3mf"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("Metadata/slice_info.config", SLICE_INFO)
        manifest = {}
        sc._append_filament_stats(path, manifest)
        stats = manifest["filament_stats"]
        self.assertEqual(stats["extruders"][1], {"g": 100.0, "m": 33.0})
        self.assertEqual(stats["extruders"][2], {"g": 5.5, "m": 1.8})
        self.assertEqual(stats["total_g"], 105.5)
        self.assertEqual(stats["plates"]["1"], {
            "extruders": {1: {"g": 40.0, "m": 13.0}, 2: {"g": 5.5, "m": 1.8}},
            "total_g": 45.5, "total_m": 14.8, "usage_known": True,
            "usage_unknown_reason": None})
        self.assertEqual(stats["plates"]["2"], {
            "extruders": {1: {"g": 60.0, "m": 20.0}},
            "total_g": 60.0, "total_m": 20.0, "usage_known": True,
            "usage_unknown_reason": None})
        self.assertEqual(sum(e["g"] for e in stats["extruders"].values()), stats["total_g"])

    def test_invalid_per_plate_filament_usage_is_explicitly_unknown(self):
        path = self.dir / "unknown.3mf"
        xml = """<config><plate><metadata key="index" value="9"/>
        <filament id="bad" used_g="2" used_m="1"/></plate></config>"""
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("Metadata/slice_info.config", xml)
        # Inventory consumers must not mistake an empty parse for proof of zero usage.
        manifest = {}
        sc._append_filament_stats(path, manifest)
        self.assertFalse(manifest["filament_stats"]["plates"]["9"]["usage_known"])
        self.assertEqual(manifest["filament_stats"]["plates"]["9"]["usage_unknown_reason"],
                         "invalid or unmapped source filament usage record")

    def test_per_plate_filament_usage_preserves_explicit_plate_ids(self):
        path = self.dir / "indexed.3mf"
        xml = """<config><plate><metadata key="index" value="7"/>
        <filament id="3" used_g="2.5" used_m="0.8"/></plate></config>"""
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("Metadata/slice_info.config", xml)
        sc._append_filament_stats(path, manifest := {})
        self.assertEqual(manifest["filament_stats"]["plates"]["7"]["extruders"][3],
                         {"g": 2.5, "m": 0.8})

    def test_plate_usage_gate_refuses_missing_or_unknown_usage(self):
        with self.assertRaisesRegex(RuntimeError, 'usage is unknown'):
            sc.require_plate_usage_known({'filament_stats': {}})
        with self.assertRaisesRegex(RuntimeError, 'plate 9.*invalid source'):
            sc.require_plate_usage_known({'filament_stats': {'plates': {
                '9': {'usage_known': False, 'usage_unknown_reason': 'invalid source filament usage record'}}}})

    def test_plate_usage_gate_accepts_only_fully_validated_plate_usage(self):
        sc.require_plate_usage_known({'filament_stats': {'plates': {
            '7': {'usage_known': True, 'extruders': {3: {'g': 2.5, 'm': .8}}}}}})

    def test_plate_usage_gate_validates_only_selected_plate_ids(self):
        manifest = {'filament_stats': {'plates': {
            '7': {'usage_known': True, 'extruders': {3: {'g': 2.5, 'm': .8}}},
            '9': {'usage_known': False, 'usage_unknown_reason': 'unselected unknown plate'}}}}
        sc.require_plate_usage_known(manifest, selected_plate_ids=['7'])
        with self.assertRaisesRegex(RuntimeError, 'plate 9.*unknown'):
            sc.require_plate_usage_known(manifest, selected_plate_ids=['9'])

    def test_plate_usage_gate_rejects_invalid_amounts_and_empty_assignment(self):
        for usage in ({'usage_known': True, 'extruders': {}},
                      {'usage_known': True, 'extruders': {1: {'g': float('nan'), 'm': 0}}}):
            with self.subTest(usage=usage), self.assertRaisesRegex(RuntimeError, 'usage is unknown'):
                sc.require_plate_usage_known({'filament_stats': {'plates': {'7': usage}}}, ['7'])

    def test_optional_source_usage_allows_slicing_when_absent(self):
        manifest = {'filament_stats': {'plates': {
            'unknown': {'usage_known': False, 'usage_unknown_reason': 'source has no slicer usage metadata'}}}}
        sc.validate_plate_usage_if_present(manifest)

    def test_optional_source_usage_fails_closed_when_claimed_invalid(self):
        manifest = {'filament_stats': {'plates': {
            '7': {'usage_known': False, 'usage_unknown_reason': 'invalid source filament usage record'}}}}
        with self.assertRaisesRegex(RuntimeError, 'plate 7'):
            sc.validate_plate_usage_if_present(manifest)

        path = Path(self.dir) / "unsliced.3mf"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("Metadata/model_settings.config", "<config/>")
        manifest = {}
        sc._append_filament_stats(path, manifest)
        self.assertFalse(manifest["filament_stats"]["plates"]["unknown"]["usage_known"])
        sc.validate_plate_usage_if_present(manifest)

        path = Path(self.dir) / "part.stl"
        path.write_bytes(b"solid x\nendsolid x\n")
        res = sc.inventory(path)
        self.assertEqual(res["parts"], [])
        self.assertFalse(res["requires_choices"])
        self.assertEqual(len(res["source_sha256"]), 64)


class ShapeWordTests(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(sc._shape_word([10, 8, 5]), "Small pin/peg")
        self.assertEqual(sc._shape_word([100, 100, 5]), "Flat round disc")
        self.assertEqual(sc._shape_word([100, 40, 5]), "Flat plate")
        self.assertEqual(sc._shape_word([60, 60, 165.3]), "Tall post")
        self.assertEqual(sc._shape_word([80, 80, 30]), "Low round piece")
        self.assertEqual(sc._shape_word([40, 50, 45]), "Chunky piece")
        self.assertEqual(sc._shape_word(None), "")

    def test_generic_cad_name_becomes_a_description(self):
        part = {"name": "Cylinder", "bounds_mm": [60, 60, 165.3], "plate": 4}
        self.assertTrue(sc._prompt_name(part).startswith('Tall post ("Cylinder")'))
        self.assertIn("plate 4", sc._prompt_name(part))


class MakerWorldProfileTests(unittest.TestCase):
    def test_a1_mini_with_04_nozzle_ranks_first(self):
        row = {"compatibility": {"devProductName": "X1 Carbon", "nozzleDiameter": 0.4},
               "other_compatibility": [{"devProductName": "A1 mini", "nozzleDiameter": 0.4},
                                       {"devProductName": "A1 mini", "nozzleDiameter": 0.4}]}
        out = mp.annotate([row])[0]
        self.assertEqual(out["recommended_profile"]["printer"], "A1 mini")
        self.assertEqual(out["recommended_profile"]["bed_fit"], "fits")
        self.assertEqual(len(out["bambu_profiles"]), 2)
