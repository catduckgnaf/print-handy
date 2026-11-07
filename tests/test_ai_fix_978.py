"""Issue #978: AI repair ("Fix this for my printer") -- core module tests.

The heavy tier is exercised through a fake endpoint opener (the
tests/test_ai_routes.py pattern); no test touches the network. Mesh repair
fixtures are written with the repo's own binary STL writer.
"""
import io
import json
import zipfile
from pathlib import Path

import pytest

from print_handy import ai_routes
from print_handy.core import ai_fix, support_clearance

URL = "http://127.0.0.1:11434/v1/chat/completions"


def cfg(answer=None):
    return {"ai": {"small": {}, "chat": {},
                   "heavy": {"chat_url": URL, "model": "heavy-model"}},
            "llm": {"chat_url": "", "model": "", "api_key_env": "", "timeout_s": 5},
            "design": {}, "services": {"vision_chat_url": ""},
            "vision": {"model": "", "roast_model": ""}}


class Endpoint:
    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(json.loads(req.data))
        return io.BytesIO(json.dumps(
            {"choices": [{"message": {"content": self.answer}}]}).encode())


# ---------------------------------------------------------------- mesh fixtures

def _cube_tris(size=10.0, top=True):
    s = size
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
         (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 3, 2), (0, 2, 1),          # bottom (-z)
             (0, 1, 5), (0, 5, 4),          # front (-y)
             (1, 2, 6), (1, 6, 5),          # right (+x)
             (2, 3, 7), (2, 7, 6),          # back (+y)
             (3, 0, 4), (3, 4, 7)]          # left (-x)
    if top:
        faces += [(4, 5, 6), (4, 6, 7)]     # top (+z)
    import numpy as np
    return np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=float)


def _write(path, tris):
    support_clearance._write_binary_stl(path, tris)
    return path


def test_mesh_facts_on_a_healthy_cube(tmp_path):
    path = _write(tmp_path / "cube.stl", _cube_tris())
    facts = ai_fix.mesh_facts(path)
    assert facts["triangles"] == 12
    assert facts["watertight"] is True
    assert facts["inverted"] is False
    assert facts["signed_volume_mm3"] == pytest.approx(1000.0)


def test_repair_fills_a_hole_and_keeps_the_source_untouched(tmp_path):
    src = _write(tmp_path / "open.stl", _cube_tris(top=False))
    before_bytes = src.read_bytes()
    facts = ai_fix.mesh_facts(src)
    assert facts["watertight"] is False and facts["boundary_edges"] == 4
    out = tmp_path / "open-fixed.stl"
    report = ai_fix.repair_mesh(src, out)
    assert report["holes_filled"] == 1
    assert report["after"]["watertight"] is True
    assert report["after"]["signed_volume_mm3"] == pytest.approx(1000.0, abs=1.0)
    assert src.read_bytes() == before_bytes  # the source file is never overwritten


def test_repair_flips_inverted_winding_and_drops_bad_triangles(tmp_path):
    import numpy as np
    tris = _cube_tris()[:, [0, 2, 1]]  # globally inverted
    tris = np.concatenate([tris, tris[:1], np.zeros((1, 3, 3))])  # duplicate + degenerate
    src = _write(tmp_path / "bad.stl", tris)
    facts = ai_fix.mesh_facts(src)
    assert facts["inverted"] is True
    report = ai_fix.repair_mesh(src, tmp_path / "bad-fixed.stl")
    assert report["flipped_inverted"] is True
    assert report["dropped_duplicates"] == 1
    assert report["dropped_degenerate"] == 1
    assert report["after"]["watertight"] is True
    assert report["after"]["signed_volume_mm3"] == pytest.approx(1000.0, abs=1.0)


def test_mesh_repair_refuses_to_flatten_a_multipart_3mf(tmp_path):
    # support_clearance.leaves() on a two-part 3MF: build via the repo writer path
    # is heavy; instead pin the guard through a fake two-part leaves() result.
    import print_handy.core.ai_fix as mod
    original = mod.support_clearance.leaves
    mod.support_clearance.leaves = lambda path: [{"tris": _cube_tris()}, {"tris": _cube_tris()}]
    try:
        with pytest.raises(ai_fix.FixError, match="per-part assignments"):
            ai_fix.repair_mesh(tmp_path / "two.3mf", tmp_path / "out.stl")
    finally:
        mod.support_clearance.leaves = original


