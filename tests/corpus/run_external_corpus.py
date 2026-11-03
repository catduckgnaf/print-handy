"""Run a shard of the external public model corpus through the fake-Discord harness."""
import json
import os
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace as S

# Ensure the repository's namespace package wins over any third-party `tests` package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.corpus import harness

GENERIC_CLICKS = [{"question": "Choose the color", "button": "White"},
                  # Explicit monochrome consent for multipart and painted models.
                  {"question": "one color for everything?", "button": "One color for everything"},
                  # STL files carry no units; the corpus models are authored in millimeters.
                  {"question": "STL files do not record units", "button": "Millimeters"},
                  # Prefer the offered shrink, then a split onto full-size plates, then a flat turn on the
                  # bed, then a reorientation (the owner accepts a rotated print, 2026-11-03). Never
                  # silently approve added supports.
                  {"question": "does not fit", "button": "Shrink to"},
                  {"question": "does not fit", "button": "split"},
                  {"question": "does not fit", "button": "turned"},
                  {"question": "does not fit", "button": "rotated"},
                  {"question": "Which plate prints first?", "button": "Plate 1"},
                  {"question": "How many copies", "button": "One copy"},
                  {"question": "·", "button": "Slice and print"}]


# Corpus-v1 models whose correct outcome is an intake refusal, keyed by their folder. The
# release tarball carries no case.toml for them, so the expectation is pinned here.
PINNED_REFUSALS = {
    # A 1 KB 3MF that names two objects but carries no mesh. It "sliced" against the fake Orca
    # until the fit check started reading geometry; refusing it at intake is the right outcome.
    "031-can-holder--4bd27ac45ab319200fd51a52196f68b19d7061f588491d66f10ba7ba40fd7d74":
        "no printable geometry",
}


# Extra answers for corpus-v2 complex cases whose own case.toml (in the release, not this repo)
# predates a question. Appended after the case's rules, so its own answers always win.
EXTRA_CLICKS = {
    # The fit question now offers only "83.3% · rotated 90° on X" on the AD5X bed; a rotated
    # print is acceptable (owner decision, 2026-11-03).
    "spiral-vase": [{"question": "does not fit", "button": "rotated"}],
}


def build_case(model, root):
    """The harness case for one model file.

    A folder carrying its own case.toml — the #458 complex cases extracted
    into the shared corpus root (corpus-v2) — runs with that file's clicks
    and expectations (P2-178): the generic case below would otherwise
    expect STARTED for every model and report the pinned intake refusals
    as regressions on every run. Plain folders keep the generic case.
    """
    case_dir = model.parent
    case_toml = case_dir / "case.toml"
    if case_toml.is_file():
        data = tomllib.loads(case_toml.read_text())
        return S(name=case_dir.name, dir=case_dir, model=model, data=data,
                 clicks=data.get("click", []) + EXTRA_CLICKS.get(case_dir.name, []),
                 expect=data.get("expect", {}))
    refusal = PINNED_REFUSALS.get(case_dir.name)
    return S(name=case_dir.name, dir=case_dir, model=model,
             data={"description": model.name},
             clicks=GENERIC_CLICKS,
             expect={"status": "FAILED", "error_contains": refusal} if refusal else {"status": "STARTED"})


def evaluate(case, run):
    """Assert what the case expects of a finished run; raises AssertionError."""
    if case.expect.get("status") == "FAILED":
        # A pinned refusal: the FAILED receipt for the expected reason, and
        # slicing/starting never occurred (P2-177).
        harness.check_refusal(case, run)
        return
    harness.check_expectations(case, run)
    if not run.slice_starts:
        raise AssertionError("actual BackgroundSlice.start was never called")
    if len(run.slice_starts) != 1:
        raise AssertionError(f"expected one slice start, got {len(run.slice_starts)}")
    if not any(event[0] == "slice" for event in run.events):
        raise AssertionError("fake slicer was never called")


def main():
    root = Path(os.environ["PRINT_HANDY_PRIVATE_CORPUS"]).resolve()
    models = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".3mf", ".stl"})
    shard_index, shard_count = int(os.environ["CORPUS_SHARD_INDEX"]), int(os.environ["CORPUS_SHARD_COUNT"])
    results = []
    workroot = Path(os.environ.get("CORPUS_WORK", "corpus-work"))
    for index, model in enumerate(models):
        if index % shard_count != shard_index:
            continue
        case = build_case(model, root)
        started = time.monotonic()
        row = {"model": model.relative_to(root).as_posix(), "result": "PASS", "seconds": 0.0, "error": ""}
        try:
            run = harness.run_case(case, workroot / f"{shard_index}-{index}")
            evaluate(case, run)
        except Exception as exc:
            row["result"] = "FAIL"
            # Preserve unanswered question text and buttons, not just the generic first line.
            row["error"] = f"{type(exc).__name__}: {exc}"
        row["seconds"] = round(time.monotonic() - started, 2)
        results.append(row)
        print(json.dumps(row), flush=True)
    out = Path(os.environ.get("CORPUS_RESULTS", "results.json"))
    out.write_text(json.dumps(results, indent=2) + "\n")
    if not results:
        raise SystemExit(f"shard {shard_index} selected no models")
    # Results are written first so the artifacts stay available; a shard in
    # which any model failed must still fail the job (P1-58).
    failed = [row["model"] for row in results if row["result"] == "FAIL"]
    if failed:
        raise SystemExit(f"shard {shard_index}: {len(failed)} model(s) failed: {', '.join(failed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
