"""#1279: a job that only renumbers filaments slices its source project remapped, verified with the real Orca.

slice_route sends a per-part job that keeps the source project and only
renumbers its filaments through package_remap.remap_package instead of
rebuilding the package. These tests slice corpus projects through the
production entry point (orca_adapter.slice_model) on the AD5X with a
non-identity mapping, and read the produced G-code: every wall, infill,
support and painted region prints on the tool the mapping names, each tool
heats to its own spool's temperature, every tool purges into the prime
tower, the palette is the job's spools in tool order and the project's purge
matrix arrives permuted on both axes.

Skipped unless PRINT_HANDY_REAL_SLICER=1 (CI's Slicer test jobs).
"""
import json
import os
import re
import tempfile
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from print_handy.core import model_job, orca_adapter, source_colors

from tests.corpus.make_models import box, t_shape, write_3mf

from .test_orca_real import WORK

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")
STATE = Path(__file__).resolve().parents[1] / "pipeline" / "fixtures" / "printer_hardware.json"
CASES = Path(__file__).resolve().parents[1] / "corpus" / "cases"
# One material family, three different nozzle targets within the one-heater
# tolerance, so every tool's temperature is told apart in the G-code.
SLOT_MATERIALS = {"1": "PLA", "2": "Silk PLA", "3": "PLA+", "4": "PLA"}
WALLS = ("Outer wall", "Inner wall")
# Orca writes numbers without a leading zero (E.71485, E-.8).
NUMBER = r"-?(?:\d+\.?\d*|\.\d+)"


def job_state():
    state = json.loads(STATE.read_text())
    for slot, material in SLOT_MATERIALS.items():
        state["filament_slots"][slot]["material"] = material
    return state


def decisions_for(source, state, slot_of):
    """Exact-source per-part decisions: source filament N prints from slot slot_of[N]."""
    manifest = source_colors.inventory(source)

    def selection(slot):
        return model_job.resolve_filament_selection(state, dict(state["filament_slots"][str(slot)], slot=slot))

    choices = {}
    for row in source_colors.canonical_prompts(manifest):
        key = row["key"]
        number = (int(key.split(":")[1]) if key.startswith("filament:") else
                  next(p["source_extruder"] for p in manifest["parts"] if "part:" + p["key"] == key))
        choices[key] = selection(slot_of[number])
    return manifest, {"schema": "part_slots_v1", "mode": "per_part", "source_sha256": manifest["source_sha256"],
                      "choices": choices}


def edited(case, tmp, *, project=None, parts=None, palette=None):
    """A copy of a corpus project with project keys and part metadata added (the edits a designer makes)."""
    path = tmp / f"{case}.3mf"
    with zipfile.ZipFile(CASES / case / "model.3mf") as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    settings = json.loads(members["Metadata/project_settings.config"])
    if palette:
        settings["filament_colour"] = [color for color, _ in palette]
        settings["filament_type"] = [kind for _, kind in palette]
    settings.update(project or {})
    members["Metadata/project_settings.config"] = json.dumps(settings, indent=2).encode()
    for part_id, keys in (parts or {}).items():
        block = re.compile(rb'(<part id="' + part_id.encode() + rb'"[^>]*>)', re.S)
        extra = b"".join(b'<metadata key="%s" value="%s"/>' % (k.encode(), v.encode()) for k, v in keys.items())
        members["Metadata/model_settings.config"], count = block.subn(
            lambda m: m.group(1) + extra, members["Metadata/model_settings.config"], count=1)
        assert count == 1, f"part {part_id} not in the corpus project"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def slice_remapped(source, tmp, slot_of, state=None):
    state = state or job_state()
    _manifest, decisions = decisions_for(source, state, slot_of)
    folder = tmp / "slice"
    out, provenance, _configs = orca_adapter.slice_model(source, folder, state, color_decisions=decisions)
    return Path(out), provenance, folder


def header(gcode, key):
    match = re.search(rf"^; {key} = (.*)$", gcode, re.M)
    assert match, f"{key} missing from the G-code settings block"
    return match.group(1)


