"""Bambu Lab AMS mapping + dual-nozzle models (#308): offline tests only — fake MQTT/FTPS.

Three layers: the project_file wire forms are pinned exactly (payload hygiene from the
#298 audit), the pure resolver is exercised against invented snapshots, and
model_job._print_bambu is driven with the guard seams stubbed to prove the mapping is
resolved inside the lease before the approval is consumed — mapped multi-colour starts,
unmapped multi-colour refuses with nothing sent and the approval intact.
"""
import json
import zipfile

import pytest

from print_handy import printers, settings
from print_handy.core import delivery, launch_state, model_job
from print_handy.printers.bambulab import ams_mapping, facts
from print_handy.printers.bambulab.client import project_file_payload

from . import bambu_family_fixture as fixture
from .test_bambu_print import CODE, DEV_ON, SERIAL, FakeClock, FakeFTP, FakeMqtt, RECORD

SID = "1700000000000"


def payload_for(mapping, **kw):
    return project_file_payload("benchy.gcode.3mf", ams_mapping=mapping, submission_id=SID, **kw)["print"]


# --------------------------------------------------------------------- payload wire forms

def test_payload_single_colour_ams_tray():
    body = payload_for([2])
    assert body["ams_mapping"] == [2] and body["ams_mapping2"] == [{"ams_id": 0, "slot_id": 2}]
    assert body["use_ams"] is True and isinstance(body["use_ams"], bool)
    assert body["sequence_id"] == "20000" and body["md5"] == "" and "cfg" not in body
    assert body["project_id"] == body["task_id"] == body["subtask_id"] == SID
    assert "nozzle_mapping" not in body  # omitted unless a rack resolution is supplied


def test_payload_multi_colour_and_ams_ht_passthrough():
    body = payload_for([0, 5, 131])
    assert body["ams_mapping"] == [0, 5, 131]
    assert body["ams_mapping2"] == [{"ams_id": 0, "slot_id": 0}, {"ams_id": 1, "slot_id": 1},
                                    {"ams_id": 131, "slot_id": 0}]
    assert body["use_ams"] is True


def test_payload_external_spool_only_is_not_ams():
    body = payload_for([254])
    # The flat list converts the external spool to -1 (raw 254/255 is rejected by the
    # firmware); the detailed list carries it. use_ams reconciles to false.
    assert body["ams_mapping"] == [-1] and body["ams_mapping2"] == [{"ams_id": 254, "slot_id": 0}]
    assert body["use_ams"] is False


def test_payload_unmapped_slot_and_mixed_external():
    body = payload_for([3, -1, 255])
    assert body["ams_mapping"] == [3, -1, -1]
    assert body["ams_mapping2"] == [{"ams_id": 0, "slot_id": 3}, {"ams_id": 255, "slot_id": 255},
                                    {"ams_id": 255, "slot_id": 0}]
    assert body["use_ams"] is True  # one real tray is enough


def test_payload_use_ams_is_never_an_int():
    # Dual-nozzle firmware reads an integer use_ams as a nozzle index (audit): bool only.
    for mapping in (None, [0], [254], [-1], [0, 254]):
        assert type(payload_for(mapping)["use_ams"]) is bool


def test_payload_legacy_default_is_the_pre_308_shape():
    body = payload_for(None)
    assert body["ams_mapping"] == [0] and body["ams_mapping2"] == [{"ams_id": 0, "slot_id": 0}]
    assert body["use_ams"] is False


def test_payload_nozzle_mapping_only_when_supplied():
    body = payload_for([0], nozzle_mapping=[{"nozzle_id": 16}])
    assert body["nozzle_mapping"] == [{"nozzle_id": 16}]


def test_payload_fresh_submission_ids_are_shared_and_clamped(monkeypatch):
    a = project_file_payload("x.gcode.3mf")["print"]
    assert a["project_id"] == a["task_id"] == a["subtask_id"]
    assert len(a["project_id"]) <= 13 and a["project_id"].isdigit()
    monkeypatch.setattr("time.time", lambda: 10**20)  # far-future clock: the id clamps to 13 digits
    assert project_file_payload("x.gcode.3mf")["print"]["project_id"] == "9999999999999"


# --------------------------------------------------------------------- resolver

def snap(ams_units=(), vt=None):
    payload = {"ams": {"ams": list(ams_units)}}
    if vt is not None:
        payload["vt_tray"] = vt
    return {"print": payload}


