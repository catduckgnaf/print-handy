"""Vendor -> printer profiles: inheritance, unchanged defaults for existing printers, config compatibility."""
import json
import sys
from pathlib import Path

import pytest

from print_handy import printers, settings
from print_handy.printers.base import (PrinterModel, SHARED_SETTINGS, VendorProfile, merge_settings,
                                       start_code_problems)
from print_handy.printers.klipper.adapter import KlipperAdapter

GOLDEN = Path(__file__).parent / "golden"
# Keys the vendor layer added. Everything else must be exactly what each printer had before it.
ADDED_PRINTER_KEYS = ("vendor", "slicer")
EXISTING = ("flashforge.ad5x", "klipper.generic", "octoprint.marlin", "sovol.sv06")


def without_added(data):
    data = json.loads(json.dumps(data, default=str))
    data.pop("delivery", None)
    data.pop("logging", None)  # added by #150, after the golden files were captured
    data.pop("flashforge_stock", None)  # added for the stock LAN vendor, after capture
    data.pop("prusa", None)  # added for the PrusaLink vendor (#242), after capture
    data.pop("elegoo_sdcp", None)  # added for the Elegoo SDCP vendor (#243), after capture
    data.pop("ai", None)  # #818 added the empty optional [ai] tier tables, after capture
    data.pop("notify", None)  # #622's section joined the defaults tree in the 2.2.2 settings-surface fix, after capture
    data.pop("material_tuning", None)  # #783's shipped PLA tuning, added after capture
    data.pop("public_text", None)  # #1020's [public_text] denylist, added after capture
    data.pop("purge", None)  # #832's purge plan settings, added after capture
    data.pop("zmod", None)  # #832's ZMod plugin settings, added after capture
    for key in ("lan_host", "lan_serial", "lan_access_code_env"):  # added by #187, after capture
        data.get("bambu", {}).pop(key, None)
    data.get("discord", {}).pop("approval_all_users", None)  # added with multiple approval users, after capture
    data.get("bambu", {}).pop("start_confirm_s", None)  # added by #271, after capture
    data.get("bot", {}).pop("platform", None)  # added by #164, after the golden files were captured
    for key in ("auto_light_off_minutes", "light_object"):  # added by #516, after capture
        data.get("vision", {}).pop(key, None)
    for key in ("local_detector_enabled", "local_model_path", "local_empty_enabled"):  # added by #455, after capture
        data.get("vision", {}).pop(key, None)
    for key in ("vertical_short", "vertical_short_mode", "vertical_short_overlay"):  # added by #317, after capture
        data.get("timelapse", {}).pop(key, None)
    data.get("timelapse", {}).pop("mode_explicit", None)  # added by the P2-261 fix (Codex backlog burn-down), after capture
    for key in ("wrapped_enabled", "wrapped_ai_video",
                "wrapped_tts_engine", "wrapped_music_file"):  # added by #956 / #981 step 4, after capture
        data.get("timelapse", {}).pop(key, None)
    data.pop("webui", None)  # added by #286 stage 1, after the golden files were captured
    data.pop("live_stream", None)  # added by #311 stage 3, after the golden files were captured
    data.pop("design", None)  # added by #388 stage 1, after the golden files were captured
    data.pop("agent", None)  # added by #524 stage 3 (the stray-chat agent lane), after capture
    data.pop("printers", None)  # added by #333 (the [[printers]] roster), after the golden files were captured
    data.get("vision", {}).pop("roast_enabled", None)  # added by the roast switch, after capture
    data.get("discord", {}).pop("clarify_ui", None)  # added by #524 stage 2, after capture
    data.get("discord", {}).pop("mention_starter_on_complete", None)  # added with starter mentions, after capture
    data.pop("spool", None)  # added by #512 (spool.prompt_brand), after the golden files were captured
    data.pop("telegram", None)  # added by #164 stage 2, after the golden files were captured
    data.pop("matrix", None)  # added by #164 stage 3, after the golden files were captured
    data.get("slicer", {}).pop("flash_studio_path", None)  # added by the Flash Studio config, after capture
    for key in ("piper", "piper_model"):  # added by #981 step 4 (Wrapped narration), after capture
        data.get("paths", {}).pop(key, None)
    data.get("timelapse", {}).pop("mode", None)  # added by #286 stage 3 (the type setting), after capture
    data.get("services", {}).pop("camera_source", None)  # added by #286 stage 3, after capture
    for key in ("capture_enabled", "capture_source_url"):  # added by #246 stage 1, after capture
        data.get("timelapse", {}).pop(key, None)
    for key in ("capture_trigger", "capture_interval_s", "capture_max_frames",
                "capture_max_bytes", "capture_encoder"):  # added by #246 stage 2, after capture
        data.get("timelapse", {}).pop(key, None)
    for key in ("target_length_s", "min_fps", "max_fps", "min_length_s", "hold_last_frame_s"):
        data.get("timelapse", {}).pop(key, None)  # added by #906, after capture
    for key in ("early_layers", "early_interval_s", "auto_pause_early"):
        data.get("visual_watch", {}).pop(key, None)  # added by #913, after capture
    data.get("timelapse", {}).pop("encoder", None)  # added by #314, after capture
    data.get("discord", {}).pop("denied_user_ids", None)  # added by #386, after capture
    data.get("discord", {}).pop("chat_llm_enabled", None)  # added by #466, after capture
    if data.get("slicer") == {}:
        data.pop("slicer", None)
    for key in ADDED_PRINTER_KEYS:
        data.get("printer", {}).pop(key, None)
    data.get("printer", {}).pop("zmod_delivery_format", None)  # added by #564, after capture
    for key in ("spacing", "arrange_gap_mm", "arrange_edge_mm"):  # layout.spacing presets, after capture
        data.get("layout", {}).pop(key, None)
    return data


