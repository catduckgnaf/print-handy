"""Post-slice delivery (issue #123): choices from the vendor profile, Discord size limits, Bambu handoff.

Offline: the fake Discord adapter from test_prepared_intake answers buttons, nothing contacts a printer.
"""
import copy
import hashlib
import json
import random
import zipfile
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy import printers, settings
from print_handy.core import delivery, model_job, prepared_intake
from print_handy.printers.base import PrinterModel
from print_handy.printers.klipper.adapter import KlipperAdapter
from .test_prepared_intake import env, run_flow  # noqa: F401  (pytest fixture)
from .helpers import external

print_next = external("print_next")

BAMBU_KEY = "bambulab.zz_test_printer"


def config_for(key, methods=None, upload_max_bytes=None):
    cfg = copy.deepcopy(settings.current())
    model = printers.MODELS[key]
    cfg["printer"].update(model=key, vendor=model.vendor.key)
    cfg["delivery"] = {"methods": list(methods or model.vendor.default_delivery)}
    if upload_max_bytes is not None:
        cfg["discord"]["upload_max_bytes"] = upload_max_bytes
    return cfg


@pytest.fixture
def bambu_model(monkeypatch):
    model = PrinterModel(key=BAMBU_KEY, label="Test Bambu", adapter=KlipperAdapter, templates="klipper_generic",
                         vendor=printers.VENDORS["bambulab"], supported_nozzle_sizes=(0.4,), settings={})
    monkeypatch.setitem(printers.MODELS, BAMBU_KEY, model)
    return model


def use_config(monkeypatch, cfg):
    """delivery reads settings through its own module reference; nothing else in the flow changes."""
    monkeypatch.setattr(delivery, "settings", S(current=lambda: cfg))


# ----------------------------------------------------------------------------- profile -> methods

def test_methods_follow_the_vendor_profile(bambu_model):
    assert delivery.methods(config_for("flashforge.ad5x")) == ("printer",)
    assert delivery.methods(config_for("klipper.generic", ["discord", "printer"])) == ("printer", "discord")
    assert delivery.methods(config_for(BAMBU_KEY)) == ("discord", "bambu_handoff")
    for key in ("flashforge.ad5x", "sovol.sv06", "klipper.generic", "octoprint.marlin"):
        assert "bambu_handoff" not in printers.MODELS[key].vendor.delivery


# ----------------------------------------------------------------------------- binding

def _job_with(tmp_path, *texts):
    gcodes = []
    for index, text in enumerate(texts, 1):
        g = tmp_path / "sliced" / f"plate_{index}.gcode"
        g.parent.mkdir(parents=True, exist_ok=True)
        g.write_text(text)
        gcodes.append(g)
    job = {"gcode": str(gcodes[0]), "derived_plates": [{"gcode": str(g), "plate_id": i}
                                                        for i, g in enumerate(gcodes, 1)]}
    return job, gcodes, {str(g): delivery.sha256(g) for g in gcodes}


def test_only_files_of_this_job_unchanged_since_slicing_are_delivered(tmp_path):
    job, gcodes, recorded = _job_with(tmp_path, "G1 X1\n", "G1 X2\n")
    assert delivery.bound_artifacts(job, [str(g) for g in gcodes], recorded) == gcodes
    stranger = tmp_path / "other.gcode"
    stranger.write_text("G1\n")
    with pytest.raises(delivery.DeliveryRefused, match="not a G-code of this job"):
        delivery.bound_artifacts(job, [str(stranger)], recorded)
    gcodes[1].write_text("G1 X3\n")
    with pytest.raises(delivery.DeliveryRefused, match="changed after slicing"):
        delivery.bound_artifacts(job, [str(g) for g in gcodes], recorded)


# ----------------------------------------------------------------------------- Discord size limits

