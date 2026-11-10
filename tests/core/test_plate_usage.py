"""Per-plate filament usage analysis, selected-plate prompts and readiness (#1110 Phase B).

Synthetic 3MF fixtures only, built the way tests/corpus/make_models.py builds
its packages: mesh objects plus one assembly object per shape, Bambu-style
model_settings.config plates, a project palette, and optional slicer members.
"""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from print_handy.core import model_job as mj
from print_handy.core import source_colors as sc

PALETTE = [("#FF0000", "PLA"), ("#00FF00", "PLA"), ("#0000FF", "PETG"), ("#FFFF00", "PLA")]

# One tetrahedron per shape; the first triangle may carry a paint code.
_VERTICES = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
_FACES = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3)]


def _paint_code(state):
    """paint_color encoding (painted_geometry): a leaf under 3 is one nibble
    (state << 2); state 3 and up is the 0xC marker plus a state-3 nibble."""
    return f"{state << 2:X}" if state < 3 else f"{state - 3:X}C"


def _mesh_xml(paint_state=None):
    tris = []
    for i, (a, b, c) in enumerate(_FACES):
        paint = f' paint_color="{_paint_code(paint_state)}"' if i == 0 and paint_state else ""
        tris.append(f'<triangle v1="{a}" v2="{b}" v3="{c}"{paint}/>')
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in _VERTICES)
    return f"<mesh><vertices>{verts}</vertices><triangles>{''.join(tris)}</triangles></mesh>"


def write_source(path, shapes, plates, *, project_extra=None, members=None, slice_info=None,
                 object_meta=None):
    """shapes: [(name, extruder or None, paint_state or None)]; plates: [[shape indices]].

    object_meta: {shape index: {key: value}} extra object-level metadata
    (per-object settings, as Orca writes them in model_settings.config); such
    an object carries its extruder at object level only, as Orca writes a
    single-volume object, so the part does not repeat (and override) it."""
    objects, items, settings = [], [], ["<config>"]
    for index, (name, extruder, paint_state) in enumerate(shapes):
        mesh_id, assembly = 2 * index + 2, 2 * index + 1
        objects.append(f'<object id="{mesh_id}" type="model">{_mesh_xml(paint_state)}</object>')
        objects.append(f'<object id="{assembly}" type="model"><components>'
                       f'<component objectid="{mesh_id}"/></components></object>')
        items.append(f'<item objectid="{assembly}" transform="1 0 0 0 1 0 0 0 1 {20 * index} 0 0"/>')
        ext = f'<metadata key="extruder" value="{extruder}"/>' if extruder is not None else ""
        extra = "".join(f'<metadata key="{k}" value="{v}"/>'
                        for k, v in (object_meta or {}).get(index, {}).items())
        settings.append(f'<object id="{assembly}"><metadata key="name" value="{name}"/>{ext}{extra}'
                        f'<part id="{mesh_id}" subtype="normal_part">'
                        f'<metadata key="name" value="{name}"/>{"" if extra else ext}</part></object>')
    for number, indices in enumerate(plates, 1):
        instances = "".join(f'<model_instance><metadata key="object_id" value="{2 * i + 1}"/></model_instance>'
                            for i in indices)
        settings.append(f'<plate><metadata key="plater_id" value="{number}"/>'
                        f'<metadata key="plater_name" value="Plate {number}"/>{instances}</plate>')
    settings.append("</config>")
    project = {"filament_colour": [c for c, _ in PALETTE], "filament_type": [m for _, m in PALETTE],
               "enable_support": "0"}
    project.update(project_extra or {})
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f"<resources>{''.join(objects)}</resources><build>{''.join(items)}</build></model>")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("3D/3dmodel.model", model)
        archive.writestr("Metadata/model_settings.config", "\n".join(settings))
        archive.writestr("Metadata/project_settings.config", json.dumps(project))
        for name, data in (members or {}).items():
            archive.writestr(name, data)
        if slice_info is not None:
            archive.writestr("Metadata/slice_info.config", slice_info)
    return path


def two_plate_source(path):
    """Plate 1 prints filaments 1 and 2; plate 2 prints filament 3 only."""
    return write_source(path, [("Base", 1, None), ("Lid", 2, None), ("Knob", 3, None)],
                        [[0, 1], [2]])


def selection(slot, spool=10):
    return {"slot": slot, "spoolman_id": spool, "color": "Red", "material": "PLA"}


class PlateUsageAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_object_assignments_are_per_plate_with_original_ids(self):
        manifest = sc.inventory(two_plate_source(self.dir / "two.3mf"))
        usage = manifest["plate_usage"]
        self.assertEqual(usage["1"]["source_filaments"], [1, 2])
        # Filament 3 keeps its original source ID; it is not renumbered to 1
        # just because plate 2 uses nothing else.
        self.assertEqual(usage["2"]["source_filaments"], [3])
        objects = usage["1"]["classes"]["objects"]
        self.assertEqual(objects["status"], "known")
        self.assertEqual(objects["source_filaments"], [1, 2])
        self.assertIn("model_settings.config", objects["evidence"])
        self.assertTrue(usage["1"]["usage_known"])
        self.assertTrue(usage["2"]["usage_known"])
        # Logical source IDs, loaded slots and nozzles stay distinct concepts:
        # the usage record itself names no slot and no nozzle.
        for entry in usage.values():
            self.assertNotIn("slot", entry)
            self.assertNotIn("nozzle", entry)
            for row in entry["classes"].values():
                self.assertNotIn("slot", row)

    def test_triangle_painting_adds_painted_filaments_to_their_plate(self):
        path = write_source(self.dir / "painted.3mf",
                            [("Holder", 1, 2), ("Plain", 1, None)], [[0], [1]])
        manifest = sc.inventory(path)
        self.assertTrue(manifest["painted"])
        painting = manifest["plate_usage"]["1"]["classes"]["painting"]
        self.assertEqual(painting["status"], "known")
        # State 2 paints filament 2; the unpainted triangles print the base
        # extruder (filament 1). Plate 2's part carries no paint at all.
        self.assertEqual(painting["source_filaments"], [1, 2])
        self.assertEqual(manifest["plate_usage"]["1"]["source_filaments"], [1, 2])
        self.assertEqual(manifest["plate_usage"]["2"]["classes"]["painting"]["source_filaments"], [1])

    def test_unpainted_source_painting_class_is_known_empty(self):
        manifest = sc.inventory(two_plate_source(self.dir / "plain.3mf"))
        painting = manifest["plate_usage"]["1"]["classes"]["painting"]
        self.assertEqual(painting["status"], "known")
        self.assertEqual(painting["source_filaments"], [])

    def test_layer_changes_are_attributed_to_their_own_plate(self):
        layer_xml = """<config>
          <plate><plate_info id="1"/></plate>
          <plate><plate_info id="2"/><layer top_z="4.2" type="0" gcode="tool_change" extruder="3" color="#0000FF"/></plate>
        </config>"""
        path = write_source(self.dir / "layered.3mf",
                            [("Base", 1, None), ("Knob", 1, None)], [[0], [1]],
                            members={"Metadata/custom_gcode_per_layer.xml": layer_xml})
        manifest = sc.inventory(path)
        self.assertEqual(manifest["plate_usage"]["2"]["classes"]["layer_changes"]["source_filaments"], [3])
        self.assertEqual(manifest["plate_usage"]["2"]["source_filaments"], [1, 3])
        self.assertEqual(manifest["plate_usage"]["1"]["classes"]["layer_changes"]["source_filaments"], [])

    def test_layer_change_without_an_extruder_is_unknown_not_zero(self):
        layer_xml = """<config>
          <plate><plate_info id="1"/><layer top_z="4.2" type="0" gcode="tool_change"/></plate>
        </config>"""
        path = write_source(self.dir / "layered.3mf", [("Base", 1, None)], [[0]],
                            members={"Metadata/custom_gcode_per_layer.xml": layer_xml})
        manifest = sc.inventory(path)
        row = manifest["plate_usage"]["1"]["classes"]["layer_changes"]
        self.assertEqual(row["status"], "unknown")
        self.assertFalse(manifest["plate_usage"]["1"]["usage_known"])
        self.assertIn("layer_changes", manifest["plate_usage"]["1"]["unknown_classes"])

    def test_height_range_extruder_assignment_is_per_plate(self):
        ranges = ('<objects><object id="1"><range min_z="0" max_z="5">'
                  '<option opt_key="extruder">2</option>'
                  '<option opt_key="layer_height">0.12</option></range></object></objects>')
        path = write_source(self.dir / "ranges.3mf",
                            [("Base", 1, None), ("Knob", 3, None)], [[0], [1]],
                            members={"Metadata/layer_config_ranges.xml": ranges})
        manifest = sc.inventory(path)
        row = manifest["plate_usage"]["1"]["classes"]["height_ranges"]
        self.assertEqual(row["status"], "known")
        self.assertEqual(row["source_filaments"], [2])
        self.assertEqual(manifest["plate_usage"]["1"]["source_filaments"], [1, 2])
        self.assertEqual(manifest["plate_usage"]["2"]["classes"]["height_ranges"]["source_filaments"], [])

    def test_malformed_height_ranges_are_unknown_not_absent(self):
        path = write_source(self.dir / "ranges.3mf", [("Base", 1, None)], [[0]],
                            members={"Metadata/layer_config_ranges.xml": "<objects><object"})
        manifest = sc.inventory(path)
        row = manifest["plate_usage"]["1"]["classes"]["height_ranges"]
        self.assertEqual(row["status"], "unknown")
        self.assertFalse(manifest["plate_usage"]["1"]["usage_known"])

    def test_part_without_extruder_assignment_is_unknown(self):
        path = write_source(self.dir / "noext.3mf", [("Base", None, None)], [[0]])
        manifest = sc.inventory(path)
        row = manifest["plate_usage"]["1"]["classes"]["objects"]
        self.assertEqual(row["status"], "unknown")
        self.assertIn("no source extruder", row["reason"])
        self.assertFalse(manifest["plate_usage"]["1"]["usage_known"])

    def test_support_and_interface_designations_are_role_usage(self):
        path = write_source(self.dir / "support.3mf",
                            [("Base", 1, None), ("Knob", 2, None)], [[0], [1]],
                            project_extra={"enable_support": "1", "support_filament": "3"})
        manifest = sc.inventory(path)
        for plate in ("1", "2"):
            classes = manifest["plate_usage"][plate]["classes"]
            self.assertEqual(classes["support"]["source_filaments"], [3])
            # No separate interface designation: the interface prints with the
            # designated support filament, as the slicer applies it.
            self.assertEqual(classes["interface"]["source_filaments"], [3])
            self.assertIn(3, manifest["plate_usage"][plate]["source_filaments"])

    def test_support_designation_outside_palette_makes_interface_usage_unknown(self):
        path = write_source(self.dir / "support-outside.3mf",
                            [("Base", 1, None), ("Knob", 2, None)], [[0], [1]],
                            project_extra={"enable_support": "1", "support_filament": "3",
                                           "support_interface_filament": "9"})
        manifest = sc.inventory(path)
        for plate in ("1", "2"):
            entry = manifest["plate_usage"][plate]
            self.assertFalse(entry["usage_known"])
            self.assertIn("support", entry["unknown_classes"])
            self.assertIn("interface", entry["unknown_classes"])
            self.assertIn("interface=9", entry["classes"]["interface"]["reason"])

    def test_wall_infill_tower_are_not_carried_by_an_orca_project(self):
        manifest = sc.inventory(two_plate_source(self.dir / "two.3mf"))
        classes = manifest["plate_usage"]["1"]["classes"]
        for name in ("wall", "infill", "tower"):
            self.assertEqual(classes[name]["status"], "not_carried_by_format", name)
            self.assertIn("reason", classes[name])
        # A class the format does not carry names no distinct filament, so it
        # cannot make the plate's usage unknown.
        self.assertTrue(manifest["plate_usage"]["1"]["usage_known"])

    def test_prusa_role_extruders_are_read_from_the_prusa_config(self):
        config = ("; generated by PrusaSlicer 2.8.0\n; filament_colour = #FF0000;#00FF00\n"
                  "; filament_type = PLA;PLA\n; perimeter_extruder = 2\n; infill_extruder = 2\n"
                  "; support_material_extruder = 2\n")
        model = ('<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                 f'<resources><object id="2" type="model">{_mesh_xml()}</object>'
                 '<object id="1" type="model"><components><component objectid="2"/></components></object>'
                 '</resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build></model>')
        path = self.dir / "prusa.3mf"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("3D/3dmodel.model", model)
            archive.writestr("Metadata/Slic3r_PE.config", config)
        manifest = sc.inventory(path)
        classes = manifest["plate_usage"]["1"]["classes"]
        self.assertEqual(classes["wall"]["status"], "known")
        self.assertEqual(classes["wall"]["source_filaments"], [2])
        self.assertEqual(classes["infill"]["source_filaments"], [2])
        self.assertIn(2, manifest["plate_usage"]["1"]["source_filaments"])

    def test_slicer_reported_totals_merge_when_they_agree(self):
        slice_info = """<config>
          <plate><metadata key="index" value="1"/><filament id="1" used_g="10" used_m="3"/><filament id="2" used_g="4" used_m="1"/></plate>
          <plate><metadata key="index" value="2"/><filament id="3" used_g="7" used_m="2"/></plate>
        </config>"""
        manifest = sc.inventory(write_source(self.dir / "two.3mf",
                                             [("Base", 1, None), ("Lid", 2, None), ("Knob", 3, None)],
                                             [[0, 1], [2]], slice_info=slice_info))
        plate = manifest["plate_usage"]["1"]
        self.assertTrue(plate["usage_known"])
        self.assertIsNone(plate["disagreement"])
        self.assertEqual(plate["slicer_reported"]["extruders"][2], {"g": 4.0, "m": 1.0})
        self.assertEqual(manifest["plate_usage"]["2"]["slicer_reported"]["extruders"][3],
                         {"g": 7.0, "m": 2.0})

    def test_slicer_disagreement_makes_the_plate_usage_unknown(self):
        slice_info = """<config>
          <plate><metadata key="index" value="1"/><filament id="1" used_g="10" used_m="3"/><filament id="3" used_g="4" used_m="1"/></plate>
        </config>"""
        path = write_source(self.dir / "two.3mf", [("Base", 1, None), ("Lid", 2, None)], [[0, 1]],
                            slice_info=slice_info)
        manifest = sc.inventory(path)
        plate = manifest["plate_usage"]["1"]
        self.assertFalse(plate["usage_known"])
        self.assertIn("disagree", plate["disagreement"])
        # Both claims are preserved; neither is silently dropped or averaged.
        self.assertEqual(plate["source_filaments"], [1, 2])
        self.assertEqual(set(plate["slicer_reported"]["extruders"]), {1, 3})

    def test_invalid_slicer_record_makes_the_plate_usage_unknown(self):
        slice_info = """<config>
          <plate><metadata key="index" value="1"/><filament id="bad" used_g="1" used_m="1"/></plate>
        </config>"""
        path = write_source(self.dir / "one.3mf", [("Base", 1, None)], [[0]], slice_info=slice_info)
        manifest = sc.inventory(path)
        plate = manifest["plate_usage"]["1"]
        self.assertFalse(plate["usage_known"])
        self.assertEqual(plate["slicer_reported"]["usage_unknown_reason"], sc.USAGE_INVALID)