def load(environ=None, local=False):
    return settings.load(home=settings.HOME, local=local, environ=environ or {})


# ----------------------------------------------------------------------------- layout

def test_every_printer_sits_under_its_vendor_package():
    assert set(printers.VENDORS) == {"anycubic", "bambulab", "creality", "elegoo", "elegoo_sdcp", "flashforge", "flashforge_stock", "klipper", "octoprint", "prusa", "qidi", "snapmaker", "sovol", "sovol_marlin"}
    for key, model in printers.MODELS.items():
        assert model.vendor is printers.VENDORS[model.vendor_key]
        assert key == f"{model.vendor.key}.{model.printer_key}"


def test_vendor_profiles_carry_the_shared_settings():
    flashforge, klipper, sovol = (printers.VENDORS[k] for k in ("flashforge", "klipper", "sovol"))
    assert flashforge.connection == "moonraker" and flashforge.start_code == "zmod"
    assert klipper.start_code == "klipper_macro" and sovol.start_code == "klipper_macro"
    assert sovol.settings == klipper.settings and sovol.label == "Sovol"
    assert printers.VENDORS["octoprint"].connection == "octoprint"
    bambu = printers.VENDORS["bambulab"]
    assert bambu.connection == "bambulab_lan" and bambu.start_code == "bambu"
    assert bambu.slicers == ("bambu_studio", "orca") and bambu.file_formats[0] == ".gcode.3mf"
    # `printer` is offered (stage d) but never a default: the operator opts in via [delivery] methods.
    assert "printer" in bambu.delivery and set(bambu.default_delivery) == {"discord", "bambu_handoff"}
    for vendor in printers.VENDORS.values():
        # Bambu Studio is the Bambu Lab default; OrcaSlicer stays the default everywhere else
        assert vendor.slicers[0] == ("bambu_studio" if vendor.key == "bambulab" else "orca")
        if vendor.key != "bambulab":
            assert "bambu_handoff" not in vendor.delivery
            # Prusa (#242) and Elegoo SDCP (#243) are opt-in exceptions: `printer` is offered
            # but not a default, so an untested LAN path never starts anything until the
            # operator opts in.
            assert vendor.default_delivery == (("discord",) if vendor.key in ("prusa", "elegoo_sdcp") else ("printer",))


def test_printer_settings_override_vendor_settings_key_by_key():
    vendor = VendorProfile(key="acme", label="Acme", connection="moonraker", start_code="klipper_macro",
                           settings=merge_settings(SHARED_SETTINGS, {"printer": {"firmware": "Klipper"}}))
    model = PrinterModel(key="acme.big", label="Acme Big", adapter=KlipperAdapter, templates="klipper_generic",
                         vendor=vendor, settings={"printer": {"name": "Big", "bed_mm": [300.0, 300.0]},
                                                  "layout": {"single_copy_edge_mm": 5.0}})
    out = model.defaults()
    assert out["printer"]["firmware"] == "Klipper" and out["printer"]["bed_mm"] == [300.0, 300.0]
    assert out["layout"]["single_copy_edge_mm"] == 5.0
    assert out["layout"]["merge_spacing_mm"] == SHARED_SETTINGS["layout"]["merge_spacing_mm"]
    assert out["printer"]["vendor"] == "acme" and out["printer"]["slicer"] == "orca"
    assert out["delivery"] == {"methods": ["printer"]}
    assert vendor.settings["layout"]["single_copy_edge_mm"] == 1.0  # the vendor is not mutated


