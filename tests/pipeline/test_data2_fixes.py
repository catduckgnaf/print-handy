"""DATA-2: intake and color-decision crashes (Codex backlog burn-down).

Each test pins one finding: it fails on the pre-fix code for the exact
scenario the audit named, and passes once the fix lands.
"""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.core import model_job, orca_adapter, prepared_intake
from .test_color_intake import state  # noqa: F401
from .test_prepared_intake import Adapter, env, run_flow  # noqa: F401
from .test_source_colors import source as painted_source


def _receipt(env, mid):
    name = hashlib.sha256(json.dumps(['3dprinting', 'c1', '', mid]).encode()).hexdigest() + '.json'
    return json.loads((env.root / name).read_text())


def test_resume_reuses_saved_color_decisions(env):
    """P1-38: a resume whose JOB.json already holds color decisions must reuse
    them, not rebuild them from an unassigned ``mode`` (UnboundLocalError)."""
    run_flow(env, [1, 0, 0], mid='first')
    prev = prepared_intake.last_job()
    saved = prev['color_decisions']
    assert saved['mode'] == 'single_color'
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='resume1', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([0, 0])  # slice-and-print choice, then the start button
    result = asyncio.run(prepared_intake.prepare_link(
        event, adapter, state(), env.root, timeout=1,
        reuse=prepared_intake.resume_job(prev)))
    receipt = _receipt(env, 'resume1')
    assert receipt['status'] != 'FAILED', receipt
    assert result['action'] != 'skip' or 'UnboundLocalError' not in str(result)
    # The saved decisions were reused: no color question was asked again, and
    # the new JOB.json carries the same decisions the operator already made.
    assert not any('Choose the color' in q['question'] or 'Choose colors' in q['question']
                   for q in adapter.questions)
    job = json.loads(Path(receipt['job_json']).read_text())
    assert job['color_decisions']['mode'] == saved['mode']
    assert job['color_decisions']['source_sha256'] == saved['source_sha256']
    assert job['color_decisions']['selection'] == saved['selection']
    assert len(env.starts) == 2  # the resumed intake reached the start


def test_painted_oversize_fit_accept_does_not_crash(env, monkeypatch):
    """P1-27: accepting a painted oversize fit must not read ``options`` —
    at that point it is still the color-mode label list (or unbound), never
    the fit-options dict, so the accept branch crashed with AttributeError."""
    painted = painted_source(env.tmp, painted=True)

    def copy_painted(job, chosen, dest):
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / 'ghost.3mf'
        out.write_bytes(painted.read_bytes())
        return out

    monkeypatch.setattr(model_job, 'printables_download', copy_painted)
    monkeypatch.setattr(orca_adapter, 'orca_plate_capacity', lambda path, st: 0)
    monkeypatch.setattr(model_job, 'painted_fit_for_job',
                        lambda path, decisions: {'scale': 0.8, 'plates': 1, 'blocking_plate': None})
    event = S(source=S(profile='3dprinting', chat_id='c1', thread_id=None, user_id='u'),
              message_id='painted1', text='https://www.printables.com/model/1-ghost')
    adapter = Adapter([0] * 20)  # per-part colors, slot picks, accept the fit, slice and print
    result = asyncio.run(prepared_intake.prepare_link(event, adapter, state(), env.root, timeout=1))
    receipt = _receipt(env, 'painted1')
    assert receipt['status'] != 'FAILED', receipt
    assert receipt['painted_fit']['scale'] == 0.8
    job = json.loads(Path(receipt['job_json']).read_text())
    assert job['scale'] == 0.8 and job['plate_mode'] == 'separate'
    assert not any('has no attribute' in post for post in adapter.sent)
    assert result['action'] != 'skip' or result.get('reason') != 'intake failed'
