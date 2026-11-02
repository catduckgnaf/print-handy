"""Contract tests for every registered printer model, plus the stock Klipper no-AD5X guarantees."""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from print_handy import printers, settings
from print_handy.printers.bambulab.a1 import A1Adapter
from print_handy.printers.bambulab.p1s import P1SAdapter
from print_handy.printers.base import BuildVolume, MultiColorUnsupported, PrinterAdapter

from print_handy.printers.anycubic import facts as anycubic_facts
from print_handy.printers.bambulab import facts as bambu_facts
from print_handy.printers.creality import facts as creality_facts
from print_handy.printers.elegoo import facts as elegoo_facts
from print_handy.printers.elegoo_sdcp import facts as sdcp_facts
from print_handy.printers.prusa import facts as prusa_facts
from print_handy.printers.qidi import facts as qidi_facts

from . import (a1_fixture, anycubic_fixture, bambu_family_fixture, creality_fixture, elegoo_fixture,
               p1s_fixture, prusa_fixture, qidi_fixture)

AD5X_ARTIFACTS = re.compile(
    r"SET_EXTRUDER_SLOT|\bIFS\b|zmod|ZMOD|CHANGE_ZCOLOR|display_off|LOAD_ZOFFSET|START_PRINT|MESH=|8898|AD5X|/detail",
    re.I)


MODELS = tuple(printers.MODELS)
AD5X, GENERIC, SV06 = "flashforge.ad5x", "klipper.generic", "sovol.sv06"
OCTOPRINT = "octoprint.marlin"
BAMBU_A1 = "bambulab.a1"
BAMBU_P1S = "bambulab.p1s"


def config_for(family):
    return settings.load(local=False, environ={"PRINT_HANDY__PRINTER__MODEL": family,
                                               "PRINT_HANDY__PATHS__HERMES_HOME": str(a1_fixture.HERMES)})


def adapter_for(family):
    return printers.get(family, config_for(family))