def unit(uid, trays, extruder=None):
    data = {"id": str(uid), "tray": [{"id": str(i), "tray_type": t} for i, t in enumerate(trays)]}
    if extruder is not None:
        data["extruder"] = extruder
    return data


def test_resolver_matches_materials_to_distinct_trays():
    text = "; filament_type = PLA;PETG\n"
    assert ams_mapping.resolve_for_print(text, snap([unit(0, ["PLA", "PETG", "PLA"])])) == ([0, 1], "snapshot")
    # One tray cannot serve two slots of the same material.
    with pytest.raises(ams_mapping.MappingError, match="filament slot 2"):
        ams_mapping.resolve_for_print("; filament_type = PLA;PLA\n", snap([unit(0, ["PLA"])]))


def test_resolver_external_spool_is_a_candidate():
    out, source = ams_mapping.resolve_for_print("; filament_type = PLA\n", snap(vt={"tray_type": "PLA"}))
    assert (out, source) == ([254], "snapshot")


def test_resolver_single_unmatched_falls_back_to_legacy_but_multi_refuses():
    assert ams_mapping.resolve_for_print("; filament_type = PLA\n", snap()) == (None, "legacy")
    with pytest.raises(ams_mapping.MappingError, match="no loaded AMS tray"):
        ams_mapping.resolve_for_print("; filament_type = PLA;PETG\n", snap([unit(0, ["PLA"])]))


def test_resolver_job_mapping_is_trusted_verbatim_and_all_unmapped_recomputes():
    text = "; filament_type = PLA;PETG\n"
    assert ams_mapping.resolve_for_print(text, snap(), job_mapping=[7, 254]) == ([7, 254], "job")
    assert ams_mapping.resolve_for_print(text, snap([unit(0, ["PLA", "PETG"])]),
                                         job_mapping=[-1, -1]) == ([0, 1], "snapshot")
    with pytest.raises(ams_mapping.MappingError, match="exactly 2 tray id"):
        ams_mapping.resolve_for_print(text, snap(), job_mapping=[0])
    with pytest.raises(ams_mapping.MappingError, match="not a canonical tray id"):
        ams_mapping.resolve_for_print(text, snap(), job_mapping=[0, 999])


def test_resolver_partial_job_mapping_resolves_the_unmapped_slots_and_keeps_manual_choices():
    # P1-68: [0, -1] must not be returned verbatim. The manual tray 0 stands;
    # the unmapped slot is resolved from the snapshot's reported trays.
    text = "; filament_type = PLA;PETG\n"
    assert ams_mapping.resolve_for_print(text, snap([unit(0, ["PLA", "PETG"])]),
                                         job_mapping=[0, -1]) == ([0, 1], "job")
    # The manual choice wins even when the snapshot would resolve differently:
    # slot 1 is pinned to tray 3, slot 2 resolves to the only PETG tray left.
    assert ams_mapping.resolve_for_print(text, snap([unit(0, ["PETG", "PLA", "PLA", "PLA"])]),
                                         job_mapping=[3, -1]) == ([3, 0], "job")
    # An unmapped first slot resolves too, and a resolved tray is never reused.
    assert ams_mapping.resolve_for_print(text, snap([unit(0, ["PLA", "PETG"])]),
                                         job_mapping=[-1, 1]) == ([0, 1], "job")


def test_resolver_partial_job_mapping_that_cannot_be_completed_refuses():
    # P1-68: no tray can serve the unmapped slot, so the job must not start
    # with an unresolved slot (the complete-mapping gate).
    text = "; filament_type = PLA;PETG\n"
    with pytest.raises(ams_mapping.MappingError, match="filament slot 2"):
        ams_mapping.resolve_for_print(text, snap([unit(0, ["PLA"])]), job_mapping=[0, -1])
    with pytest.raises(ams_mapping.MappingError, match="filament slot 2"):
        ams_mapping.resolve_for_print(text, snap(), job_mapping=[0, -1])


def test_resolver_job_mapping_cannot_assign_one_tray_to_two_slots():
    # Bonus (found during SAFE-11): resolve() enforces "a tray serves one slot",
    # but a supplied mapping was shape-checked only per entry, so [0, 0] passed
    # verbatim — one physical tray claimed by two different filaments.
    text = "; filament_type = PLA;PETG\n"
    with pytest.raises(ams_mapping.MappingError, match="more than one filament slot"):
        ams_mapping.resolve_for_print(text, snap([unit(0, ["PLA", "PETG"])]), job_mapping=[0, 0])
    with pytest.raises(ams_mapping.MappingError, match="more than one filament slot"):
        ams_mapping.resolve_for_print(text, snap(), job_mapping=[254, 254])
    # Repeated unmapped markers are not tray assignments and stay legal.
    assert ams_mapping.resolve_for_print(text, snap([unit(0, ["PLA", "PETG"])]),
                                         job_mapping=[-1, -1]) == ([0, 1], "snapshot")


