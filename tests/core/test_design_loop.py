"""Offline tests for the design self-check, repair, question and undo loop.

The model is a scripted fake and the renderer is an injected runner that
writes a real box STL whose size comes from the source's own w/d/h
parameters (plus z0 to lift it, pieces=2 for two separate boxes, and a literal SYNTAX token to fail like a parser error), so
every check here measures a real mesh. The same scenarios were run against
the real OpenSCAD 2021.01 binary while the loop was built; the CI
real-binary tests in test_design.py cover the renderer itself.
"""
import asyncio
import subprocess
from pathlib import Path

import pytest

from print_handy.core import design

CFG = {"design": {"enabled": True, "chat_url": "", "model": "", "api_key_env": "",
                  "timeout_s": 180, "repair_attempts": 2},
       "printer": {"bed_mm": [220, 220], "max_print_height_mm": 250}}


def part(w, d, h, *, size=None, extra="", part_line="Test plate", assumes=("PLA",)):
    size = size or (w, d, h)
    head = [f"// Part: {part_line}", f"// Size: {size[0]} x {size[1]} x {size[2]} mm"]
    head += [f"// Assumes: {text}" for text in assumes]
    return "\n".join(head + [f"w = {w}; // width", f"d = {d}; // depth",
                             f"height = {h}; // overall height", extra,
                             "cube([w, d, height]);", ""])


def _box(dx, dy, dz, ox=0.0, oy=0.0, oz=0.0):
    v = [(ox, oy, oz), (ox + dx, oy, oz), (ox + dx, oy + dy, oz), (ox, oy + dy, oz),
         (ox, oy, oz + dz), (ox + dx, oy, oz + dz), (ox + dx, oy + dy, oz + dz),
         (ox, oy + dy, oz + dz)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (3, 0, 4), (3, 4, 7)]
    return [[v[a], v[b], v[c]] for a, b, c in faces]


class BoxRunner:
    """A fake openscad that renders the source's w/d/h box and counts renders."""

    def __init__(self):
        self.renders = 0

    def __call__(self, cmd, **kwargs):
        from print_handy.core.geometry_layout import write_binary_stl
        self.renders += 1
        scad = Path(cmd[-1]).read_text(encoding="utf-8")
        if "SYNTAX" in scad:
            return subprocess.CompletedProcess(
                cmd, 1, "", "ERROR: Parser error in file x.scad, line 3: syntax error")
        p = design.scad_params(scad)
        tris = _box(p.get("w", 10), p.get("d", 10), p.get("height", 10), oz=p.get("z0", 0))
        if p.get("pieces", 1) >= 2:
            tris += _box(5, 5, 5, ox=p.get("w", 10) + 10)
        write_binary_stl(tris, Path(cmd[cmd.index("-o") + 1]))
        return subprocess.CompletedProcess(cmd, 0, "", "")


def scripted(replies):
    calls = []

    def complete(messages):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    complete.calls = calls
    return complete


def engine(tmp_path, complete, cfg=CFG):
    runner = BoxRunner()
    eng = design.DesignEngine(tmp_path / "cache", complete, cfg=cfg, runner=runner,
                              preview=lambda stl: None)
    return eng, runner


def run(coro):
    return asyncio.run(coro)


def user_prompt(call):
    return call[1]["content"]


# ------------------------------------------------------------------ notes and checks

def test_design_notes_read_the_header_and_parameter_comments():
    source = part(40, 20, 3, assumes=("PLA", "M4 screws"))
    notes = design.design_notes(source)
    assert notes == {"part": "Test plate", "size_mm": [40.0, 20.0, 3.0],
                     "assumptions": ["PLA", "M4 screws"]}
    assert design.scad_param_notes(source) == {"w": "width", "d": "depth",
                                               "height": "overall height"}
    assert design.design_notes("cube([1, 1, 1]);") == {"part": None, "size_mm": None,
                                                       "assumptions": []}
    assert design.design_notes("// Size: 25×12.5×4 mm\n")["size_mm"] == [25.0, 12.5, 4.0]


