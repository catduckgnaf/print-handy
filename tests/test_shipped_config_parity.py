"""The two shipped Docker configs resolve to the same settings (#1400, M1).

config/docker.yaml and config/docker.toml are the same configuration in two
formats; an install picks either. Comparing the files' text cannot catch a
default one of them leaves out or a key the loader reads differently, so both
go through the real settings loader with an empty environment and must give
identical settings.
"""
from pathlib import Path

from print_handy import settings

ROOT = Path(__file__).resolve().parents[1]


def _differences(left, right, prefix=""):
    if isinstance(left, dict) and isinstance(right, dict):
        out = []
        for key in sorted(set(left) | set(right), key=str):
            where = f"{prefix}.{key}" if prefix else str(key)
            if key not in left or key not in right:
                out.append(f"{where}: only in {'toml' if key in right else 'yaml'}")
            else:
                out += _differences(left[key], right[key], where)
        return out
    return [] if left == right else [f"{prefix}: yaml={left!r} toml={right!r}"]


def test_the_yaml_and_toml_docker_configs_load_to_the_same_settings():
    from_yaml = settings.load(local=ROOT / "config" / "docker.yaml", environ={})
    from_toml = settings.load(local=ROOT / "config" / "docker.toml", environ={})
    assert _differences(from_yaml, from_toml) == []


def test_the_comparison_sees_a_difference():
    """Guard against a vacuous pass: a changed value is reported."""
    data = settings.load(local=ROOT / "config" / "docker.yaml", environ={})
    changed = settings.load(local=ROOT / "config" / "docker.yaml",
                            environ={"PRINT_HANDY__VISION__AI_ENABLED": "true"})
    assert _differences(data, changed) == ["vision.ai_enabled: yaml=False toml=True"]
