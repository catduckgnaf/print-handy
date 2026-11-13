"""Run the Stage 0 harness with the local detector and print the report.

Usage::

    python -m tests.hermes.vision_eval
    python -m tests.hermes.vision_eval --model obico-failure-detector.onnx --frames saved-frames/
    python -m tests.hermes.vision_eval --model obico-failure-detector.onnx \\
        --frames healthy-print/ --frames failed-print/=spaghetti

With no arguments it replays the shipped fixture set with the local
detector as the process configuration has it; on the synthetic format
fixtures the point is the accounting, not accuracy.

``--model`` points the detector at an ONNX file (the real 202 MB
``obico-failure-detector.onnx``, or any file with its output contract)
and enables it for this run only; nothing is downloaded. ``--frames``
replays a plain folder of saved camera frames (``.jpg``/``.png``, in
file name order) as one session; repeat it for several folders. A folder
written ``FOLDER=LABEL`` gets that label (``ok``, ``spaghetti``, ...);
one without a label gets ``--label`` (default ``ok``, a healthy print).
The report lists, per session, the checks the watch schedule made, their
verdicts, and every check whose verdict would have been posted as an
alert. A missing or empty folder, or a missing model, ends the run with
one error line and exit code 2.
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

from print_handy.runtime import _config
from tests.hermes.vision_eval.harness import (
    SESSION_LABELS, EvalSetError, evaluate, folder_session, format_report,
    local_detector_producer)

PROG = "python -m tests.hermes.vision_eval"


def frames_arg(value, default_label):
    """``(folder, label)`` from a ``--frames`` value: ``FOLDER`` or ``FOLDER=LABEL``."""
    folder, sep, label = value.rpartition("=")
    if sep and label in SESSION_LABELS and folder:
        return folder, label
    return value, default_label


def _fail(message):
    print(f"{PROG}: error: {message}", file=sys.stderr)
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", help="ONNX detector file to use for this run")
    parser.add_argument("--frames", action="append", default=[], metavar="FOLDER[=LABEL]",
                        help="a folder of saved camera frames, replayed as one session; "
                             f"LABEL is one of {', '.join(sorted(SESSION_LABELS))}")
    parser.add_argument("--label", default="ok", choices=sorted(SESSION_LABELS),
                        help="the label of a --frames folder given without one (default: ok)")
    parser.add_argument("--cadence", type=float, default=600.0,
                        help="seconds between the saved frames (default: 600)")
    parser.add_argument("--sensitivity", type=int, default=3, help="watch sensitivity 1-5")
    parser.add_argument("--plain", action="store_true",
                        help="replay the plain schedule instead of the early-layer window")
    args = parser.parse_args(argv)
    if args.model and not Path(args.model).is_file():
        # The detector would start a background download for a missing
        # file; a measurement run must never fetch anything.
        return _fail(f"--model {args.model}: no such file")
    try:
        sessions = [folder_session(folder, label=label, cadence_s=args.cadence)
                    for folder, label in (frames_arg(v, args.label) for v in args.frames)]
    except (EvalSetError, OSError) as exc:
        return _fail(str(exc).splitlines()[0] if str(exc) else type(exc).__name__)
    previous = None
    if args.model:
        previous = copy.deepcopy(_config.cfg())
        data = copy.deepcopy(previous)
        data.setdefault("vision", {})
        data["vision"]["local_detector_enabled"] = True
        data["vision"]["local_model_path"] = args.model
        _config.set_override(data)
    try:
        report = evaluate(local_detector_producer, sessions=sessions or None,
                          sensitivity=args.sensitivity, risky=not args.plain)
    finally:
        if previous is not None:
            _config.set_override(previous)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
