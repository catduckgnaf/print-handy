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
                  # The paint flow first asks, for the whole model, which loaded color/material to
                  # use for the painted regions (question text "Choose the loaded color/material for
                  # painted regions ...", buttons White/Pink/Black/DarkGreen PLA + Cancel). A second
                  # flow, for segmented models such as the articulated octopus, asks which color for
                  # each part ("Choose one color/material for the N octopus ..."). Both offer the same
                  # four spool buttons + Cancel; matching "color/material" covers both phrasings and
                  # picking the first palette button lets intake proceed to the per-region confirm.
                  # A plain monochrome model instead sees "Choose the color" above and never reaches
                  # here. The generic choice is not a regression target; intake must reach "Slice and
                  # print" to START.
                  {"question": "color/material", "button": "White"},
                  {"question": "color/material", "button": "Pink"},
                  {"question": "color/material", "button": "Black"},
                  {"question": "color/material", "button": "Dark Green"},
                  {"question": "color/material", "button": "Cancel"},
                  {"question": "one color/material", "button": "White"},
                  # Prefer the offered shrink, then a split onto full-size plates, then a flat turn on the
                  # bed, then a reorientation (the owner accepts a rotated print, 2026-11-03). Never
                  # silently approve added supports.
                  {"question": "does not fit", "button": "Shrink to"},
                  {"question": "does not fit", "button": "split"},
                  {"question": "does not fit", "button": "turned"},
                  {"question": "does not fit", "button": "rotated"},
                  # Some corpus models (e.g. 081-hello-kitty-shelf) are too big to split and turn
                  # on the bed, so intake offers an automatic "X% · N plates" shrink-and-plate
                  # suggestion (buttons like ['75% · 2 plates', 'Cancel']) instead of the manual
                  # shrink/split/turned/rotated choices above. Match the "%" in the suggested size so
                  # intake accepts the offered shrink-and-plate and proceeds to slice.
                  {"question": "is too big", "button": "%"},
                  {"question": "too big", "button": "%"},
                  {"question": "Which plate prints first?", "button": "Plate 1"},
                  {"question": "How many copies", "button": "One copy"},
                  # Mixed multi-color intake: some colors match a loaded spool while others say
                  # "no spool suggested", so intake offers to use the suggested slots and pick the
                  # rest (not every color, not one-for-all). The confirm question ends
                  # "Use those and pick the rest?" but the button reads "Use these and pick the
                  # rest"; match on the stable "pick the rest" phrase so the button label
                  # wording is not required. This is the rule the corpus-v1/v2 mixed-color models
                  # (e.g. 012-articulated-octopus, 168-valentines-headphones-stand) need.
                  {"question": "pick the rest", "button": "Use these and pick the rest"},
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
# Palette pick plus the "Use those and pick the rest?" confirm (both phrasings matched on their
# stable words), shared by the mixed-color corpus-v2 cases below.
MIXED_COLOR_CLICKS = [
    {"question": "color/material", "button": "White"},
    {"question": "color/material", "button": "Pink"},
    {"question": "color/material", "button": "Black"},
    {"question": "color/material", "button": "Dark Green"},
    {"question": "pick the rest", "button": "Use these and pick the rest"},
]
OCTOPUS = "012-articulated-octopus--317d1e8d5f728b44ff0ab8e6abb49f157216b0e1055705699c0070560b49dc05"

EXTRA_CLICKS = {
    # The fit question now offers only "83.3% · rotated 90° on X" on the AD5X bed; a rotated
    # print is acceptable (owner decision, 2026-11-03).
    "spiral-vase": [{"question": "does not fit", "button": "rotated"}],
    # MMU / painted v2 cases (cute-dino-mmu, jukebox-face) intake a palette selection step and
    # a "Use those and pick the rest?" confirm that their shipped case.toml predates. Both offer
    # the same four spool buttons + Cancel; matching "color/material" covers "Choose the loaded
    # color/material for painted regions ...", and the confirm rule matches on the stable
    # "pick the rest" phrase. Picking any loaded palette color lets intake proceed to slice.
    "cute-dino-mmu": MIXED_COLOR_CLICKS,
    "jukebox-face": MIXED_COLOR_CLICKS,
    # The octopus asks the same mixed-color confirm and per-part color question before its
    # slice-stage refusal. A case.toml folder gets only its own clicks plus these, never
    # GENERIC_CLICKS, so the answers ride here too or intake stalls before SLICE_FAILED.
    OCTOPUS: MIXED_COLOR_CLICKS,
}


# corpus-v2 folders whose shipped case.toml (or the default expectation) is wrong against the live
# corpus, so the outcome is flipped here without editing the release tarball. gyroid-surface and
# fnaf-mask are painted/colored models that slice cleanly once intake asks its second question
# (GENERIC_CLICKS picks a palette button, which fits on a single plate): they START, they do not
# refuse. The 190-part articulated octopus is a genuine negative case instead. After the two
# mixed-color intake prompts ("Use those and pick the rest?" then "Choose one color/material for the
# N octopus…") it stops at the slice stage with a SLICE_FAILED receipt because its colors form a
# single assembly that leaves no room on one plate, so nothing is sliced or started (see
# https://catduckgnaf.github.io/print-handy-docs/diagnostics#ph-slicing-configuration). It is pinned
# SLICE_FAILED: evaluate() checks it through check_expectations (receipt status, slice error and the
# operator message) and requires that slicing started, while intake FAILED cases keep
# check_refusal's strict receipt-reason, no-slice, no-start contract.
OVERRIDE_EXPECT = {
    "gyroid-surface": {"status": "STARTED"},
    "fnaf-mask": {"status": "STARTED"},
    OCTOPUS: {
        "status": "SLICE_FAILED",
        "slice_error_contains": "an assembly cannot be split per slot",
        "says": ["cannot be split onto separate plates", "PH_SLICING_CONFIGURATION"],
    },
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
                 # gyroid-surface / fnaf-mask ship as intake refusals but slice cleanly here.
                 expect=OVERRIDE_EXPECT.get(case_dir.name, data.get("expect", {})))
    refusal = PINNED_REFUSALS.get(case_dir.name)
    return S(name=case_dir.name, dir=case_dir, model=model,
             data={"description": model.name},
             clicks=GENERIC_CLICKS,
             expect=OVERRIDE_EXPECT.get(case_dir.name,
                        {"status": "FAILED", "error_contains": refusal} if refusal
                        else {"status": "STARTED"}))


def evaluate(case, run):
    """Assert what the case expects of a finished run; raises AssertionError."""
    if case.expect.get("status") == "FAILED":
        # Intake refusals must carry the pinned reason on the FAILED receipt.
        needle = case.expect.get("error_contains")
        if needle and needle not in str(run.receipt.get("error") or ""):
            raise AssertionError(
                f"refusal receipt {run.receipt.get('error')!r} does not name the pinned "
                f"reason {needle!r}")
        harness.check_refusal(case, run)
        return
    # SLICE_FAILED is a distinct expected terminal outcome: slicing was attempted,
    # but the slicer rejected the assembly. check_expectations verifies the receipt,
    # refusal message and that printing never started.
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
