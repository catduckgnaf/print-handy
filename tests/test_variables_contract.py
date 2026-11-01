"""Variables cross-surface contract (#657, Stage 1): the ratchet.

Every settings variable in the defaults tree (the same inventory walk as
tests/test_docker_release.py: every model's defaults merged over
config/home.yaml, plus settings.OPTIONAL_KEYS) must be present on three
surfaces, or carry an exemption in EXEMPTIONS below:

  env      a scalar variable has its PRINT_HANDY__SECTION__KEY line in
           .env.example, or a LEGACY_ENV name. Lists and tables are
           file-only by loader design and count as covered.
  webui    the (section, key) pair has a field in print_handy/webui/catalog.py.
  consumer code outside the definition files reads the variable: its quoted
           key literal or its dotted "section.key" name appears somewhere
           under print_handy/, bin/ or scripts/. Definition files are
           settings.py, webui/catalog.py, printers/base.py (SHARED_SETTINGS)
           and the printer model/vendor modules that define MODEL / VENDOR.

The consumer detection is deliberately conservative: it proves a variable is
referenced somewhere outside its definition, not that every read is correct,
and a generic key name can be carried by a same-named key in another section.
The check exists to catch the failure mode of the 2.2.1 settings audit — a
variable that exists on some surfaces and is read by nothing at all — not to
prove liveness. Open sections (settings.OPEN_SECTIONS) hold operator-chosen
keys, not variables, and are out of scope.

EXEMPTIONS is the ratchet: one entry per uncovered variable, naming the
surfaces it misses and a tracking reference. The test fails on any uncovered
variable without an entry and on any stale entry (a variable that is covered,
or misses different surfaces than listed), so the list can only shrink.
References: "#657" is this issue (the Web UI catalog is a curated subset by
design; a listed variable gains a catalog field or keeps its recorded no-UI
verdict here), "#286" is the Web UI catalog stage whose secret-name rule
excludes *_env / token keys from the catalog on purpose, "#524" retired the
two discord keys (kept so existing configs still load), and "PR #655" is the
settings-wiring fix that gives services.camera_source a consumer and the
[notify] MQTT keys their Web UI fields. Exemptions resolved by that merged
change have been removed; the stale-entry check remains enforced.

#672 renamed printer.ifs_slots to printer.filament_slots (the exemption row
below follows the rename). The old spelling survives only as a loader alias
(settings.KEY_ALIASES): it is not in the defaults tree or OPTIONAL_KEYS, so
it never enters this inventory — the alias is not a phantom second variable,
and filament_slots is judged on its own surfaces like every other variable.
"""
import re
import unittest
from pathlib import Path

from print_handy import printers, settings
from print_handy.webui import CATALOG

ROOT = settings.ROOT