def test_resolver_dual_nozzle_filters_by_the_units_reported_extruder():
    text = "; filament_type = PLA;PETG\n; filament_extruder_id = 2,1\n"  # slot 1 -> left (1), slot 2 -> right (0)
    dual_snap = snap([unit(0, ["PETG"], extruder=0), unit(1, ["PLA"], extruder=1)])
    assert ams_mapping.resolve_for_print(text, dual_snap, dual=True) == ([4, 0], "snapshot")
    # A unit that reports no extruder is never assumed: nothing is a candidate.
    unknown = snap([unit(0, ["PETG", "PLA"])])
    with pytest.raises(ams_mapping.MappingError, match="for extruder 1"):
        ams_mapping.resolve_for_print(text, unknown, dual=True)
    # Single-nozzle matching ignores extruders entirely.
    assert ams_mapping.resolve_for_print(text, unknown) == ([1, 0], "snapshot")


def test_resolver_fts_skips_the_extruder_filter():
    text = "; filament_type = PLA;PETG\n; filament_extruder_id = 2,1\n"
    # Both trays sit on the extruder-0 unit: without FTS the extruder-1 slot has no
    # candidate; with FTS any tray routes anywhere and the material match wins.
    reqs = ams_mapping.requirements_from_gcode(text)
    trays = ams_mapping.trays_from_snapshot(snap([unit(0, ["PLA", "PETG"], extruder=0)]))
    assert ams_mapping.resolve(reqs, trays, dual=True, fts=True) == [0, 1]
    with pytest.raises(ams_mapping.MappingError):
        ams_mapping.resolve(reqs, trays, dual=True, fts=False)


def test_trays_from_snapshot_shapes():
    trays = ams_mapping.trays_from_snapshot(snap(
        [unit(2, ["PLA", "", "PETG"]), {"id": "130", "tray": [{"id": "0", "tray_type": "TPU"}]}],
        vt={"tray_type": "ABS"}))
    by_id = {t["tray"]: t for t in trays}
    assert set(by_id) == {8, 10, 130, 254}  # empty slot skipped; HT id passes through; vt -> 254
    assert by_id[254]["external"] is True and by_id[130]["material"] == "TPU"


# --------------------------------------------------------------------- print path (fake MQTT)

def ams_snapshot(state="IDLE", filename="", ams_units=(), vt=None, fun=DEV_ON):
    version = {"info": {"command": "get_version", "module": [{"name": "ota", "sw_ver": "01.05.00.00"}]}}
    full = {"print": {"command": "push_status", "msg": 0, "gcode_state": state, "mc_percent": 0,
                      "subtask_name": filename, "mc_print_stage": "1", "fun": fun, "print_error": 0,
                      "ams": {"ams": list(ams_units)}}}
    if vt is not None:
        full["print"]["vt_tray"] = vt
    return [version, full]


def lan_adapter_for(monkeypatch, key):
    monkeypatch.setenv("BAMBU_LAN_PASSWORD", CODE)
    cfg = settings.load(local=False, environ={
        "PRINT_HANDY__PRINTER__MODEL": key,
        "PRINT_HANDY__BAMBU__LAN_HOST": "192.168.1.50", "PRINT_HANDY__BAMBU__LAN_SERIAL": SERIAL})
    return printers.get(key, cfg)


class _Lease:
    def __init__(self, events):
        self.events = events

    def submitting(self):
        self.events.append("submitting")

    def uncertain(self, detail):
        self.events.append("uncertain")

    def release(self):
        self.events.append("release")