def test_discord_plan_attaches_small_files_zips_big_gcode_and_reports_what_cannot_fit(tmp_path):
    small = tmp_path / "small.gcode"
    small.write_text("G1 X1\n" * 10)
    big_text = tmp_path / "big.gcode"
    big_text.write_text("G1 X10 Y10 E0.5\n" * 20000)  # compresses well
    noise = tmp_path / "noise.gcode"
    noise.write_bytes(random.Random(1).randbytes(256_000))  # random bytes do not compress
    package = tmp_path / "plate.gcode.3mf"
    package.write_bytes(b"x" * 50_000)
    limit = 40_000
    plan = {item.source.name: item for item in delivery.discord_plan([small, big_text, noise, package], limit,
                                                                     tmp_path / "out")}
    assert plan["small.gcode"].mode == "attach" and plan["small.gcode"].file == small
    zipped = plan["big.gcode"]
    assert zipped.mode == "zip" and zipped.sent_size <= limit
    assert zipfile.ZipFile(zipped.file).read("big.gcode") == big_text.read_bytes()
    assert plan["noise.gcode"].mode == "too_big" and plan["noise.gcode"].file is None
    assert not (tmp_path / "out" / "noise.gcode.zip").exists()  # a useless zip is not kept
    assert plan["plate.gcode.3mf"].mode == "too_big"  # already compressed: no zip attempt
    msg = delivery.discord_message(plan["noise.gcode"], limit)
    assert "was not sent" in msg and str(noise) in msg and "MEDIA:" not in msg
    assert delivery.discord_message(plan["small.gcode"], limit).endswith(f"MEDIA:{small}")
    assert "sent zipped" in delivery.discord_message(zipped, limit)


# ----------------------------------------------------------------------------- early size check (#272)

