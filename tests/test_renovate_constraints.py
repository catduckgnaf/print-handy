"""Renovate keeps constraints.txt moving (#1589).

CI installs with ``-c constraints.txt``. Renovate's pip_requirements
manager only finds ``requirements*.txt`` on its own, so the config must
name constraints.txt or the pins freeze, security fixes included.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _config():
    return json.loads((ROOT / ".github" / "renovate.json").read_text())


def _patterns(config):
    out = []
    for raw in config.get("pip_requirements", {}).get("managerFilePatterns", []):
        assert raw.startswith("/") and raw.endswith("/"), f"not a regex pattern: {raw}"
        out.append(re.compile(raw[1:-1]))
    return out


def test_constraints_file_is_matched_by_the_pip_requirements_manager():
    assert (ROOT / "constraints.txt").is_file()
    patterns = _patterns(_config())
    assert any(p.search("constraints.txt") for p in patterns)


def test_constraints_pins_are_grouped_with_the_pyproject_ranges():
    rules = _config()["packageRules"]
    pip_group = [r for r in rules if r.get("groupName") == "pip requirements"]
    assert len(pip_group) == 1
    assert {"pep508", "pip_requirements"} <= set(pip_group[0]["matchManagers"])
