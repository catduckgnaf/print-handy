"""Creator 5 / 5 Pro with the real Orca CLI: 2-3 filament jobs and filaments on heads 3 and 4 (#868, edge case 13).

ForgeBridge fixed a flush-matrix mismatch for 2-3 filament Creator jobs (1.5.3) and a filament on head 3 or 4
that blocked slicing (1.5.5). Print Handy loads one filament profile per toolhead whatever the job uses
(tests/pipeline/test_forgebridge_edge_cases.py); here the real Orca slices such jobs through slice_model, with
every guard and the shipped Creator baseline, and the G-code must print on every picked toolhead.

Skipped unless PRINT_HANDY_REAL_SLICER=1 (CI's Slicer test jobs).
"""
import os
import re
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from print_handy import printers
from print_handy.core import orca_adapter, spoolman_client
from tests.pipeline.test_multi_tool_intake import CASES, MODELS, creator_config, four_colour_decisions, tool_state

from .test_orca_real import WORK

REAL = os.environ.get("PRINT_HANDY_REAL_SLICER") == "1"
pytestmark = pytest.mark.skipif(not REAL, reason="set PRINT_HANDY_REAL_SLICER=1 to slice with the real Orca CLI")


@pytest.mark.parametrize("model_key", MODELS)
@pytest.mark.parametrize("picks,heads", [([3, 4, 3, 4], {2, 3}), ([1, 3, 4, 1], {0, 2, 3})])
def test_creator_job_on_heads_3_and_4_slices(model_key, picks, heads):
    WORK.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORK) as tmp, \
            mock.patch.object(spoolman_client, "get_spool", return_value=None):
        tmp = Path(tmp)
        adapter = printers.get(model_key, creator_config(model_key))
        out, provenance, _configs = orca_adapter.slice_model(
            CASES / "four-colors-one-plate" / "model.3mf", tmp / "slice", tool_state(), adapter=adapter,
            color_decisions=four_colour_decisions(tmp, picks))
        text = Path(out).read_text(errors="replace")
        used = {int(t) for t in re.findall(r"^T(\d)", text, re.M)}
        print(f"\n{model_key} picks {picks}: tools used {sorted(used)}; mapping {provenance['tool_mapping']}")
        assert heads <= used, used  # the start sequence may also select a tool; every picked head prints
        assert {int(tool) for tool in provenance["tool_mapping"]} == heads
