"""Renamed-key reader completeness (testing-hardening pass, E6).

The spool handoff's ``ifs_slots`` lesson: a state key was renamed
(``ifs_slots`` -> ``filament_slots``, #672), the sanctioned readers
adopted the old spelling, and any RAW reader written later could
silently read the old key (getting nothing from current records) or
reinvent the adoption in a fourth place. This test makes the set of
sanctioned old-key touches a registry: an AST scan of the production
tree finds every string literal naming a renamed key, and each
occurrence must live in a registered file. A new raw reader anywhere
else fails here until it is registered — with a reason — or, better,
routed through the sanctioned adoption path.

Docstrings and comments are prose, not readers: docstring constants
are excluded from the scan (a comment never reaches the AST at all).
"""
import ast
from pathlib import Path

from print_handy import settings
from print_handy.core import filament_color

ROOT = Path(__file__).parents[1]

# old key -> {repo-relative file: why the touch is sanctioned there}
SANCTIONED = {
    "ifs_slots": {
        "print_handy/settings.py":
            "KEY_ALIASES table + the one sanctioned fallback reader in "
            "the model-default warning (record.get(new, record.get(old)))",
        "print_handy/core/filament_color.py":
            "adopt_renamed_slots_key(): the single adoption function both "
            "state loaders call right after parsing",
    },
    "secure_cookie": {
        "print_handy/settings.py": "KEY_ALIASES table (config-layer alias only)",
    },
    "mention_starter_on_complete": {
        "print_handy/settings.py": "KEY_ALIASES table (config-layer alias only)",
    },
}


def _string_constants(tree):
    """String constants in a module, excluding docstrings."""
    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef,
                             ast.AsyncFunctionDef, ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                docstring_nodes.add(id(node.body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstring_nodes]


def test_every_renamed_key_touch_is_registered():
    old_keys = {old for (_sec, old) in settings.KEY_ALIASES}
    assert old_keys == set(SANCTIONED), (
        "settings.KEY_ALIASES and this registry disagree on the renamed "
        "keys — register the new rename here before touching its key")
    offenders = []
    for path in sorted((ROOT / "print_handy").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        constants = _string_constants(ast.parse(path.read_text(encoding="utf-8")))
        for key in old_keys:
            if key in constants and rel not in SANCTIONED[key]:
                offenders.append(f"{rel} touches renamed key {key!r}")
    assert offenders == [], (
        "unregistered raw readers of renamed keys (route them through "
        "the sanctioned adoption path or register them here with a "
        "reason): " + "; ".join(offenders))


def test_registry_entries_point_at_real_files():
    for key, files in SANCTIONED.items():
        for rel, why in files.items():
            assert (ROOT / rel).is_file(), (key, rel)
            assert len(why) >= 20, (key, rel, "a reason is required")


def test_adoption_prefers_the_current_name_and_keeps_legacy_records():
    state = {"ifs_slots": {"0": {"spoolman_id": 7}}}
    filament_color.adopt_renamed_slots_key(state)
    assert state == {"filament_slots": {"0": {"spoolman_id": 7}}}

    both = {"ifs_slots": {"0": {"spoolman_id": 7}},
            "filament_slots": {"1": {"spoolman_id": 9}}}
    filament_color.adopt_renamed_slots_key(both)
    assert both == {"filament_slots": {"1": {"spoolman_id": 9}}}

    current_only = {"filament_slots": {"1": {"spoolman_id": 9}}}
    filament_color.adopt_renamed_slots_key(current_only)
    assert current_only == {"filament_slots": {"1": {"spoolman_id": 9}}}


def test_settings_aliases_fold_the_old_spelling_into_the_current_name():
    layer = {"printer": {"ifs_slots": {"0": {}}}}
    settings._apply_key_aliases(layer)
    assert layer == {"printer": {"filament_slots": {"0": {}}}}
    both = {"printer": {"ifs_slots": {"old": {}}, "filament_slots": {"new": {}}}}
    settings._apply_key_aliases(both)
    assert both == {"printer": {"filament_slots": {"new": {}}}}