def _prusa_source(path, object_extruder, *, perimeter=1, infill=1):
    """A PrusaSlicer project as PrusaSlicer writes it: the print-wide role
    extruders are always present (1 by default), and the object names its
    own extruder in Slic3r_PE_model.config when the author set one."""
    config = ("; generated by PrusaSlicer 2.8.0\n; filament_colour = #FF0000;#00FF00\n"
              "; filament_type = PLA;PLA\n"
              f"; perimeter_extruder = {perimeter}\n; infill_extruder = {infill}\n"
              f"; solid_infill_extruder = {infill}\n"
              "; support_material_extruder = 0\n; support_material_interface_extruder = 0\n")
    model = ('<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources><object id="2" type="model">{_mesh_xml()}</object>'
             '<object id="1" type="model"><components><component objectid="2"/></components></object>'
             '</resources><build><item objectid="1" transform="1 0 0 0 1 0 0 0 1 0 0 0"/></build></model>')
    ext = (f'<metadata type="object" key="extruder" value="{object_extruder}"/>'
           if object_extruder is not None else '')
    model_config = ('<?xml version="1.0" encoding="UTF-8"?><config><object id="1" instances_count="1">'
                    f'<metadata type="object" key="name" value="Body"/>{ext}'
                    '<volume firstid="0" lastid="3"><metadata type="volume" key="name" value="Body"/>'
                    '</volume></object></config>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("3D/3dmodel.model", model)
        archive.writestr("Metadata/Slic3r_PE.config", config)
        archive.writestr("Metadata/Slic3r_PE_model.config", model_config)
    return path


class PrusaQuestionSetTests(unittest.TestCase):
    """#1155: the print-wide perimeter/infill extruders PrusaSlicer always
    writes must not add a filament (and a color question) to a job whose
    object names its own extruder."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_object_extruder_overrides_the_print_wide_role_defaults(self):
        manifest = sc.inventory(_prusa_source(self.dir / "prusa.3mf", 2))
        part = manifest["parts"][0]
        self.assertEqual(part["source_extruder"], 2)
        entry = manifest["plate_usage"]["1"]
        self.assertEqual(entry["source_filaments"], [2])
        self.assertEqual(entry["classes"]["wall"]["source_filaments"], [2])
        self.assertEqual(entry["classes"]["infill"]["source_filaments"], [2])
        # Exactly the questions a 2.2.8 job asked: the part, nothing else.
        rows = sc.canonical_prompts(manifest)
        self.assertEqual([row["key"] for row in rows], ["part:" + part["key"]])
        self.assertEqual([row["keys"] for row in sc.grouped_prompts(manifest)],
                         [["part:" + part["key"]]])
        self.assertEqual(len(sc.distinct_source_colours(manifest)), 1)

    def test_saved_decisions_for_an_existing_prusa_job_still_validate(self):
        manifest = sc.inventory(_prusa_source(self.dir / "prusa.3mf", 2))
        part = manifest["parts"][0]

        def resolve(_state, value):
            return value

        per_part = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                    "mode": "per_part", "choices": {"part:" + part["key"]: selection(1)}}
        self.assertEqual(sc.validate_decisions(manifest, per_part, {}, resolve)["choices"],
                         per_part["choices"])
        single = {"source_sha256": manifest["source_sha256"], "mode": "single_color",
                  "selection": selection(1)}
        self.assertEqual(sc.validate_decisions(manifest, single, {}, resolve)["mode"], "single_color")
        # Nothing the source uses is left without a mapping by that decision.
        self.assertEqual(sc.plate_readiness_problems(manifest, per_part, ["1"]), [])

    def test_explicit_object_extruder_1_ignores_a_print_wide_wall_extruder(self):
        manifest = sc.inventory(_prusa_source(self.dir / "prusa.3mf", 1, perimeter=2))
        self.assertEqual(manifest["plate_usage"]["1"]["source_filaments"], [1])
        self.assertEqual(len(sc.canonical_prompts(manifest)), 1)

    def test_object_without_an_extruder_takes_the_print_wide_role_extruders(self):
        # PrusaSlicer prints such an object's walls with perimeter_extruder:
        # that filament really prints, so it is counted and asked about.
        manifest = sc.inventory(_prusa_source(self.dir / "prusa.3mf", None, perimeter=2))
        entry = manifest["plate_usage"]["1"]
        self.assertEqual(entry["classes"]["wall"]["source_filaments"], [2])
        self.assertEqual(entry["classes"]["infill"]["source_filaments"], [1])
        self.assertEqual(entry["source_filaments"], [1, 2])
        self.assertIn("filament:2", {row["key"] for row in sc.canonical_prompts(manifest)})

    def test_default_role_extruders_add_nothing_to_an_object_without_an_extruder(self):
        manifest = sc.inventory(_prusa_source(self.dir / "prusa.3mf", None))
        self.assertEqual(manifest["plate_usage"]["1"]["source_filaments"], [1])
        self.assertEqual(len(sc.canonical_prompts(manifest)), 1)



# OrcaSlicer writes every per-feature filament key into project_settings.config,
# at its default when the author never touched it.
_ORCA_DEFAULTS = {"wall_filament": "1", "sparse_infill_filament": "1", "solid_infill_filament": "1",
                  "wipe_tower_filament": "0", "enable_prime_tower": "1",
                  "support_filament": "0", "support_interface_filament": "0"}


def _question_keys(manifest):
    return {row["key"] for row in sc.canonical_prompts(manifest)}


class UsageUndercountTests(unittest.TestCase):
    """#1155 Train 2 step 1: the five under-counts and the smaller items.
    Out-of-palette or unresolvable values become UNKNOWN usage (a warning
    the operator may accept, #1189), never a used filament (#1184)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _parts(self, manifest):
        return {"part:" + p["key"] for p in manifest["parts"]}

    def test_interface_only_support_designation_is_counted(self):
        path = write_source(self.dir / "iface.3mf", [("Base", 1, None), ("Knob", 2, None)], [[0], [1]],
                            project_extra={"enable_support": "1", "support_interface_filament": "3"})
        manifest = sc.inventory(path)
        for plate in ("1", "2"):
            classes = manifest["plate_usage"][plate]["classes"]
            self.assertEqual(classes["support"]["source_filaments"], [])
            self.assertEqual(classes["interface"]["status"], "known")
            self.assertEqual(classes["interface"]["source_filaments"], [3])
            self.assertIn(3, manifest["plate_usage"][plate]["source_filaments"])
        # The interface spool was already a question of its own (#867): no new one.
        self.assertEqual(_question_keys(manifest), self._parts(manifest) | {"filament:3"})

    def test_height_range_uses_the_bambu_object_index_not_the_resource_id(self):
        # Object index 2 is the second model object (resource id 3, the Knob
        # on plate 2). Read as a resource id it named the Knob's mesh and
        # the range was silently dropped.
        ranges = ('<objects><object id="2"><range min_z="0" max_z="5">'
                  '<option opt_key="extruder">2</option></range></object></objects>')
        path = write_source(self.dir / "ranges.3mf", [("Base", 1, None), ("Knob", 2, None)], [[0], [1]],
                            members={"Metadata/layer_config_ranges.xml": ranges})
        manifest = sc.inventory(path)
        usage = manifest["plate_usage"]
        self.assertEqual(usage["1"]["classes"]["height_ranges"]["source_filaments"], [])
        self.assertEqual(usage["2"]["classes"]["height_ranges"]["source_filaments"], [2])
        self.assertTrue(usage["2"]["usage_known"])

    def test_height_range_index_naming_no_object_is_unknown(self):
        ranges = ('<objects><object id="7"><range min_z="0" max_z="5">'
                  '<option opt_key="extruder">2</option></range></object></objects>')
        path = write_source(self.dir / "ranges.3mf", [("Base", 1, None), ("Knob", 2, None)], [[0], [1]],
                            members={"Metadata/layer_config_ranges.xml": ranges})
        entry = sc.inventory(path)["plate_usage"]["1"]
        self.assertIn("height_ranges", entry["unknown_classes"])
        self.assertIn("object index", entry["classes"]["height_ranges"]["reason"])

    def test_height_range_extruder_0_is_the_objects_own_filament(self):
        ranges = ('<objects><object id="1"><range min_z="0" max_z="5">'
                  '<option opt_key="extruder">0</option></range></object></objects>')
        path = write_source(self.dir / "ranges.3mf", [("Base", 3, None), ("Knob", 2, None)], [[0], [1]],
                            members={"Metadata/layer_config_ranges.xml": ranges})
        manifest = sc.inventory(path)
        entry = manifest["plate_usage"]["1"]
        self.assertTrue(entry["usage_known"])
        self.assertEqual(entry["classes"]["height_ranges"]["source_filaments"], [3])
        self.assertTrue(manifest["plate_usage"]["2"]["usage_known"])
        self.assertEqual(_question_keys(manifest), self._parts(manifest))

    def test_height_range_extruder_outside_the_palette_is_unknown_not_used(self):
        ranges = ('<objects><object id="1"><range min_z="0" max_z="5">'
                  '<option opt_key="extruder">9</option></range></object></objects>')
        path = write_source(self.dir / "ranges.3mf", [("Base", 1, None)], [[0]],
                            members={"Metadata/layer_config_ranges.xml": ranges})
        manifest = sc.inventory(path)
        entry = manifest["plate_usage"]["1"]
        self.assertIn("height_ranges", entry["unknown_classes"])
        self.assertNotIn(9, entry["source_filaments"])
        self.assertNotIn("filament:9", _question_keys(manifest))
        self.assertNotIn(("", ""), sc.distinct_source_colours(manifest))

    def test_object_with_instances_on_two_plates_counts_on_both(self):
        path = write_source(self.dir / "copies.3mf", [("Base", 1, None), ("Knob", 2, None)],
                            [[0], [0, 1]])
        usage = sc.inventory(path)["plate_usage"]
        self.assertEqual(usage["1"]["source_filaments"], [1])
        self.assertEqual(usage["2"]["source_filaments"], [1, 2])

    def test_orca_default_feature_filaments_add_no_filament_or_question(self):
        # Every Orca project carries wall_filament = 1 and friends; objects
        # on filaments 2 and 3 must not gain filament 1 (the #1155 Prusa
        # regression, Orca spelling).
        path = write_source(self.dir / "orca.3mf", [("Base", 2, None), ("Knob", 3, None)], [[0, 1]],
                            project_extra=_ORCA_DEFAULTS)
        manifest = sc.inventory(path)
        entry = manifest["plate_usage"]["1"]
        self.assertEqual(entry["classes"]["wall"]["status"], "known")
        self.assertEqual(entry["classes"]["wall"]["source_filaments"], [2, 3])
        self.assertEqual(entry["classes"]["infill"]["source_filaments"], [2, 3])
        self.assertEqual(entry["classes"]["tower"]["source_filaments"], [])
        self.assertEqual(entry["source_filaments"], [2, 3])
        self.assertTrue(entry["usage_known"])
        self.assertEqual(_question_keys(manifest), self._parts(manifest))

    def test_orca_object_wall_filament_override_is_counted(self):
        path = write_source(self.dir / "orca.3mf", [("Base", 1, None), ("Knob", 3, None)], [[0], [1]],
                            project_extra=_ORCA_DEFAULTS, object_meta={0: {"wall_filament": "3"}})
        manifest = sc.inventory(path)
        entry = manifest["plate_usage"]["1"]
        self.assertEqual(entry["classes"]["wall"]["source_filaments"], [3])
        self.assertEqual(entry["classes"]["infill"]["source_filaments"], [1])
        self.assertEqual(entry["source_filaments"], [1, 3])
        self.assertEqual(entry["filament_classes"]["3"], ["wall"])
        self.assertEqual(_question_keys(manifest), self._parts(manifest))

    def test_orca_infill_override_to_an_unasked_filament_is_unknown_not_a_question(self):
        path = write_source(self.dir / "orca.3mf", [("Base", 1, None), ("Knob", 2, None)], [[0, 1]],
                            project_extra=_ORCA_DEFAULTS,
                            object_meta={0: {"sparse_infill_filament": "4"}})
        manifest = sc.inventory(path)
        entry = manifest["plate_usage"]["1"]
        self.assertIn("infill", entry["unknown_classes"])
        self.assertNotIn(4, entry["source_filaments"])
        self.assertEqual(_question_keys(manifest), self._parts(manifest))
        self.assertTrue(sc.filament_readiness_warnings(manifest, {}, ["1"]))

    def test_orca_feature_filament_outside_the_palette_is_unknown(self):
        path = write_source(self.dir / "orca.3mf", [("Base", 1, None)], [[0]],
                            project_extra=_ORCA_DEFAULTS, object_meta={0: {"wall_filament": "9"}})
        entry = sc.inventory(path)["plate_usage"]["1"]
        self.assertIn("wall", entry["unknown_classes"])
        self.assertIn("outside the project palette", entry["classes"]["wall"]["reason"])
        self.assertNotIn(9, entry["source_filaments"])

    def test_orca_wipe_tower_filament_counts_only_where_a_tower_prints(self):
        path = write_source(self.dir / "tower.3mf",
                            [("Base", 1, None), ("Lid", 2, None), ("Knob", 3, None)], [[0, 1], [2]],
                            project_extra={**_ORCA_DEFAULTS, "wipe_tower_filament": "3"})
        manifest = sc.inventory(path)
        usage = manifest["plate_usage"]
        self.assertEqual(usage["1"]["classes"]["tower"]["source_filaments"], [3])
        self.assertEqual(usage["1"]["source_filaments"], [1, 2, 3])
        # Plate 2 prints one filament: no prime tower, no tower filament.
        self.assertEqual(usage["2"]["classes"]["tower"]["source_filaments"], [])
        self.assertEqual(_question_keys(manifest), self._parts(manifest))

    def test_bambu_project_without_feature_keys_keeps_them_not_carried(self):
        entry = sc.inventory(two_plate_source(self.dir / "two.3mf"))["plate_usage"]["1"]
        for name in ("wall", "infill", "tower"):
            self.assertEqual(entry["classes"][name]["status"], "not_carried_by_format")

    def test_unmapped_slice_info_filament_makes_the_plate_unknown(self):
        slice_info = """<config>
          <plate><metadata key="index" value="1"/><filament id="1" used_g="10" used_m="3"/><filament id="7" used_g="2" used_m="1"/></plate>
          <plate><metadata key="index" value="2"/><filament id="2" used_g="4" used_m="1"/></plate>
        </config>"""
        path = write_source(self.dir / "unmapped.3mf", [("Base", 1, None), ("Knob", 2, None)], [[0], [1]],
                            slice_info=slice_info)
        manifest = sc.inventory(path)
        entry = manifest["plate_usage"]["1"]
        self.assertFalse(entry["usage_known"])
        self.assertEqual(entry["slicer_unknown_reason"], sc.USAGE_UNMAPPED)
        self.assertNotIn(7, entry["source_filaments"])
        self.assertTrue(manifest["plate_usage"]["2"]["usage_known"])
        warnings = sc.filament_readiness_warnings(manifest, {}, ["1", "2"])
        self.assertTrue(warnings and all("plate 1" in w for w in warnings), warnings)
        # One warning, through the readiness rows; the damaged-claim check
        # stays silent for unmapped records (#1190), so it is not doubled.
        self.assertEqual(sc.validate_plate_usage_if_present(manifest), [])
        self.assertEqual(_question_keys(manifest), self._parts(manifest))

    def test_unparseable_support_designation_is_unknown_not_none(self):
        path = write_source(self.dir / "badsupport.3mf", [("Base", 1, None)], [[0]],
                            project_extra={"support_filament": "two"})
        entry = sc.inventory(path)["plate_usage"]["1"]
        self.assertIn("support", entry["unknown_classes"])
        self.assertIn("not a filament number", entry["classes"]["support"]["reason"])

    def test_bad_project_json_makes_the_support_designation_unknown(self):
        from print_handy.core import author_settings
        path = self.dir / "bad.3mf"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("Metadata/project_settings.config", "{not json")
        designation, problem = author_settings.support_designation_status(path)
        self.assertEqual(designation, {"base": None, "interface": None})
        self.assertIn("could not be read", problem)
        self.assertEqual(author_settings.support_designation(path), {"base": None, "interface": None})

    def test_prusa_role_extruder_outside_the_palette_is_unknown_not_used(self):
        manifest = sc.inventory(_prusa_source(self.dir / "prusa.3mf", None, perimeter=5))
        entry = manifest["plate_usage"]["1"]
        self.assertIn("wall", entry["unknown_classes"])
        self.assertNotIn(5, entry["source_filaments"])
        self.assertNotIn("filament:5", _question_keys(manifest))

    def test_selected_plate_stats_tolerates_a_record_missing_an_amount(self):
        manifest = {"filament_stats": {"plates": {"1": {"usage_known": True,
                                                        "extruders": {"1": {"g": 3.0}}}}}}
        self.assertIsNone(mj._selected_plate_stats(manifest, ["1"]))
        good = {"filament_stats": {"plates": {"1": {"usage_known": True,
                                                    "extruders": {"1": {"g": 3.0, "m": 1.0}}}}}}
        self.assertEqual(mj._selected_plate_stats(good, ["1"])["filament_stats"]["total_g"], 3.0)


class SelectedPlatePromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_canonical_prompts_cover_only_the_selected_plate(self):
        manifest = sc.inventory(two_plate_source(self.dir / "two.3mf"))
        keys = {row["key"] for row in sc.canonical_prompts(manifest, selected_plate_ids=[2])}
        knob = next(p["key"] for p in manifest["parts"] if p["name"] == "Knob")
        self.assertEqual(keys, {"part:" + knob})
        keys = {row["key"] for row in sc.canonical_prompts(manifest, selected_plate_ids=[1])}
        self.assertEqual(keys, {"part:" + p["key"] for p in manifest["parts"] if p["name"] != "Knob"})
        # Unscoped prompts are unchanged: every part is asked about.
        self.assertEqual(len(sc.canonical_prompts(manifest)), 3)

    def test_grouped_and_legacy_prompts_scope_the_same_way(self):
        manifest = sc.inventory(two_plate_source(self.dir / "two.3mf"))
        grouped = sc.grouped_prompts(manifest, selected_plate_ids=[2])
        covered = {key for row in grouped for key in row["keys"]}
        knob = next(p["key"] for p in manifest["parts"] if p["name"] == "Knob")
        self.assertEqual(covered, {"part:" + knob})
        legacy = sc.prompts(manifest, selected_plate_ids=[2])
        self.assertEqual({row["key"] for row in legacy if row["key"].startswith("part:")},
                         {"part:" + knob})

    def test_painted_prompts_scope_to_the_selected_plates_filaments(self):
        path = write_source(self.dir / "painted.3mf",
                            [("Holder", 1, 2), ("Badge", 1, 3)], [[0], [1]])
        manifest = sc.inventory(path)
        keys = {row["key"] for row in sc.canonical_prompts(manifest, selected_plate_ids=[2])}
        self.assertEqual(keys, {"filament:1", "filament:3"})
        keys = {row["key"] for row in sc.canonical_prompts(manifest, selected_plate_ids=[1])}
        self.assertEqual(keys, {"filament:1", "filament:2"})

    def test_layer_change_rows_scope_to_their_plate(self):
        layer_xml = """<config>
          <plate><plate_info id="1"/><layer top_z="4.2" type="0" gcode="tool_change" extruder="2"/></plate>
          <plate><plate_info id="2"/><layer top_z="4.2" type="0" gcode="tool_change" extruder="3"/></plate>
        </config>"""
        path = write_source(self.dir / "layered.3mf",
                            [("Base", 1, None), ("Knob", 1, None)], [[0], [1]],
                            members={"Metadata/custom_gcode_per_layer.xml": layer_xml})
        manifest = sc.inventory(path)
        keys = {row["key"] for row in sc.canonical_prompts(manifest, selected_plate_ids=[2])}
        self.assertIn("filament:3", keys)
        self.assertNotIn("filament:2", keys)

    def test_height_range_filament_gets_a_question(self):
        ranges = ('<objects><object id="1"><range min_z="0" max_z="5">'
                  '<option opt_key="extruder">2</option></range></object></objects>')
        path = write_source(self.dir / "ranges.3mf", [("Base", 1, None)], [[0]],
                            members={"Metadata/layer_config_ranges.xml": ranges})
        manifest = sc.inventory(path)
        rows = {row["key"]: row for row in sc.canonical_prompts(manifest)}
        self.assertIn("filament:2", rows)
        self.assertIn("height", rows["filament:2"]["question"].lower())

    def test_scoped_decisions_validate_against_the_scoped_questions(self):
        manifest = sc.inventory(two_plate_source(self.dir / "two.3mf"))
        knob = next(p for p in manifest["parts"] if p["name"] == "Knob")
        decisions = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                     "mode": "per_part", "choices": {"part:" + knob["key"]: selection(4)}}
        out = sc.validate_decisions(manifest, decisions, None, lambda state, value: value,
                                    selected_plate_ids=[2])
        self.assertEqual(set(out["choices"]), {"part:" + knob["key"]})
        # The same decisions are incomplete for the whole source.
        with self.assertRaises(ValueError):
            sc.validate_decisions(manifest, decisions, None, lambda state, value: value)


class PlateReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _manifest_and_decisions(self):
        manifest = sc.inventory(two_plate_source(self.dir / "two.3mf"))
        parts = {p["name"]: p for p in manifest["parts"]}
        decisions = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                     "mode": "per_part",
                     "choices": {"part:" + parts["Base"]["key"]: selection(1),
                                 "part:" + parts["Lid"]["key"]: selection(2),
                                 "part:" + parts["Knob"]["key"]: selection(3)}}
        return manifest, decisions

    def test_ready_when_every_selected_plate_filament_is_mapped(self):
        manifest, decisions = self._manifest_and_decisions()
        self.assertEqual(sc.plate_readiness_problems(manifest, decisions, [1]), [])
        self.assertEqual(sc.plate_readiness_problems(manifest, decisions, [1, 2]), [])
        sc.require_plate_readiness(manifest, decisions, [1, 2])

    def test_unmapped_selected_filament_blocks_with_a_named_reason(self):
        manifest, decisions = self._manifest_and_decisions()
        knob = next(p for p in manifest["parts"] if p["name"] == "Knob")
        del decisions["choices"]["part:" + knob["key"]]
        problems = sc.plate_readiness_problems(manifest, decisions, [2])
        self.assertEqual(len(problems), 1)
        self.assertIn("plate 2", problems[0])
        self.assertIn("filament 3", problems[0])
        with self.assertRaisesRegex(RuntimeError, "plate 2"):
            sc.require_plate_readiness(manifest, decisions, [2])

    def test_unselected_plates_never_block(self):
        manifest, decisions = self._manifest_and_decisions()
        knob = next(p for p in manifest["parts"] if p["name"] == "Knob")
        del decisions["choices"]["part:" + knob["key"]]
        self.assertEqual(sc.plate_readiness_problems(manifest, decisions, [1]), [])

    def test_unknown_usage_blocks_only_the_selected_plate(self):
        path = write_source(self.dir / "noext.3mf",
                            [("Base", 1, None), ("Knob", None, None)], [[0], [1]])
        manifest = sc.inventory(path)
        base = manifest["parts"][0]
        decisions = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                     "mode": "per_part", "choices": {"part:" + base["key"]: selection(1)}}
        self.assertEqual(sc.plate_readiness_problems(manifest, decisions, [1]), [])
        problems = sc.plate_readiness_problems(manifest, decisions, [2])
        self.assertTrue(any("plate 2" in p and "objects" in p for p in problems))

    def test_disagreement_blocks_the_selected_plate(self):
        slice_info = """<config>
          <plate><metadata key="index" value="1"/><filament id="1" used_g="10" used_m="3"/><filament id="3" used_g="4" used_m="1"/></plate>
          <plate><metadata key="index" value="2"/><filament id="3" used_g="7" used_m="2"/></plate>
        </config>"""
        path = write_source(self.dir / "two.3mf",
                            [("Base", 1, None), ("Lid", 2, None), ("Knob", 3, None)],
                            [[0, 1], [2]], slice_info=slice_info)
        manifest = sc.inventory(path)
        parts = {p["name"]: p for p in manifest["parts"]}
        decisions = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                     "mode": "per_part",
                     "choices": {"part:" + parts[n]["key"]: selection(i + 1)
                                 for i, n in enumerate(("Base", "Lid", "Knob"))}}
        problems = sc.plate_readiness_problems(manifest, decisions, [1])
        self.assertTrue(any("disagree" in p for p in problems))
        self.assertEqual(sc.plate_readiness_problems(manifest, decisions, [2]), [])

    def test_single_color_selection_maps_every_used_filament(self):
        manifest, _ = self._manifest_and_decisions()
        decisions = {"source_sha256": manifest["source_sha256"], "schema": "part_slots_v1",
                     "mode": "single_color", "selection": selection(2)}
        self.assertEqual(sc.plate_readiness_problems(manifest, decisions, [1, 2]), [])

    def test_missing_decisions_block(self):
        manifest, _ = self._manifest_and_decisions()
        problems = sc.plate_readiness_problems(manifest, None, [1])
        self.assertTrue(any("plate 1" in p for p in problems))

    def test_shared_nozzle_destination_still_validates_real_slot_capacity(self):
        # AD5X shape: one shared nozzle, four IFS slots. A mapping that names
        # a fifth slot, or spreads a plate over five distinct slots, is not
        # validated by the shared nozzle; it exceeds the real spool capacity.
        manifest, decisions = self._manifest_and_decisions()
        knob = next(p for p in manifest["parts"] if p["name"] == "Knob")
        decisions["choices"]["part:" + knob["key"]] = selection(5)
        problems = sc.plate_readiness_problems(manifest, decisions, [2], slot_capacity=4)
        self.assertTrue(any("slot 5" in p and "4" in p for p in problems))
        self.assertEqual(sc.plate_readiness_problems(manifest, decisions, [2], slot_capacity=5), [])
        # The logical source ID is untouched by the slot it maps to.
        self.assertEqual(manifest["plate_usage"]["2"]["source_filaments"], [3])


class SelectedPlateUsageTotalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_spool_projection_counts_only_the_selected_plate(self):
        slice_info = """<config>
          <plate><metadata key="index" value="1"/><filament id="1" used_g="10" used_m="3"/><filament id="2" used_g="4" used_m="1"/></plate>
          <plate><metadata key="index" value="2"/><filament id="3" used_g="7" used_m="2"/></plate>
        </config>"""
        path = write_source(self.dir / "two.3mf",
                            [("Base", 1, None), ("Lid", 2, None), ("Knob", 3, None)],
                            [[0, 1], [2]], slice_info=slice_info)
        manifest = sc.inventory(path)
        knob = next(p for p in manifest["parts"] if p["name"] == "Knob")
        job = {"selected_plates": [2],
               "color_decisions": {"mode": "per_part",
                                   "choices": {"part:" + knob["key"]: selection(3, spool=30)}}}
        rows = mj.filament_usage_by_selection(job, manifest)
        self.assertEqual([(row[0]["spoolman_id"], row[1]) for row in rows], [(30, 7.0)])
        # Without a plate selection the same choices still count (the knob is
        # the only chosen part), against whole-source extruder totals.
        job.pop("selected_plates")
        total = sum(grams for _, grams in mj.filament_usage_by_selection(job, manifest))
        self.assertEqual(total, 7.0)
        job["color_decisions"]["choices"] = {
            "part:" + p["key"]: selection(p["source_extruder"], spool=p["source_extruder"] * 10)
            for p in manifest["parts"]}
        total = sum(grams for _, grams in mj.filament_usage_by_selection(job, manifest))
        self.assertEqual(total, 21.0)


if __name__ == "__main__":
    unittest.main()