def _info(dims, low=(0.0, 0.0, 0.0), shells=1, watertight=True):
    return {"dimensions_mm": list(dims), "bounds_min_mm": list(low), "faces": 12,
            "watertight": watertight, "manifold": watertight, "shell_count": shells,
            "non_manifold_edges": 0, "winding_consistent": True, "degenerate_triangles": 0,
            "abs_volume_mm3": 100.0}


def test_check_design_passes_a_matching_part_on_the_bed():
    notes = {"size_mm": [40.0, 20.0, 3.0]}
    assert design.check_design(_info([40, 20, 3]), notes=notes, cfg=CFG) == []
    # Curves are polygons: a 20 mm cylinder at $fn=48 measures 19.96 mm.
    assert design.check_design(_info([40, 19.96, 3]), notes=notes, cfg=CFG) == []
    # The same box declared in another axis order is the same part.
    assert design.check_design(_info([20, 40, 3]), notes=notes, cfg=CFG) == []


def test_check_design_names_each_problem_in_measured_numbers():
    texts = lambda findings: " | ".join(text for _level, text in findings)
    wrong = design.check_design(_info([80, 20, 3]), notes={"size_mm": [40, 20, 3]}, cfg=CFG)
    assert wrong == [("warn", "it measures 80 x 20 x 3 mm, not the 40 x 20 x 3 mm on the "
                              "Size line")]
    floating = design.check_design(_info([10, 10, 10], low=(0, 0, 5)), cfg=CFG)
    assert "lowest point is at Z=5 mm" in texts(floating)
    two = design.check_design(_info([30, 10, 10], shells=2), cfg=CFG)
    assert two[0][0] == "warn" and "2 separate pieces" in texts(two)
    big = design.check_design(_info([300, 20, 3]), cfg=CFG)
    assert big[0][0] == "warn" and "does not fit the printer" in texts(big)
    tall = design.check_design(_info([20, 20, 300]), cfg=CFG)
    assert "300 mm tall on a 250 mm build height" in texts(tall)
    leaky = design.check_design(_info([10, 10, 10], watertight=False), cfg=CFG)
    assert leaky[0] == ("fail", "the mesh has problems: not watertight")
    # A deterministic edit leaves the Size line stale on purpose.
    assert design.check_design(_info([80, 20, 3]), notes={"size_mm": [40, 20, 3]}, cfg=CFG,
                               declared=False) == []


def test_build_volume_reads_the_bed_and_height():
    assert design.build_volume(CFG) == [220.0, 220.0, 250.0]
    assert design.build_volume({"printer": {"bed_mm": [180, 180]}}) == [180.0, 180.0, None]
    assert design.build_volume({}) is None


# ------------------------------------------------------------------ the prompt

def test_prompt_asks_for_a_parametric_fdm_aware_file_with_a_checkable_header(tmp_path):
    fake = scripted([part(40, 20, 3)])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a 40x20x3 plate")
    run(eng.run_turn(session, "a 40x20x3 plate"))
    system, user = fake.calls[0][0]["content"], user_prompt(fake.calls[0])
    for needle in ("// Part:", "// Size:", "// Assumes:", "named top-level variable",
                   "width, length and height", "45 degrees", "Z=0", "0.2-0.3 mm", "OpenSCAD 2021.01", "QUESTION:",
                   "may NOT use import"):
        assert needle in system
    assert "Printer build volume: 220 x 220 x 250 mm" in user


# ------------------------------------------------------------------ verify and repair

def test_a_wrong_size_is_fed_back_and_repaired_before_anything_is_shown(tmp_path):
    fake = scripted([part(80, 20, 3, size=(40, 20, 3)), part(40, 20, 3)])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a 40x20x3 plate")
    result = run(eng.run_turn(session, "a 40x20x3 plate"))
    assert len(fake.calls) == 2
    repair = user_prompt(fake.calls[1])
    assert "failed these checks" in repair
    assert "it measures 80 x 20 x 3 mm, not the 40 x 20 x 3 mm on the Size line" in repair
    assert "w = 80" in repair  # the model sees the file it is fixing
    assert result["geometry"]["dimensions_mm"] == [40.0, 20.0, 3.0]
    assert result["findings"] == [] and result["attempts"] == 2
    assert result["notes"]["part"] == "Test plate"
    folder = eng.root / session["id"]
    assert (folder / "turn-1.scad").is_file() and (folder / "turn-1.stl").is_file()
    assert not list(folder.glob("*try*"))  # attempt files never outlive the turn
    assert design.load_session(eng.root, session["id"])["turns"] == 1