# ---------------------------------------------------------------------------
# EXEMPTIONS: (section, key) -> (surfaces the variable misses, tracking ref).
# Seeded from live main on 2026-10-30; see the module docstring for the refs.
# ---------------------------------------------------------------------------
EXEMPTIONS = {
    ("agent", "api_key_env"): (frozenset({"webui"}), "#286"),
    ("agent", "timeout_s"): (frozenset({"webui"}), "#657"),
    ("bambu", "command"): (frozenset({"webui"}), "#657"),
    ("bambu", "lan_access_code_env"): (frozenset({"webui"}), "#286"),
    ("bambu", "start_confirm_s"): (frozenset({"webui"}), "#657"),
    ("bot", "health_port"): (frozenset({"webui"}), "#657"),
    ("bot", "health_stale_s"): (frozenset({"webui"}), "#657"),
    ("bot", "sync_commands"): (frozenset({"webui"}), "#657"),
    ("design", "api_key_env"): (frozenset({"webui"}), "#286"),
    ("design", "enabled"): (frozenset({"webui"}), "#657"),
    ("design", "repair_attempts"): (frozenset({"webui"}), "#657"),
    ("design", "timeout_s"): (frozenset({"webui"}), "#657"),
    ("discord", "bot_name"): (frozenset({"webui"}), "#657"),
    ("discord", "cold_pull_outcome_s"): (frozenset({"webui"}), "#657"),
    ("discord", "daily_reset_hour"): (frozenset({"webui", "consumer"}), "#524"),
    ("discord", "post_print_quiet_s"): (frozenset({"webui", "consumer"}), "#524"),
    ("discord", "question_timeout_s"): (frozenset({"webui"}), "#657"),
    ("discord", "safety_confirm_timeout_s"): (frozenset({"webui"}), "#657"),
    ("discord", "token_env"): (frozenset({"webui"}), "#286"),
    ("discord", "upload_max_bytes"): (frozenset({"webui"}), "#657"),
    ("discord", "upload_wait_s"): (frozenset({"webui"}), "#657"),
    ("layout", "arrange_edge_mm"): (frozenset({"webui"}), "#657"),
    ("layout", "arrange_gap_mm"): (frozenset({"webui"}), "#657"),
    ("layout", "max_merged_hours"): (frozenset({"webui"}), "#657"),
    ("layout", "merge_edge_margin_mm"): (frozenset({"webui"}), "#657"),
    ("layout", "merge_spacing_mm"): (frozenset({"webui"}), "#657"),
    ("layout", "multi_copy_edge_mm"): (frozenset({"webui"}), "#657"),
    ("layout", "single_copy_edge_mm"): (frozenset({"webui"}), "#657"),
    ("listener", "ifs_presence_poll_s"): (frozenset({"webui"}), "#657"),
    ("listener", "spool_nag_s"): (frozenset({"webui"}), "#657"),
    ("listener", "start_claim_ttl_s"): (frozenset({"webui"}), "#657"),
    ("listener", "tick_s"): (frozenset({"webui"}), "#657"),
    ("llm", "api_key_env"): (frozenset({"webui"}), "#286"),
    ("llm", "timeout_s"): (frozenset({"webui"}), "#657"),
    ("logging", "file"): (frozenset({"webui"}), "#657"),
    ("logging", "levels"): (frozenset({"webui"}), "#657"),
    ("maintenance", "full_days"): (frozenset({"webui"}), "#657"),
    ("maintenance", "full_hours"): (frozenset({"webui"}), "#657"),
    ("maintenance", "quick_hours"): (frozenset({"webui"}), "#657"),
    ("matrix", "access_token_env"): (frozenset({"webui"}), "#286"),
    ("matrix", "bot_user_id"): (frozenset({"webui"}), "#657"),
    ("matrix", "upload_max_bytes"): (frozenset({"webui"}), "#657"),
    ("orca", "command"): (frozenset({"webui"}), "#657"),
    ("orca", "templates"): (frozenset({"webui"}), "#657"),
    ("paths", "archive"): (frozenset({"webui"}), "#657"),
    ("paths", "cache"): (frozenset({"webui"}), "#657"),
    ("paths", "ffmpeg"): (frozenset({"webui"}), "#657"),
    ("paths", "hermes_agent"): (frozenset({"webui", "consumer"}), "#657"),
    ("paths", "hermes_bin"): (frozenset({"webui"}), "#657"),
    ("paths", "hermes_home"): (frozenset({"webui"}), "#657"),
    ("paths", "hermes_restart_helper"): (frozenset({"webui"}), "#657"),
    ("paths", "records_vault"): (frozenset({"webui"}), "#657"),
    ("prime_tower", "edge_mm"): (frozenset({"webui"}), "#657"),
    ("prime_tower", "merge_slack_mm"): (frozenset({"webui"}), "#657"),
    ("prime_tower", "part_clearance_mm"): (frozenset({"webui"}), "#657"),
    ("prime_tower", "size_mm"): (frozenset({"webui"}), "#657"),
    ("printer", "bed_mm"): (frozenset({"webui"}), "#657"),
    ("printer", "end_macro"): (frozenset({"webui"}), "#657"),
    ("printer", "firmware"): (frozenset({"webui"}), "#657"),
    ("printer", "filament_slots"): (frozenset({"webui"}), "#657"),
    ("printer", "max_print_height_mm"): (frozenset({"webui"}), "#657"),
    ("printer", "name"): (frozenset({"webui"}), "#657"),
    ("printer", "nozzle_diameter_mm"): (frozenset({"webui"}), "#657"),
    ("printer", "nozzle_type"): (frozenset({"webui"}), "#657"),
    ("printer", "start_macro"): (frozenset({"webui"}), "#657"),
    ("printer", "tool_count"): (frozenset({"webui"}), "#657"),
    ("printer", "vendor"): (frozenset({"webui"}), "#657"),
    ("prusa", "api_key_env"): (frozenset({"webui"}), "#286"),
    ("services", "moonraker_ws_url"): (frozenset({"webui"}), "#657"),
    ("services", "openviking_url"): (frozenset({"webui"}), "#657"),
    ("services", "stock_api_port"): (frozenset({"webui"}), "#657"),
    ("slicer", "flash_studio_path"): (frozenset({"webui"}), "#657"),
    ("telegram", "token_env"): (frozenset({"webui"}), "#286"),
    ("telegram", "upload_max_bytes"): (frozenset({"webui"}), "#657"),
    ("timelapse", "capture_encoder"): (frozenset({"webui"}), "#657"),
    ("timelapse", "capture_interval_s"): (frozenset({"webui"}), "#657"),
    ("timelapse", "capture_max_bytes"): (frozenset({"webui"}), "#657"),
    ("timelapse", "capture_max_frames"): (frozenset({"webui"}), "#657"),
    ("timelapse", "capture_trigger"): (frozenset({"webui"}), "#657"),
    ("timelapse", "encoder"): (frozenset({"webui"}), "#657"),
    ("timelapse", "vertical_short"): (frozenset({"webui"}), "#657"),
    ("timelapse", "vertical_short_mode"): (frozenset({"webui"}), "#657"),
    ("timelapse", "vertical_short_overlay"): (frozenset({"webui"}), "#657"),
    ("webui", "access_token_env"): (frozenset({"webui"}), "#286"),
    # Deliberately not editable from the Web UI it protects: turning auth
    # off is a file/env decision, never a click on the page it unlocks.
    ("webui", "require_auth"): (frozenset({"webui"}), "#286"),
    # Like require_auth: arming printer controls is a file decision, not a
    # click on the page it arms, and not an .env.example default.
    ("webui", "controls_enabled"): (frozenset({"env", "webui"}), "#708"),
}
# --- end of EXEMPTIONS ------------------------------------------------------


