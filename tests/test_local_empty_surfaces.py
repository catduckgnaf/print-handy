"""Stage 1 surfaces (#455 item 4): the ``vision.local_empty_enabled``
setting everywhere a vision setting lives, and bot check's plate
reference row. Mirrors the watch_source treatment from PR #1034: schema
+ validation, .env.example, compose, config examples, the in-repo Unraid
template, the Web UI catalog, docs/SETTINGS.md, and the vendor-profile
golden strip. The catduck-unraid-templates repo is deliberately not
synced (routine-variable rule).
"""
from pathlib import Path

import pytest

from print_handy import settings
from print_handy.bot import __main__ as bot_main
from print_handy.hermes import local_empty as le

ROOT = Path(__file__).parents[1]


def _load(tmp_path, text):
    # home=None loads the repo's own shipped config/home.yaml as the
    # defaults base (the load() contract); the file under test rides
    # as the operator layer, where [vision] keys are validated.
    path = tmp_path / "home.toml"
    path.write_text(text, encoding="utf-8")
    return settings.load(local=path, environ={})


def test_default_is_off(tmp_path):
    data = _load(tmp_path, "")
    assert data["vision"]["local_empty_enabled"] is False


def test_env_form_loads(tmp_path):
    data = settings.load(
        home=None, local=None,
        environ={"PRINT_HANDY__PRINTER__MODEL": "klipper.generic",
                 "PRINT_HANDY__VISION__LOCAL_EMPTY_ENABLED": "true"})
    assert data["vision"]["local_empty_enabled"] is True


@pytest.mark.parametrize("bad", ["yes", "1", 1, 0])
def test_non_bool_is_refused(tmp_path, bad):
    with pytest.raises(settings.SettingsError, match="local_empty_enabled"):
        _load(tmp_path, f'[vision]\nlocal_empty_enabled = {bad!r}\n'.replace("'", '"'))


def test_env_example_carries_the_key():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "PRINT_HANDY__VISION__LOCAL_EMPTY_ENABLED" in text


def test_compose_carries_the_key():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "PRINT_HANDY__VISION__LOCAL_EMPTY_ENABLED" in text


def test_config_examples_carry_the_key():
    for name in ("config/home.yaml", "config/home.toml"):
        assert "local_empty_enabled" in (ROOT / name).read_text(encoding="utf-8"), name


def test_unraid_template_carries_the_key():
    text = (ROOT / "docker/unraid/print-handy.xml").read_text(encoding="utf-8")
    assert "PRINT_HANDY__VISION__LOCAL_EMPTY_ENABLED" in text


def test_settings_doc_carries_the_row():
    text = (ROOT / "docs/SETTINGS.md").read_text(encoding="utf-8")
    assert "`vision.local_empty_enabled`" in text
    assert "plate reference" in text


def test_webui_catalog_carries_the_field():
    from print_handy.webui import catalog
    text = Path(catalog.__file__).read_text(encoding="utf-8")
    assert '"vision.local_empty_enabled": "camera"' in text
    assert '_field("vision", "local_empty_enabled"' in text


# ------------------------------------------------------------- bot check

@pytest.fixture()
def cfg(tmp_path):
    from print_handy import settings as _settings
    data = _settings.load(
        home=None, local=None,
        environ={"PRINT_HANDY__PRINTER__MODEL": "klipper.generic"})
    data["paths"]["hermes_home"] = str(tmp_path / "hermes")
    return data


def _rows(cfg, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x" * 20)
    return {label: (ok, detail) for label, ok, detail in bot_main.checks(cfg)}


def test_bot_check_row_off_by_default(cfg, monkeypatch):
    ok, detail = _rows(cfg, monkeypatch)["plate reference"]
    assert ok is True
    assert detail.startswith("off: vision.local_empty_enabled is false")


def test_bot_check_row_on_without_reference_names_calibration(cfg, monkeypatch, tmp_path):
    cfg["vision"]["local_empty_enabled"] = True
    cfg["paths"]["hermes_home"] = str(tmp_path)
    ok, detail = _rows(cfg, monkeypatch)["plate reference"]
    assert ok is True
    assert "no reference" in detail
    assert "/print-plate-calibrate capture" in detail


def test_bot_check_row_ready_shows_the_reference_age(cfg, monkeypatch, tmp_path):
    cfg["vision"]["local_empty_enabled"] = True
    cfg["paths"]["hermes_home"] = str(tmp_path)
    le.begin_calibration(cfg, _frame())
    assert le.confirm_calibration(cfg, _frame())
    ok, detail = _rows(cfg, monkeypatch)["plate reference"]
    assert ok is True
    assert "reference captured" in detail and "age" in detail


def test_auto_light_row_names_the_local_reference_route(cfg, monkeypatch, tmp_path):
    """With the reference producer on and calibrated, the auto light row
    no longer claims an endpoint is required."""
    cfg["vision"].update(auto_light_off_minutes=30, light_object="led chamber_led",
                         local_empty_enabled=True)
    cfg["paths"]["hermes_home"] = str(tmp_path)
    cfg["services"]["vision_chat_url"] = ""
    monkeypatch.delenv("PRINT_VISION_URL", raising=False)
    le.begin_calibration(cfg, _frame())
    assert le.confirm_calibration(cfg, _frame())
    ok, detail = _rows(cfg, monkeypatch)["auto light"]
    assert ok is True
    assert "local plate reference" in detail


def _frame():
    import io
    from PIL import Image, ImageDraw
    image = Image.new("RGB", (320, 240), (16, 16, 18))
    ImageDraw.Draw(image).rectangle([20, 30, 300, 225], fill=(120, 122, 125))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return out.getvalue()