class ContractTests(unittest.TestCase):
    def setUp(self):
        # The A1 ships no templates: generate them from the invented fixture tree, trusted for this test only.
        patcher = mock.patch.object(A1Adapter, "facts", a1_fixture.fixture_facts())
        patcher.start()
        self.addCleanup(patcher.stop)
        a1_fixture.install(adapter_for(BAMBU_A1))
        patcher = mock.patch.object(P1SAdapter, "facts", p1s_fixture.fixture_facts())
        patcher.start()
        self.addCleanup(patcher.stop)
        p1s_fixture.install(adapter_for(BAMBU_P1S))
        # The #127 Stage 1 models share the family fixture: trust + install each one.
        for key, fact_name in (("bambulab.a1mini", "A1MINI"), ("bambulab.p1p", "P1P"), ("bambulab.x1", "X1"),
                               ("bambulab.x1c", "X1C"), ("bambulab.x1e", "X1E"), ("bambulab.p2s", "P2S"),
                               ("bambulab.h2s", "H2S"), ("bambulab.h2d", "H2D"), ("bambulab.h2dpro", "H2DPRO"),
                               ("bambulab.x2d", "X2D"), ("bambulab.h2c", "H2C"), ("bambulab.a2l", "A2L")):
            model_facts = getattr(bambu_facts, fact_name)
            patcher = mock.patch.object(printers.MODELS[key].adapter, "facts",
                                        bambu_family_fixture.fixture_facts(model_facts))
            patcher.start()
            self.addCleanup(patcher.stop)
            bambu_family_fixture.install(adapter_for(key), model_facts)
        # The Prusa models (#242) share the Prusa fixture: trust + install each one.
        for key, fact_name in (("prusa.mk4s", "MK4S"), ("prusa.coreone", "COREONE"),
                               ("prusa.xl", "XL"), ("prusa.miniplus", "MINIPLUS")):
            model_facts = getattr(prusa_facts, fact_name)
            patcher = mock.patch.object(printers.MODELS[key].adapter, "facts",
                                        prusa_fixture.fixture_facts(model_facts))
            patcher.start()
            self.addCleanup(patcher.stop)
            prusa_fixture.install(adapter_for(key), model_facts)
        # The Elegoo models (#243) share the Elegoo fixture: trust + install each one.
        for key, model_facts in (("elegoo.neptune4", elegoo_facts.NEPTUNE4),
                                 ("elegoo.neptune4pro", elegoo_facts.NEPTUNE4PRO),
                                 ("elegoo.neptune4plus", elegoo_facts.NEPTUNE4PLUS),
                                 ("elegoo.neptune4max", elegoo_facts.NEPTUNE4MAX),
                                 ("elegoo_sdcp.centauricarbon", sdcp_facts.CENTAURI_CARBON)):
            patcher = mock.patch.object(printers.MODELS[key].adapter, "facts",
                                        elegoo_fixture.fixture_facts(model_facts))
            patcher.start()
            self.addCleanup(patcher.stop)
            elegoo_fixture.install(adapter_for(key), model_facts)
        # The Qidi models share the Qidi fixture: trust + install each one.
        for key, model_facts in (("qidi.q1pro", qidi_facts.Q1PRO),
                                 ("qidi.xplus3", qidi_facts.XPLUS3),
                                 ("qidi.xplus4", qidi_facts.XPLUS4),
                                 ("qidi.xmax3", qidi_facts.XMAX3),
                                 ("qidi.xsmart3", qidi_facts.XSMART3)):
            patcher = mock.patch.object(printers.MODELS[key].adapter, "facts",
                                        qidi_fixture.fixture_facts(model_facts))
            patcher.start()
            self.addCleanup(patcher.stop)
            qidi_fixture.install(adapter_for(key), model_facts)
        # The Creality models share the Creality fixture: trust + install each one.
        for key, model_facts in (("creality.ender3v3", creality_facts.ENDER3V3),
                                 ("creality.ender3v3ke", creality_facts.ENDER3V3KE),
                                 ("creality.ender3v3plus", creality_facts.ENDER3V3PLUS),
                                 ("creality.k1", creality_facts.K1),
                                 ("creality.k1c", creality_facts.K1C),
                                 ("creality.k1max", creality_facts.K1MAX)):
            patcher = mock.patch.object(printers.MODELS[key].adapter, "facts",
                                        creality_fixture.fixture_facts(model_facts))
            patcher.start()
            self.addCleanup(patcher.stop)
            creality_fixture.install(adapter_for(key), model_facts)
        # The Anycubic models (#552) share the Anycubic fixture: trust + install each one.
        for key, model_facts in (("anycubic.kobra2", anycubic_facts.KOBRA2),
                                 ("anycubic.kobra2neo", anycubic_facts.KOBRA2NEO),
                                 ("anycubic.kobra2pro", anycubic_facts.KOBRA2PRO),
                                 ("anycubic.kobra2plus", anycubic_facts.KOBRA2PLUS),
                                 ("anycubic.kobra2max", anycubic_facts.KOBRA2MAX)):
            patcher = mock.patch.object(printers.MODELS[key].adapter, "facts",
                                        anycubic_fixture.fixture_facts(model_facts))
            patcher.start()
            self.addCleanup(patcher.stop)
            anycubic_fixture.install(adapter_for(key), model_facts)

    def test_registry_finds_every_model_file(self):
        self.assertEqual(set(MODELS), {AD5X, GENERIC, SV06, OCTOPRINT, BAMBU_A1, BAMBU_P1S, "flashforge_stock.ad5x",
                                       "flashforge.ad5m", "flashforge.ad5m_pro",
                                       "flashforge.creator5", "flashforge.creator5_pro",
                                       "bambulab.a1mini", "bambulab.p1p", "bambulab.x1", "bambulab.x1c",
                                       "bambulab.x1e", "bambulab.p2s", "bambulab.h2s", "bambulab.h2d",
                                       "bambulab.h2dpro", "bambulab.x2d", "bambulab.h2c", "bambulab.a2l",
                                       "prusa.mk4s", "prusa.coreone", "prusa.xl", "prusa.miniplus",
                                       "elegoo.neptune4", "elegoo.neptune4pro", "elegoo.neptune4plus",
                                       "elegoo.neptune4max", "elegoo_sdcp.centauricarbon",
                                       "qidi.q1pro", "qidi.xplus3", "qidi.xplus4", "qidi.xmax3", "qidi.xsmart3",
                                       "creality.ender3v3", "creality.ender3v3ke", "creality.ender3v3plus",
                                       "creality.k1", "creality.k1c", "creality.k1max",
                                       "octoprint.ender3", "octoprint.ender3pro", "octoprint.ender3v2",
                                       "octoprint.ender3v3se",
                                       "anycubic.kobra2", "anycubic.kobra2neo", "anycubic.kobra2pro",
                                       "anycubic.kobra2plus", "anycubic.kobra2max",
                                       "snapmaker.u1",
                                       "octoprint.i3megas", "octoprint.vyper", "octoprint.chiron",
                                       "octoprint.maxpro2", "octoprint.kobra", "octoprint.kobraneo",
                                       "octoprint.kobraplus", "octoprint.kobramax",
                                       "sovol.sv01", "sovol.sv01_pro", "sovol.sv02", "sovol.sv03",
                                       "sovol.sv04", "sovol.sv05", "sovol.sv06_plus", "sovol.sv06_ace",
                                       "sovol.sv06_plus_ace", "sovol.sv07", "sovol.sv07_plus",
                                       "sovol.sv08", "sovol.sv08_max", "sovol.zero", "sovol.m1d",
                                       "sovol_marlin.sv01", "sovol_marlin.sv01_pro", "sovol_marlin.sv02",
                                       "sovol_marlin.sv03", "sovol_marlin.sv04", "sovol_marlin.sv05",
                                       "sovol_marlin.sv06", "sovol_marlin.sv06_plus"})
        for key, model in printers.MODELS.items():
            self.assertEqual(model.key, key)
            shipped = settings.ROOT / "print_handy" / "templates" / "orca" / model.templates
            # Generated templates (third-party profiles) are never shipped; every other model ships its own.
            self.assertEqual(shipped.is_dir(), not model.generated_templates, key)
            self.assertTrue(issubclass(model.adapter, PrinterAdapter))

    def test_shipped_template_height_matches_model_height(self):
        # A model whose guards refuse above max_print_height_mm must not ship a
        # template that slices taller (#661: klipper.generic and sovol.sv06 said
        # 220 in the model and registry while their templates sliced 250).
        for key, model in printers.MODELS.items():
            if model.generated_templates:
                continue  # generated from the operator's slicer; nothing shipped to compare
            machine = json.loads((settings.ROOT / "print_handy" / "templates" / "orca"
                                  / model.templates / "machine.json").read_text())
            with self.subTest(model=key):
                self.assertEqual(float(machine["printable_height"]),
                                 float(model.defaults()["printer"]["max_print_height_mm"]))

    def test_every_family_has_an_adapter(self):
        for family in MODELS:
            with self.subTest(family=family):
                adapter = adapter_for(family)
                self.assertIsInstance(adapter, PrinterAdapter)
                self.assertEqual(adapter.key, family)

    def test_contract_for_every_adapter(self):
        for family in MODELS:
            with self.subTest(family=family):
                a = adapter_for(family)
                vol = a.build_volume()
                self.assertIsInstance(vol, BuildVolume)
                self.assertTrue(all(v > 0 for v in (*vol.bed_mm, vol.max_height_mm)))
                self.assertGreaterEqual(a.slot_count, 1)
                if family == "flashforge_stock.ad5x":
                    # One tool and four physical IFS bays: the model's class is
                    # multi_slot (#656), and multicolor stays refused by the
                    # adapter's own mapping/start paths, not by the class.
                    self.assertEqual((a.slot_count, a.multi_slot), (4, True))
                elif a.multi_tool:
                    # Toolheads are filament positions without being IFS slots:
                    # slot_count counts them, multi_slot stays off, multi_tool is on.
                    self.assertEqual((a.slot_count, a.multi_slot), (a.tool_count, False))
                else:
                    self.assertEqual(a.multi_slot, a.slot_count > 1)
                folder = a.templates_dir()
                for name in ("machine", "process", "filament"):
                    json.loads((folder / f"{name}.json").read_text())
                if family != "flashforge_stock.ad5x" and not family.startswith(("octoprint.", "sovol_marlin.", "bambulab.", "prusa.", "elegoo", "qidi.", "creality.", "anycubic.")):
                    self.assertIn("[bed_temperature_initial_layer_single]", a.machine_start_gcode("X MESH=auto", "auto"))
                plan = a.start_plan(60, 215)
                if family == "flashforge_stock.ad5x":
                    self.assertEqual(plan, [])  # the slicer file owns heating
                else:
                    self.assertTrue(plan and all(isinstance(line, str) for line in plan))
                if family.startswith("bambulab."):
                    # Offline only: the vendor start runs first, and nothing live is available.
                    machine = json.loads((a.pinned_templates() / "machine.json").read_text())
                    self.assertEqual(plan, machine["machine_start_gcode"].splitlines())
                    self.assertRaises(Exception, a.machine_start_gcode, "X MESH=auto", "auto")
                    self.assertRaises(Exception, a.moonraker_start_commands, "part.gcode")
                    self.assertRaises(Exception, a.moonraker_objects)
                    self.assertEqual(a.normalize_status({"print_stats": {"state": "printing"}})["state"], "unknown")
                    continue
                if family.startswith("prusa."):
                    # Offline only: the slicer owns the start sequence, and nothing live is available.
                    machine = json.loads((a.pinned_templates() / "machine.json").read_text())
                    self.assertEqual(plan, str(machine["machine_start_gcode"]).splitlines())
                    self.assertRaises(Exception, a.moonraker_start_commands, "part.gcode")
                    self.assertRaises(Exception, a.moonraker_objects)
                    self.assertIn("state", a.normalize_status({}))
                    continue
                if family == "elegoo_sdcp.centauricarbon":
                    # Offline only: the slicer owns the start sequence, and nothing live is available.
                    machine = json.loads((a.pinned_templates() / "machine.json").read_text())
                    self.assertEqual(plan, str(machine["machine_start_gcode"]).splitlines())
                    self.assertRaises(Exception, a.moonraker_start_commands, "part.gcode")
                    self.assertRaises(Exception, a.moonraker_objects)
                    self.assertIn("state", a.normalize_status({}))
                    continue
                if family.startswith(("octoprint.", "sovol_marlin.")):
                    self.assertEqual(plan, ["M190 S60", "M109 S215"])
                    self.assertEqual(a.machine_end_gcode("END_PRINT"), "END_PRINT")
                    self.assertRaises(Exception, a.moonraker_start_commands, "part.gcode")
                    self.assertRaises(Exception, a.moonraker_objects)
                    self.assertEqual(a.normalize_status({})["state"], "unknown")
                    continue
                if family == "flashforge_stock.ad5x":
                    self.assertEqual(a.machine_start_gcode("SLICER START"), "SLICER START")
                    self.assertEqual(a.machine_end_gcode("END_PRINT"), "END_PRINT")
                    self.assertRaises(Exception, a.moonraker_start_commands, "part.gcode")
                    self.assertRaises(Exception, a.moonraker_objects)
                    self.assertIn("state", a.normalize_status({}))
                    continue
                cmds = a.moonraker_start_commands("part.gcode")
                self.assertEqual(cmds[-1]["path"], "/printer/print/start")
                self.assertEqual(cmds[-1]["body"], {"filename": "part.gcode"})
                self.assertIn("print_stats", a.moonraker_objects())
                status = a.normalize_status({"print_stats": {"state": "standby", "filename": "a.gcode"},
                                             "extruder": {"temperature": 21.5}})
                self.assertEqual((status["state"], status["filename"], status["hotend_c"]), ("standby", "a.gcode", 21.5))
                self.assertEqual(a.normalize_status({})["state"], "unknown")

    def test_home_config_model_is_the_ad5x_and_current_returns_it(self):
        home = settings.load(local=False, environ={})
        self.assertEqual(home["printer"]["model"], AD5X)
        self.assertEqual(printers.current(home).key, AD5X)

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(ValueError):
            printers.get("prusa.mk4")


