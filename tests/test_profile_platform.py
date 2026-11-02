"""#384: reading the gateway platform switch in the Hermes profile config.

The incident: platforms.discord.enabled was false in the profile config, so
the Hermes gateway never connected Discord and attachment intake was silently
absent — credentials do not override an explicit platform disable, and the
gateway only logged a warning. Print Handy never writes this file; these
tests pin the reader the check surfaces (bot check, doctor) rely on.
"""
import copy

import pytest

from print_handy.hermes import _config


@pytest.fixture
def profile(tmp_path):
    previous = copy.deepcopy(_config.cfg())
    data = copy.deepcopy(previous)
    data["paths"]["hermes_home"] = tmp_path
    _config.set_override(data)
    yield tmp_path
    _config.set_override(previous)


def _write(profile, text):
    (profile / "config.yaml").write_text(text)


def test_missing_profile_config(profile):
    state, path = _config.profile_platform_state("discord")
    assert state == "missing"
    assert path == profile / "config.yaml"


def test_enabled_true(profile):
    _write(profile, "platforms:\n  discord:\n    enabled: true\n")
    assert _config.profile_platform_state("discord")[0] == "enabled"


def test_explicit_false_is_disabled(profile):
    _write(profile, "platforms:\n  discord:\n    enabled: false\n")
    assert _config.profile_platform_state("discord")[0] == "disabled"


def test_quoted_false_is_disabled(profile):
    _write(profile, 'platforms:\n  discord:\n    enabled: "false"\n')
    assert _config.profile_platform_state("discord")[0] == "disabled"


def test_no_platforms_section_is_unset(profile):
    _write(profile, "model:\n  default: test\n")
    assert _config.profile_platform_state("discord")[0] == "unset"


def test_platform_without_enabled_key_is_unset(profile):
    _write(profile, "platforms:\n  discord:\n    gateway_restart_notification: false\n")
    assert _config.profile_platform_state("discord")[0] == "unset"


def test_unparseable_config_is_unreadable(profile):
    _write(profile, "platforms: [unclosed\n")
    assert _config.profile_platform_state("discord")[0] == "unreadable"


def test_non_mapping_config_is_unreadable(profile):
    _write(profile, "- just\n- a\n- list\n")
    assert _config.profile_platform_state("discord")[0] == "unreadable"


def test_shipped_template_enables_the_platform():
    """Root-cause guard: the template the installer renders can only ever
    create the profile with the Discord platform on."""
    import yaml
    from pathlib import Path
    text = (Path(__file__).parents[1] / "hermes" / "profile" / "config.yaml.template").read_text()
    data = yaml.safe_load(text)
    assert data["platforms"]["discord"]["enabled"] is True


# --- #391 expansion: reader branches #389's first tests did not pin ---


def test_quoted_no_and_off_are_disabled(profile):
    _write(profile, 'platforms:\n  discord:\n    enabled: "no"\n')
    assert _config.profile_platform_state("discord")[0] == "disabled"
    _write(profile, 'platforms:\n  discord:\n    enabled: "off"\n')
    assert _config.profile_platform_state("discord")[0] == "disabled"


def test_false_string_matching_ignores_case_and_whitespace(profile):
    _write(profile, 'platforms:\n  discord:\n    enabled: " False "\n')
    assert _config.profile_platform_state("discord")[0] == "disabled"


def test_other_platforms_switch_does_not_leak_into_the_discord_read(profile):
    _write(profile, "platforms:\n  telegram:\n    enabled: false\n  discord:\n    enabled: true\n")
    assert _config.profile_platform_state("discord")[0] == "enabled"
    assert _config.profile_platform_state("telegram")[0] == "disabled"


def test_non_mapping_platforms_section_is_unset(profile):
    _write(profile, "platforms: [discord]\n")
    assert _config.profile_platform_state("discord")[0] == "unset"


def test_non_mapping_platform_entry_is_unset(profile):
    _write(profile, "platforms:\n  discord: yes\n")
    assert _config.profile_platform_state("discord")[0] == "unset"


def test_empty_profile_config_is_unreadable(profile):
    """An empty file parses to None, not a mapping: the switch cannot be
    verified, so the reader must not report it as enabled or unset."""
    _write(profile, "")
    assert _config.profile_platform_state("discord")[0] == "unreadable"


def test_comment_only_profile_config_is_unreadable(profile):
    _write(profile, "# rendered by the installer\n")
    assert _config.profile_platform_state("discord")[0] == "unreadable"


# --- P2-149: PyYAML absent (ruamel-only Hermes host) ---


def test_platform_state_reads_without_pyyaml(profile, monkeypatch):
    """The Hermes plugin runtime ships ruamel.yaml instead of PyYAML
    (#170); the profile read must fall back the way settings does
    instead of raising ImportError out of the check."""
    monkeypatch.setitem(__import__("sys").modules, "yaml", None)
    _write(profile, "platforms:\n  discord:\n    enabled: false\n")
    assert _config.profile_platform_state("discord")[0] == "disabled"


def test_platform_state_unreadable_when_no_yaml_library_at_all(profile, monkeypatch):
    """With neither YAML library reachable the switch cannot be
    verified: the state is 'unreadable', never an exception escaping
    into the doctor / bot check."""
    from print_handy import settings

    monkeypatch.setitem(__import__("sys").modules, "yaml", None)
    monkeypatch.setattr(settings, "_yaml_load", None)
    _write(profile, "platforms:\n  discord:\n    enabled: false\n")
    assert _config.profile_platform_state("discord")[0] == "unreadable"