def inventory():
    """{(section, key): example value} for every concrete settings variable."""
    home = settings._read(settings.HOME)
    found = {}
    for model in printers.MODELS.values():
        for section, values in settings._merge(model.defaults(), home).items():
            if not isinstance(values, dict) or section in settings.OPEN_SECTIONS:
                continue
            for key, value in values.items():
                found[(section, key)] = value
    for (section, key), kind in settings.OPTIONAL_KEYS.items():
        found.setdefault((section, key), kind())
    return found


def _definition_files():
    files = {ROOT / "print_handy" / "settings.py",
             ROOT / "print_handy" / "webui" / "catalog.py",
             ROOT / "print_handy" / "printers" / "base.py"}
    for path in (ROOT / "print_handy" / "printers").rglob("*.py"):
        if re.search(r"^(MODEL|VENDOR) = ", path.read_text(errors="ignore"), re.M):
            files.add(path)
    return files


def corpus_text():
    definitions = _definition_files()
    texts = []
    for base in ("print_handy", "bin", "scripts"):
        for path in sorted((ROOT / base).rglob("*")):
            if path.is_file() and path not in definitions and path.suffix not in {".pyc", ".png", ".jpg", ".ico"}:
                texts.append(path.read_text(errors="ignore"))
    return "\n".join(texts)


def surface_gaps(inventory_map, catalog_pairs, env_example, legacy_pairs, corpus):
    """{(section, key): frozenset of missed surfaces} for uncovered variables."""
    gaps = {}
    for (section, key), value in sorted(inventory_map.items()):
        missing = set()
        if not isinstance(value, (list, dict)):
            name = f"PRINT_HANDY__{section.upper()}__{key.upper()}"
            if not re.search(rf"(?m)^#? ?{re.escape(name)}=", env_example) and (section, key) not in legacy_pairs:
                missing.add("env")
        if (section, key) not in catalog_pairs:
            missing.add("webui")
        if not re.search(r"""["']""" + re.escape(key) + r"""["']""", corpus) and f"{section}.{key}" not in corpus:
            missing.add("consumer")
        if missing:
            gaps[(section, key)] = frozenset(missing)
    return gaps


def problems(gaps, exemptions, known_keys):
    """Ratchet verdicts: uncovered-without-entry and stale entries."""
    found = []
    for key, missing in sorted(gaps.items()):
        if key not in exemptions:
            found.append(f"{key[0]}.{key[1]}: uncovered on {sorted(missing)} with no exemption")
        elif exemptions[key][0] != missing:
            found.append(f"{key[0]}.{key[1]}: exemption lists {sorted(exemptions[key][0])} but the variable misses {sorted(missing)}")
    for key in sorted(exemptions):
        if key not in known_keys:
            found.append(f"{key[0]}.{key[1]}: exemption for a variable that does not exist")
        elif key not in gaps:
            found.append(f"{key[0]}.{key[1]}: stale exemption, the variable is covered on every surface")
    return found


class VariablesContractTests(unittest.TestCase):
    def test_every_variable_is_covered_or_exempted_and_no_exemption_is_stale(self):
        inv = inventory()
        gaps = surface_gaps(inv, {(f.section, f.key) for f in CATALOG},
                            (ROOT / ".env.example").read_text(),
                            set(settings.LEGACY_ENV.values()), corpus_text())
        self.assertEqual([], problems(gaps, EXEMPTIONS, set(inv)))


class RatchetFixtureTests(unittest.TestCase):
    """The ratchet itself, proven on synthetic variables (not by breaking main)."""

    def test_synthetic_uncovered_variable_is_caught(self):
        inv = {("demo", "zx_uncovered_key"): 1}
        gaps = surface_gaps(inv, set(), "", set(), "")
        self.assertEqual({("demo", "zx_uncovered_key"): frozenset({"env", "webui", "consumer"})}, gaps)
        self.assertTrue(problems(gaps, {}, set(inv)))

    def test_synthetic_covered_variable_passes(self):
        inv = {("demo", "covered_key"): 1}
        gaps = surface_gaps(inv, {("demo", "covered_key")}, "PRINT_HANDY__DEMO__COVERED_KEY=\n",
                            set(), 'value = cfg["covered_key"]')
        self.assertEqual({}, gaps)
        self.assertEqual([], problems(gaps, {}, set(inv)))

    def test_stale_exemption_is_caught(self):
        inv = {("demo", "covered_key"): 1}
        stale = {("demo", "covered_key"): (frozenset({"webui"}), "#657")}
        self.assertTrue(problems({}, stale, set(inv)))


if __name__ == "__main__":
    unittest.main()
