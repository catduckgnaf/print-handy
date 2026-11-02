"""A job must be judged by its own printer, not the default one.

#761: print_now resolved the job's printer adapter, but the filament
readiness check re-resolved printers.current() — the default printer's
adapter decided which readiness path ran and judged the sample. These
tests pin the readiness check to the job printer's own adapter.

This file is new on main; the regression test below was cherry-picked
from the codex-backlog-2026-11-02 branch (commit d9f219a), where it was
appended to the SAFE-4 routing tests. Those tests cover other backlog
findings whose fixes are not in this PR, so only the #761 test is
carried here.
"""
from types import SimpleNamespace as S

from print_handy.core import model_job


# -- Bonus: print_now's filament-readiness check --------------------------------


def test_filament_readiness_uses_the_printers_own_adapter(monkeypatch):
    # Found while threading P1-75 (not in the audit): print_now resolves
    # the job's adapter, but _verify_filament_readiness re-resolved
    # printers.current() — the default printer's adapter decided which
    # readiness path ran and judged the sample.
    judged = []
    job_adapter = S(multi_slot=False, label="Workshop Klipper",
                    moonraker_objects=lambda: ["print_stats"],
                    readiness_problems=lambda state, status, selection: judged.append(status) or [])
    # The default printer is a multi-slot IFS machine: resolving it here
    # would take the IFS path against the workshop record and never call
    # the job adapter's own readiness check.
    monkeypatch.setattr(model_job.printers, "current",
                        lambda *a, **k: S(multi_slot=True, label="Garage AD5X"))
    monkeypatch.setattr(model_job, "request_json",
                        lambda url, **kw: {"result": {"status": {"print_stats": {"state": "standby"}}}})
    model_job._verify_filament_readiness({"moonraker_url": "http://192.0.2.61:7125"}, None,
                                         adapter=job_adapter)
    assert judged == [{"print_stats": {"state": "standby"}}]
