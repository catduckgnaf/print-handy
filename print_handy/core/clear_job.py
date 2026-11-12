#!/usr/bin/env python3
"""Fail-closed, readback-verified Moonraker job clear. No automatic cancellation."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from .. import settings

CTL = settings.ROOT / "bin" / "moonrakerctl"
ACTIVE = {"printing", "paused"}
IDLE_NATIVE = {"ready", "cancel", "cancelled"}
EVIDENCE_DIR = settings.current()["paths"]["state_home"] / "printer-audit" / "clear-job"


def moonraker():
    p = subprocess.run([str(CTL), "status"], check=True, capture_output=True, text=True, timeout=20)
    return json.loads(p.stdout)


def safe_idle(m, n):
    return (m.get("state") == "ready" and isinstance(m.get("print"), dict)
            and m["print"].get("state") == "standby"
            and float(m.get("extruder", {}).get("target", -1)) == 0
            and float(m.get("bed", {}).get("target", -1)) == 0
            and n.get("status") in IDLE_NATIVE
            and float(n.get("leftTargetTemp", -1)) == 0
            and float(n.get("rightTargetTemp", -1)) == 0
            and float(n.get("platTargetTemp", -1)) == 0
            and float(n.get("chamberTargetTemp", -1)) == 0)


def clear(before, native_reader, cancel, allow_active_cancel=False, moonraker_reader=None):
    n0 = native_reader()
    if not isinstance(n0, dict):
        raise RuntimeError("native readback malformed")
    active = before.get("print", {}).get("state") in ACTIVE or n0.get("status") in {"printing", "paused"}
    if active and not allow_active_cancel:
        raise RuntimeError("active job detected; cancellation requires --allow-active-cancel")
    if not active and not safe_idle(before, n0):
        raise RuntimeError("precondition failed: native and Moonraker do not corroborate idle with zero targets")
    result = {"operation": "no-op", "before": before, "native_before": n0}
    # Native cancel is already idle; never replay against a retained filename.
    if n0.get("status") not in {"cancel", "cancelled"}:
        result["cancel_attempted"] = True
        try:
            receipt = cancel()
            result["cancel_receipt"] = {"stdout": receipt.stdout, "stderr": receipt.stderr,
                                         "returncode": receipt.returncode}
        except subprocess.CalledProcessError as exc:
            result["cancel_receipt"] = {"stdout": exc.stdout, "stderr": exc.stderr, "returncode": exc.returncode}
            result["outcome"] = "cancel-command-failed; outcome uncertain"
            raise CancelFailure(result) from exc
        except Exception as exc:
            result["cancel_receipt"] = {"stdout": getattr(exc, "stdout", None), "stderr": getattr(exc, "stderr", None),
                                         "returncode": getattr(exc, "returncode", None), "exception": repr(exc)}
            result["outcome"] = "cancel-command-failed; outcome uncertain"
            raise CancelFailure(result) from exc
        result["operation"] = "moonraker-cancel"
    after = (moonraker_reader or moonraker)()
    n1 = native_reader()
    if not isinstance(n1, dict) or not safe_idle(after, n1):
        result.update({"after": after, "native_after": n1, "verified": False,
                       "outcome": "readback failed; outcome uncertain"})
        raise CancelFailure(result)
    if result["operation"] == "moonraker-cancel" and n1.get("status") not in {"cancel", "cancelled"}:
        result.update({"after": after, "native_after": n1, "verified": False,
                       "outcome": "native cancellation not confirmed; outcome uncertain"})
        raise CancelFailure(result)
    result.update({"after": after, "native_after": n1, "verified": True, "outcome": "verified"})
    return result


class CancelFailure(RuntimeError):
    def __init__(self, evidence):
        super().__init__(evidence.get("outcome", "cancellation failed"))
        self.evidence = evidence


def native_read():
    from . import model_job
    return model_job.read_stock_detail(model_job.printer_state.load())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify idle only; never issue cancellation")
    parser.add_argument("--allow-active-cancel", action="store_true",
                        help="explicitly authorize one cancellation of a currently active job")
    parser.add_argument("--evidence", type=Path, help="audit JSON path; defaults to a timestamped file under <state_home>/printer-audit/clear-job")
    args = parser.parse_args()
    from datetime import datetime, timezone
    evidence_path = args.evidence or EVIDENCE_DIR / (datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z") + ".json")
    record = {"started_at": datetime.now(timezone.utc).astimezone().isoformat(), "before": None,
              "native_before": None, "after": None, "native_after": None, "cancel_receipt": None}
    result = 1
    try:
        before = moonraker()
        record["before"] = before
        if args.check:
            n = native_read()
            record["native_before"] = n
            record.update({"outcome": "verified-idle" if safe_idle(before, n) else "not-idle",
                           "verified_idle": safe_idle(before, n)})
            result = 0 if safe_idle(before, n) else 1
        else:
            def cancel_once():
                return subprocess.run([str(CTL), "cancel"], check=True, timeout=40, capture_output=True, text=True)
            n = native_read()
            record["native_before"] = n
            try:
                cleared = clear(before, native_read, cancel_once,
                                allow_active_cancel=args.allow_active_cancel)
                record.update(cleared)
                result = 0
            except CancelFailure as exc:
                record.update(exc.evidence)
                result = 1
            except Exception as exc:
                record.update({"outcome": "refused-or-failed", "error": f"{type(exc).__name__}: {exc}"})
                result = 1
        record["finished_at"] = datetime.now(timezone.utc).astimezone().isoformat()
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps({"evidence": str(evidence_path), **record}, indent=2))
        if result:
            print(f"clear_job: {record.get('outcome', record.get('error', 'operation failed'))}", file=sys.stderr)
        return result
    except Exception as exc:
        record.update({"finished_at": datetime.now(timezone.utc).astimezone().isoformat(),
                       "outcome": "evidence-or-operation-failed", "error": f"{type(exc).__name__}: {exc}"})
        try:
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            evidence_path.write_text(json.dumps(record, indent=2) + "\n")
        except Exception as write_exc:
            print(f"clear_job: evidence persistence failed at {evidence_path}: {write_exc}", file=sys.stderr)
        print(f"clear_job: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