@pytest.fixture
def rig(monkeypatch, tmp_path):
    events = []
    package = tmp_path / "plate_1.gcode.3mf"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", "G1 X1\n")
    gcode = tmp_path / "plate_1.gcode"
    monkeypatch.setattr(model_job, "verify_gcode", lambda path: {"offline": True})
    monkeypatch.setattr(delivery, "bambu_handoff", lambda job, gcodes, config=None: [package])
    monkeypatch.setattr(launch_state, "acquire", lambda *a, **kw: events.append("acquire") or _Lease(events))
    monkeypatch.setattr(model_job, "_consume_approval", lambda *a, **kw: events.append("consume_approval"))
    monkeypatch.setattr(model_job, "record_start", lambda g, remote: "/records/1")
    clock = FakeClock()
    monkeypatch.setattr(model_job, "_confirm_clock", clock)
    monkeypatch.setattr(model_job, "_confirm_sleep", clock.sleep)
    monkeypatch.setattr(model_job, "_bambu_start_confirm_window", lambda: 30.0)
    return {"package": package, "gcode": gcode, "events": events, "ftp": FakeFTP(),
            "job_path": tmp_path / "JOB.json"}


def wire(monkeypatch, adapter, scripts, ftp):
    from print_handy.printers.bambulab.client import BambuLanClient
    queue = iter(script if isinstance(script, FakeMqtt) else FakeMqtt(script) for script in scripts)
    fakes = []

    def factory(h, s, c):
        fake = next(queue)
        fakes.append(fake)
        return fake

    client = BambuLanClient("192.168.1.50", SERIAL, CODE, mqtt_factory=factory,
                            ftp_factory=lambda h, c: ftp, timeout_s=5)
    monkeypatch.setattr(adapter, "client", lambda **kw: client)
    return fakes


def run_print(rig, adapter, gcode_text, job=None):
    rig["gcode"].write_text(gcode_text)
    return model_job._print_bambu(adapter, RECORD, rig["gcode"],
                                  job or {"printer_model": adapter.key}, rig["job_path"], 25, False)


def published_project_file(fakes):
    for fake in fakes:
        for _topic, payload in fake.published:
            if payload.get("print", {}).get("command") == "project_file":
                return payload["print"]
    return None


MULTI = "; filament_type = PLA;PETG\nG1 X10 Y10 E1\nT1\nG1 X20 Y20 E1\n"


def test_print_path_multi_colour_mapped_sends_the_resolved_mapping(monkeypatch, rig):
    adapter = lan_adapter_for(monkeypatch, "bambulab.a1")
    frames = ams_snapshot(ams_units=[unit(0, ["PLA", "PETG"])])
    fakes = wire(monkeypatch, adapter, [frames, frames, [],
                                        ams_snapshot(state="RUNNING", filename=rig["package"].name,
                                                     ams_units=[unit(0, ["PLA", "PETG"])])], rig["ftp"])
    out = run_print(rig, adapter, MULTI)
    assert out["started"] is True and out["ams_mapping"] == [0, 1] and out["ams_mapping_source"] == "snapshot"
    body = published_project_file(fakes)
    assert body["ams_mapping"] == [0, 1] and body["use_ams"] is True and type(body["use_ams"]) is bool
    assert rig["events"] == ["acquire", "consume_approval", "submitting", "release"]


def test_print_path_multi_colour_unmapped_refuses_before_the_approval(monkeypatch, rig):
    adapter = lan_adapter_for(monkeypatch, "bambulab.a1")
    frames = ams_snapshot(ams_units=[unit(0, ["PLA"])])  # no PETG anywhere
    fakes = wire(monkeypatch, adapter, [frames, frames], rig["ftp"])
    with pytest.raises(SystemExit, match="REFUSED.*no loaded AMS tray"):
        run_print(rig, adapter, MULTI)
    assert published_project_file(fakes) is None and rig["ftp"].stored == {}
    assert "consume_approval" not in rig["events"]  # the bed-clear approval survives the refusal


def test_print_path_job_supplied_mapping_is_sent_verbatim(monkeypatch, rig):
    adapter = lan_adapter_for(monkeypatch, "bambulab.a1")
    frames = ams_snapshot(ams_units=[unit(0, ["PETG", "PLA"])])
    fakes = wire(monkeypatch, adapter, [frames, frames, [],
                                        ams_snapshot(state="RUNNING", filename=rig["package"].name,
                                                     ams_units=[unit(0, ["PETG", "PLA"])])], rig["ftp"])
    out = run_print(rig, adapter, MULTI, job={"printer_model": "bambulab.a1", "ams_mapping": [1, 0]})
    assert out["ams_mapping_source"] == "job"
    assert published_project_file(fakes)["ams_mapping2"] == [{"ams_id": 0, "slot_id": 1},
                                                             {"ams_id": 0, "slot_id": 0}]