def test_repairs_are_bounded_and_the_best_attempt_is_kept_with_its_findings(tmp_path):
    floating_two = part(40, 20, 3, extra="z0 = 4;\npieces = 2;")
    floating = part(40, 20, 3, extra="z0 = 4;")
    fake = scripted([floating_two, floating, floating_two])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a plate")
    result = run(eng.run_turn(session, "a plate"))
    assert len(fake.calls) == 1 + CFG["design"]["repair_attempts"]  # never more
    assert result["attempts"] == 3
    assert [text for _level, text in result["findings"]] == [
        "the part's lowest point is at Z=4 mm, not on the bed at Z=0"]
    assert "z0 = 4;\n" in result["scad"] and "pieces" not in result["scad"]


def test_render_errors_and_checks_share_one_repair_budget(tmp_path):
    fake = scripted(["SYNTAX\n" + part(40, 20, 3), part(80, 20, 3, size=(40, 20, 3)),
                     part(40, 20, 3)])
    eng, runner = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a plate")
    result = run(eng.run_turn(session, "a plate"))
    assert len(fake.calls) == 3 and runner.renders == 3
    assert "Parser error" in user_prompt(fake.calls[1])
    assert "failed these checks" in user_prompt(fake.calls[2])
    assert result["findings"] == []


def test_an_unprintable_mesh_is_never_handed_over(tmp_path, monkeypatch):
    # validate_stl already rejects most broken meshes (no enclosed volume);
    # a mesh that still encloses volume but has non-manifold edges is what
    # reaches the checks, so the measured facts are made to say so here.
    real = design.validate_stl

    def non_manifold(path):
        info = dict(real(path))
        info.update(watertight=False, manifold=False, non_manifold_edges=3)
        return info

    monkeypatch.setattr(design, "validate_stl", non_manifold)
    fake = scripted([part(40, 20, 3)])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a plate")
    with pytest.raises(design.DesignError, match="not a printable solid: the mesh has problems: not watertight, non-manifold edges"):
        run(eng.run_turn(session, "a plate"))
    saved = design.load_session(eng.root, session["id"])
    assert saved["stl"] is None and saved["turns"] == 0
    assert not list((eng.root / session["id"]).glob("turn-*"))


def test_a_render_that_never_succeeds_still_fails_honestly(tmp_path):
    fake = scripted(["SYNTAX\ncube([1, 1, 1]);"])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a plate")
    with pytest.raises(design.DesignError, match="could not render the design after 3 attempt"):
        run(eng.run_turn(session, "a plate"))


# ------------------------------------------------------------------ one question at most

def test_one_blocking_question_then_the_answer_designs_the_part(tmp_path):
    fake = scripted(["QUESTION: How wide is the rail the hook clips onto?",
                     part(30, 25, 40, part_line="Rail hook")])
    eng, runner = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a hook for my rail")
    with pytest.raises(design.DesignQuestion) as asked:
        run(eng.run_turn(session, "a hook for my rail"))
    assert asked.value.question == "How wide is the rail the hook clips onto?"
    saved = design.load_session(eng.root, session["id"])
    assert saved["status"] == "awaiting_revision" and saved["turns"] == 0
    assert runner.renders == 0
    result = run(eng.run_turn(saved, "25 mm"))
    prompt = user_prompt(fake.calls[1])
    assert "Design request: a hook for my rail" in prompt
    assert "You asked: How wide is the rail" in prompt and "The user answered: 25 mm" in prompt
    assert "Change requested" not in prompt  # it is the first design, not an edit
    assert result["notes"]["part"] == "Rail hook"


def test_a_second_question_is_refused_and_answered_by_an_assumption(tmp_path):
    fake = scripted(["QUESTION: Which rail?", "QUESTION: Really, which rail?",
                     part(30, 25, 40)])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a hook")
    with pytest.raises(design.DesignQuestion):
        run(eng.run_turn(session, "a hook"))
    result = run(eng.run_turn(design.load_session(eng.root, session["id"]), "the usual"))
    assert len(fake.calls) == 3
    assert "no further questions are possible" in user_prompt(fake.calls[2])
    assert result["geometry"]["dimensions_mm"] == [30.0, 25.0, 40.0]


