"""#574, after the Hermes removal: the hosting identity is a constant.

It used to be derived from the Hermes install layout (a profiles-layout
home, or the plugin's install symlink). With that install gone,
_config.hosting_profile always returns the fixed DEFAULT_PROFILE the
standalone runtime stamps on its sources, never derived — whatever home,
mode or plugin location it is handed — and the plugin's gates answer
exactly that stamp.
"""
from pathlib import Path

from print_handy.runtime import _config


def load_plugin():
    from print_handy.bot.handlers import common
    return common


def test_hosting_profile_is_always_the_default(tmp_path):
    home = tmp_path / ".hermes" / "profiles" / "printshop"
    assert _config.hosting_profile() == (_config.DEFAULT_PROFILE, False)
    assert _config.hosting_profile(Path(__file__), home=home, mode="hermes") == (
        _config.DEFAULT_PROFILE, False)
    assert _config.hosting_profile(home=tmp_path / "data", mode="standalone") == (
        _config.DEFAULT_PROFILE, False)


def test_plugin_gates_answer_only_the_runtime_stamp():
    plugin = load_plugin()
    assert plugin._hosting_profile() == _config.DEFAULT_PROFILE
    assert plugin._is_hosting_profile(_config.DEFAULT_PROFILE)
    assert not plugin._is_hosting_profile("printshop")
    assert not plugin._is_hosting_profile("")
    assert not plugin._is_hosting_profile(None)
