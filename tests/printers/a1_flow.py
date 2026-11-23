"""The Bambu Lab A1 production path, offline, in a process configured as bambulab.a1.

    PRINT_HANDY__PRINTER__MODEL=bambulab.a1 python3 -m tests.printers.a1_flow <workdir> \
        (--resources ORCA_RESOURCES | --fixture-resources DIR) [--fake-gcode FILE] [--stl FILE]

Runs what the Discord intake runs after an approval: jobs.slice.slice_job (slice, post-slice guards, offline
verification, JOB.json binding), then delivery.bambu_handoff (the .gcode.3mf package), then launch.print_now.print_now,
which must refuse. Every network attempt is recorded and refused except the Spoolman spool lookup, answered
locally. Nothing contacts a printer. With --fake-gcode, Orca is replaced by a copy of that file; without it the
configured Orca command slices for real. The A1 templates are generated first into the configured profile:
--resources from an installed OrcaSlicer (production identities only), --fixture-resources from the invented
test tree, trusted for this process only. Prints one JSON line with the results.
"""
import copy
import json
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "tests" / "pipeline" / "fixtures" / "printer_hardware.json"


def a1_state(*_args, **_kwargs):
    st = copy.deepcopy(json.loads(STATE.read_text()))
    st.update(printer="Bambu Lab A1", moonraker_url="", filament_slots={}, bed_mm=[256, 256], max_print_height_mm=256,
              axis_limits_mm={"x": [0, 256], "y": [0, 256], "z": [0, 256]})
    st["nozzle"]["type"] = "stainless_steel"
    return st


def main(argv):
    work = Path(argv[0])
    fake = Path(argv[argv.index("--fake-gcode") + 1]) if "--fake-gcode" in argv else None
    stl = Path(argv[argv.index("--stl") + 1]) if "--stl" in argv else ROOT / "tests" / "slicer" / "fixtures" / "cube.stl"
    from print_handy import printers
    from print_handy.core import delivery, orca_adapter, printer_state, slice_guard
    from print_handy.jobs import common as jobs_common
    from print_handy.jobs import slice as jobs_slice
    from print_handy.jobs import store as jobs_store
    from print_handy.launch import print_now as launch_print_now
    from print_handy.printers.bambulab import profiles
    from tests import bound_names

    assert printers.current().key == "bambulab.a1", printers.current().key
    if "--fixture-resources" in argv:
        from . import a1_fixture
        resources = Path(argv[argv.index("--fixture-resources") + 1])
        a1_fixture.trust(resources=resources)
    else:
        resources = Path(argv[argv.index("--resources") + 1])
    identity = profiles.install(printers.current(), resources)
    contacted = []

    def refuse(url, *a, **k):
        contacted.append(str(getattr(url, "full_url", url)))
        raise RuntimeError(f"network refused in the offline A1 flow: {url}")

    def spoolman(url, data=None, method=None, timeout=6):
        contacted.append(url)
        if "/api/v1/spool/" in url:
            return {"id": 10, "archived": False, "filament": {"name": "white", "material": "PLA"},
                    "remaining_weight": 900}
        return refuse(url)

    urllib.request.urlopen = refuse
    printer_state.load = a1_state
    printer_state._get = spoolman
    for module in bound_names.binders(jobs_common, "_mr"):
        module._mr = refuse
    if fake is not None:
        real_run = orca_adapter.subprocess.run

        def run(command, *a, **k):
            if "--outputdir" not in command:
                return real_run(command, *a, **k)
            out = Path(command[command.index("--outputdir") + 1]) / "plate_1.gcode"
            out.write_text(fake.read_text())
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        orca_adapter.subprocess.run = run

    job_dir = work / "job"
    (job_dir / "source").mkdir(parents=True)
    source = job_dir / "source" / stl.name
    shutil.copy(stl, source)
    job_path = job_dir / "JOB.json"
    jobs_store.write_job(job_dir, {"downloaded_file": str(source), "audit": {"plates": 1}, "approvals": [],
                                  "slug": "a1-flow"})
    result = {"model": printers.current().key, "connection": printers.current().connection,
              "profiles": identity["label"], "templates": str(printers.current().templates_dir())}
    try:
        out = jobs_slice.slice_job(job_path)
        job = json.loads(job_path.read_text())
        result.update(sliced=str(out), printer_model=job.get("printer_model"), gcode_verified=job.get("gcode_verified"),
                      slice_guard=job["provenance"]["slice_guard"]["status"],
                      generator=slice_guard.generator_identity(Path(out).read_text(errors="replace")))
        packages = delivery.bambu_handoff(job, [out])
        result["packages"] = [str(p) for p in packages]
        before = job_path.read_text()
        try:
            launch_print_now.print_now(job_path)
            result["print_now"] = "NOT REFUSED"
        except SystemExit as exc:
            result["print_now"] = str(exc)
        result["job_unchanged_by_print_now"] = job_path.read_text() == before
    except Exception as exc:  # reported, not raised: the caller asserts on the JSON
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["contacted"] = contacted
    print("A1_FLOW " + json.dumps(result))


if __name__ == "__main__":
    main(sys.argv[1:])