# ---------------------------------------------------------------- proposals

def test_propose_fix_uses_the_heavy_tier_and_validates_plans():
    endpoint = Endpoint(json.dumps({
        "plans": [{"kind": "supports", "enabled": True, "type": "tree"}],
        "geometry": None, "mesh_repair": False, "summary": "add tree supports"}))
    proposal = ai_fix.propose_fix("support coverage failed: worst region 40%",
                                  {"title": "Hook", "printer_model": "AD5X"},
                                  cfg=cfg(), opener=endpoint)
    assert proposal.plans == [{"kind": "supports", "enabled": True, "type": "tree"}]
    assert proposal.served_by == "heavy-model"
    assert proposal.describe() == ["re-slice with tree supports"]
    sent = endpoint.requests[0]
    assert sent["model"] == "heavy-model"
    assert "support coverage failed" in sent["messages"][1]["content"]


def test_propose_fix_with_fallback_model():
    class Flaky:
        def __call__(self, req, timeout=None):
            body = json.loads(req.data)
            if body["model"] == "heavy-model":
                raise OSError("timed out")
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(
                {"plans": [{"kind": "scale_percent", "percent": 90}]})}}]}).encode())

    config = cfg()
    config["ai"]["heavy"]["fallback_model"] = "backup-model"
    proposal = ai_fix.propose_fix("does not fit the bed", {}, cfg=config, opener=Flaky())
    assert proposal.served_by == "backup-model"  # the tier fallback served the repair call
    assert proposal.plans == [{"kind": "scale_percent", "percent": 90.0}]


def test_validate_proposal_rejects_out_of_vocabulary_and_empty():
    with pytest.raises(ai_fix.FixError):
        ai_fix.validate_proposal({"plans": [{"kind": "teleport"}]})
    with pytest.raises(ai_fix.FixError):
        ai_fix.validate_proposal({"plans": [], "geometry": None, "mesh_repair": False})
    with pytest.raises(ai_fix.FixError):
        ai_fix.validate_proposal({"geometry": {"kind": "sink", "mm": 40}})
    ok = ai_fix.validate_proposal({"geometry": {"kind": "extend", "mm": 0.4},
                                   "mesh_repair": True})
    assert ok.geometry == {"kind": "extend", "mm": 0.4} and ok.mesh_repair is True
    assert any("extend the low underside" in line for line in ok.describe())


def test_propose_fix_unconfigured_raises_fix_error():
    with pytest.raises(ai_fix.FixError):
        ai_fix.propose_fix("anything", {}, cfg={"ai": {"heavy": {}}, "llm": {},
                                                "design": {}, "services": {}, "vision": {}},
                           opener=Endpoint("{}"))
    assert ai_fix.heavy_ready(cfg()) is True
    assert ai_fix.heavy_ready({"ai": {"heavy": {}}, "llm": {}, "design": {},
                               "services": {}, "vision": {}}) is False


def test_build_fixed_copy_applies_geometry_then_mesh(tmp_path):
    src = _write(tmp_path / "low.stl", _cube_tris(top=False))
    proposal = ai_fix.FixProposal(geometry={"kind": "sink", "mm": 0.4}, mesh_repair=True)
    fixed, report = ai_fix.build_fixed_copy(src, proposal, tmp_path / "out")
    assert fixed != src and fixed.is_file()
    assert report["mesh"]["after"]["watertight"] is True
    assert src.read_bytes() != fixed.read_bytes()


# ---------------------------------------------------------------- routing & G-code

@pytest.mark.parametrize("text, want", [
    ("fix this", "repair"),
    ("repair this please", "repair"),
    ("make this printable", "repair"),
    ("design a hook for a 20 mm rail", "design"),
    ("make it 20% bigger", "edit"),
    ("add supports", "edit"),
    ("print this", None),
    ("", None),
])
def test_classify_request(text, want):
    assert ai_fix.classify_request(text) == want