class ZmodTests(unittest.TestCase):
    def setUp(self):
        self.a = adapter_for(AD5X)

    def test_ad5x_start_matches_the_template(self):
        home = settings.load(local=False, environ={})
        machine = json.loads((home["orca"]["templates"] / "machine.json").read_text())
        out = self.a.machine_start_gcode(machine["machine_start_gcode"], "bed_a")
        self.assertTrue(out.startswith("M190 S[bed_temperature_initial_layer_single]\nM104 S[nozzle_temperature_initial_layer] T0\nSTART_PRINT "))
        self.assertIn("MESH=bed_a", out)
        self.assertEqual(self.a.machine_end_gcode("END_PRINT"), "END_PRINT")
        self.assertEqual(self.a.slot_count, 4)
        self.assertTrue(self.a.multi_slot)

    def test_ad5x_screen_and_stock_api(self):
        self.assertEqual(self.a.screen_command(2, "PLA", "ff0000"), "CHANGE_ZCOLOR SLOT=2 TYPE=PLA HEX=FF0000")
        self.assertEqual(self.a.stock_api_url("printer.test"), "http://printer.test:8898/detail")
        self.assertIn("START_PRINT BED_TEMP=60 EXTRUDER_TEMP=215 MESH=auto", self.a.start_plan(60, 215)[-1])