def test_edits_and_revisions_never_ask(tmp_path):
    fake = scripted([part(40, 20, 3), "QUESTION: How much taller?", part(40, 20, 8)])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a plate")
    first = run(eng.run_turn(session, "a plate"))
    second = run(eng.run_turn(first["session"], "make it a lot taller"))
    assert second["geometry"]["dimensions_mm"] == [40.0, 20.0, 8.0]
    assert "no further questions are possible" in user_prompt(fake.calls[2])


# ------------------------------------------------------------------ memory, undo, cache

def test_undo_and_go_back_reuse_earlier_versions_without_a_model_call_or_render(tmp_path):
    fake = scripted([part(40, 20, 3)])
    eng, runner = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a plate")
    run(eng.run_turn(session, "a plate"))
    second = run(eng.run_turn(session, "make it 5 mm taller"))  # deterministic
    assert second["geometry"]["dimensions_mm"] == [40.0, 20.0, 8.0]
    assert len(fake.calls) == 1 and runner.renders == 2
    undone = run(eng.run_turn(second["session"], "undo"))
    assert undone["geometry"]["dimensions_mm"] == [40.0, 20.0, 3.0]
    assert undone["reverted_to"] == 1 and undone["session"]["turns"] == 3
    assert undone["param_changes"] == [("height", 8.0, 3.0)]
    again = run(eng.run_turn(undone["session"], "go back to version 2"))
    assert again["geometry"]["dimensions_mm"] == [40.0, 20.0, 8.0]
    first = run(eng.run_turn(again["session"], "go back to the first one"))
    assert first["reverted_to"] == 1
    assert len(fake.calls) == 1  # no model call for any of it
    assert runner.renders == 2  # and no re-render: identical sources reuse the STL
    with pytest.raises(design.DesignError, match="no version 9"):
        run(eng.run_turn(first["session"], "revert to version 9"))


def test_undo_with_nothing_earlier_says_so(tmp_path):
    eng, _ = engine(tmp_path, scripted([part(40, 20, 3)]))
    session = eng.start_session("o", "c", "a plate")
    run(eng.run_turn(session, "a plate"))
    with pytest.raises(design.DesignError, match="no earlier version"):
        run(eng.run_turn(session, "undo"))


@pytest.mark.parametrize("text,target", [
    ("undo", "previous"), ("Undo that.", "previous"), ("go back", "previous"),
    ("back to the previous version", "previous"), ("revert", "previous"),
    ("go back to the first one", 1), ("back to the original", 1),
    ("go back to version 3", 3), ("revert to v2", 2),
    ("undo the chamfer but keep the holes", None), ("make it taller", None),
])
def test_parse_revert_is_narrow(text, target):
    assert design.parse_revert(text) == target


def test_a_revision_prompt_carries_the_earlier_changes(tmp_path):
    fake = scripted([part(40, 20, 3), part(40, 30, 3), part(40, 30, 3, extra="// hole")])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a plate")
    run(eng.run_turn(session, "a plate"))
    run(eng.run_turn(session, "make it deeper"))
    run(eng.run_turn(session, "now add a hole on the other side"))
    prompt = user_prompt(fake.calls[2])
    assert "Earlier changes in this session, oldest first:\n- make it deeper" in prompt
    assert "Change requested: now add a hole on the other side" in prompt
    history = design.load_session(eng.root, session["id"])["history"]
    assert [entry["turn"] for entry in history] == [1, 2, 3]
    assert history[0]["instruction"] == "a plate"


def _heights(eng, session, steps):
    out = []
    for text in steps:
        try:
            out.append(run(eng.run_turn(session, text))["geometry"]["dimensions_mm"][2])
        except design.DesignError as exc:
            out.append("no earlier" if "no earlier version" in str(exc) else str(exc))
    return out