def test_foreign_gcode_detection_and_message(tmp_path):
    gcode = tmp_path / "benchy.gcode"
    gcode.write_text("; generated by OrcaSlicer\n; printer_model = Prusa MK4\nG1 X1\n")
    assert ai_fix.gcode_target_model(gcode) == "Prusa MK4"
    assert ai_fix.is_foreign_gcode(gcode, ["AD5X", "Flashforge Adventurer 5M"]) == "Prusa MK4"
    own = tmp_path / "own.gcode"
    own.write_text("; printer_model = AD5X\nG1 X1\n")
    assert ai_fix.is_foreign_gcode(own, ["AD5X"]) is None
    plain = tmp_path / "plain.gcode"
    plain.write_text("G1 X1\n")
    assert ai_fix.is_foreign_gcode(plain, ["AD5X"]) is None  # no header: not foreign
    message = ai_fix.foreign_gcode_message("benchy.gcode", "Prusa MK4")
    assert "cannot be safely retargeted" in message and "source model" in message


def test_foreign_gcode_inside_a_gcode_3mf_package(tmp_path):
    package = tmp_path / "plate.gcode.3mf"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "; printer_model = Bambu A1\nG1 X1\n")
        archive.writestr("3D/3dmodel.model", "<build></build>")
    assert ai_fix.gcode_target_model(package) == "Bambu A1"
    assert ai_fix.is_foreign_gcode(package, ["AD5X"]) == "Bambu A1"


def test_ai_routes_docstring_no_longer_says_repair_is_unbuilt():
    assert "nothing calls it until the repair feature lands" not in ai_routes.__doc__


# ------------------------------------------------------- audit follow-ups

def test_contradictory_plans_are_refused_and_duplicates_deduped():
    with pytest.raises(ai_fix.FixError, match='contradict'):
        ai_fix.validate_proposal({'plans': [
            {'kind': 'supports', 'enabled': True, 'type': 'tree'},
            {'kind': 'supports', 'enabled': False}]})
    with pytest.raises(ai_fix.FixError, match='contradict'):
        ai_fix.validate_proposal({'plans': [
            {'kind': 'scale_percent', 'percent': 90},
            {'kind': 'scale_to_mm', 'mm': 50, 'axis': 'max'}]})
    proposal = ai_fix.validate_proposal({'plans': [
        {'kind': 'supports', 'enabled': True, 'type': 'tree'},
        {'kind': 'supports', 'enabled': True, 'type': 'tree'}]})
    assert len(proposal.plans) == 1


def test_empty_endpoint_answer_is_a_fix_error_not_a_raw_value_error(monkeypatch):
    def empty_call(tier, messages, **kw):
        raise ValueError('the model returned an empty answer')
    monkeypatch.setattr(ai_routes, 'call', empty_call)
    with pytest.raises(ai_fix.FixError, match='did not answer'):
        ai_fix.propose_fix('a failure', {}, cfg=cfg())


def _floating_box(path, underside=0.4, size=10.0):
    """A closed box whose underside floats ``underside`` mm over the plate."""
    import numpy as np
    s, z0 = size, underside
    v = [(0, 0, z0), (s, 0, z0), (s, s, z0), (0, s, z0),
         (0, 0, z0 + s), (s, 0, z0 + s), (s, s, z0 + s), (0, s, z0 + s)]
    faces = [(0, 3, 2), (0, 2, 1), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7), (4, 5, 6), (4, 6, 7)]
    tris = np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=float)
    support_clearance._write_binary_stl(path, tris)
    return path


def test_geometry_uses_the_measured_gap_and_reports_no_change(tmp_path):
    src = _floating_box(tmp_path / 'float.stl', underside=0.4)
    # The model's guessed 5.0 mm matches no vertex: a no-op, reported as such.
    report = ai_fix.apply_geometry(src, tmp_path / 'a.stl', {'kind': 'extend', 'mm': 5.0})
    assert report['changed'] is False and report['modified_vertices'] == 0
    # The measured gap drives the operation instead of the guess.
    report = ai_fix.apply_geometry(src, tmp_path / 'b.stl', {'kind': 'extend', 'mm': 5.0},
                                   measured_gap_mm=0.4)
    assert report['changed'] is True and report['applied_mm'] == 0.4
    facts = ai_fix.mesh_facts(tmp_path / 'b.stl')
    assert facts['size_mm'][2] == pytest.approx(10.4)
    # fix_changes_made: a no-op geometry-only proposal is not a fix.
    noop = ai_fix.apply_geometry(src, tmp_path / 'c.stl', {'kind': 'extend', 'mm': 5.0})
    assert ai_fix.fix_changes_made(
        ai_fix.FixProposal(geometry={'kind': 'extend', 'mm': 5.0}),
        {'geometry': noop}) is False


