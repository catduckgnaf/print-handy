"""[[printers]] roster: parsing, migration equivalence, refusals, env overrides (#333).

Stage 2a of multi-printer support (#257) is a config-model change only: the
proof is equivalence. A legacy config and the one-entry roster spelling of
the same printer must resolve to the identical settings dict, and every
existing consumer keeps reading the legacy singleton view.
"""
import tempfile
import unittest
from pathlib import Path

from print_handy import printers as printer_registry
from print_handy import settings


def load_local(text, environ=None, suffix=".yaml"):
    with tempfile.TemporaryDirectory() as td:
        path = Path(td, f"local{suffix}")
        path.write_text(text)
        return settings.load(home=settings.HOME, local=path, environ=environ or {})


TWO_PRINTERS = """
discord:
  chat_id: "111"
printers:
  - id: garage-a1
    name: Garage A1
    model: bambulab.a1
    chat_id: "222"
    lan_host: 192.168.1.60
    lan_serial: "00M00A000000000"
  - id: workshop-ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
"""


class SynthesisTests(unittest.TestCase):
    """A config without [[printers]] synthesises a one-entry roster from the legacy sections."""

    def test_synthesised_entry_mirrors_the_legacy_view(self):
        data = settings.load(home=settings.HOME, local=False, environ={})
        (entry,) = data["printers"]
        self.assertEqual(entry["model"], data["printer"]["model"])
        self.assertEqual(entry["name"], data["printer"]["name"])
        self.assertEqual(entry["moonraker_url"], data["services"]["moonraker_url"])
        self.assertEqual(entry["moonraker_ws_url"], data["services"]["moonraker_ws_url"])
        self.assertEqual(entry["octoprint_url"], data["services"].get("octoprint_url", ""))
        self.assertEqual(entry["camera_snapshot_url"], data["services"]["camera_snapshot_url"])
        self.assertEqual(entry["lan_host"], data["bambu"]["lan_host"])
        self.assertEqual(entry["lan_serial"], data["bambu"]["lan_serial"])
        self.assertEqual(entry["host"], "")

    def test_synthesised_id_is_the_slug_of_the_printer_name(self):
        data = load_local('printer:\n  name: "Garage Printer!"\n')
        self.assertEqual(data["printers"][0]["id"], "garage-printer")
        self.assertEqual(data["printers"][0]["name"], "Garage Printer!")

    def test_id_slug_falls_back_to_printer(self):
        self.assertEqual(settings._printer_id_slug(""), "printer")
        self.assertEqual(settings._printer_id_slug(None), "printer")
        self.assertEqual(settings._printer_id_slug("!!!"), "printer")
        self.assertEqual(settings._printer_id_slug("Flashforge AD5X"), "flashforge-ad5x")


class EquivalenceTests(unittest.TestCase):
    """The same printer spelled legacy and as a one-entry roster resolves identically."""

    def test_bambu_legacy_and_roster_spellings_are_identical(self):
        legacy = load_local("""
printer:
  model: bambulab.a1
bambu:
  lan_host: 192.168.1.60
  lan_serial: "00M00A000000000"
""")
        roster = load_local("""
printers:
  - id: bambu-lab-a1
    model: bambulab.a1
    lan_host: 192.168.1.60
    lan_serial: "00M00A000000000"
""")
        self.assertEqual(legacy, roster)

    def test_moonraker_legacy_and_roster_spellings_are_identical(self):
        legacy = load_local("""
services:
  moonraker_url: http://192.168.1.61:7125
""")
        roster = load_local("""
printers:
  - id: flashforge-ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
""")
        self.assertEqual(legacy, roster)

    def test_toml_roster_parses(self):
        data = load_local("""
[[printers]]
id = "garage-a1"
model = "bambulab.a1"
lan_host = "192.168.1.60"
lan_serial = "00M00A000000000"
""", suffix=".toml")
        self.assertEqual(data["printer"]["model"], "bambulab.a1")
        self.assertEqual(data["printers"][0]["lan_host"], "192.168.1.60")