IFS_ARTIFACTS = re.compile(r"SET_EXTRUDER_SLOT|CHANGE_ZCOLOR|\bIFS\b|zmod_color|zmod_ifs|8898|/detail", re.I)


class ZmodSingleExtruderTests(unittest.TestCase):
    """AD5M / AD5M Pro (#301): ZMod start code and Moonraker transport, IFS fully off."""

    FAMILIES = ("flashforge.ad5m", "flashforge.ad5m_pro")

    def test_zmod_start_but_no_ifs(self):
        for family in self.FAMILIES:
            with self.subTest(family=family):
                a = adapter_for(family)
                self.assertEqual((a.slot_count, a.multi_slot, a.has_native_screen), (1, False, False))
                # The ZMod start sequence is shared with the AD5X...
                self.assertIn("START_PRINT BED_TEMP=60 EXTRUDER_TEMP=215 MESH=auto", a.start_plan(60, 215)[-1])
                out = a.machine_start_gcode("START_PRINT MESH=auto", "bed_a")
                self.assertTrue(out.startswith("M190 S[bed_temperature_initial_layer_single]\n"
                                               "M104 S[nozzle_temperature_initial_layer] T0\n"))
                self.assertIn("MESH=bed_a", out)
                # ...but nothing IFS/native-mode leaks in: no screen sync, no stock API,
                # no IFS telemetry objects, and status carries no slots.
                self.assertIsNone(a.screen_command(1, "PLA", "FF0000"))
                self.assertIsNone(a.stock_api_url("printer.test"))
                self.assertNotIn("slots", a.normalize_status({}))
                texts = [*a.start_plan(60, 215), " ".join(a.moonraker_objects()),
                         json.dumps(a.normalize_status({}))]
                for text in texts:
                    self.assertIsNone(IFS_ARTIFACTS.search(text), text)
                with self.assertRaises(MultiColorUnsupported):
                    a.native

    def test_templates_have_no_ifs_artifacts(self):
        for family in self.FAMILIES:
            folder = adapter_for(family).templates_dir()
            for name in ("machine", "process", "filament"):
                text = (folder / f"{name}.json").read_text()
                self.assertIsNone(IFS_ARTIFACTS.search(text), (family, name))

    def test_multicolor_is_refused_and_single_is_accepted(self):
        for family in self.FAMILIES:
            a = adapter_for(family)
            self.assertEqual(a.normalize_tool_mapping({"0": 1}, {}), {"0": 1})
            with self.assertRaises(MultiColorUnsupported):
                a.normalize_tool_mapping({"0": 1, "1": 2}, {})

    def test_readiness_is_the_plain_check(self):
        for family in self.FAMILIES:
            a = adapter_for(family)
            self.assertEqual(a.readiness_problems({}, {"print_stats": {"state": "standby"},
                                                       "webhooks": {"state": "ready"}}), [])
            self.assertTrue(a.readiness_problems({}, {"print_stats": {"state": "printing"}}))
            self.assertTrue(a.readiness_problems({}, {}))

    def test_each_model_has_its_own_templates(self):
        folders = {adapter_for(f).templates_dir() for f in self.FAMILIES}
        self.assertEqual(len(folders), 2)
        for family in self.FAMILIES:
            machine = json.loads((adapter_for(family).templates_dir() / "machine.json").read_text())
            self.assertEqual(machine["printable_area"], ["0x0", "220x0", "220x220", "0x220"])
            self.assertEqual(machine["printable_height"], "220")