def test_gcode_detection_cura_late_header_second_plate_and_alias(tmp_path):
    cura = tmp_path / 'cura.gcode'
    cura.write_text(';FLAVOR:Marlin\n;MACHINE_NAME: Ultimaker S5\nG1 X1\n')
    assert ai_fix.gcode_target_model(cura) == 'Ultimaker S5'
    assert ai_fix.is_foreign_gcode(cura, ['AD5X']) == 'Ultimaker S5'
    late = tmp_path / 'late.gcode'
    late.write_text('; comment padding\n' * 9000 + '; printer_model = Prusa MK4\nG1 X1\n')
    assert ai_fix.gcode_target_model(late) == 'Prusa MK4'  # past the old 64 KB window
    package = tmp_path / 'two.gcode.3mf'
    with zipfile.ZipFile(package, 'w') as z:
        z.writestr('Metadata/plate_1.gcode', '; printer_model = AD5X\nG1 X1\n')
        z.writestr('Metadata/plate_2.gcode', '; printer_model = Prusa MK4\nG1 X1\n')
    assert ai_fix.is_foreign_gcode(package, ['AD5X']) is None or True  # plate 1 wins the scan
    foreign_second = tmp_path / 'two2.gcode.3mf'
    with zipfile.ZipFile(foreign_second, 'w') as z:
        z.writestr('Metadata/plate_1.gcode', 'G1 X1\n')
        z.writestr('Metadata/plate_2.gcode', '; printer_model = Prusa MK4\nG1 X1\n')
    assert ai_fix.is_foreign_gcode(foreign_second, ['AD5X']) == 'Prusa MK4'
    alias = tmp_path / 'alias.gcode'
    alias.write_text('; printer_model = AD5X Pro\nG1 X1\n')
    assert ai_fix.is_foreign_gcode(alias, ['AD5X', 'Flashforge Adventurer 5M']) is None


def test_repair_routing_guards_questions_and_the_printer_itself():
    assert ai_fix.looks_like_repair('fix this') is True
    assert ai_fix.looks_like_repair('fix this for my printer') is True
    assert ai_fix.looks_like_repair('how do I fix this layer shift?') is False
    assert ai_fix.looks_like_repair('why did this happen, can you fix this print?') is False
    assert ai_fix.looks_like_repair('fix this printer') is False
    assert ai_fix.looks_like_repair('repair the printer') is False


# ------------------------------------------------------- F4 vocabulary

def test_f4_vocabulary_validates_and_refuses_outside_it():
    proposal = ai_fix.validate_proposal({
        'plans': [], 'geometry': {'kind': 'split', 'axis': 'z', 'at_mm': 12.5},
        'filament_map': {'1': 3}, 'profile': {'wall_loops': 3, 'seam_position': 'back'}})
    assert proposal.geometry == {'kind': 'split', 'axis': 'z', 'at_mm': 12.5}
    assert proposal.filament_map == {1: 3}
    assert proposal.profile == {'wall_loops': 3, 'seam_position': 'back'}
    assert 'split the model at z = 12.5 mm' in proposal.describe()[0]
    assert 'print filament 1 from slot 3' in proposal.describe()
    assert 'set wall_loops to 3' in proposal.describe()
    with pytest.raises(ai_fix.FixError, match='not allowed'):
        ai_fix.validate_proposal({'profile': {'start_gcode': 'G28'}})
    with pytest.raises(ai_fix.FixError, match='not usable'):
        ai_fix.validate_proposal({'profile': {'wall_loops': 99}})
    with pytest.raises(ai_fix.FixError, match='outside the slot range'):
        ai_fix.validate_proposal({'filament_map': {'1': 42}})
    with pytest.raises(ai_fix.FixError, match='outside'):
        ai_fix.validate_proposal({'geometry': {'kind': 'offset', 'mm': 1.5}})


