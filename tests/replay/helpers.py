"""Recorded-state helpers for the replay tests: fixtures, the hardware record, jobs, and how the chat reads a start."""
from __future__ import annotations

import contextlib
import io
import json
import re
import traceback
from pathlib import Path

from print_handy.core import prepared_intake
from print_handy.jobs import cli as jobs_cli
from print_handy.launch import print_now as launch_print_now

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "printers" / "ad5x_zmod"
MOONRAKER_TIMEOUT_S = 30  # model_job._mr's default timeout, in printer seconds
SCALE = 1 / 20  # one printer second runs in 50 ms


def recorded(name):
    return json.loads((FIXTURES / name).read_text())


def reported_name(remote):
    """The file name ZMod reports for an upload: X.gcode.3mf unpacks to 3mf_print/X.gcode (#691)."""
    return "3mf_print/" + remote[:-len(".3mf")] if remote.endswith(".gcode.3mf") else remote


def hardware_record(url):
    """The hardware record of the recorded AD5X: screenless ZMod, four slots as the printer reports them.

    temps_c is deliberately the stale printer-wide 210/60 an earlier PLA spool left (#692)."""
    status = recorded("objects_query.idle.json")["result"]["status"]
    variables = status["save_variables"]["variables"]
    spools = {"1": (16, "PLA", "white"), "2": (17, "Silk PLA", "Rose red blue green"),
              "3": (15, "PLA+", "black"), "4": (12, "PLA", "dark green")}
    slots = {}
    for row in status["zmod_color"]["slots"]:
        sid, material, name = spools[str(row["ID"])]
        slots[str(row["ID"])] = {"spoolman_id": sid, "color": name, "material": material, "hex": row["HEX"],
                                 "physical_color_verified": True, "physical_loading_verified": True}
    probe = recorded("configfile_settings.json")["result"]["status"]["configfile"]["settings"]["probe"]
    return {
        "printer": "Flashforge AD5X", "moonraker_url": url, "spoolman_url": url,
        "bed_mm": [220, 220], "max_print_height_mm": 220,
        "nozzle": {"diameter_mm": 0.4, "type": "stock", "installed": "2026-01-01T00:00:00"},
        "z_offset_mm": variables["gcode_offsets"]["z"], "z_offset_verified": True,
        "temps_c": {"nozzle": 210, "bed": 60},
        # The stale record names the PLA spool that set it, not the Silk spool now loaded (#692).
        "temperature_resolution": {"material_family": "PLA", "material": "PLA", "spoolman_id": 12,
                                   "source": "material default / Spoolman filament"},
        "spool": {"spoolman_id": 17, "name": "Rose red blue green", "material": "Silk PLA",
                  "density": 1.24, "diameter_mm": 1.75, "color_code": "0C6283"},
        "filament_slots": slots,
        "axis_limits_mm": {"x": [0, 223], "y": [0, 223], "z": [0, 250]},
        "calibration_baseline": {"native_mesh": "MESH_DATA", "native_zProbeOffset": 0.025,
                                 "probe_z_offset": probe["z_offset"]},
        "calibration_authority": {"mode": "screenless", "operator_confirmed": True},
    }


def sliced_gcode(nozzle=220, bed=60):
    """The recorded Silk PLA slot-2 slice (real header and config block) at a chosen nozzle target."""
    text = (FIXTURES / "silk_pla_slot2_sliced.gcode.txt").read_text()
    for key in ("nozzle_temperature", "nozzle_temperature_initial_layer", "first_layer_temperature"):
        text = re.sub(rf"^; {key} = .*$", f"; {key} = {nozzle}", text, flags=re.M)
    return text.replace("@NOZZLE@", str(nozzle)).replace("@BED@", str(bed))


def make_job(root, name="replay", nozzle=220, title="Replay Silk Part"):
    """A prepared JOB.json beside its slice, as print-now receives it."""
    folder = Path(root) / name
    (folder / "out").mkdir(parents=True)
    gcode = folder / "out" / "plate_1.gcode"
    gcode.write_text(sliced_gcode(nozzle))
    job = folder / "JOB.json"
    job.write_text(json.dumps({"title": title, "gcode": str(gcode), "printer_model": "flashforge.ad5x"}))
    return job, gcode


def run_print_now(job_path, wait_s=1):
    """What print_next.start sees from `print-now`: (succeeded, the text the process printed).

    Runs the real CLI entry point in this process; a SystemExit carries the final line, as it does
    on stderr in the child process."""
    # Before #693 the CLI called print_now directly, so a bare exception ended the process with a traceback.
    entry = getattr(jobs_cli, "_print_now_cli", None) or launch_print_now.print_now
    out = io.StringIO()
    ok = True
    with contextlib.redirect_stdout(out):
        try:
            entry(str(job_path), wait_s=wait_s)
        except SystemExit as exc:
            ok = False
            print(exc)
        except Exception:
            ok = False
            traceback.print_exc(file=out)
    return ok, out.getvalue()


def intake_outcome(ok, tail):
    """The status and chat wording the intake derives from a print-now run.

    Mirrors the classification in prepared_intake.prepare_link (the `phase` / `unknown` / `pending`
    block after print_next.start returns), calling its real parsers: a refusal reads "Didn't start",
    an unresolved start reads START_PENDING, an accepted one STARTED."""
    phase = ""
    for line in reversed(str(tail).splitlines()):
        if "MAPPED START READBACK " in line:
            phase = str(json.loads(line.split("MAPPED START READBACK ", 1)[1]).get("phase") or "")
            break
    unknown = (phase in ("preparing_or_unresolved", "awaiting_confirmation")
               or (not ok and not prepared_intake._safe_preupload_refusal(tail)))
    pending = unknown or phase in ("preparing", "executing")
    status = "START_PENDING" if unknown else "STARTED" if ok or pending else "START_REFUSED"
    message = prepared_intake.plain_start_failure(tail) if status == "START_REFUSED" else ""
    return status, message
