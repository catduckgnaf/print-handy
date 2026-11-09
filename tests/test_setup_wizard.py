"""Setup wizard (#337): the add-a-printer loop and the config round-trip rule.

Driven entirely from --answers dictionaries, the way an unattended install
runs it. The wizard's one-printer output must stay the legacy shape; two
printers must come out as a [[printers]] roster that the real settings
loader resolves; and merge_config must never rewrite a file it cannot
parse and re-emit faithfully — the refusal leaves the file byte-identical.
"""
import io
from pathlib import Path

import pytest
import yaml

from print_handy import settings
from print_handy.bot import routing
from print_handy.setup_wizard import Wizard, apply, build_plan, merge_config


def _wizard(answers):
    return Wizard(answers=answers, input_stream=io.StringIO(""), output=io.StringIO())


ONE_PRINTER = {
    "brand": "flashforge", "model": "flashforge.ad5x",
    "has_printer_software": "yes", "moonraker_url": "http://192.168.1.61:7125",
    "nozzle_diameter_mm": "0.4", "nozzle_type": "hardened_steel", "material": "PLA",
    "slicer": "orca", "install_slicer": "no",
    "spoolman": "existing", "spoolman_url": "http://spoolman.home:7912",
    "camera": "no",
    "have_bot": "yes", "discord_app_id": "", "discord_chat_id": "100000000000000001",
    "discord_user_ids": "200000000000000002", "bot_name": "Print Handy",
    "discord_bot_token": "tok",
    "add_printer_2": "no",
}

TWO_PRINTERS = {
    **ONE_PRINTER,
    "add_printer_2": "yes",
    "printer1_id": "workshop-ad5x", "printer1_chat_id": "",
    "p2_brand": "bambulab", "p2_model": "bambulab.a1",
    "p2_bambu_lan": "yes", "p2_lan_host": "192.168.1.60", "p2_lan_serial": "00M00A000000000",
    "p2_bambu_lan_password": "code2", "p2_nozzle_type": "stainless_steel",
    "p2_id": "garage-a1", "p2_chat_id": "100000000000000003", "p2_camera": "no",
    "add_printer_3": "no",
}


def test_one_printer_plan_keeps_the_legacy_shape():
    plan = build_plan(_wizard(ONE_PRINTER))
    assert plan["printers"] == []
    config = plan["config"]
    assert "printers" not in config
    assert config["printer"]["model"] == "flashforge.ad5x"
    assert config["services"]["moonraker_url"] == "http://192.168.1.61:7125"


def test_two_printer_plan_writes_a_resolvable_roster(tmp_path):
    plan = build_plan(_wizard(TWO_PRINTERS))
    config = plan["config"]
    assert "printer" not in config and "bambu" not in config
    first, second = config["printers"]
    assert first == {"id": "workshop-ad5x", "name": "Flashforge AD5X (ZMod)",
                     "model": "flashforge.ad5x",
                     "moonraker_url": "http://192.168.1.61:7125",
                     "nozzle_type": "hardened_steel"}
    assert second["id"] == "garage-a1" and second["chat_id"] == "100000000000000003"
    assert second["lan_access_code_env"] == "BAMBU_LAN_PASSWORD_GARAGE_A1"
    # Spoolman stays the one global service; per-printer secrets are per-printer.
    assert config["services"] == {"spoolman_url": "http://spoolman.home:7912"}
    assert plan["secrets"]["BAMBU_LAN_PASSWORD_GARAGE_A1"] == "code2"
    assert "BAMBU_LAN_PASSWORD" not in plan["secrets"]
    assert [row["id"] for row in plan["printers"]] == ["workshop-ad5x", "garage-a1"]

    local = tmp_path / "local.yaml"
    assert merge_config(local, config, _wizard({}))
    data = settings.load(local=local, environ={})
    assert [p["id"] for p in settings.printers(data)] == ["workshop-ad5x", "garage-a1"]
    assert settings.default_printer_id(data) == "workshop-ad5x"
    assert routing.channel_map(data, "discord") == {
        "100000000000000001": "workshop-ad5x", "100000000000000003": "garage-a1"}
    assert routing.printer_for_chat(data, "discord", "100000000000000001") == "workshop-ad5x"