def test_deflated_size_is_what_a_zip_entry_would_weigh(tmp_path):
    g = tmp_path / "plate.gcode"
    g.write_text("G1 X10 Y10 E0.5\n" * 5000)
    with zipfile.ZipFile(tmp_path / "plate.zip", "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.write(g, g.name)
    with zipfile.ZipFile(tmp_path / "plate.zip") as archive:
        assert delivery.deflated_size(g) == archive.getinfo("plate.gcode").compress_size
    assert delivery.package_target(g) == tmp_path / "bambu" / "plate.gcode.3mf"


def test_discord_candidates_size_each_file_before_the_button_is_offered(tmp_path, monkeypatch, bambu_model):
    small = tmp_path / "small.gcode"
    small.write_text("G1 X1\n" * 10)
    big_text = tmp_path / "big.gcode"
    big_text.write_text("G1 X10 Y10 E0.5\n" * 20000)  # compresses well: fits zipped
    noise = tmp_path / "noise.gcode"
    noise.write_bytes(random.Random(1).randbytes(256_000))  # does not compress: never fits
    limit = 40_000
    measured = []
    real = delivery.deflated_size
    monkeypatch.setattr(delivery, "deflated_size", lambda path, level=9: measured.append(path) or real(path, level))
    cache = {}
    plain = config_for("sovol.sv06", ["printer", "discord"], upload_max_bytes=limit)
    fit = {c.source.name: c for c in delivery.discord_candidates([small, big_text, noise], limit, plain, cache)}
    assert fit["small.gcode"].fits and fit["small.gcode"].packed is None  # under the limit: not measured
    assert fit["big.gcode"].fits and fit["big.gcode"].packed <= limit < fit["big.gcode"].size
    assert not fit["noise.gcode"].fits and fit["noise.gcode"].packed > limit
    assert measured == [big_text, noise]
    delivery.discord_candidates([small, big_text, noise], limit, plain, cache)
    assert measured == [big_text, noise]  # a menu shown twice measures once
    msg = delivery.oversize_message(list(fit.values()), limit)
    assert msg.startswith(f"noise.gcode is 0.3 MB and {fit['noise.gcode'].packed / 1e6:.1f} MB zipped, over the "
                          f"0.0 MB Discord upload limit, so it cannot be sent here. It stays on the Print Handy host at {noise}.")
    assert "is not offered" not in msg  # the others still fit, so the button stays
    assert delivery.oversize_message([fit["small.gcode"], fit["big.gcode"]], limit) == ""
    # Bambu Lab: the attachment is the package. Not built yet, it is sized from its G-code ...
    bambu = config_for(BAMBU_KEY, upload_max_bytes=limit)
    [est] = delivery.discord_candidates([noise], limit, bambu, cache)
    assert est.source == delivery.package_target(noise) and est.size is None and est.built_from == noise
    assert not est.fits and est.packed == fit["noise.gcode"].packed
    msg = delivery.oversize_message([est], limit)
    assert msg.startswith(f"noise.gcode.3mf would be about {est.packed / 1e6:.1f} MB (its G-code noise.gcode is 0.3 MB), "
                          "over the 0.0 MB Discord upload limit, so it cannot be sent here. "
                          f"'Package for Bambu (no upload)' writes it to {est.source} on the Print Handy host.")
    assert msg.endswith("'Send the files here' is not offered for this slice; "
                        "[discord] upload_max_bytes is the limit (docs/SETTINGS.md).")
    # ... and once built, exactly, with no deflate pass (a .3mf is already compressed).
    package = delivery.bambu_package(noise, est.source)
    [exact] = delivery.discord_candidates([noise], limit, bambu, cache)
    assert exact.source == package and exact.size == package.stat().st_size > limit and exact.packed is None
    assert not exact.fits and measured == [big_text, noise]
    assert delivery.oversize_message([exact], limit, for_handoff=True) == (
        f"noise.gcode.3mf is {exact.size / 1e6:.1f} MB, over the 0.0 MB Discord upload limit, so it cannot be "
        f"sent here. It stays on the Print Handy host at {package}.")
    [tiny] = delivery.discord_candidates([small], limit, bambu, cache)
    assert tiny.fits and tiny.size is None and tiny.packed is None


# ----------------------------------------------------------------------------- Bambu package

def test_bambu_package_has_the_plate_gcode_and_its_md5(tmp_path):
    g = tmp_path / "plate_1.gcode"
    g.write_text("; generated by OrcaSlicer\nG28\nG1 X1 E1\n")
    out = delivery.bambu_package(g, tmp_path / "plate_1.gcode.3mf", plate=2, nozzle_mm=0.4)
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model", "Metadata/plate_2.gcode",
                "Metadata/plate_2.gcode.md5", "Metadata/slice_info.config"} == names
        assert archive.read("Metadata/plate_2.gcode") == g.read_bytes()
        assert archive.read("Metadata/plate_2.gcode.md5").decode() == hashlib.md5(g.read_bytes()).hexdigest().upper()
        assert 'key="index" value="2"' in archive.read("Metadata/slice_info.config").decode()
    first = out.read_bytes()
    delivery.bambu_package(g, out, plate=2, nozzle_mm=0.4)
    assert out.read_bytes() == first  # deterministic


def test_bambu_handoff_refuses_non_bambu_printers_and_foreign_start_code(tmp_path, bambu_model):
    job, gcodes, _ = _job_with(tmp_path, "G28\nG1 X1 E1\n")
    with pytest.raises(delivery.DeliveryRefused, match="not a Bambu Lab printer"):
        delivery.bambu_handoff({**job, "printer_model": "klipper.generic"}, gcodes, config_for("klipper.generic"))
    cfg = config_for(BAMBU_KEY)
    with pytest.raises(delivery.DeliveryRefused, match="sliced for flashforge.ad5x"):
        delivery.bambu_handoff(job, gcodes, cfg)  # no printer_model: an AD5X job
    packages = delivery.bambu_handoff({**job, "printer_model": BAMBU_KEY}, gcodes, cfg)
    assert [p.name for p in packages] == ["plate_1.gcode.3mf"] and packages[0].parent.name == "bambu"
    gcodes[0].write_text("START_PRINT EXTRUDER_TEMP=215 BED_TEMP=60 MESH=auto\nG1 X1\n")
    with pytest.raises(delivery.DeliveryRefused, match="non-Bambu commands"):
        delivery.bambu_handoff({**job, "printer_model": BAMBU_KEY}, gcodes, cfg)