# The Creator's own artifacts: zmod_color is legitimate here (it carries the active
# tool); IFS machinery is not.
TOOL_ARTIFACTS = re.compile(r"SET_EXTRUDER_SLOT|CHANGE_ZCOLOR|\bIFS\b|zmod_ifs|8898|/detail", re.I)


def _tool_status(**overrides):
    status = {"print_stats": {"state": "standby"}, "webhooks": {"state": "ready"},
              "extruder": {"temperature": 25.0}, "extruder1": {"temperature": 24.0},
              "extruder2": {"temperature": 26.0}, "extruder3": {"temperature": 25.0},
              "zmod_color": {"active_tool_id": 2}}
    status.update(overrides)
    return status


class ZmodMultiToolTests(unittest.TestCase):
    """Creator 5 / 5 Pro (#301 stage 2): 4 toolheads modelled as tools, never IFS slots."""

    FAMILIES = ("flashforge.creator5", "flashforge.creator5_pro")

    def test_tools_not_slots(self):
        for family in self.FAMILIES:
            with self.subTest(family=family):
                a = adapter_for(family)
                self.assertEqual((a.tool_count, a.slot_count), (4, 4))
                self.assertEqual((a.multi_tool, a.multi_slot, a.has_native_screen), (True, False, False))
                # The settings pin the same split: tool_count names the heads,
                # filament_slots stays 1 so no IFS behaviour in core switches on.
                self.assertEqual(a.config["printer"]["tool_count"], 4)
                self.assertEqual(a.config["printer"]["filament_slots"], 1)
                with self.assertRaises(MultiColorUnsupported):
                    a.native

    def test_zmod_start_and_telemetry_without_ifs(self):
        for family in self.FAMILIES:
            with self.subTest(family=family):
                a = adapter_for(family)
                self.assertIn("START_PRINT BED_TEMP=60 EXTRUDER_TEMP=215 MESH=auto", a.start_plan(60, 215)[-1])
                out = a.machine_start_gcode("START_PRINT MESH=auto", "bed_a")
                self.assertTrue(out.startswith("M190 S[bed_temperature_initial_layer_single]\n"
                                               "M104 S[nozzle_temperature_initial_layer] T0\n"))
                self.assertIn("MESH=bed_a", out)
                objects = a.moonraker_objects()
                self.assertEqual([o for o in objects if o.startswith("extruder")],
                                 ["extruder", "extruder1", "extruder2", "extruder3"])
                self.assertIn("zmod_color", objects)
                self.assertIsNone(a.screen_command(1, "PLA", "FF0000"))
                self.assertIsNone(a.stock_api_url("printer.test"))
                for text in (*a.start_plan(60, 215), " ".join(objects)):
                    self.assertIsNone(TOOL_ARTIFACTS.search(text), text)

    def test_identity_tool_mapping_only(self):
        for family in self.FAMILIES:
            a = adapter_for(family)
            self.assertEqual(a.normalize_tool_mapping({"0": 1}, {}), {"0": 1})
            self.assertEqual(a.normalize_tool_mapping({"0": 1, "1": 2, "2": 3, "3": 4}, {}),
                             {"0": 1, "1": 2, "2": 3, "3": 4})
            # A tool remapped to another head cannot be represented: refused.
            with self.assertRaises(MultiColorUnsupported):
                a.normalize_tool_mapping({"0": 2}, {})
            with self.assertRaises(MultiColorUnsupported):
                a.normalize_tool_mapping({"0": 1, "1": 1}, {})
            # A fifth tool does not exist: refused, not dropped.
            with self.assertRaises(MultiColorUnsupported):
                a.normalize_tool_mapping({"0": 1, "4": 5}, {})

    def test_readiness_is_per_toolhead(self):
        for family in self.FAMILIES:
            a = adapter_for(family)
            self.assertEqual(a.readiness_problems({}, _tool_status()), [])
            self.assertTrue(a.readiness_problems({}, _tool_status(print_stats={"state": "printing"})))
            missing = _tool_status()
            del missing["extruder2"]
            self.assertTrue(any("toolhead 3" in p for p in a.readiness_problems({}, missing)))
            self.assertTrue(any("active tool" in p for p in
                                a.readiness_problems({}, _tool_status(zmod_color={"active_tool_id": 7}))))
            self.assertTrue(a.readiness_problems({}, {}))

    def test_status_reports_toolheads_and_active_tool(self):
        for family in self.FAMILIES:
            a = adapter_for(family)
            out = a.normalize_status({"print_stats": {"state": "printing"},
                                      "extruder": {"temperature": 210.0}, "extruder2": {"temperature": 215.0},
                                      "zmod_color": {"active_tool_id": 2}})
            self.assertEqual(out["active_tool"], 2)
            self.assertEqual([h["temperature"] for h in out["toolheads"]], [210.0, None, 215.0, None])
            self.assertNotIn("slots", out)

    def test_templates_are_four_tool_and_ifs_free(self):
        folders = {adapter_for(f).templates_dir() for f in self.FAMILIES}
        self.assertEqual(len(folders), 2)
        for family in self.FAMILIES:
            folder = adapter_for(family).templates_dir()
            machine = json.loads((folder / "machine.json").read_text())
            self.assertEqual(machine["printable_area"], ["0x0", "256x0", "256x256", "0x256"])
            self.assertEqual(machine["printable_height"], "256")
            self.assertEqual(machine["nozzle_diameter"], ["0.4"] * 4)
            self.assertEqual(machine["single_extruder_multi_material"], "0")
            for name in ("machine", "process", "filament"):
                text = (folder / f"{name}.json").read_text()
                self.assertIsNone(TOOL_ARTIFACTS.search(text), (family, name))
        pro = json.loads((adapter_for("flashforge.creator5_pro").templates_dir() / "machine.json").read_text())
        base = json.loads((adapter_for("flashforge.creator5").templates_dir() / "machine.json").read_text())
        self.assertEqual((pro["support_chamber_temp_control"], pro["support_air_filtration"]), ("1", "1"))
        self.assertEqual((base["support_chamber_temp_control"], base["support_air_filtration"]), ("0", "0"))


