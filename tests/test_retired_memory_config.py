"""Old installations must upgrade without retaining the retired memory setting."""
from print_handy import settings


def test_retired_memory_setting_is_discarded_in_files_and_environment(tmp_path):
    local = tmp_path / "local.toml"
    local.write_text('[services]\nopenviking_url = "http://old.invalid"\n')
    data = settings.load(local=local, user=False, environ={
        "PRINT_HANDY__SERVICES__OPENVIKING_URL": "http://old-env.invalid",
        "OPENVIKING_URL": "http://old-legacy.invalid",
    })
    assert "openviking_url" not in data["services"]