def test_discord_files_are_the_vendor_primary_format(tmp_path, bambu_model):
    job, gcodes, _ = _job_with(tmp_path, "G28\n")
    assert delivery.files_for_discord(job, gcodes, config_for("sovol.sv06", ["printer", "discord"])) == gcodes
    files = delivery.files_for_discord({**job, "printer_model": BAMBU_KEY}, gcodes, config_for(BAMBU_KEY))
    assert [f.name for f in files] == ["plate_1.gcode.3mf"]


# ----------------------------------------------------------------------------- Discord intake flow

def _delivery_menu(adapter):
    return [q for q in adapter.questions if delivery.DONE_LABEL in q["choices"]]


def test_default_profile_keeps_the_exact_start_question(env):  # noqa: F811
    adapter, result, receipt = run_flow(env, [1, 0, 1, 1])
    assert adapter.questions[-1]["choices"] == [print_next.START_LABEL, print_next.CANCEL_LABEL]
    assert "deliveries" not in receipt and env.starts == []


def test_send_files_then_done_never_starts_or_authorizes(env, monkeypatch):  # noqa: F811
    use_config(monkeypatch, config_for("flashforge.ad5x", ["printer", "discord"]))
    # one color, slot 1, Slice only, Send the files here, Done
    adapter, result, receipt = run_flow(env, [1, 0, 1, 1, 1])
    menus = _delivery_menu(adapter)
    assert menus[0]["choices"] == [print_next.START_LABEL, "Send the files here", delivery.DONE_LABEL]
    assert menus[1]["choices"] == [print_next.START_LABEL, delivery.DONE_LABEL]  # never offered twice
    attached = [m for m in adapter.sent if "MEDIA:" in m]
    assert len(attached) == 1 and attached[0].endswith(f"MEDIA:{env.sliced[0]}")
    assert any(m.startswith("Sent 1 of 1 file(s) here.") for m in adapter.sent)
    assert env.starts == [] and "bed_clear_approval" not in receipt
    assert receipt["status"] == "SLICED_NOT_STARTED" and receipt["started"] is False
    [entry] = receipt["deliveries"]
    assert entry["method"] == "discord" and entry["complete"] is True
    assert entry["files"][0]["source"] == env.sliced[0] and entry["files"][0]["ok"] is True


def test_send_files_then_start_uses_the_same_bed_clear_button(env, monkeypatch):  # noqa: F811
    use_config(monkeypatch, config_for("klipper.generic", ["printer", "discord"]))
    adapter, result, receipt = run_flow(env, [1, 0, 1, 1, 0])
    assert len(env.starts) == 1 and receipt["status"] == "STARTED"
    assert receipt["bed_clear_approval"]["gcode_sha256"] == env.starts[0]["gcode_sha256"]
    assert _delivery_menu(adapter)[-1]["question"].startswith("Anything else for this slice?")


def test_too_big_for_discord_is_said_up_front_and_the_send_is_not_offered(env, monkeypatch):  # noqa: F811
    """#272: a file over the limit gets its size, the limit and its host path in the menu, not a button that fails."""
    use_config(monkeypatch, config_for("flashforge.ad5x", ["printer", "discord"], upload_max_bytes=10))
    # one color, slot 1, Slice only, Done (the send button is absent)
    adapter, result, receipt = run_flow(env, [1, 0, 1, 1])
    [menu] = _delivery_menu(adapter)
    assert menu["choices"] == [print_next.START_LABEL, delivery.DONE_LABEL]
    size = Path(env.sliced[0]).stat().st_size / 1e6
    assert (f"\nplate_1.gcode is {size:.1f} MB and {delivery.deflated_size(env.sliced[0]) / 1e6:.1f} MB zipped, "
            f"over the 0.0 MB Discord upload limit, so it cannot be sent here. It stays on the Print Handy host at "
            f"{env.sliced[0]}.\n'Send the files here' is not offered for this slice; [discord] upload_max_bytes "
            "is the limit (docs/SETTINGS.md).") in menu["question"]
    assert not any("MEDIA:" in m or "Too big for Discord" in m for m in adapter.sent)
    assert "deliveries" not in receipt and env.starts == []
    assert receipt["status"] == "SLICED_NOT_STARTED"