class GenericKlipperTests(unittest.TestCase):
    FAMILIES = (GENERIC, SV06)

    def test_start_plan_and_commands_have_no_ad5x_artifacts(self):
        for family in self.FAMILIES:
            with self.subTest(family=family):
                a = adapter_for(family)
                texts = [*a.start_plan(60, 215), a.machine_start_gcode("START_PRINT MESH=auto", "auto"),
                         a.machine_end_gcode("END_PRINT"), json.dumps(a.moonraker_start_commands("a.gcode")),
                         " ".join(a.moonraker_objects()), json.dumps(a.normalize_status({}))]
                for text in texts:
                    self.assertIsNone(AD5X_ARTIFACTS.search(text), text)
                self.assertEqual(a.start_plan(60, 215)[0], "PRINT_START BED=60 HOTEND=215")
                self.assertEqual(a.machine_end_gcode("END_PRINT"), "PRINT_END")

    def test_templates_have_no_ad5x_artifacts(self):
        for family in self.FAMILIES:
            folder = adapter_for(family).templates_dir()
            for name in ("machine", "process", "filament"):
                text = (folder / f"{name}.json").read_text()
                self.assertIsNone(AD5X_ARTIFACTS.search(text.replace("SET_PRINT_STATS_INFO", "")), (family, name))
                self.assertNotIn("TIMELAPSE", text)

    def test_macro_names_come_from_settings(self):
        cfg = config_for(GENERIC)
        cfg["printer"].update(start_macro="START_MY_PRINT", end_macro="END_MY_PRINT")
        a = printers.get(GENERIC, cfg)
        self.assertEqual(a.start_plan(70, 240)[0], "START_MY_PRINT BED=70 HOTEND=240")
        self.assertEqual(a.machine_end_gcode(""), "END_MY_PRINT")

    def test_no_screen_no_vendor_api_single_slot(self):
        for family in self.FAMILIES:
            a = adapter_for(family)
            self.assertIsNone(a.screen_command(1, "PLA", "FF0000"))
            self.assertIsNone(a.stock_api_url("printer.test"))
            self.assertEqual((a.slot_count, a.multi_slot), (1, False))

    def test_multicolor_is_refused_and_single_is_accepted(self):
        a = adapter_for(GENERIC)
        self.assertEqual(a.normalize_tool_mapping({"0": 1}, {}), {"0": 1})
        with self.assertRaises(MultiColorUnsupported):
            a.normalize_tool_mapping({"0": 1, "1": 2}, {})

    def test_readiness(self):
        a = adapter_for(GENERIC)
        self.assertEqual(a.readiness_problems({}, {"print_stats": {"state": "standby"}, "webhooks": {"state": "ready"}}), [])
        self.assertTrue(a.readiness_problems({}, {"print_stats": {"state": "printing"}}))
        self.assertTrue(a.readiness_problems({}, {}))

    def test_sovol_has_its_own_templates(self):
        self.assertNotEqual(adapter_for(SV06).templates_dir(), adapter_for(GENERIC).templates_dir())
        machine = json.loads((adapter_for(SV06).templates_dir() / "machine.json").read_text())
        self.assertIn("SV06", machine["name"])

    def test_status_reports_targets_layers_and_remaining_time(self):
        raw = {"print_stats": {"state": "printing", "filename": "a.gcode", "print_duration": 1200.0,
                               "info": {"current_layer": 12, "total_layer": 80}},
               "virtual_sdcard": {"progress": 0.25},
               "extruder": {"temperature": 214.8, "target": 215.0},
               "heater_bed": {"temperature": 59.9, "target": 60}}
        families = (*self.FAMILIES, "flashforge.ad5x", "flashforge.ad5m", "flashforge.creator5")
        for family in families:
            with self.subTest(family=family):
                out = adapter_for(family).normalize_status(raw)
                self.assertEqual((out["hotend_target_c"], out["bed_target_c"]), (215.0, 60))
                self.assertEqual((out["layer"], out["total_layers"]), (12, 80))
                # 1200 s at 25 % -> 3600 s left = 60 min.
                self.assertEqual(out["remaining_min"], 60)

    def test_status_extended_fields_are_none_when_absent(self):
        keys = ("hotend_target_c", "bed_target_c", "layer", "total_layers", "remaining_min")
        samples = (
            {},
            {"print_stats": {"state": "printing", "info": {"current_layer": None, "total_layer": None}}},
            # Not printing, zero progress, bools and strings are no estimate and no numbers.
            {"print_stats": {"state": "paused", "print_duration": 600.0}, "virtual_sdcard": {"progress": 0.5}},
            {"print_stats": {"state": "printing", "print_duration": 600.0}, "virtual_sdcard": {"progress": 0.0}},
            {"print_stats": {"state": "printing", "print_duration": True, "info": {"current_layer": True}},
             "virtual_sdcard": {"progress": 0.5}, "extruder": {"target": True}, "heater_bed": {"target": "60"}},
        )
        for raw in samples:
            with self.subTest(raw=raw):
                out = adapter_for(GENERIC).normalize_status(raw)
                for key in keys:
                    self.assertIn(key, out)
                    self.assertIsNone(out[key], key)

    def test_stock_klipper_does_not_import_zmod_code(self):
        code = (f"import sys\nsys.path.insert(0, {str(settings.ROOT)!r})\nfrom print_handy import printers\n"
                "printers.get('klipper.generic'); printers.get('sovol.sv06')\n"
                "bad=[m for m in sys.modules if m.startswith(('print_handy.printers.flashforge.zmod',"
                "'print_handy.hermes.spool_sync'))]\n"
                "print(bad)\n")
        root = Path(settings.ROOT)
        out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                             env={"PATH": ""})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")