def test_print_path_external_spool_only(monkeypatch, rig):
    adapter = lan_adapter_for(monkeypatch, "bambulab.a1")
    frames = ams_snapshot(vt={"tray_type": "PLA", "tray_color": "FFFFFFFF"})
    fakes = wire(monkeypatch, adapter, [frames, frames, [],
                                        ams_snapshot(state="RUNNING", filename=rig["package"].name,
                                                     vt={"tray_type": "PLA"})], rig["ftp"])
    out = run_print(rig, adapter, "; filament_type = PLA\nG1 X10 Y10 E1\n")
    body = published_project_file(fakes)
    assert out["ams_mapping"] == [254] and body["ams_mapping"] == [-1] and body["use_ams"] is False


def test_print_path_single_colour_without_reported_trays_keeps_the_legacy_payload(monkeypatch, rig):
    adapter = lan_adapter_for(monkeypatch, "bambulab.a1")
    frames = ams_snapshot()
    fakes = wire(monkeypatch, adapter, [frames, frames, [],
                                        ams_snapshot(state="RUNNING", filename=rig["package"].name)], rig["ftp"])
    out = run_print(rig, adapter, "; filament_type = PLA\nG1 X10 Y10 E1\n")
    body = published_project_file(fakes)
    assert out["ams_mapping"] is None and out["ams_mapping_source"] == "legacy"
    assert body["ams_mapping"] == [0] and body["use_ams"] is False


def test_print_path_dual_nozzle_routes_per_extruder(monkeypatch, rig):
    adapter = lan_adapter_for(monkeypatch, "bambulab.h2d")
    units = [unit(0, ["PETG"], extruder=0), unit(1, ["PLA"], extruder=1)]
    text = "; filament_type = PLA;PETG\n; filament_extruder_id = 2,1\nG1 X10 Y10 E1\nT1\nG1 X20 Y20 E1\n"
    frames = ams_snapshot(ams_units=units)
    fakes = wire(monkeypatch, adapter, [frames, frames, [],
                                        ams_snapshot(state="RUNNING", filename=rig["package"].name,
                                                     ams_units=units)], rig["ftp"])
    out = run_print(rig, adapter, text)
    assert out["ams_mapping"] == [4, 0]  # PLA slot -> left unit's tray 4; PETG slot -> right unit's tray 0
    assert published_project_file(fakes)["use_ams"] is True


# --------------------------------------------------------------------- dual models + guard

DUAL_MODELS = {"bambulab.h2d": "H2D", "bambulab.h2dpro": "H2DPRO", "bambulab.x2d": "X2D",
               "bambulab.h2c": "H2C"}  # H2C joined with OrcaSlicer v2.5.0-alpha (#356)


@pytest.mark.parametrize("key", sorted(DUAL_MODELS))
def test_dual_model_registration(key, monkeypatch):
    f = getattr(facts, DUAL_MODELS[key])
    cfg = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": key,
                                              "PRINT_HANDY__PATHS__HERMES_HOME": str(fixture.HERMES)})
    a = printers.get(key, cfg)
    assert a.extruder_count() == 2 and (a.slot_count, a.multi_slot) == (1, False)
    # #671's treatment is load-bearing, not just a label: the model stays
    # "single" in the settings layer too, so a slot count above 1 is refused
    # for it exactly as for any single model.
    with pytest.raises(settings.SettingsError, match="filament_slots must be 1"):
        settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": key,
                                            "PRINT_HANDY__PRINTER__FILAMENT_SLOTS": "4",
                                            "PRINT_HANDY__PATHS__HERMES_HOME": str(fixture.HERMES)})
    assert a.build_volume().bed_mm == tuple(float(v) for v in f["bed_mm"].value)
    fixture.trust(type(a), f, monkeypatch.setattr)
    folder = fixture.install(a, f)
    from print_handy.printers.bambulab import profiles
    assert profiles.problems(a) == []
    machine = json.loads((folder / "machine.json").read_text())
    assert machine["nozzle_diameter"] == ["0.4", "0.4"]


PETG_H2D = "Generic PETG @BBL H2D"  # the checked PETG profile name in facts.H2D's orca_filaments