def test_under_the_limit_the_send_is_offered_without_a_size_note(env, monkeypatch):  # noqa: F811
    use_config(monkeypatch, config_for("flashforge.ad5x", ["printer", "discord"], upload_max_bytes=10_000))
    adapter, result, receipt = run_flow(env, [1, 0, 1, 1, 1])
    menus = _delivery_menu(adapter)
    assert menus[0]["choices"] == [print_next.START_LABEL, "Send the files here", delivery.DONE_LABEL]
    assert "upload limit" not in menus[0]["question"] and "upload limit" not in menus[1]["question"]
    attached = [m for m in adapter.sent if "MEDIA:" in m]
    assert len(attached) == 1 and attached[0].endswith(f"MEDIA:{env.sliced[0]}")


def test_discord_only_profile_still_tells_the_operator_where_a_too_big_file_is(env, monkeypatch):  # noqa: F811
    """No menu can carry the note when the send was the only option: it is posted on its own."""
    use_config(monkeypatch, config_for("klipper.generic", ["discord"], upload_max_bytes=10))
    adapter, result, receipt = run_flow(env, [1, 0, 0])
    assert _delivery_menu(adapter) == []
    note = next(m for m in adapter.sent if m.startswith("plate_1.gcode is "))
    assert "over the 0.0 MB Discord upload limit" in note and env.sliced[0] in note
    assert "'Send the files here' is not offered for this slice" in note
    assert adapter.sent.index(note) < adapter.sent.index("Sliced. Nothing uploaded or started.")
    assert not any("MEDIA:" in m for m in adapter.sent) and "deliveries" not in receipt and env.starts == []


def test_discord_failure_is_reported_and_retry_sends_only_the_rest(env, monkeypatch):  # noqa: F811
    use_config(monkeypatch, config_for("flashforge.ad5x", ["printer", "discord"]))
    from .test_prepared_intake import Adapter
    real_send = Adapter.send
    failures = iter([True, False])

    async def flaky(self, chat_id, content, reply_to=None, metadata=None):
        await real_send(self, chat_id, content, reply_to, metadata)
        if "MEDIA:" in content and next(failures):
            return S(success=False, error="Discord 413")
        return S(success=True)
    monkeypatch.setattr(Adapter, "send", flaky)
    # send (fails), send again (works), done
    adapter, result, receipt = run_flow(env, [1, 0, 1, 1, 1, 1])
    assert any("Failed: plate_1.gcode (Discord 413)" in m for m in adapter.sent)
    first, second = receipt["deliveries"]
    assert first["complete"] is False and second["complete"] is True
    assert sum("MEDIA:" in m for m in adapter.sent) == 2 and env.starts == []


def test_changed_gcode_is_not_delivered(env, monkeypatch):  # noqa: F811
    use_config(monkeypatch, config_for("flashforge.ad5x", ["printer", "discord"]))
    real = prepared_intake._deliver

    async def tamper(adapter, source, metadata, method, receipt, jp, gcodes, pending):
        Path(gcodes[0]).write_text("G1 X999\n")
        return await real(adapter, source, metadata, method, receipt, jp, gcodes, pending)
    monkeypatch.setattr(prepared_intake, "_deliver", tamper)
    adapter, result, receipt = run_flow(env, [1, 0, 1, 1, 1])
    assert any(m.startswith("The files were not sent here: plate_1.gcode changed after slicing") for m in adapter.sent)
    assert not any("MEDIA:" in m for m in adapter.sent) and env.starts == []


