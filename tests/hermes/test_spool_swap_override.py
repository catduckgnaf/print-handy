"""Mid-job spool swap: the active spool record follows the swap, and print_next passes the operator's
confirmation to print-now."""
import asyncio
import json

from print_handy.runtime import print_next, spool_sync


def _spool(sid, name, material="PLA"):
    return {"id": sid, "filament": {"name": name, "material": material, "density": 1.24, "diameter": 1.75,
                                   "color_hex": "6F4E37"}}


def test_spool_change_refreshes_the_active_spool_name(tmp_path):
    state = tmp_path / "hw.json"
    state.write_text(json.dumps({
        "spool": {"spoolman_id": 3, "name": "Pink PLA", "material": "PLA", "density": 1.24, "diameter_mm": 1.75},
        "filament_slots": {"2": {"spoolman_id": 3, "material": "PLA", "color": "pink"}}}))
    spools = {3: _spool(3, "Pink PLA"), 7: _spool(7, "Coffee PLA+", "PLA+")}

    def client(method, path, body=None):
        return spools[int(path.rsplit("/", 1)[1])] if method == "GET" else {}

    def printer(method, path, body=None):
        return {"result": {"spool_id": None}}

    spool_sync.bind_slot(2, 7, client=client, mr=printer, state_path=state, push_screen_update=False)
    active = json.loads(state.read_text())["spool"]
    assert active["spoolman_id"] == 7 and active["name"] == "Coffee PLA+" and active["material"] == "PLA+"


def _target(tmp_path, **extra):
    gcode = tmp_path / "plate2.gcode"
    gcode.write_text("G1 X1\n")
    return {"plan_id": "p", "part_id": "plate2", "copy": 1, "gcode": str(gcode),
            "gcode_sha256": print_next._sha256(gcode), **extra}


def _run(tmp_path, target):
    fake = tmp_path / "fake_model_job.sh"
    fake.write_text('#!/bin/sh\necho "$@"\nexit 0\n')
    fake.chmod(0o755)
    return asyncio.run(print_next.start(target, model_job=fake, idle_check=lambda: None,
                                        claims_dir=tmp_path / "claims"))


def test_print_next_passes_the_confirmation_to_print_now(tmp_path):
    ok, out = _run(tmp_path, _target(tmp_path, accept_spool_change=True))
    assert ok and "--accept-spool-change" in out


def test_print_next_does_not_pass_it_by_default(tmp_path):
    ok, out = _run(tmp_path, _target(tmp_path))
    assert ok and "--accept-spool-change" not in out
