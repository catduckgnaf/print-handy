"""A TOML rendering of the canonical config/home.yaml, for tests of the legacy TOML loader path.

config/home.toml was retired in #1322: home.yaml always ships and always wins, so no tree
ever read the TOML twin, and keeping it meant editing two files for every new key. The
loader still accepts a TOML home (settings.LEGACY_HOME, used when home.yaml is absent), so
the tests that pin that path render one from home.yaml here instead of reading a twin.

home.yaml holds only tables, scalars and lists of scalars; dumps() refuses anything else
rather than writing TOML that would parse to different data.
"""
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HOME_YAML = ROOT / "config" / "home.yaml"
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(name):
    name = str(name)
    return name if _BARE_KEY.match(name) else json.dumps(name)


def _value(value, where):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_value(item, where) for item in value) + "]"
    raise TypeError(f"{where}: {type(value).__name__} has no TOML form here")


def dumps(data):
    """TOML text that tomllib.loads() turns back into exactly data."""
    lines = []

    def table(mapping, path):
        scalars = {k: v for k, v in mapping.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in mapping.items() if isinstance(v, dict)}
        if path and (scalars or not tables):
            lines.append("[" + ".".join(_key(p) for p in path) + "]")
        for key, value in scalars.items():
            lines.append(f"{_key(key)} = {_value(value, '.'.join(map(str, path + [key])))}")
        if scalars:
            lines.append("")
        for key, value in tables.items():
            table(value, path + [key])

    table(data, [])
    return "\n".join(lines) + "\n"


def home_data():
    """The canonical home defaults, as data a test may edit before rendering."""
    return yaml.safe_load(HOME_YAML.read_text(encoding="utf-8"))


def home_toml(data=None):
    """config/home.yaml (or an edited copy of its data) as TOML text."""
    return dumps(home_data() if data is None else data)


def write_home_toml(path, data=None):
    path = Path(path)
    path.write_text(home_toml(data), encoding="utf-8")
    return path