def _bambu_job(monkeypatch):
    """The intake job is recorded for the test Bambu model from creation.

    The shared env inventory names no printer, so a job created from it belongs to the
    configured model (the AD5X default) and the slice recipe is bound to that model.
    Recording the Bambu model at inventory time binds the recipe to it at slice launch,
    at approval and at delivery alike; rewriting JOB.json after slicing instead would
    race the slice-approval freshness guard, which rightly refuses a job whose model
    changed mid-flow.
    """
    inventory = model_job.printables_inventory

    def bambu_inventory(url):
        return {**inventory(url), "printer_model": BAMBU_KEY}

    monkeypatch.setattr(model_job, "printables_inventory", bambu_inventory)


def test_bambu_printer_offers_no_print_and_hands_off_a_package(env, monkeypatch, bambu_model):  # noqa: F811
    use_config(monkeypatch, config_for(BAMBU_KEY))
    _bambu_job(monkeypatch)
    # one color, slot 1, "Slice only" (first button: no print button exists), package, send, (menu ends)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 1, 0])
    first = [q for q in adapter.questions if "Slice only" in q["choices"]][0]
    assert first["choices"] == ["Slice only", "Scale the model", "Cancel print request"]
    assert not any(print_next.START_LABEL in q["choices"] or any("print now" in c for c in q["choices"])
                   for q in adapter.questions)
    menus = _delivery_menu(adapter)
    assert menus[0]["choices"] == ["Send the files here", "Package for Bambu (no upload)", delivery.DONE_LABEL]
    assert menus[0]["question"].endswith("Sliced and validated. What should I do with the files?")
    assert menus[1]["choices"] == ["Send the files here", delivery.DONE_LABEL]
    ready = next(m for m in adapter.sent if m.startswith("Bambu package ready: plate_1.gcode.3mf."))
    assert ready.startswith("Bambu package ready: plate_1.gcode.3mf. Use 'Send the files here' to download it.\n")
    assert "Nothing was uploaded to the Test Bambu and no print was started" in ready
    attached = [m for m in adapter.sent if "MEDIA:" in m]
    assert len(attached) == 1 and attached[0].endswith("plate_1.gcode.3mf")
    assert env.starts == [] and receipt["status"] == "SLICED_NOT_STARTED"
    assert [d["method"] for d in receipt["deliveries"]] == ["bambu_handoff", "discord"]


def test_bambu_package_over_the_limit_is_never_offered_for_download(env, monkeypatch, bambu_model):  # noqa: F811
    """#272: the operator learns the size, the limit and the host path before and after packaging, never
    from a send that fails. The A1 package from the issue was 22.9 MB against a 9.5 MB limit."""
    limit = 100_000
    use_config(monkeypatch, config_for(BAMBU_KEY, upload_max_bytes=limit))
    _bambu_job(monkeypatch)
    real_slice = model_job.slice_job
    rng = random.Random(272)

    def bambu_slice(job_path, **kw):
        out = real_slice(job_path, **kw)
        # Incompressible enough that the package stays well over the limit.
        Path(out[0]).write_text("".join(f"G1 X{rng.uniform(1, 99):.3f} Y{rng.uniform(1, 99):.3f} "
                                         f"E{rng.uniform(0, 1):.5f}\n" for _ in range(20000)))
        return out
    monkeypatch.setattr(model_job, "slice_job", bambu_slice)
    # one color, slot 1, "Slice only", package (the menu then ends: nothing is left to offer)
    adapter, result, receipt = run_flow(env, [1, 0, 0, 0])
    gcode = Path(env.sliced[0])
    package = delivery.package_target(gcode)
    packed = delivery.deflated_size(gcode)
    assert packed > limit
    [menu] = _delivery_menu(adapter)
    assert menu["choices"] == ["Package for Bambu (no upload)", delivery.DONE_LABEL]
    assert (f"\nplate_1.gcode.3mf would be about {packed / 1e6:.1f} MB (its G-code plate_1.gcode is "
            f"{gcode.stat().st_size / 1e6:.1f} MB), over the 0.1 MB Discord upload limit, so it cannot be sent here. "
            f"'Package for Bambu (no upload)' writes it to {package} on the Print Handy host.\n"
            "'Send the files here' is not offered for this slice; [discord] upload_max_bytes is the limit "
            "(docs/SETTINGS.md).") in menu["question"]
    ready = next(m for m in adapter.sent if m.startswith("Bambu package ready: plate_1.gcode.3mf."))
    assert package.is_file() and package.stat().st_size > limit
    assert ready.startswith(f"Bambu package ready: plate_1.gcode.3mf. plate_1.gcode.3mf is "
                            f"{package.stat().st_size / 1e6:.1f} MB, over the 0.1 MB Discord upload limit, so it "
                            f"cannot be sent here. It stays on the Print Handy host at {package}.\n")
    assert "Use 'Send the files here'" not in ready
    assert not any("MEDIA:" in m or "Too big for Discord" in m for m in adapter.sent)
    assert sum("over the 0.1 MB Discord upload limit" in m for m in adapter.sent) == 1  # said once after packaging
    assert [d["method"] for d in receipt["deliveries"]] == ["bambu_handoff"]
    assert env.starts == [] and receipt["status"] == "SLICED_NOT_STARTED" and "bed_clear_approval" not in receipt