@pytest.mark.parametrize("changes, message", [
    ({"connection": "offline", "delivery": ("printer",), "default_delivery": ("printer",)}, "offline vendor"),
    ({"delivery": ("discord", "bambu_handoff"), "default_delivery": ("discord",)}, "Bambu start code"),
    ({"slicers": ("cura",)}, "slicers"),
    ({"default_delivery": ("discord",), "delivery": ("printer",)}, "subset"),
    ({"connection": "serial"}, "connection"),
])
def test_vendor_profile_refuses_inconsistent_settings(changes, message):
    with pytest.raises(ValueError, match=message):
        VendorProfile(**{"key": "acme", "label": "Acme", "connection": "moonraker",
                         "start_code": "klipper_macro", **changes})


# ----------------------------------------------------------------------------- no behaviour change

@pytest.mark.parametrize("key", EXISTING)
def test_existing_printer_defaults_are_unchanged(key):
    """model_defaults.json was captured from main before the vendor layer existed."""
    want = json.loads((GOLDEN / "model_defaults.json").read_text())[key]
    meta = want.pop("_model")
    model = printers.MODELS[key]
    assert without_added(model.defaults()) == want
    assert meta == {"label": model.label, "templates": model.templates, "baseline": model.baseline,
                    "adapter": f"{model.adapter.__module__}.{model.adapter.__name__}",
                    "supported_nozzle_sizes": list(model.supported_nozzle_sizes),
                    "supported_nozzle_types": list(model.supported_nozzle_types)}


@pytest.mark.parametrize("key", EXISTING)
def test_existing_printer_loaded_settings_are_unchanged(key):
    """model_settings.json: settings.load() for each model on main, before the vendor layer."""
    want = json.loads((GOLDEN / "model_settings.json").read_text())[key]
    got = load({"PRINT_HANDY__PRINTER__MODEL": key})

    def norm(value):
        if isinstance(value, dict):
            return {k: norm(v) for k, v in value.items()}
        if isinstance(value, list):
            return [norm(v) for v in value]
        if isinstance(value, Path):
            return str(value).replace(str(settings.ROOT), "<repo>").replace(str(Path.home()), "~")
        return value
    assert without_added(norm(got)) == want
    assert got["printer"]["vendor"] == key.split(".")[0] and got["printer"]["slicer"] == "orca"
    assert got["delivery"]["methods"] == ["printer"]
    # The roster (#333) synthesised from this legacy load mirrors the golden-checked sections;
    # without_added above is what proves those sections themselves are unchanged.
    (entry,) = got["printers"]
    assert entry["model"] == key and entry["name"] == got["printer"]["name"]
    assert entry["moonraker_url"] == got["services"]["moonraker_url"]
    assert entry["lan_host"] == got["bambu"]["lan_host"]


# ----------------------------------------------------------------------------- config compatibility

def test_legacy_full_model_key_still_selects_the_printer():
    for key in EXISTING:
        assert load({"PRINT_HANDY__PRINTER__MODEL": key})["printer"]["model"] == key


def test_vendor_plus_printer_name_is_stored_as_the_full_key():
    data = load({"PRINT_HANDY__PRINTER__VENDOR": "sovol", "PRINT_HANDY__PRINTER__MODEL": "sv06"})
    assert data["printer"]["model"] == "sovol.sv06" and data["printer"]["vendor"] == "sovol"
    assert printers.current(data).key == "sovol.sv06"


def test_printer_adapters_expose_display_and_firmware_names():
    bambu = printers.current(load({"PRINT_HANDY__PRINTER__MODEL": "bambulab.a1"}))
    octoprint = printers.current(load({"PRINT_HANDY__PRINTER__MODEL": "octoprint.marlin"}))
    klipper = printers.current(load({"PRINT_HANDY__PRINTER__MODEL": "klipper.generic"}))

    assert (bambu.display_name, bambu.firmware_name) == (
        "Bambu Lab A1", "Bambu Lab (offline handoff only)")
    assert (octoprint.display_name, octoprint.firmware_name) == (
        "Marlin via OctoPrint", "Marlin")
    assert (klipper.display_name, klipper.firmware_name) == ("Generic Klipper", "Klipper")