def moves(gcode):
    """Every extruding move as (tool, feature, x, y, z), and {tool: {nozzle targets set for it}}.

    A single-nozzle multi-material slice sets the next filament's nozzle
    target while it unloads the current one, before the T line selects it,
    and the start G-code sets the first tool's before the first T line: so
    the last target set before each T line belongs to the tool it selects.
    """
    tool, feature, x, y, z = None, None, 0.0, 0.0, 0.0
    relative, last_e, pending = True, 0.0, None
    temps, out = defaultdict(set), []
    for raw in gcode.splitlines():
        line = raw.split(";", 1)[0].strip() if not raw.startswith(";TYPE:") else raw
        if raw.startswith(";TYPE:"):
            feature = raw[len(";TYPE:"):].strip()
            continue
        if not line:
            continue
        word = line.split()[0]
        if re.fullmatch(r"T\d+", word):
            tool = int(word[1:])
            if pending is not None:
                temps[tool].add(pending)
                pending = None
            continue
        if word in ("M104", "M109"):
            target = re.search(r"\bS(\d+(?:\.\d+)?)", line)
            if target and float(target.group(1)) > 0:
                pending = float(target.group(1))
            continue
        if word == "M82":
            relative = False
        elif word == "M83":
            relative = True
        elif word == "G92":
            e = re.search(r"\bE(" + NUMBER + ")", line)
            if e:
                last_e = float(e.group(1))
        elif word in ("G0", "G1", "G2", "G3"):
            values = dict(re.findall(r"\b([XYZE])(" + NUMBER + ")", line))
            x = float(values.get("X", x))
            y = float(values.get("Y", y))
            z = float(values.get("Z", z))
            if "E" in values:
                e = float(values["E"])
                amount = e if relative else e - last_e
                last_e = last_e if relative else e
                if amount > 0 and tool is not None:
                    out.append((tool, feature, x, y, z))
    return out, temps


def part_at(geometry, x, y, margin=0.6):
    for row in geometry["parts"]:
        (x0, x1), (y0, y1) = row["bounds_mm"][0], row["bounds_mm"][1]
        if x0 - margin <= x <= x1 + margin and y0 - margin <= y <= y1 + margin:
            return row["part_key"]
    return None


def tools_by(moves_, geometry, features):
    """{part key: the tool printing most of these features there}, checked to print nearly all of them.

    The slicer anchors the first infill lines of a layer to the walls with
    the wall's filament (a handful of moves where a part's walls and infill
    differ), so a feature is read as the tool carrying at least 95% of its
    extruding moves in that part, never as every tool that touched it.
    """
    counts = defaultdict(Counter)
    for tool, feature, x, y, _z in moves_:
        if feature in features:
            part = part_at(geometry, x, y)
            if part is not None:
                counts[part][tool] += 1
    out = {}
    for part, seen in counts.items():
        tool, n = seen.most_common(1)[0]
        assert n >= 0.95 * sum(seen.values()), (part, features, dict(seen))
        out[part] = tool
    return out


def assert_common(gcode, provenance, folder, state, slot_of, filament_map):
    """Route, palette, per-tool temperatures and prime-tower purge for a remapped slice."""
    route = provenance["geometry"]["slice_route"]
    assert route["route"] == "remap", route
    assert {int(k): v for k, v in route["filament_map"].items()} == filament_map
    tools = sorted(int(t) for t in provenance["tool_mapping"])
    used = sorted({int(t) for t in re.findall(r"^T(\d+)", gcode, re.M)})
    assert used == tools, f"G-code selects tools {used}, the job loads {tools}"
    # Palette: the job's spools in tool order (tool n-1 is job filament n).
    slot_by_tool = {int(t): int(s) for t, s in provenance["tool_mapping"].items()}
    colours = [c.strip().upper() for c in header(gcode, "filament_colour").split(";")]
    expected = ["#" + state["filament_slots"][str(slot_by_tool[t])]["hex"].upper().lstrip("#") for t in tools]
    assert colours == expected
    for number, slot in slot_of.items():
        assert slot_by_tool[filament_map[number] - 1] == slot, f"source filament {number} must print from slot {slot}"
    moves_, temps = moves(gcode)
    targets = set()
    for tool in tools:
        profile = json.loads((folder / f"filament_tool_{tool}.json").read_text())
        want = {float(profile[key][0]) for key in ("nozzle_temperature", "nozzle_temperature_initial_layer")}
        assert temps[tool] and temps[tool] <= want, f"tool {tool} heats to {temps[tool]}, its spool profile says {want}"
        targets |= want
    assert len(targets) > 1, "the test spools must differ in temperature to tell tools apart"
    tower = {tool for tool, feature, *_ in moves_ if feature == "Prime tower"}
    assert tower == set(tools), f"tools {sorted(set(tools) - tower)} never purge into the prime tower"
    return moves_


