"""Run the #458 complex corpus cases through the fake-Discord harness.

Unlike run_external_corpus.py (one generic case for every file, default
printer fixture), each complex case runs with a printer state built from its
manifest target: the AD5X fixture state with printer name, bed and max height
replaced from printer registry values recorded in complex_cases.toml.
That exercises fit / height / colour logic against the target printer's
geometry. It does NOT switch the slicer vendor profile or the slot/tool
layout - the state still carries the fixture's spool/IFS layout - and the
README (tests/corpus/complex/README.md) says so plainly.

Usage:
    COMPLEX_CORPUS_ROOT=~/print-handy-complex-corpus \
        python tests/corpus/run_complex_corpus.py [--tier public|private] [--only slug,...] [--real-slicer]

--real-slicer (or PRINT_HANDY_REAL_SLICER=1) runs the harness's real tier:
the installed Orca CLI does the fit probes and the slice, instead of the
mock tier's Python fit model and fake G-code writer.

Writes JSON rows (model, result, seconds, error, receipt facts) to
$CORPUS_RESULTS or ./complex-results.json and prints them as it goes.
A case whose model has not been fetched is reported as NOT_FETCHED, never
silently dropped. A case with expected_failure in the manifest (a recorded
product bug: a list of error substrings) is
XFAIL while one of those errors reproduces and XPASS once the case passes;
any other failure is FAIL.
"""
import argparse
import json
import os
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace as S

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.corpus import harness  # noqa: E402

MANIFEST = Path(__file__).resolve().parent / "complex_cases.toml"


def target_state(case, work):
    """The fixture state with the manifest target's printer/bed/height."""
    state = json.loads(harness.FIXTURE_STATE.read_text())
    bed = case["target_bed_mm"]
    height = case["target_max_height_mm"]
    state["printer"] = case["target_printer_label"]
    state["bed_mm"] = [int(bed[0]), int(bed[1])]
    state["max_print_height_mm"] = int(height)
    state["axis_limits_mm"] = {"x": [0, int(bed[0]) + 3], "y": [0, int(bed[1]) + 3],
                               "z": [0, int(height)]}
    path = work / f"state-{case['slug']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tier", default="public")
    parser.add_argument("--only", default=None, help="comma-separated slugs")
    parser.add_argument("--real-slicer", action="store_true",
                        default=os.environ.get("PRINT_HANDY_REAL_SLICER") == "1",
                        help="real tier: the installed Orca CLI probes and slices")
    parser.add_argument("--root", default=os.environ.get("COMPLEX_CORPUS_ROOT")
                        or os.environ.get("PRINT_HANDY_PRIVATE_CORPUS"))
    args = parser.parse_args()
    if not args.root:
        raise SystemExit("set --root or COMPLEX_CORPUS_ROOT to the fetched corpus root")
    root = Path(args.root).resolve()
    manifest = tomllib.loads(MANIFEST.read_text())
    only = set(args.only.split(",")) if args.only else None
    workroot = Path(os.environ.get("CORPUS_WORK", "complex-work"))
    results = []
    for case in manifest["case"]:
        if case["tier"] != args.tier or (only and case["slug"] not in only):
            continue
        row = {"model": case["slug"], "target_domain": case["target_domain"],
               "target_printer": case["target_printer"],
               "slicer_tier": "real" if args.real_slicer else "mock", "result": "PASS",
               "seconds": 0.0, "error": ""}
        if case["status"] != "ready":
            row.update(result="BLOCKED", error=case.get("blocked_reason", case["status"]))
            results.append(row)
            print(json.dumps(row), flush=True)
            continue
        folder = root / case["slug"]
        model = folder / case["file"]
        if not model.is_file():
            row.update(result="NOT_FETCHED", error=f"{model} missing - run fetch_complex_corpus.py")
            results.append(row)
            print(json.dumps(row), flush=True)
            continue
        data = tomllib.loads((folder / "case.toml").read_text())
        case_ns = S(name=case["slug"], dir=folder, model=model, data=data,
                    clicks=data.get("click", []), expect=data.get("expect", {}))
        started = time.monotonic()
        run = None
        try:
            run = harness.run_case(case_ns, workroot / case["slug"],
                                   state_file=target_state(case, workroot),
                                   real_slicer=args.real_slicer)
            if case_ns.expect.get("status") == "FAILED":
                # A pinned refusal: the harness's generic check also asserts a
                # print started, which a refused case by definition never does.
                # check_refusal pins the FAILED receipt, the expected refusal
                # reason (error_contains), the adapter checks, and that
                # slicing/starting never occurred (P2-177).
                harness.check_refusal(case_ns, run)
            else:
                harness.check_expectations(case_ns, run)
                if not run.slice_starts:
                    raise AssertionError("actual BackgroundSlice.start was never called")
            row["receipt"] = {"status": run.receipt.get("status"),
                              "scale": run.receipt.get("scale"),
                              "copies": run.receipt.get("copies")}
        except Exception as exc:  # noqa: BLE001 - recorded per case, like run_external_corpus
            row.update(result="FAIL", error=f"{type(exc).__name__}: {exc}")
            if run is not None:
                # The expectation message carries the receipt status; the
                # underlying slice/fit error lives in its own receipt fields.
                row["receipt"] = {"status": run.receipt.get("status"),
                                  "scale": run.receipt.get("scale"),
                                  "copies": run.receipt.get("copies")}
                for field in ("slice_error", "fit_error"):
                    if run.receipt.get(field):
                        row["receipt"][field] = run.receipt[field]
                        row["error"] += f"\nreceipt {field}: {run.receipt[field]}"
        known =case.get("expected_failure") or []
        if known:
            # A recorded product bug: XFAIL while a known error still
            # reproduces, XPASS once the case passes (drop expected_failure
            # then). Any other failure stays a plain FAIL.
            if row["result"] == "PASS":
                row["result"] = "XPASS"
            elif any(text in row["error"] for text in known):
                row["result"] = "XFAIL"
        row["seconds"] = round(time.monotonic() - started, 2)
        results.append(row)
        print(json.dumps(row), flush=True)
    out = Path(os.environ.get("CORPUS_RESULTS", "complex-results.json"))
    out.write_text(json.dumps(results, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