class SettingsModelTests(unittest.TestCase):
    def load(self, **printer):
        env = {f"PRINT_HANDY__PRINTER__{k.upper()}": str(v) for k, v in printer.items()}
        return settings.load(local=False, environ=env)

    def test_selection_accepts_registered_models(self):
        for key in MODELS:
            self.assertEqual(printers.current(self.load(model=key)).key, key)

    def test_unknown_model_rejected(self):
        with self.assertRaisesRegex(settings.SettingsError, "model must be one of"):
            self.load(model="prusa.mk4")

    def test_family_key_is_rejected_by_name(self):
        with self.assertRaisesRegex(settings.SettingsError, r"unknown setting \[printer\] family .*model"):
            self.load(family="flashforge_zmod")
        local = Path(tempfile.mkdtemp()) / "local.toml"
        local.write_text('[printer]\nfamily = "flashforge_zmod"\n')
        with self.assertRaisesRegex(settings.SettingsError, r"unknown setting \[printer\] family"):
            settings.load(local=local, environ={})

    def test_model_defaults_come_from_the_model_file(self):
        for key, model in printers.MODELS.items():
            data = self.load(model=key)
            for section, values in model.settings.items():
                for name, value in values.items():
                    expected = Path(settings.ROOT / value) if section == "orca" and name == "templates" else value
                    self.assertEqual(data[section][name], expected, (key, section, name))

    def test_local_toml_overrides_the_model_defaults_with_type_checks(self):
        folder = Path(tempfile.mkdtemp())
        good = folder / "good.toml"
        good.write_text('[printer]\nbed_mm = [250.0, 250.0]\n[layout]\nsingle_copy_edge_mm = 2.5\n')
        data = settings.load(local=good, environ={})
        self.assertEqual((data["printer"]["bed_mm"], data["layout"]["single_copy_edge_mm"]), ([250.0, 250.0], 2.5))
        self.assertEqual(data["printer"]["model"], AD5X)
        bad = folder / "bad.toml"
        bad.write_text('[printer]\nnozzle_diameter_mm = "big"\n')
        with self.assertRaisesRegex(settings.SettingsError, "nozzle_diameter_mm should be like"):
            settings.load(local=bad, environ={})

    def test_local_toml_can_pick_the_model(self):
        local = Path(tempfile.mkdtemp()) / "local.toml"
        local.write_text('[printer]\nmodel = "sovol.sv06"\n')
        data = settings.load(local=local, environ={})
        self.assertEqual((data["printer"]["model"], data["printer"]["filament_slots"]), (SV06, 1))
        self.assertEqual(data["orca"]["templates"].name, "sovol_sv06")

    def test_klipper_models_need_one_slot(self):
        with self.assertRaisesRegex(settings.SettingsError, "filament_slots must be 1"):
            self.load(model=GENERIC, filament_slots=4)

    def test_each_model_uses_its_template_folder_by_default(self):
        for key, model in printers.MODELS.items():
            self.assertEqual(self.load(model=key)["orca"]["templates"].name, model.templates)

    def test_bad_macro_name_rejected(self):
        with self.assertRaisesRegex(settings.SettingsError, "start_macro"):
            self.load(start_macro="PRINT START; M112")

    def test_doctor_reports_the_model(self):
        from print_handy import doctor
        self.assertEqual(doctor.check_model(self.load()), "flashforge.ad5x (Flashforge AD5X (ZMod)), 4 slots (multi-slot)")
        self.assertIn("single extruder", doctor.check_model(self.load(model=SV06)))


if __name__ == "__main__":
    unittest.main()