class RosterParsingTests(unittest.TestCase):
    def test_two_printers_load_with_the_first_as_the_legacy_view(self):
        data = load_local(TWO_PRINTERS)
        first, second = data["printers"]
        self.assertEqual(first["id"], "garage-a1")
        self.assertEqual(first["lan_serial"], "00M00A000000000")
        self.assertEqual(second["id"], "workshop-ad5x")
        self.assertEqual(second["moonraker_url"], "http://192.168.1.61:7125")
        # Derived exactly as the legacy view derives them.
        self.assertEqual(second["moonraker_ws_url"], "ws://192.168.1.61:7125/websocket")
        self.assertEqual(second["camera_snapshot_url"], "http://192.168.1.61/webcam/?action=snapshot")
        # The legacy singleton view is the first entry, and nothing else moved.
        self.assertEqual(data["printer"]["model"], "bambulab.a1")
        self.assertEqual(data["printer"]["name"], "Garage A1")
        self.assertEqual(data["bambu"]["lan_host"], "192.168.1.60")
        self.assertEqual(data["services"]["moonraker_url"], "")
        # printers.current() — the adapter every existing caller gets — follows the first entry.
        self.assertEqual(printer_registry.current(data).model.key, "bambulab.a1")
        self.assertEqual(printer_registry.current(data).display_name, "Garage A1")

    def test_roster_api(self):
        data = load_local(TWO_PRINTERS)
        self.assertEqual([entry["id"] for entry in settings.printers(data)],
                         ["garage-a1", "workshop-ad5x"])
        self.assertEqual(settings.by_id("workshop-ad5x", data)["model"], "flashforge.ad5x")
        with self.assertRaisesRegex(settings.SettingsError, "no printer with id 'nope'"):
            settings.by_id("nope", data)

    def test_all_vendor_connection_keys_per_entry(self):
        data = load_local("""
printers:
  - id: a1
    model: bambulab.a1
    lan_host: 192.168.1.60
    lan_serial: "00M00A000000000"
    lan_access_code_env: MY_BAMBU_CODE
    start_confirm_s: 45
  - id: mk4s
    model: prusa.mk4s
    link_host: 192.168.1.70
    api_key_env: MY_PRUSA_KEY
  - id: centauri
    model: elegoo_sdcp.centauricarbon
    host: 192.168.1.71
  - id: klipper-box
    model: klipper.generic
    moonraker_url: http://192.168.1.72:7125
""")
        a1, mk4s, centauri, klipper = data["printers"]
        self.assertEqual((a1["lan_access_code_env"], a1["start_confirm_s"]), ("MY_BAMBU_CODE", 45))
        self.assertEqual((mk4s["link_host"], mk4s["api_key_env"]), ("192.168.1.70", "MY_PRUSA_KEY"))
        self.assertEqual(centauri["host"], "192.168.1.71")
        self.assertEqual(klipper["moonraker_ws_url"], "ws://192.168.1.72:7125/websocket")
        # An entry's name defaults to the name that model's own defaults give [printer].
        self.assertEqual(mk4s["name"], "Prusa MK4S")
        self.assertEqual(centauri["name"], "Elegoo Centauri Carbon")