def test_repeated_undo_walks_back_through_the_versions(tmp_path):
    """#1206 finding 3: undo restored history[-2], and the undo itself was the
    newest entry, so a second undo went forward again (8, 13, 8). Undo is a
    stack: each one steps back from the version on screen."""
    eng, _ = engine(tmp_path, scripted([part(40, 20, 3)]))
    session = eng.start_session("o", "c", "a plate")
    steps = ["a plate", "make it 5 mm taller", "make it 5 mm taller", "make it 5 mm taller",
             "undo", "undo", "undo", "undo"]
    assert _heights(eng, session, steps) == [3, 8, 13, 18, 13, 8, 3, "no earlier"]


def test_undo_after_go_back_and_after_an_edit_of_a_restored_version(tmp_path):
    eng, _ = engine(tmp_path, scripted([part(40, 20, 3)]))
    session = eng.start_session("o", "c", "a plate")
    steps = ["a plate", "make it 5 mm taller", "make it 5 mm taller",
             "go back to version 2", "undo",           # before version 2 is version 1
             "make it 1 mm taller", "undo", "undo"]    # back to the restored 3, then none
    assert _heights(eng, session, steps) == [3, 8, 13, 8, 3, 4, 3, "no earlier"]


def test_going_back_to_a_parameter_edit_does_not_warn_about_its_stale_size_line(tmp_path):
    """A deterministic edit leaves the Size line stale by design and is checked
    without it; restoring that version must be checked the same way."""
    eng, _ = engine(tmp_path, scripted([part(40, 20, 3)]))
    session = eng.start_session("o", "c", "a plate")
    run(eng.run_turn(session, "a plate"))
    edited = run(eng.run_turn(session, "make it 5 mm taller"))
    assert edited["findings"] == []
    run(eng.run_turn(session, "undo"))
    back = run(eng.run_turn(session, "go back to version 2"))
    assert back["reverted_to"] == 2 and back["findings"] == []


# ------------------------------------------------------------------ question flow edges (#1206)

def test_the_answer_survives_a_failed_answer_turn_and_a_retry_keeps_it(tmp_path):
    """#1206 finding 4: the answer lived only in memory, so after a failed
    answer turn the next message ("try again") became the answer."""
    fake = scripted(["QUESTION: How wide is the rail?", "no code", "no code", "no code",
                     part(30, 25, 40)])
    eng, _ = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a hook")
    with pytest.raises(design.DesignQuestion):
        run(eng.run_turn(session, "a hook"))
    with pytest.raises(design.DesignError):
        run(eng.run_turn(design.load_session(eng.root, session["id"]), "25 mm"))
    saved = design.load_session(eng.root, session["id"])
    assert saved["question_answer"] == "25 mm"
    result = run(eng.run_turn(saved, "try again, and make it 40 mm tall"))
    prompt = user_prompt(fake.calls[-1])
    assert "The user answered: 25 mm" in prompt
    assert "try again, and make it 40 mm tall" in prompt
    assert "no further questions" not in prompt
    assert result["geometry"]["dimensions_mm"] == [30.0, 25.0, 40.0]


def test_a_model_that_only_asks_is_reported_as_such_not_as_a_render_failure(tmp_path):
    """#1206 finding 5: every refused question used to end in "OpenSCAD could
    not render the design ... Last error:" with nothing rendered."""
    fake = scripted(["QUESTION: What size?"])
    eng, runner = engine(tmp_path, fake)
    session = eng.start_session("o", "c", "a hook")
    with pytest.raises(design.DesignQuestion):
        run(eng.run_turn(session, "a hook"))
    with pytest.raises(design.DesignError) as failed:
        run(eng.run_turn(design.load_session(eng.root, session["id"]), "you pick"))
    text = str(failed.value)
    assert "OpenSCAD" not in text and runner.renders == 0
    assert "kept asking" in text and "3 tries" in text


@pytest.mark.parametrize("raw,budget", [(-1, 1), (0, 1), ("x", 4), (None, 4), (2, 3)])
def test_repair_attempts_is_clamped_to_a_usable_budget(tmp_path, raw, budget):
    cfg = {**CFG, "design": {**CFG["design"], "repair_attempts": raw}}
    fake = scripted(["SYNTAX\ncube([1, 1, 1]);"])
    eng, _ = engine(tmp_path, fake, cfg=cfg)
    session = eng.start_session("o", "c", "a plate")
    with pytest.raises(design.DesignError, match=f"after {budget} attempt"):
        run(eng.run_turn(session, "a plate"))
    assert len(fake.calls) == budget