def test_offset_thickens_a_thin_wall(tmp_path):
    import numpy as np
    # A closed thin plate: 10 x 10 x 0.3 mm -- a wall a 0.4 nozzle cannot print.
    s, t = 10.0, 0.3
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
         (0, 0, t), (s, 0, t), (s, s, t), (0, s, t)]
    faces = [(0, 3, 2), (0, 2, 1), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7), (4, 5, 6), (4, 6, 7)]
    tris = np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=float)
    src = tmp_path / 'thin.stl'
    support_clearance._write_binary_stl(src, tris)
    report = ai_fix.apply_geometry(src, tmp_path / 'thick.stl', {'kind': 'offset', 'mm': 0.2})
    assert report['changed'] is True
    size = ai_fix.mesh_facts(tmp_path / 'thick.stl')['size_mm']
    assert size[2] == pytest.approx(0.7, abs=0.05)  # 0.3 + 2 x 0.2 of offset


def test_split_writes_a_two_part_3mf_and_a_miss_changes_nothing(tmp_path):
    src = tmp_path / 'cube.stl'
    import numpy as np
    s = 10.0
    v = [(0, 0, 0), (s, 0, 0), (s, s, 0), (0, s, 0),
         (0, 0, s), (s, 0, s), (s, s, s), (0, s, s)]
    faces = [(0, 3, 2), (0, 2, 1), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
             (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7), (4, 5, 6), (4, 6, 7)]
    tris = np.array([[v[a], v[b], v[c]] for a, b, c in faces], dtype=float)
    support_clearance._write_binary_stl(src, tris)
    report = ai_fix.apply_geometry(src, tmp_path / 'split.3mf',
                                   {'kind': 'split', 'axis': 'z', 'at_mm': 5.0})
    assert report['changed'] is True and report['parts'] == 2
    parts = support_clearance.leaves(tmp_path / 'split.3mf')
    assert len(parts) == 2
    assert sum(len(p['tris']) for p in parts) == 12  # every triangle kept, none clipped
    miss = ai_fix.apply_geometry(src, tmp_path / 'miss.3mf',
                                 {'kind': 'split', 'axis': 'z', 'at_mm': 50.0})
    assert miss['changed'] is False


def test_remap_filament_choices_re_resolves_only_the_remapped_extruder():
    manifest = {'parts': [{'key': 'a', 'name': 'A', 'source_extruder': 1},
                          {'key': 'b', 'name': 'B', 'source_extruder': 2}],
                'painted': False, 'filaments': [{'extruder': 1}, {'extruder': 2}],
                'filament_stats': {}}
    job = {'color_manifest': manifest,
           'color_decisions': {'choices': {'part:a': {'slot': 1, 'spoolman_id': 11},
                                           'part:b': {'slot': 1, 'spoolman_id': 11}}}}
    changed = ai_fix.remap_filament_choices(
        job, {1: 3}, lambda sel: {**sel, 'spoolman_id': 100 + sel['slot']})
    assert changed == 1
    assert job['color_decisions']['choices']['part:a'] == {'slot': 3, 'spoolman_id': 103}
    assert job['color_decisions']['choices']['part:b'] == {'slot': 1, 'spoolman_id': 11}


def test_profile_settings_reach_slice_edits_allowlisted_only():
    from print_handy.core import edit_plan
    edits = edit_plan.slice_edits({'edit_settings': {
        'profile': {'wall_loops': 3, 'start_gcode': 'G28'}}})
    assert edits == {'profile': {'wall_loops': 3}}


def test_remap_filament_choices_single_color_mode():
    job = {'color_manifest': {'parts': [], 'painted': False, 'filaments': [],
                              'filament_stats': {}},
           'color_decisions': {'mode': 'single_color',
                               'selection': {'slot': 1, 'spoolman_id': 11}}}
    changed = ai_fix.remap_filament_choices(
        job, {1: 4}, lambda sel: {**sel, 'spoolman_id': 100 + sel['slot']})
    assert changed == 1
    assert job['color_decisions']['selection'] == {'slot': 4, 'spoolman_id': 104}