def test_duplicate_printer_id_is_rejected_by_the_answers_gate():
    bad = {**TWO_PRINTERS, "p2_id": "workshop-ad5x"}
    with pytest.raises(SystemExit):  # Wizard.ask fails the run on an invalid recorded answer
        build_plan(_wizard(bad))


GENERIC_SECOND = {
    **ONE_PRINTER,
    "add_printer_2": "yes",
    "printer1_id": "workshop-ad5x", "printer1_chat_id": "",
    "p2_brand": "other", "p2_firmware": "klipper", "p2_printer_name": "Bench Klipper",
    "p2_bed_x": "100", "p2_bed_y": "120", "p2_bed_z": "130",
    "p2_has_printer_software": "yes", "p2_moonraker_url": "http://192.168.1.62:7125",
    "p2_nozzle_type": "brass",
    "p2_id": "bench-klipper", "p2_chat_id": "100000000000000004", "p2_camera": "no",
    "add_printer_3": "no",
}


def test_generic_second_printer_keeps_its_custom_build_volume(tmp_path):
    # P1-83: the Other/generic questions collect a custom bed and height;
    # the roster entry must carry them, or the settings view silently
    # substitutes the generic model's 220x220x220 defaults.
    plan = build_plan(_wizard(GENERIC_SECOND))
    first, second = plan["config"]["printers"]
    assert second["bed_mm"] == [100.0, 120.0]
    assert second["max_print_height_mm"] == 130.0
    assert "bed_mm" not in first  # a known model keeps its own defaults

    local = tmp_path / "local.yaml"
    assert merge_config(local, plan["config"], _wizard({}))
    data = settings.load(local=local, environ={})
    view = settings.view_for("bench-klipper", data)
    assert view["printer"]["bed_mm"] == [100.0, 120.0]
    assert view["printer"]["max_print_height_mm"] == 130.0


def test_env_colliding_printer_ids_are_rejected_by_the_answers_gate():
    # P2-138: garage-a1 and garage_a1 are distinct ids but generate the
    # same per-printer secret names (BAMBU_LAN_PASSWORD_GARAGE_A1), so the
    # later printer's secret would silently overwrite the earlier one's.
    bad = {**TWO_PRINTERS, "printer1_id": "garage-a1", "p2_id": "garage_a1"}
    with pytest.raises(SystemExit):
        build_plan(_wizard(bad))


def test_merge_refuses_to_add_a_roster_to_a_legacy_config(tmp_path):
    # P1-84: merging a [[printers]] plan into an existing single-printer
    # file would keep its legacy [printer] model, a shape the settings
    # loader refuses at startup — the wizard must refuse first and leave
    # the working file intact.
    target = tmp_path / "local.yaml"
    content = ("printer:\n  model: flashforge.ad5x\n"
               "services:\n  moonraker_url: http://192.168.1.61:7125\n")
    target.write_text(content)
    roster = {"printers": [{"id": "workshop-ad5x", "model": "flashforge.ad5x",
                            "moonraker_url": "http://192.168.1.61:7125"}]}
    out = io.StringIO()
    w = Wizard(answers={"merge_config": "yes"}, input_stream=io.StringIO(""), output=out)
    assert merge_config(target, roster, w) is False
    assert target.read_text() == content
    assert "left unchanged" in out.getvalue()


def test_two_printers_may_not_both_ride_the_main_channel():
    # The first printer falls back to the main channel, so the second one must
    # claim its own — two entries claiming one channel are refused at load.
    bad = {**TWO_PRINTERS, "p2_chat_id": ""}
    with pytest.raises(SystemExit):
        build_plan(_wizard(bad))


# ---------------------------------------------------------------------- the round-trip rule

