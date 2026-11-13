"""The 2.3.x compatibility shims are removed in 2.4.0, and this file makes sure of it (#1400).

docs/PROJECT-REFERENCE.md, "Scheduled removals in 2.4.0", lists each one.
Before 2.4.0 the shims must exist and name their removal release; once the
package version reaches 2.4.0, any shim still present fails the suite, so
the removal cannot be forgotten.
"""
import re
from pathlib import Path

import print_handy

ROOT = Path(__file__).resolve().parents[1]
REMOVAL = (2, 4, 0)
DOC = ROOT / "docs" / "PROJECT-REFERENCE.md"


def _version(text):
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


def _still_present():
    """Each scheduled removal that is still in the tree, by a short name."""
    found = []
    if (ROOT / "print_handy" / "hermes").exists():
        found.append("print_handy/hermes (the old package name)")
    if (ROOT / "print_handy" / "_relocated.py").exists():
        found.append("print_handy/_relocated.py (old module paths)")
    if (ROOT / "plugins" / "print-feedback" / "__init__.py").exists():
        found.append("plugins/print-feedback (the handler plugin loaded by path)")
    if "plugins/print-feedback" in (ROOT / "MANIFEST.in").read_text(encoding="utf-8"):
        found.append("MANIFEST.in line for plugins/print-feedback")
    settings_text = (ROOT / "print_handy" / "settings.py").read_text(encoding="utf-8")
    if '("paths", "hermes_home")' in settings_text:
        found.append("paths.hermes_home key alias in print_handy/settings.py")
    config_text = (ROOT / "print_handy" / "runtime" / "_config.py").read_text(encoding="utf-8")
    if re.search(r"^hermes_home\s*=", config_text, re.M):
        found.append("hermes_home helpers in print_handy/runtime/_config.py")
    return found


def _removal_due(version, present):
    """The failure message when the release is due and a shim remains, else None."""
    if version < REMOVAL or not present:
        return None
    return ("version %s reached %s, so these compatibility shims must be removed "
            "(docs/PROJECT-REFERENCE.md, Scheduled removals in 2.4.0): %s"
            % (".".join(map(str, version)), ".".join(map(str, REMOVAL)), "; ".join(present)))


def test_no_scheduled_shim_survives_its_removal_release():
    message = _removal_due(_version(print_handy.__version__), _still_present())
    assert message is None, message


def test_the_guard_fires_at_the_removal_release():
    assert _removal_due((2, 4, 0), ["x"]) is not None
    assert _removal_due((3, 0, 0), ["x"]) is not None
    assert _removal_due((2, 3, 9), ["x"]) is None
    assert _removal_due((2, 4, 0), []) is None


def test_each_present_shim_names_its_removal_release():
    if _version(print_handy.__version__) >= REMOVAL:
        return  # the guard above owns this case
    for path in ("print_handy/hermes/__init__.py", "print_handy/_relocated.py",
                 "plugins/print-feedback/__init__.py", "MANIFEST.in", "print_handy/settings.py",
                 "print_handy/runtime/_config.py"):
        text = (ROOT / path).read_text(encoding="utf-8")
        assert "2.4.0" in text, f"{path} does not name its removal release (2.4.0)"
        assert "after 2.3.x" not in text, f"{path} still says 'after 2.3.x'; name 2.4.0"


def test_the_docs_list_every_scheduled_removal():
    text = DOC.read_text(encoding="utf-8")
    section = text.split("## Scheduled removals in 2.4.0", 1)[1].split("\n## ", 1)[0]
    for name in ("print_handy/hermes/", "print_handy/_relocated.py", "plugins/print-feedback/__init__.py",
                 "MANIFEST.in", "paths.hermes_home", "tests/test_scheduled_removals.py"):
        assert name in section, f"Scheduled removals in 2.4.0 does not list {name}"
