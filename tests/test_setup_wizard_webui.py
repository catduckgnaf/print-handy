"""Wizard Web UI access question: same writers, token guidance, completion."""
import io
from pathlib import Path

from print_handy import setup_state
from print_handy.setup_wizard import Wizard, apply, build_plan
from print_handy.webui import access

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

PASSWORD = "correct horse battery staple"


def _wizard(answers, out=None):
    return Wizard(answers=answers, input_stream=io.StringIO(""),
                  output=out if out is not None else io.StringIO())


def _apply(plan, tmp_path):
    home = tmp_path / "home"
    plan["data_home"] = str(home)
    # The services resolve the data home from the written config, so the
    # plan's config must name the temp home for a test apply.
    plan["config"]["paths"]["state_home"] = str(home)

    class W:
        def __init__(self):
            self.lines = []

        def say(self, text=""):
            self.lines.append(str(text))

        def confirm(self, key, prompt, default=True):
            return False

    class Result:
        returncode = 0

    w = W()
    apply(plan, w, repo=tmp_path, local=tmp_path / "local.yaml",
          runner=lambda cmd: Result())
    return w, home


def test_default_plan_leaves_the_webui_as_it_is():
    plan = build_plan(_wizard(ONE_PRINTER))
    assert plan["webui_access"] == {"choice": "keep"}


def test_password_plan_and_summary_never_show_the_value():
    answers = {**ONE_PRINTER, "webui_access": "password",
               "webui_username": "catduck", "webui_password": PASSWORD,
               "webui_password_confirm": PASSWORD}
    out = io.StringIO()
    plan = build_plan(_wizard(answers, out=out))
    assert plan["webui_access"]["choice"] == "password"
    from print_handy.setup_wizard import summary
    assert PASSWORD not in summary(plan) and PASSWORD not in out.getvalue()


def test_apply_with_password_writes_a_login_and_the_completion_record(tmp_path):
    answers = {**ONE_PRINTER, "webui_access": "password",
               "webui_username": "catduck", "webui_password": PASSWORD,
               "webui_password_confirm": PASSWORD}
    plan = build_plan(_wizard(answers))
    _w, home = _apply(plan, tmp_path)
    data = {"paths": {"state_home": home},
            "webui": {"access_token_env": "WEBUI_ACCESS_TOKEN", "require_auth": True}}
    resolved = access.resolve(data, "0.0.0.0", environ={})
    assert resolved.mode == access.PASSWORD
    assert resolved.matches_login("catduck", PASSWORD)
    record = setup_state.load_completion_in_state_dir(home / "state")
    assert record["done"] is True and record["surface"] == "wizard"


def test_apply_with_open_records_the_saved_choice(tmp_path):
    answers = {**ONE_PRINTER, "webui_access": "open"}
    plan = build_plan(_wizard(answers))
    _w, home = _apply(plan, tmp_path)
    data = {"paths": {"state_home": home},
            "webui": {"access_token_env": "WEBUI_ACCESS_TOKEN", "require_auth": True}}
    saved = access.access_path(data)
    assert saved.exists()
    assert access.resolve(data, "0.0.0.0", environ={}).mode == access.OPEN


def test_apply_with_keep_writes_no_access_file_but_explains_the_token(tmp_path):
    plan = build_plan(_wizard(ONE_PRINTER))
    w, home = _apply(plan, tmp_path)
    data = {"paths": {"state_home": home},
            "webui": {"access_token_env": "WEBUI_ACCESS_TOKEN", "require_auth": True}}
    assert not access.access_path(data).exists()
    assert any("WEBUI_ACCESS_TOKEN" in line for line in w.lines)


def test_a_refused_password_does_not_fail_the_install(tmp_path):
    answers = {**ONE_PRINTER, "webui_access": "password",
               "webui_username": "catduck", "webui_password": "password",
               "webui_password_confirm": "password"}
    plan = build_plan(_wizard(answers))
    w, home = _apply(plan, tmp_path)
    data = {"paths": {"state_home": home},
            "webui": {"access_token_env": "WEBUI_ACCESS_TOKEN", "require_auth": True}}
    assert not access.access_path(data).exists()
    assert any("was not saved" in line or "not saved" in line for line in w.lines)
    # The install itself still completed and recorded completion.
    record = setup_state.load_completion_in_state_dir(home / "state")
    assert record["done"] is True