def test_unique_printer_name_alone_resolves():
    assert load({"PRINT_HANDY__PRINTER__MODEL": "zero"})["printer"]["model"] == "sovol.zero"
    # sv06 was unique before sovol_marlin.sv06 existed; existing configs keep meaning the Klipper one.
    assert load({"PRINT_HANDY__PRINTER__MODEL": "sv06"})["printer"]["model"] == "sovol.sv06"
    assert load({"PRINT_HANDY__PRINTER__MODEL": "sv06", "PRINT_HANDY__PRINTER__VENDOR": "sovol_marlin"}
                )["printer"]["model"] == "sovol_marlin.sv06"
    # A name that was never unique is still refused rather than guessed.
    with pytest.raises(settings.SettingsError, match="several vendors"):
        load({"PRINT_HANDY__PRINTER__MODEL": "sv01"})
    # ad5x exists for both the ZMod vendor and the stock vendor, so the bare name is refused.
    with pytest.raises(settings.SettingsError, match="several vendors"):
        load({"PRINT_HANDY__PRINTER__MODEL": "ad5x"})


def test_local_yaml_with_vendor_and_printer_sections(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("printer:\n  vendor: klipper\n  model: generic\n")
    data = settings.load(home=settings.HOME, local=local, environ={})
    assert data["printer"]["model"] == "klipper.generic"


def test_existing_local_toml_keeps_working(tmp_path):
    local = tmp_path / "local.toml"
    local.write_text('[printer]\nmodel = "sovol.sv06"\nnozzle_diameter_mm = 0.6\n')
    data = settings.load(home=settings.HOME, local=local, environ={})
    assert data["printer"]["model"] == "sovol.sv06" and data["printer"]["nozzle_diameter_mm"] == 0.6
    assert data["delivery"]["methods"] == ["printer"]


def test_vendor_alias_and_mismatch():
    assert printers.vendor_key("Bambu") == "bambulab"
    assert printers.vendor_key("bambu_lab") == "bambulab" and printers.vendor_key("klipper") == "klipper"
    with pytest.raises(settings.SettingsError, match="does not match"):
        load({"PRINT_HANDY__PRINTER__MODEL": "sovol.sv06", "PRINT_HANDY__PRINTER__VENDOR": "flashforge"})
    # "creality" used to be this test's example of an unknown vendor; it is a real vendor
    # now (the Klipper/Creality-OS pack), so the unknown-vendor case uses a made-up name
    # and creality/qidi resolution is asserted positively instead.
    with pytest.raises(settings.SettingsError, match="vendor must be one of"):
        load({"PRINT_HANDY__PRINTER__VENDOR": "acme", "PRINT_HANDY__PRINTER__MODEL": "mk4"})
    assert load({"PRINT_HANDY__PRINTER__VENDOR": "creality",
                 "PRINT_HANDY__PRINTER__MODEL": "k1"})["printer"]["model"] == "creality.k1"
    assert load({"PRINT_HANDY__PRINTER__VENDOR": "qidi",
                 "PRINT_HANDY__PRINTER__MODEL": "q1pro"})["printer"]["model"] == "qidi.q1pro"
    with pytest.raises(settings.SettingsError, match="model must be one of"):
        load({"PRINT_HANDY__PRINTER__MODEL": "mk4"})


def test_slicer_and_delivery_must_be_offered_by_the_vendor(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("printer:\n  slicer: bambu_studio\n")
    with pytest.raises(settings.SettingsError, match="slicer 'bambu_studio' is not available for vendor 'flashforge'"):
        settings.load(home=settings.HOME, local=local, environ={})
    local.write_text("delivery:\n  methods: [printer, bambu_handoff]\n")
    with pytest.raises(settings.SettingsError, match=r"\['bambu_handoff'\] are not offered for vendor 'flashforge'"):
        settings.load(home=settings.HOME, local=local, environ={})
    local.write_text("delivery:\n  methods: [discord, printer, discord]\n")
    data = settings.load(home=settings.HOME, local=local, environ={})
    assert data["delivery"]["methods"] == ["printer", "discord"]  # vendor order, no repeats
    local.write_text("delivery:\n  methods: []\n")
    with pytest.raises(settings.SettingsError, match="non-empty list"):
        settings.load(home=settings.HOME, local=local, environ={})


# ----------------------------------------------------------------------------- dropping a printer in

@pytest.fixture
def dropped_in_bambu_printer(tmp_path, monkeypatch):
    """A printer module added to bambulab/ the way the A1 (issue #127) lands: MODEL without vendor=."""
    (tmp_path / "zz_test_printer.py").write_text(
        "from ..base import PrinterModel\n"
        "from ..klipper.adapter import KlipperAdapter\n"
        "MODEL = PrinterModel(key='bambulab.zz_test_printer', label='Test Bambu', adapter=KlipperAdapter,\n"
        "    templates='klipper_generic', supported_nozzle_sizes=(0.4,),\n"
        "    settings={'printer': {'name': 'Test Bambu', 'bed_mm': [256.0, 256.0], 'max_print_height_mm': 256.0,\n"
        "                          'nozzle_diameter_mm': 0.4, 'filament_slots': 1}})\n")
    package = sys.modules["print_handy.printers.bambulab"]
    monkeypatch.setattr(package, "__path__", [*package.__path__, str(tmp_path)])
    vendors, models = printers._discover()
    yield models["bambulab.zz_test_printer"]
    sys.modules.pop("print_handy.printers.bambulab.zz_test_printer", None)


def test_a_printer_dropped_into_a_vendor_package_inherits_it(dropped_in_bambu_printer, monkeypatch):
    model = dropped_in_bambu_printer
    assert model.vendor is printers.VENDORS["bambulab"]
    out = model.defaults()
    assert out["printer"]["firmware"] == "Bambu Lab (offline handoff only)"
    assert out["printer"]["bed_mm"] == [256.0, 256.0]
    assert out["delivery"]["methods"] == ["discord", "bambu_handoff"]
    monkeypatch.setitem(printers.MODELS, model.key, model)
    data = load({"PRINT_HANDY__PRINTER__VENDOR": "bambu", "PRINT_HANDY__PRINTER__MODEL": "zz_test_printer",
                 "PRINT_HANDY__PRINTER__SLICER": "bambu_studio"})
    assert data["printer"]["model"] == "bambulab.zz_test_printer" and data["printer"]["slicer"] == "bambu_studio"


def test_a_model_naming_another_vendor_is_refused(tmp_path, monkeypatch):
    (tmp_path / "zz_wrong_vendor.py").write_text(
        "from ..base import PrinterModel\n"
        "from ..klipper import VENDOR\n"
        "from ..klipper.adapter import KlipperAdapter\n"
        "MODEL = PrinterModel(key='bambulab.zz_wrong_vendor', label='x', adapter=KlipperAdapter,\n"
        "    templates='klipper_generic', vendor=VENDOR, settings={})\n")
    package = sys.modules["print_handy.printers.bambulab"]
    monkeypatch.setattr(package, "__path__", [*package.__path__, str(tmp_path)])
    try:
        with pytest.raises(RuntimeError, match="must be the bambulab package VENDOR"):
            printers._discover()
    finally:
        sys.modules.pop("print_handy.printers.bambulab.zz_wrong_vendor", None)


# ----------------------------------------------------------------------------- start code

def test_klipper_and_zmod_start_code_never_passes_for_bambu():
    bambu = printers.VENDORS["bambulab"]
    clean = "; generated by OrcaSlicer\nM140 S60\nG28\nM1002 gcode_claim_action : 2\nG1 X10 Y10 E1 ; move\nT0\n"
    assert start_code_problems(clean, bambu) == []
    for foreign in ("PRINT_START BED=60 HOTEND=215", "START_PRINT EXTRUDER_TEMP=215 BED_TEMP=60 MESH=auto",
                    "CHANGE_ZCOLOR SLOT=1", "LOAD_ZOFFSET", "BED_MESH_CALIBRATE"):
        problems = start_code_problems(clean + foreign + "\n", bambu)
        assert problems and foreign.split()[0] in problems[0]
    # Other vendors keep their own guards; their macros are expected there.
    assert start_code_problems("PRINT_START BED=60\n", printers.VENDORS["klipper"]) == []
    stock = printers.VENDORS["flashforge_stock"]
    assert stock.connection == "flashforge_stock" and stock.start_code == "flashforge_stock"
    assert start_code_problems(clean, stock) == []
    assert start_code_problems(clean + "START_PRINT EXTRUDER_TEMP=215\n", stock)[0].count("START_PRINT") == 1



def test_every_marlin_template_homes_before_it_moves():
    """A stock-Marlin start sequence must home (G28) before positioning: Marlin
    runs the template unchanged, and an unhomed printer prints from a wrong origin."""
    import json
    from pathlib import Path
    root = Path(printers.__file__).resolve().parent.parent / "templates" / "orca"
    checked = 0
    for key, model in printers.MODELS.items():
        if model.vendor.connection != "octoprint":
            continue
        machine = root / key.replace(".", "_") / "machine.json"
        if not machine.is_file():
            continue
        lines = [line.split(";")[0].strip() for line in
                 json.loads(machine.read_text())["machine_start_gcode"].splitlines()]
        assert "G28" in lines, key
        moves = [i for i, line in enumerate(lines) if line.split(" ")[0] in ("G0", "G1", "G90")]
        assert not moves or lines.index("G28") < moves[0], key
        checked += 1
    assert checked >= 9