def test_remapped_bambu_project_prints_walls_infill_palette_temperatures_and_purge_on_the_mapped_filaments():
    """Corpus four-colors-one-plate with a designer's purge matrix and per-object feature filaments,
    remapped 1->slot 3, 2->slot 1, 3->slot 4, 4->slot 2."""
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        tmp = Path(tmp)
        matrix = [str(0 if i == j else 100 + 10 * i + j) for i in range(4) for j in range(4)]
        # Part 1 (part 2, filament 1) has its walls on filament 2; Part 3
        # (part 6, filament 3) has its sparse infill on filament 4, in the
        # OrcaSlicer 2.5 spelling the real slicer reads.
        source = edited("four-colors-one-plate", tmp, project={"flush_volumes_matrix": matrix},
                        parts={"2": {"outer_wall_filament_id": "2", "inner_wall_filament_id": "2"},
                               "6": {"sparse_infill_filament_id": "4"}})
        slot_of = {1: 3, 2: 1, 3: 4, 4: 2}
        filament_map = {1: 3, 2: 1, 3: 4, 4: 2}
        state = job_state()
        out, provenance, folder = slice_remapped(source, tmp, slot_of, state)
        gcode = out.read_text(errors="replace")
        moves_ = assert_common(gcode, provenance, folder, state, slot_of, filament_map)
        geometry = provenance["geometry"]
        filament_of = {p["key"]: p["source_extruder"] for p in source_colors.inventory(source)["parts"]}
        tool = {n: filament_map[n] - 1 for n in filament_map}
        walls = tools_by(moves_, geometry, WALLS)
        infill = tools_by(moves_, geometry, ("Sparse infill",))
        solid = tools_by(moves_, geometry, ("Internal solid infill", "Bottom surface", "Top surface"))
        assert set(walls) == set(filament_of), "every part prints walls"
        for part, number in filament_of.items():
            wall_filament = 2 if number == 1 else number
            assert walls[part] == tool[wall_filament], (part, walls[part])
            infill_filament = 4 if number == 3 else number
            if part in infill:
                assert infill[part] == tool[infill_filament], (part, infill[part])
            assert solid[part] == tool[number], (part, solid[part])
        assert next(k for k, n in filament_of.items() if n == 3) in infill, "Part 3 prints sparse infill"
        # The designer's purge matrix, permuted on both axes: row/column n-1 is
        # job filament n. Orca reports the volumes it purges, the matrix
        # times flush_multiplier, rounded to whole cubic millimeters.
        source_of = {job: number for number, job in filament_map.items()}
        want = [float(matrix[(source_of[i + 1] - 1) * 4 + (source_of[j + 1] - 1)]) for i in range(4) for j in range(4)]
        multiplier = float(header(gcode, "flush_multiplier"))
        got = [float(v) for v in header(gcode, "flush_volumes_matrix").split(",")]
        assert len(got) == 16 and all(abs(g - w * multiplier) <= 0.5 for g, w in zip(got, want)), (got, want)
        # A slice of the unpermuted matrix would differ: the permutation is what the check sees.
        assert any(abs(g - float(m) * multiplier) > 0.5 for g, m in zip(got, matrix))


