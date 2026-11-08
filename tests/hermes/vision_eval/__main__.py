"""Run the Stage 0 harness over the fixture set with the local detector.

Usage: ``python -m tests.hermes.vision_eval`` — prints the per-print
report. Requires the local detector to be enabled with a model path the
same way the test-suite fixtures configure it; on the synthetic format
fixtures the point is the accounting, not accuracy.
"""
from __future__ import annotations

import sys

from tests.hermes.vision_eval.harness import (
    evaluate, format_report, local_detector_producer)

if __name__ == "__main__":
    print(format_report(evaluate(local_detector_producer)))
    sys.exit(0)
