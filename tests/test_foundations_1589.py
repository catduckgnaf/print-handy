"""Contract tests for the #1589 foundations change.

The widened lint gate, the non-blocking pyright job, the pinned
constraints file, the explicit pytest configuration and the Linux-only
statement are all configuration surfaces: a later edit could quietly
revert any one of them (a narrower ruff selection, a blocking pyright
job, a CI install without constraints) without any behavior test
noticing. These tests pin each surface at the file that declares it.
"""

import pathlib
import tomllib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_pyproject():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def load_ci():
    return yaml.load((ROOT / ".github" / "workflows" / "ci.yml").read_text(), Loader=yaml.BaseLoader)


class RuffGateTests(unittest.TestCase):
    def test_widened_rule_sets_are_selected(self):
        select = load_pyproject()["tool"]["ruff"]["lint"]["select"]
        for code in ("E9", "F63", "F7", "F82", "F", "B", "ASYNC", "PLE"):
            self.assertIn(code, select)

    def test_baseline_is_per_file_and_names_rules(self):
        ignores = load_pyproject()["tool"]["ruff"]["lint"]["per-file-ignores"]
        self.assertIn("plugins/print-feedback/__init__.py", ignores)
        self.assertIn("F821", ignores["plugins/print-feedback/__init__.py"])
        for filename, codes in ignores.items():
            self.assertTrue(codes, filename)
            for code in codes:
                self.assertRegex(code, r"^(E9|F|B|ASYNC|PLE)")


class ListenerAttributeTests(unittest.TestCase):
    def test_lazily_created_attributes_are_declared_in_init(self):
        from print_handy.runtime.moonraker_event_listener import Listener

        listener = Listener()
        self.assertEqual(listener.visual_timeouts, 0)
        self.assertIsNone(listener._next_plate_task)
        self.assertFalse(listener._watch_context_seen)
        self.assertIsNone(listener.oc_recovery_identity)
        self.assertIsNone(listener.oc_prompt)
        self.assertFalse(listener.moonraker_connected)
        self.assertIsNone(listener.print_seen_started_at)
        self.assertIsNone(listener.shutdown_alerted)
        self.assertIsNotNone(listener._loop_tick)
        self.assertIsNone(listener._heartbeat_thread)


class PytestConfigTests(unittest.TestCase):
    def test_strict_markers_and_explicit_asyncio_mode(self):
        options = load_pyproject()["tool"]["pytest"]["ini_options"]
        self.assertIn("--strict-markers", options["addopts"])
        self.assertEqual(options["asyncio_mode"], "strict")

    def test_slow_marker_is_registered(self):
        options = load_pyproject()["tool"]["pytest"]["ini_options"]
        self.assertTrue(any(str(marker).startswith("slow:") for marker in options["markers"]))


class ConstraintsTests(unittest.TestCase):
    def test_constraints_pin_the_direct_dependencies(self):
        text = (ROOT / "constraints.txt").read_text()
        for package in ("discord-py==", "websockets==", "pytest==", "ruff=="):
            self.assertIn(package, text)

    def test_ci_installs_with_constraints(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn('python -m pip install -c constraints.txt ".[test]"', workflow)


class PyrightJobTests(unittest.TestCase):
    def test_pyright_job_is_non_blocking_and_scoped_to_runtime(self):
        job = load_ci()["jobs"]["pyright"]
        self.assertEqual(job["continue-on-error"], "true")
        runs = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("pyright print_handy/runtime", runs)

    def test_pyright_config_is_basic_over_runtime(self):
        config = load_pyproject()["tool"]["pyright"]
        self.assertEqual(config["typeCheckingMode"], "basic")
        self.assertEqual(config["include"], ["print_handy/runtime"])


class LinuxOnlyStatementTests(unittest.TestCase):
    def test_ci_and_contributing_state_the_suite_is_linux_only(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertIn("the test suite is Linux only", workflow)
        contributing = (ROOT / "CONTRIBUTING.md").read_text()
        self.assertIn("The test suite is Linux only", contributing)


if __name__ == "__main__":
    unittest.main()