def test_multi_plate_partial_delivery_reports_each_plate(tmp_path, monkeypatch):
    import asyncio
    job, gcodes, recorded = _job_with(tmp_path, "G1 X1\n", "G1 X2 Y3\n" * 50)
    jp = tmp_path / "JOB.json"
    jp.write_text(json.dumps(job))
    use_config(monkeypatch, config_for("sovol.sv06", ["printer", "discord"], upload_max_bytes=100))
    monkeypatch.setattr(delivery, "discord_plan", lambda files, limit, workdir: [
        delivery.Attachment(files[0], files[0], 6, 6, "attach"),
        delivery.Attachment(files[1], None, 450, 120, "too_big")])
    sent = []

    class Adapter:
        async def send(self, chat_id, content, reply_to=None, metadata=None):
            sent.append(content)
            return S(success=True)
    receipt = {"gcode_sha256": recorded}
    source = S(chat_id="c1", thread_id=None)
    asyncio.run(prepared_intake._deliver(Adapter(), source, None, "discord", receipt, jp,
                                         [str(g) for g in gcodes], ["discord"]))
    assert sent[0].startswith("Plate 1 of 2: plate_1.gcode") and sent[0].endswith(f"MEDIA:{gcodes[0]}")
    assert sent[1].startswith("Plate 2 of 2: plate_2.gcode is 0.0 MB and 0.0 MB zipped") and "not sent" in sent[1]
    assert sent[2] == ("Sent 1 of 2 file(s) here. Too big for Discord: plate_2.gcode. "
                       "Nothing was uploaded to a printer or started.")
    [entry] = receipt["deliveries"]
    assert [f["ok"] for f in entry["files"]] == [True, False] and entry["detail"] == "1 of 2 sent"


def test_effective_upload_limit_takes_the_lower_of_config_and_adapter(tmp_path, monkeypatch):
    """The sending adapter's own cap lowers the planning limit (P2-92): the
    Matrix adapter's cap is the homeserver's discovered media limit, which
    lives nowhere in settings. A higher adapter cap never raises the
    configured limit, and an adapter reporting none changes nothing."""
    cfg = config_for("sovol.sv06", ["discord"], upload_max_bytes=50_000_000)
    use_config(monkeypatch, cfg)
    assert delivery.effective_upload_limit(S(upload_max_bytes=10_000_000)) == 10_000_000
    assert delivery.effective_upload_limit(S(upload_max_bytes=99_000_000)) == 50_000_000
    assert delivery.effective_upload_limit(S(upload_max_bytes=None)) == 50_000_000
    assert delivery.effective_upload_limit(S()) == 50_000_000
    assert delivery.effective_upload_limit(None) == 50_000_000