def test_merge_writes_a_fresh_file(tmp_path):
    target = tmp_path / "local.yaml"
    assert merge_config(target, {"bot": {"mode": "standalone"}}, _wizard({}))
    assert yaml.safe_load(target.read_text()) == {"bot": {"mode": "standalone"}}


def test_merge_keeps_existing_values_and_adds_new_ones(tmp_path):
    target = tmp_path / "local.yaml"
    target.write_text("discord:\n  chat_id: '9'\n")
    assert merge_config(target, {"bot": {"mode": "standalone"}, "discord": {"chat_id": "1"}},
                        _wizard({"merge_config": "yes"}))  # the merge consent defaults to no
    merged = yaml.safe_load(target.read_text())
    assert merged["discord"]["chat_id"] == "9"  # existing keys win
    assert merged["bot"]["mode"] == "standalone"


# ------------------------------------------------- secrets land in the effective data folder

class _OkResult:
    returncode = 0


def _apply_standalone(tmp_path, monkeypatch, local):
    """Run apply() for a standalone plan with every external command stubbed out."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    plan = build_plan(_wizard(ONE_PRINTER))
    w = _wizard({"merge_config": "yes", "run_linger": "no", "run_start": "no"})
    apply(plan, w, repo=tmp_path, local=local, runner=lambda cmd: _OkResult())
    return plan


def test_secrets_go_to_a_preserved_custom_data_folder(tmp_path, monkeypatch):
    # An existing config pins paths.hermes_home elsewhere; the merge preserves it
    # (existing keys win) and install-standalone.sh points the services' env file
    # at that folder — so the token must be written there, not to the proposed
    # default (P2-60).
    custom = tmp_path / "elsewhere"
    local = tmp_path / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {custom}\n")
    plan = _apply_standalone(tmp_path, monkeypatch, local)
    assert plan["data_home"] != str(custom)  # the wizard proposed its own default
    env = (custom / ".env").read_text()
    assert "DISCORD_BOT_TOKEN=tok" in env
    assert not (tmp_path / "home" / ".local" / "share" / "print-handy" / ".env").exists()


def test_secrets_go_to_a_custom_data_folder_even_when_the_merge_is_declined(tmp_path, monkeypatch):
    # Declining the merge leaves the existing config in force, so its
    # paths.hermes_home is still the effective folder for the services.
    custom = tmp_path / "elsewhere"
    local = tmp_path / "local.yaml"
    local.write_text(f"paths:\n  hermes_home: {custom}\n")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    plan = build_plan(_wizard(ONE_PRINTER))
    w = _wizard({"merge_config": "no", "run_linger": "no", "run_start": "no"})
    apply(plan, w, repo=tmp_path, local=local, runner=lambda cmd: _OkResult())
    assert "DISCORD_BOT_TOKEN=tok" in (custom / ".env").read_text()


def test_secrets_use_the_proposed_folder_when_the_config_sets_none(tmp_path, monkeypatch):
    local = tmp_path / "local.yaml"  # fresh install: the merge writes the plan's config
    _apply_standalone(tmp_path, monkeypatch, local)
    env = tmp_path / "home" / ".local" / "share" / "print-handy" / ".env"
    assert "DISCORD_BOT_TOKEN=tok" in env.read_text()


@pytest.mark.parametrize("content", [
    "printer: [unclosed\n",                                   # does not parse
    "printer:\n  model: flashforge.ad5x\n  model: bambulab.a1\n",  # duplicate key
    "- just\n- a\n- list\n",                                   # parses, but not a mapping
])
def test_merge_refuses_a_file_it_cannot_round_trip(tmp_path, content):
    target = tmp_path / "local.yaml"
    target.write_text(content)
    out = io.StringIO()
    w = Wizard(answers={}, input_stream=io.StringIO(""), output=out)
    assert merge_config(target, {"bot": {"mode": "standalone"}}, w) is False
    assert target.read_text() == content  # byte-for-byte unchanged
    assert "left unchanged" in out.getvalue()
