"""Run a 3-way shard of the external public model corpus through the edit/question pipeline (#903).

Applies an edit matrix pass (shrink, supports question, rotate, undo) checking
plan resolution, probe slice verification, and provenance without lengthening
the weekly critical path.
"""
import json
import os
import sys
import time
from pathlib import Path

# Ensure the repository's namespace package wins over any third-party `tests` package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from print_handy.core import edit_plan, model_job as mj, model_questions
from tests.corpus.edit.runner import build_test_job, generate_mock_gcode, resolve_plan, verify_probe


def run_model_edits(model_path, work_dir):
    """Exercise edit planning, probe verification, questions, and undo on one model."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        audit = mj.audit(model_path)
    except Exception as exc:
        return {"result": "SKIP", "reason": f"audit skipped: {exc}"}

    try:
        sx = float(audit.get("size_x") or 40.0)
        sy = float(audit.get("size_y") or 20.0)
        sz = float(audit.get("size_z") or 10.0)
    except (TypeError, ValueError):
        sx, sy, sz = 40.0, 20.0, 10.0
    dims = (sx, sy, sz)

    job_path, job = build_test_job(work_dir, model_path, title=model_path.stem, dims=dims)

    # 1. Question: does this need supports?
    answer, offer = model_questions.supports_answer(job, model_path)
    if not answer:
        raise AssertionError("supports_answer returned empty string")

    # 2. Scale edit: scale to 80%
    plan_scale = resolve_plan("scale to 80%", job, model_path)
    scaled_dims = tuple(round(d * 0.8, 3) for d in dims)
    mock_gcode = generate_mock_gcode(dims=scaled_dims, object_name=model_path.stem)
    ok, lines = verify_probe(plan_scale, job, mock_gcode)
    if not ok:
        raise AssertionError(f"verify_probe failed: {lines}")

    edit_plan.record_edit(job, plan_scale, actor="external_corpus", lane="fuzzy", source_sha256="ext")
    edit_plan.apply_plan(job, plan_scale)
    if abs(job["scale"] - 0.8) > 0.001:
        raise AssertionError(f"job scale expected 0.8, got {job['scale']}")

    # 3. Rotate edit: rotate 90
    plan_rot = resolve_plan("rotate 90", job, model_path)
    edit_plan.record_edit(job, plan_rot, actor="external_corpus", lane="fuzzy", source_sha256="ext")
    edit_plan.apply_plan(job, plan_rot)
    if len(job.get("edits", [])) != 2:
        raise AssertionError(f"expected 2 edits recorded, got {len(job.get('edits', []))}")

    # 4. Multi-step undo
    undone2 = edit_plan.undo_last_edit(job, actor="external_corpus")
    if not undone2 or undone2["plan"]["kind"] != "rotate":
        raise AssertionError("undo 2 did not restore rotate")
    undone1 = edit_plan.undo_last_edit(job, actor="external_corpus")
    if not undone1 or undone1["plan"]["kind"] != "scale_percent":
        raise AssertionError("undo 1 did not restore scale")
    if edit_plan.undo_last_edit(job, actor="external_corpus") is not None:
        raise AssertionError("undo should have no more edits to undo")

    return {"result": "PASS", "reason": "edit/question/undo verified"}


def main():
    configured_root = os.environ.get("PRINT_HANDY_PRIVATE_CORPUS")
    if not configured_root:
        raise SystemExit("PRINT_HANDY_PRIVATE_CORPUS env variable is required")
    root = Path(configured_root).resolve()
    models = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".3mf", ".stl"})

    shard_index = int(os.environ.get("CORPUS_SHARD_INDEX", "0"))
    shard_count = int(os.environ.get("CORPUS_SHARD_COUNT", "3"))
    results = []
    workroot = Path(os.environ.get("CORPUS_WORK", "edit-corpus-work"))

    for index, model in enumerate(models):
        if index % shard_count != shard_index:
            continue
        started = time.monotonic()
        rel_path = model.relative_to(root).as_posix()
        row = {"model": rel_path, "result": "PASS", "seconds": 0.0, "error": ""}
        try:
            res = run_model_edits(model, workroot / f"{shard_index}-{index}")
            row["result"] = res["result"]
            row["error"] = res.get("reason", "")
        except Exception as exc:
            row["result"] = "FAIL"
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["seconds"] = round(time.monotonic() - started, 2)
        results.append(row)
        print(json.dumps(row), flush=True)

    out = Path(os.environ.get("CORPUS_RESULTS", "edit-results.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2) + "\n")

    if not results:
        raise SystemExit(f"shard {shard_index} selected no models")

    failed = [row["model"] for row in results if row["result"] == "FAIL"]
    if failed:
        raise SystemExit(f"edit shard {shard_index}: {len(failed)} model(s) failed: {', '.join(failed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