class PerEntryValidationTests(unittest.TestCase):
    def test_bambu_entry_refuses_a_moonraker_url(self):
        with self.assertRaisesRegex(settings.SettingsError, "workshop-a1.*moonraker_url and octoprint_url must be empty"):
            load_local("""
printers:
  - id: ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
  - id: workshop-a1
    model: bambulab.a1
    moonraker_url: http://192.168.1.60:7125
""")

    def test_first_entry_refuses_a_moonraker_url_with_the_legacy_error(self):
        with self.assertRaisesRegex(settings.SettingsError, "moonraker_url and octoprint_url must be empty for model 'bambulab.a1'"):
            load_local("""
printers:
  - id: a1
    model: bambulab.a1
    moonraker_url: http://192.168.1.60:7125
""")

    def test_entry_refuses_another_vendors_keys(self):
        with self.assertRaisesRegex(settings.SettingsError, "link_host only applies to Prusa"):
            load_local("""
printers:
  - id: ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
  - id: other
    model: klipper.generic
    link_host: 192.168.1.70
""")

    def test_prusa_entry_validates_its_link_host(self):
        data = load_local("""
printers:
  - id: ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
  - id: mk4s
    model: prusa.mk4s
    link_host: 192.168.1.70
""")
        self.assertEqual(data["printers"][1]["link_host"], "192.168.1.70")
        with self.assertRaisesRegex(settings.SettingsError, "lan_host and lan_serial must be set together"):
            load_local("""
printers:
  - id: ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
  - id: a1
    model: bambulab.a1
    lan_host: 192.168.1.60
""")

    def test_host_on_a_model_without_a_host_section_refuses(self):
        with self.assertRaisesRegex(settings.SettingsError, "host only applies to Elegoo SDCP and stock Flashforge"):
            load_local("""
printers:
  - id: klipper-box
    model: klipper.generic
    host: 192.168.1.72
    moonraker_url: http://192.168.1.72:7125
""")

    def test_unknown_model_and_missing_model_refuse(self):
        with self.assertRaisesRegex(settings.SettingsError, "model"):
            load_local("""
printers:
  - id: mystery
    model: acme.unknown
""")
        with self.assertRaisesRegex(settings.SettingsError, "model is required"):
            load_local("""
printers:
  - id: mystery
""")

    def test_bad_id_missing_id_unknown_key_and_empty_roster_refuse(self):
        for text, message in (
            ('printers:\n  - id: "Not A Slug!"\n    model: flashforge.ad5x\n', "id is required and must be a slug"),
            ('printers:\n  - model: flashforge.ad5x\n', "id is required and must be a slug"),
            ('printers:\n  - id: ad5x\n    model: flashforge.ad5x\n    bogus: 1\n', "unknown key"),
            ('printers: []\n', "at least one printer"),
            ('printers:\n  id: ad5x\n', "must be a list of printer entries"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(settings.SettingsError, message):
                    load_local(text)


class EntryModelDefaultsTests(unittest.TestCase):
    """A non-default entry is validated against a scratch view seeded with
    its model's defaults, so the model's nozzle_type / tool_count reach the
    resolved entry exactly as they do for the default entry (P2-125)."""

    def test_non_default_entries_keep_their_model_defaults(self):
        data = load_local("""
printers:
  - id: bench-ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
  - id: workshop-creator5
    model: flashforge.creator5
    moonraker_url: http://192.168.1.62:7125
  - id: spare-a1
    model: bambulab.a1
    lan_host: 192.168.1.63
    lan_serial: "00M00A000000001"
""")
        entries = {entry["id"]: entry for entry in data["printers"]}
        self.assertEqual(entries["workshop-creator5"].get("tool_count"), 4)
        self.assertEqual(entries["spare-a1"].get("nozzle_type"), "stainless_steel")

    def test_explicit_entry_values_still_win_over_model_defaults(self):
        data = load_local("""
printers:
  - id: bench-ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
  - id: workshop-creator5
    model: flashforge.creator5
    moonraker_url: http://192.168.1.62:7125
    tool_count: 2
""")
        entries = {entry["id"]: entry for entry in data["printers"]}
        self.assertEqual(entries["workshop-creator5"].get("tool_count"), 2)


class EntryScalarTypeTests(unittest.TestCase):
    """Roster scalar values are type-checked when the entry is copied, so a
    wrong-shaped value refuses with the entry named instead of crashing a
    consumer later (P2-126)."""

    def test_wrong_scalar_types_refuse_with_the_entry_named(self):
        for text, message in (
            ('printers:\n  - id: ad5x\n    model: flashforge.ad5x\n'
             '    camera_snapshot_url: 123\n', "camera_snapshot_url should be a string"),
            ('printers:\n  - id: ad5x\n    model: flashforge.ad5x\n'
             '    name: [a, b]\n', "name should be a string"),
            ('printers:\n  - id: ad5x\n    model: flashforge.ad5x\n'
             '    tool_count: "4"\n', "tool_count should be a whole number"),
            ('printers:\n  - id: ad5x\n    model: flashforge.ad5x\n'
             '    start_confirm_s: true\n', "start_confirm_s should be a whole number"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(settings.SettingsError, message):
                    load_local(text)


class ClaimRefusalTests(unittest.TestCase):
    def test_duplicate_id_refuses(self):
        with self.assertRaisesRegex(settings.SettingsError, "duplicate printer id 'ad5x'"):
            load_local("""
printers:
  - id: ad5x
    model: flashforge.ad5x
  - id: ad5x
    model: klipper.generic
""")

    def test_duplicate_chat_id_loads_as_a_shared_claim(self):
        # Two entries claiming one chat is a shared channel now (#536), not a refusal.
        data = load_local("""
printers:
  - id: a1
    model: bambulab.a1
    chat_id: "222"
  - id: ad5x
    model: flashforge.ad5x
    chat_id: "222"
""")
        self.assertEqual([entry["id"] for entry in data["printers"]], ["a1", "ad5x"])
        self.assertEqual([entry["chat_id"] for entry in data["printers"]], ["222", "222"])

    def test_two_fallback_claims_on_the_global_channel_load(self):
        # Two fallback claims on the global channel: a shared channel (#536).
        data = load_local("""
discord:
  chat_id: "111"
printers:
  - id: a1
    model: bambulab.a1
  - id: ad5x
    model: flashforge.ad5x
""")
        self.assertEqual([entry["id"] for entry in data["printers"]], ["a1", "ad5x"])

    def test_one_fallback_claim_is_the_design_example(self):
        # Design §3: one entry claims its own channel, the other falls back to the global one.
        data = load_local(TWO_PRINTERS)
        self.assertEqual(data["printers"][0]["chat_id"], "222")
        self.assertEqual(data["printers"][1]["chat_id"], "")


class MixedShapeTests(unittest.TestCase):
    def test_roster_plus_legacy_printer_model_refuses_and_names_it(self):
        with self.assertRaisesRegex(settings.SettingsError, r"\[\[printers\]\] is declared.*\[printer\] model"):
            load_local(TWO_PRINTERS + """
printer:
  model: flashforge.ad5x
""")

    def test_roster_plus_legacy_services_url_refuses(self):
        with self.assertRaisesRegex(settings.SettingsError, r"\[services\] moonraker_url"):
            load_local(TWO_PRINTERS + """
services:
  moonraker_url: http://192.168.1.99:7125
""")

    def test_roster_plus_legacy_environment_refuses(self):
        with self.assertRaisesRegex(settings.SettingsError, r"\$PRINT_HANDY__PRINTER__MODEL"):
            load_local(TWO_PRINTERS, environ={"PRINT_HANDY__PRINTER__MODEL": "flashforge.ad5x"})
        with self.assertRaisesRegex(settings.SettingsError, r"\$MOONRAKER_URL"):
            load_local(TWO_PRINTERS, environ={"MOONRAKER_URL": "http://192.168.1.99:7125"})

    def test_roster_plus_empty_legacy_values_loads(self):
        # The shipped home file always carries legacy values (they are the defaults base this
        # roster replaces); a local file that merely restates emptiness is not a second roster.
        data = load_local(TWO_PRINTERS + """
services:
  moonraker_url: ""
bambu:
  lan_access_code_env: BAMBU_LAN_PASSWORD
""")
        self.assertEqual(len(data["printers"]), 2)


class EnvOverrideTests(unittest.TestCase):
    def test_override_applies_to_a_declared_entry(self):
        data = load_local(TWO_PRINTERS, environ={
            "PRINT_HANDY__PRINTERS__WORKSHOP-AD5X__MOONRAKER_URL": "http://10.0.0.5:7125"})
        second = settings.by_id("workshop-ad5x", data)
        self.assertEqual(second["moonraker_url"], "http://10.0.0.5:7125")
        self.assertEqual(second["moonraker_ws_url"], "ws://10.0.0.5:7125/websocket")
        # The first entry and the legacy view (the A1) are untouched.
        self.assertEqual(data["printer"]["model"], "bambulab.a1")
        self.assertEqual(data["services"]["moonraker_url"], "")

    def test_override_of_the_default_entry_flows_into_the_legacy_view(self):
        data = load_local(TWO_PRINTERS, environ={
            "PRINT_HANDY__PRINTERS__GARAGE-A1__LAN_HOST": "10.0.0.9",
            "PRINT_HANDY__PRINTERS__GARAGE-A1__START_CONFIRM_S": "45"})
        self.assertEqual(data["bambu"]["lan_host"], "10.0.0.9")
        self.assertEqual(data["printers"][0]["start_confirm_s"], 45)

    def test_env_cannot_create_an_entry(self):
        with self.assertRaisesRegex(settings.SettingsError, "no printer with id 'nope' is declared"):
            load_local(TWO_PRINTERS, environ={"PRINT_HANDY__PRINTERS__NOPE__LAN_HOST": "10.0.0.9"})
        # ... and with no roster declared at all, an indexed name is refused the same way.
        with self.assertRaisesRegex(settings.SettingsError, "no printer with id 'ad5x' is declared"):
            settings.load(home=settings.HOME, local=False,
                          environ={"PRINT_HANDY__PRINTERS__AD5X__LAN_HOST": "10.0.0.9"})

    def test_malformed_indexed_names_are_refused_loudly(self):
        for name in ("PRINT_HANDY__PRINTERS__GARAGE-A1",
                     "PRINT_HANDY__PRINTERS__GARAGE-A1__LAN_HOST__EXTRA",
                     "PRINT_HANDY__PRINTERS__GARAGE-A1__BOGUS",
                     "PRINT_HANDY__PRINTERS__GARAGE-A1__MODEL",
                     "PRINT_HANDY__PRINTERS__GARAGE-A1__ID"):
            with self.subTest(name=name):
                with self.assertRaises(settings.SettingsError):
                    load_local(TWO_PRINTERS, environ={name: "x"})


GENERIC_GEOMETRY = """
printers:
  - id: workshop-ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
  - id: bench-klipper
    name: Bench Klipper
    model: klipper.generic
    moonraker_url: http://192.168.1.62:7125
    bed_mm: [100, 120]
    max_print_height_mm: 130
"""


class EntryGeometryTests(unittest.TestCase):
    """Custom build volumes on roster entries (P1-83): an Other/generic
    printer's measured geometry must survive into its settings view
    instead of being replaced by the model's defaults."""

    def test_entry_geometry_reaches_the_printer_view(self):
        data = load_local(GENERIC_GEOMETRY)
        entry = settings.by_id("bench-klipper", data)
        self.assertEqual(entry["bed_mm"], [100.0, 120.0])
        self.assertEqual(entry["max_print_height_mm"], 130.0)
        view = settings.view_for("bench-klipper", data)
        self.assertEqual(view["printer"]["bed_mm"], [100.0, 120.0])
        self.assertEqual(view["printer"]["max_print_height_mm"], 130.0)

    def test_entry_without_geometry_keeps_the_model_defaults(self):
        data = load_local(GENERIC_GEOMETRY)
        view = settings.view_for("workshop-ad5x", data)
        self.assertEqual(view["printer"]["bed_mm"], [220.0, 220.0])

    def test_default_entry_geometry_projects_into_the_legacy_view(self):
        data = load_local("""
printers:
  - id: bench-klipper
    model: klipper.generic
    moonraker_url: http://192.168.1.62:7125
    bed_mm: [100, 120]
    max_print_height_mm: 130
""")
        self.assertEqual(data["printer"]["bed_mm"], [100.0, 120.0])
        self.assertEqual(data["printer"]["max_print_height_mm"], 130.0)
        self.assertEqual(data["printers"][0]["bed_mm"], [100.0, 120.0])

    def test_bad_entry_geometry_is_refused(self):
        for bad in ("bed_mm: [220]", "bed_mm: [220, -1]", "bed_mm: 220",
                    "max_print_height_mm: -5", "max_print_height_mm: tall"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(settings.SettingsError, "bed_mm|max_print_height_mm"):
                    load_local(f"""
printers:
  - id: workshop-ad5x
    model: flashforge.ad5x
    moonraker_url: http://192.168.1.61:7125
  - id: bench-klipper
    model: klipper.generic
    moonraker_url: http://192.168.1.62:7125
    {bad}
""")


if __name__ == "__main__":
    unittest.main()