def _install_dual(adapter, f, monkeypatch):
    """Install fixture templates for H2D with a second (PETG) checked filament profile.

    The fixture tree gains a filament resource under the real checked PETG profile
    name, and the adapter's facts are swapped for fixture facts whose identity
    covers both profiles — the same trust swap the family fixture performs for
    the default profile, extended to the model's orca_filaments set.
    """
    import tempfile
    from pathlib import Path
    from print_handy.printers.bambulab import profiles
    from print_handy.printers.bambulab.facts import Fact
    root = fixture.resources(Path(tempfile.mkdtemp(prefix="bambu-dual-resources-")), f)
    bbl = root / "profiles" / "BBL"
    petg = {"type": "filament", "name": PETG_H2D, "from": "system", "instantiation": "true",
            "filament_type": ["PETG"], "nozzle_temperature": ["250", "250"],
            "nozzle_temperature_initial_layer": ["250", "250"],
            "filament_start_gcode": ["; fixture petg start\\n"], "filament_end_gcode": ["; fixture petg end\\n"],
            "compatible_printers": [f["orca_profiles"].value["machine"]]}
    (bbl / "filament" / "filament2.json").write_text(json.dumps(petg))
    index = json.loads((root / "profiles" / "BBL.json").read_text())
    index["filament_list"].append({"name": PETG_H2D, "sub_path": "filament/filament2.json"})
    (root / "profiles" / "BBL.json").write_text(json.dumps(index))
    petg_flat = profiles.flatten_filaments(root, [PETG_H2D])[PETG_H2D]
    ident = {**fixture.identity(f), "filaments": {PETG_H2D: profiles.sha256(petg_flat)}}
    names = f["orca_profiles"].value
    swapped = {**f, "orca_identity": Fact((ident,), "TEST", False),
               "orca_filaments": Fact({"PLA": names["filament"], "PETG": PETG_H2D}, "TEST", False)}
    monkeypatch.setattr(type(adapter), "facts", swapped)
    profiles.install(adapter, root)
    return adapter.pinned_templates_folder()


def _dual_gcode(adapter, f, second_type="PETG", tools=("T1",)):
    """A synthetic dual-filament file against the installed dual fixture templates."""
    folder = adapter.pinned_templates_folder()
    machine = json.loads((folder / "machine.json").read_text())
    filament = json.loads((folder / "filament.json").read_text())
    start = machine["machine_start_gcode"].replace("[bed_temperature_initial_layer_single]", "60") \
        .replace("[nozzle_temperature_initial_layer]", "220")
    body = "G1 Z.2 F720\nG1 X100 Y100 F30000\nG1 X120 Y100 E.8\n" + "".join(f"{t}\nG1 X130 Y110 E.8\n" for t in tools)
    names = f["orca_profiles"].value
    return "\n".join([
        "; HEADER_BLOCK_START", "; generated by OrcaSlicer 2.4.2", "; HEADER_BLOCK_END", "; CONFIG_BLOCK_START",
        f"; printer_model = {f['printer_model'].value}", f"; printer_settings_id = {names['machine']}",
        f"; print_settings_id = {names['process']}",
        f"; filament_settings_id = \"{names['filament']}\";\"{PETG_H2D}\"",
        f"; filament_type = PLA;{second_type}", "; nozzle_diameter = 0.4,0.4", "; nozzle_type = hardened_steel",
        "; curr_bed_type = Textured PEI Plate", "; z_hop_types = Normal Lift", "; enable_arc_fitting = 0",
        "; brim_type = outer_only", "; CONFIG_BLOCK_END", "; EXECUTABLE_BLOCK_START",
        "M73 P0 R20", "; FEATURE: Custom", start, filament["filament_start_gcode"][0].replace("\\n", ""),
        "G90", "G21", "M83 ; use relative distances for extrusion", "M106 S0",
        "; CHANGE_LAYER", "; Z_HEIGHT: 0.2", body, "; FEATURE: Custom",
        filament["filament_end_gcode"][0].replace("\\n", ""), machine["machine_end_gcode"], "M73 P100 R0",
        "; EXECUTABLE_BLOCK_END", ""])


def test_dual_guard_accepts_a_fully_declared_two_filament_file(monkeypatch):
    f = facts.H2D
    cfg = settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": "bambulab.h2d",
                                              "PRINT_HANDY__PATHS__HERMES_HOME": str(fixture.HERMES)})
    a = printers.get("bambulab.h2d", cfg)
    _install_dual(a, f, monkeypatch)
    assert a.gcode_problems(_dual_gcode(a, f)) == []
    # A tool beyond the declared slots still refuses, with the mapping reason.
    found = a.gcode_problems(_dual_gcode(a, f, tools=("T2",)))
    assert any("cannot be mapped to a filament slot" in p for p in found), found
    # A slot whose profile material disagrees with its declared type refuses per slot.
    found = a.gcode_problems(_dual_gcode(a, f, second_type="PLA"))
    assert any("filament slot 2" in p and "PETG" in p for p in found), found