def test_remapped_bambu_project_prints_supports_on_the_designated_filament():
    """The corpus needs-supports T bracket (filament 1) beside a block on filament 2, with the designer's
    support filament 2, swapped. Built with the corpus generator: a filament the project names only for
    supports keeps the rebuild (it is loaded only when supports are generated), so filament 2 also
    prints a part here."""
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        tmp = Path(tmp)
        source = tmp / "supports.3mf"
        write_3mf(source, [("T bracket", 1, t_shape(60, 12, 40, 6, 30), (80, 90)),
                           ("Block", 2, box(12, 12, 6), (170, 170))],
                  [("Plate 1", [0, 1])], [("#FFFFFF", "PLA"), ("#101010", "PLA")], support=True,
                  project_extra={"support_filament": "2", "support_interface_filament": "2"})
        slot_of = {1: 2, 2: 1}
        filament_map = {1: 2, 2: 1}
        state = job_state()
        out, provenance, folder = slice_remapped(source, tmp, slot_of, state)
        gcode = out.read_text(errors="replace")
        moves_ = assert_common(gcode, provenance, folder, state, slot_of, filament_map)
        support = {tool for tool, feature, *_ in moves_ if feature and feature.startswith("Support")}
        assert support == {filament_map[2] - 1}, f"supports print on tools {support}"
        walls = tools_by(moves_, provenance["geometry"], WALLS)
        filament_of = {p["key"]: p["source_extruder"] for p in source_colors.inventory(source)["parts"]}
        assert walls == {part: filament_map[n] - 1 for part, n in filament_of.items()}, walls


def test_remapped_height_color_changes_print_on_the_mapped_filaments():
    """Corpus height-color-changes (filament 1, then 2 above 4 mm, then 3 above 8 mm), remapped 1->3, 2->1, 3->2."""
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        tmp = Path(tmp)
        source = tmp / "height.3mf"
        source.write_bytes((CASES / "height-color-changes" / "model.3mf").read_bytes())
        slot_of = {1: 3, 2: 1, 3: 2}
        filament_map = {1: 3, 2: 1, 3: 2}
        state = job_state()
        out, provenance, folder = slice_remapped(source, tmp, slot_of, state)
        gcode = out.read_text(errors="replace")
        moves_ = assert_common(gcode, provenance, folder, state, slot_of, filament_map)
        bands = defaultdict(set)
        for tool, feature, _x, _y, z in moves_:
            if feature in WALLS:
                bands[1 if z <= 4.0 + 1e-6 else 2 if z <= 8.0 + 1e-6 else 3].add(tool)
        assert dict(bands) == {n: {filament_map[n] - 1} for n in (1, 2, 3)}, dict(bands)


def _tool_extrusion_by_layer(moves_):
    out = defaultdict(lambda: defaultdict(int))
    for tool, feature, _x, _y, z in moves_:
        if feature != "Prime tower":
            out[round(z, 2)][tool] += 1
    return {z: dict(tools) for z, tools in out.items()}


def test_remapped_prusa_project_prints_painted_regions_and_base_on_the_mapped_filaments():
    """Corpus prusa-mmu-project (a banner half painted with extruder 2), sliced as authored and swapped.

    Paint has no geometry the test can name, so the swapped slice is compared
    with the identity slice: every layer extrudes the same amount on the
    swapped tools, and the two tools are both used.
    """
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp:
        tmp = Path(tmp)
        source = tmp / "prusa.3mf"
        source.write_bytes((CASES / "prusa-mmu-project" / "model.3mf").read_bytes())
        state = job_state()
        runs = {}
        for name, slot_of in (("identity", {1: 1, 2: 2}), ("swapped", {1: 2, 2: 1})):
            (tmp / name).mkdir()
            out, provenance, folder = slice_remapped(source, tmp / name, slot_of, state)
            gcode = out.read_text(errors="replace")
            runs[name] = _tool_extrusion_by_layer(
                assert_common(gcode, provenance, folder, state, slot_of, dict(slot_of)))
        identity, swapped = runs["identity"], runs["swapped"]
        assert set(identity) == set(swapped)
        assert any(len(tools) == 2 for tools in identity.values()), "the paint must put two tools on a layer"
        swap = {0: 1, 1: 0}
        for z, tools in identity.items():
            assert swapped[z] == {swap[t]: n for t, n in tools.items()}, (z, tools, swapped[z])
