"""Full source-color manifest parity for deterministic scaled 3MFs (#1715)."""
import json
import os
import zipfile

import pytest

from print_handy.core import painted_geometry as pg
from print_handy.core import source_colors as sc


_MODEL = "3D/3dmodel.model"
_PALETTE = ["#FF0000", "#00FF00", "#0000FF", "#FFFF00"]


def _paint_code(state):
    # TriangleSelector nibble encoding used by painted_geometry.decode_states.
    return f"{state << 2:X}" if state < 3 else f"{state - 3:X}C"


def _scaled_source(path, *, parts, triangles_per_part, plates):
    """Write a compact deterministic inventory package, scaling only triangle rows."""
    objects, settings = [], ["<config>"]
    assignments = []
    for part_index in range(parts):
        object_id = str(2 * part_index + 2)
        rows = []
        for triangle in range(triangles_per_part):
            # Fixed modular distribution: 80% painted, spread over 3/4 states.
            if triangle % 5:
                state = 1 + ((triangle + part_index) % 3)
                attr = f' paint_color="{_paint_code(state)}"'
            else:
                attr = ""
            rows.append(f'<triangle v1="0" v2="1" v3="2"{attr}/>' )
        mesh = ('<mesh><vertices><vertex x="0" y="0" z="0"/>'
                '<vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>'
                f'<triangles>{"".join(rows)}</triangles></mesh>')
        objects.append(f'<object id="{object_id}" type="model">{mesh}</object>')
        assembly_id = str(2 * part_index + 1)
        objects.append(f'<object id="{assembly_id}" type="model"><components>'
                       f'<component objectid="{object_id}"/></components></object>')
        assignments.append((part_index, assembly_id))
        settings.append(f'<object id="{assembly_id}"><metadata key="name" value="Part {part_index + 1}"/>'
                        f'<metadata key="extruder" value="{part_index % 4 + 1}"/>'
                        f'<part id="{object_id}" subtype="normal_part"><metadata key="name" '
                        f'value="Part {part_index + 1}"/><metadata key="extruder" '
                        f'value="{part_index % 4 + 1}"/></part></object>')
    for plate_number, part_indexes in enumerate(plates, 1):
        instances = "".join(
            f'<model_instance><metadata key="object_id" value="{assignments[i][1]}"/></model_instance>'
            for i in part_indexes
        )
        settings.append(f'<plate><metadata key="plater_id" value="{plate_number}"/>'
                        f'<metadata key="plater_name" value="Plate {plate_number}"/>{instances}</plate>')
    settings.append("</config>")
    model = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources>{"".join(objects)}</resources><build>'
             + "".join(f'<item objectid="{assembly}"/>' for _, assembly in assignments)
             + '</build></model>')
    project = {"filament_colour": _PALETTE, "filament_type": ["PLA"] * len(_PALETTE)}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.writestr(_MODEL, model)
        archive.writestr("Metadata/model_settings.config", "\n".join(settings))
        archive.writestr("Metadata/project_settings.config", json.dumps(project, sort_keys=True))
    return path


@pytest.mark.parametrize(
    ("parts", "triangles", "plates"),
    [
        (3, 120, [[0, 1], [2]]),
        (4, 60, [[0, 1, 2, 3]]),
    ],
    ids=["primary-scale-shape", "second-scale-shape"],
)
def test_full_inventory_manifest_matches_native_and_python(tmp_path, monkeypatch, parts, triangles, plates):
    source = _scaled_source(tmp_path / "scaled.3mf", parts=parts,
                            triangles_per_part=triangles, plates=plates)
    monkeypatch.setattr(pg, "_load_native_paint_census", lambda: None)
    python_manifest = sc.inventory(source)
    assert python_manifest["painted"] is True

    # Resolve through the production loader, which is cached and can be absent in
    # ordinary development installs; strict CI can require it explicitly.
    monkeypatch.undo()
    native_census = pg._load_native_paint_census()
    if native_census is None:
        if os.environ.get("PRINT_HANDY_PAINT_CENSUS_REQUIRE_NATIVE") == "1":
            pytest.fail("native paint census must be built for strict parity jobs")
        pytest.skip("optional native paint census is not built")
    with monkeypatch.context() as patch:
        patch.setattr(pg, "_load_native_paint_census", lambda: native_census)
        native_manifest = sc.inventory(source)
    assert native_manifest == python_manifest
    assert native_manifest["parts"]
    assert native_manifest["plate_usage"].keys() == python_manifest["plate_usage"].keys()