def test_deliver_plans_against_the_adapters_discovered_cap(tmp_path, monkeypatch):
    """A 20 MB file on a server that reported a 10 MB cap must be planned
    against 10 MB (P2-92), not the configured 50 MB: planning hands the
    adapter's cap to discord_plan, so the file is zipped or reported too
    big instead of being uploaded whole and rejected by the server."""
    import asyncio
    job, gcodes, recorded = _job_with(tmp_path, "G1 X1\n")
    jp = tmp_path / "JOB.json"
    jp.write_text(json.dumps(job))
    use_config(monkeypatch, config_for("sovol.sv06", ["printer", "discord"],
                                       upload_max_bytes=50_000_000))
    seen_limits = []

    def plan(files, limit, workdir):
        seen_limits.append(limit)
        return [delivery.Attachment(files[0], files[0], 6, 6, "attach")]

    monkeypatch.setattr(delivery, "discord_plan", plan)
    sent = []

    class Adapter:
        upload_max_bytes = 10_000_000  # the discovered homeserver cap

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            sent.append(content)
            return S(success=True)

    receipt = {"gcode_sha256": recorded}
    source = S(chat_id="c1", thread_id=None)
    asyncio.run(prepared_intake._deliver(Adapter(), source, None, "discord", receipt, jp,
                                         [str(g) for g in gcodes], ["discord"]))
    assert seen_limits == [10_000_000]


def test_bambu_package_carries_the_plate_previews_the_a1_and_the_apps_read(tmp_path, bambu_model):
    """#275: Metadata/plate_<n>.png (512) and plate_<n>_small.png (128), rendered offline from the sliced geometry."""
    import io
    from PIL import Image
    from .test_plate_thumbnail import box_stl
    job, gcodes, _ = _job_with(tmp_path, "G28\nG1 X1 E1\n")
    box_stl(gcodes[0].parent / "geometry.stl", origin=(100, 100, 0))
    cfg = config_for(BAMBU_KEY)
    package = delivery.bambu_handoff({**job, "printer_model": BAMBU_KEY}, gcodes, cfg)[0]
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert {"Metadata/plate_1.gcode", "Metadata/plate_1.gcode.md5", "Metadata/slice_info.config",
                "Metadata/plate_1.png", "Metadata/plate_1_small.png"} <= names
        assert not any(n.startswith("Metadata/pick_") or n.startswith("Metadata/top_") for n in names)
        assert archive.read("Metadata/plate_1.gcode") == gcodes[0].read_bytes()
        assert archive.read("Metadata/plate_1.gcode.md5").decode() == hashlib.md5(gcodes[0].read_bytes()).hexdigest().upper()
        for name, size in (("Metadata/plate_1.png", 512), ("Metadata/plate_1_small.png", 128)):
            image = Image.open(io.BytesIO(archive.read(name)))
            assert image.format == "PNG" and image.size == (size, size)
        assert "png" not in archive.read("Metadata/slice_info.config").decode()  # Bambu's slice_info names no images
    first = package.read_bytes()
    assert delivery.bambu_handoff({**job, "printer_model": BAMBU_KEY}, gcodes, cfg)[0].read_bytes() == first


def test_bambu_package_without_a_preview_source_is_still_built(tmp_path, bambu_model, monkeypatch):
    job, gcodes, _ = _job_with(tmp_path, "G28\nG1 X1 E1\n")
    cfg = config_for(BAMBU_KEY)
    with zipfile.ZipFile(delivery.bambu_handoff({**job, "printer_model": BAMBU_KEY}, gcodes, cfg)[0]) as archive:
        assert not [n for n in archive.namelist() if n.endswith(".png")]
    # A renderer failure is cosmetic: logged, never a refused delivery.
    monkeypatch.setattr(delivery.plate_thumbnail, "plate_thumbnails",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no preview")))
    with zipfile.ZipFile(delivery.bambu_handoff({**job, "printer_model": BAMBU_KEY}, gcodes, cfg)[0]) as archive:
        assert "Metadata/plate_1.gcode" in archive.namelist() and not [n for n in archive.namelist() if n.endswith(".png")]
