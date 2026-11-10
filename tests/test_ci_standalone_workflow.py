"""Contract tests for the standalone and offline CI legs (.github/workflows/ci.yml).

The standalone-no-hermes job (#73) proves the bot installs from
the runtime dependencies alone (`pip install .`) and passes `bot check` with no Hermes module loaded.
Discord was its only platform for a long time, but Telegram and Matrix
(#164 stages 2-3) are standalone platforms too and stdlib-only by design:
these tests pin a clean-venv `bot check` step for each of them, so a stray
non-runtime import in their paths fails CI instead of a real install.

The offline-tests job pins two smaller things: jq is installed, because
bin/moonrakerctl is a shipped CLI that parses Moonraker JSON with it and
tests/pipeline/test_shell_cli.py skips without it; and pytest runs with
-rs, because a plain -q run reports a skip count with no names or reasons,
which made a 76-skip CI run impossible to attribute.

Coverage upload is report-only: only the Python 3.12 offline legs are
instrumented (the 3.14 legs stay a timing cross-check and no timed job is
instrumented), the Codecov step can never fail CI, and codecov.yml keeps
project coverage off with patch coverage informational only, so the signal
can inform review without ever gating a merge.
"""

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def load_workflow():
    return yaml.load(WORKFLOW_PATH.read_text(), Loader=yaml.BaseLoader)


def steps_by_name(job):
    return {step.get("name"): step for step in job["steps"]}


class StandaloneWorkflowContract(unittest.TestCase):
    def test_job_builds_one_clean_venv_from_runtime_dependencies_only(self):
        job = load_workflow()["jobs"]["standalone-no-hermes"]
        runs = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn('python -m venv "/tmp/clean"', runs)
        self.assertIn('"/tmp/clean/bin/python" -m pip install .', runs)
        self.assertNotIn("[test]", runs)

    def test_installed_wheel_is_checked_from_outside_the_checkout_and_the_gap_is_labeled(self):
        """The other steps run from the checkout and so test the checkout. This one runs where the checkout's
        print_handy cannot be imported. While the wheel lacks config/ and plugins/print-feedback it cannot
        pass, so it is non-blocking and named a known gap; when packaging is fixed, drop continue-on-error
        (and this assertion) so it blocks."""
        steps = steps_by_name(load_workflow()["jobs"]["standalone-no-hermes"])
        step = steps["Installed wheel check from outside the checkout (known gap, not blocking)"]
        self.assertEqual(step["continue-on-error"], "true")
        self.assertIn("cd /tmp/outside", step["run"])
        self.assertIn('"/tmp/clean/bin/python" -m print_handy.bot check', step["run"])
        self.assertIn("'/tmp/clean/' in print_handy.__file__", step["run"])
        names = [s.get("name") for s in load_workflow()["jobs"]["standalone-no-hermes"]["steps"]]
        self.assertIn("Say plainly that the wheel check failed", names)

    def test_bot_check_runs_for_discord(self):
        step = steps_by_name(load_workflow()["jobs"]["standalone-no-hermes"])[
            "Standalone bot setup check"]
        self.assertEqual(step["env"]["PRINT_HANDY__BOT__MODE"], "standalone")
        self.assertNotIn("PRINT_HANDY__BOT__PLATFORM", step["env"])
        self.assertIn("DISCORD_BOT_TOKEN", step["env"])
        self.assertIn('"/tmp/clean/bin/python" -m print_handy.bot check', step["run"])

    def test_bot_check_runs_for_telegram(self):
        step = steps_by_name(load_workflow()["jobs"]["standalone-no-hermes"])[
            "Standalone bot setup check (Telegram)"]
        self.assertEqual(step["env"]["PRINT_HANDY__BOT__MODE"], "standalone")
        self.assertEqual(step["env"]["PRINT_HANDY__BOT__PLATFORM"], "telegram")
        # [telegram] token_env defaults to TELEGRAM_BOT_TOKEN, the chat ID is
        # a signed group ID and allowed users are numeric Telegram IDs.
        self.assertIn("TELEGRAM_BOT_TOKEN", step["env"])
        self.assertRegex(step["env"]["PRINT_HANDY__TELEGRAM__CHAT_ID"], r"^-\d+$")
        self.assertRegex(step["env"]["PRINT_HANDY__TELEGRAM__ALLOWED_USER_IDS"], r"^\d+$")
        self.assertIn('"/tmp/clean/bin/python" -m print_handy.bot check', step["run"])

    def test_bot_check_runs_for_matrix(self):
        step = steps_by_name(load_workflow()["jobs"]["standalone-no-hermes"])[
            "Standalone bot setup check (Matrix)"]
        self.assertEqual(step["env"]["PRINT_HANDY__BOT__MODE"], "standalone")
        self.assertEqual(step["env"]["PRINT_HANDY__BOT__PLATFORM"], "matrix")
        # [matrix] access_token_env defaults to MATRIX_ACCESS_TOKEN, the
        # room ID starts with "!" and allowed users are full @user:server IDs.
        self.assertIn("MATRIX_ACCESS_TOKEN", step["env"])
        self.assertTrue(
            step["env"]["PRINT_HANDY__MATRIX__HOMESERVER_URL"].startswith("https://"))
        self.assertTrue(step["env"]["PRINT_HANDY__MATRIX__ROOM_ID"].startswith("!"))
        self.assertRegex(step["env"]["PRINT_HANDY__MATRIX__ALLOWED_USER_IDS"],
                         r"^@[^:]+:[^:]+$")
        self.assertIn('"/tmp/clean/bin/python" -m print_handy.bot check', step["run"])


class OfflineTestsWorkflowContract(unittest.TestCase):
    def test_jq_is_installed_so_the_shell_cli_tests_execute(self):
        job = load_workflow()["jobs"]["offline-tests"]
        runs = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertIn("apt-get install -y -q jq", runs)

    def test_skip_reasons_are_reported(self):
        job = load_workflow()["jobs"]["offline-tests"]
        runs = [step.get("run", "") for step in job["steps"]]
        offline = next(run for run in runs if "pytest" in run)
        self.assertIn("-rs", offline)

    def test_shards_cover_every_python_and_report_the_required_names(self):
        jobs = load_workflow()["jobs"]
        self.assertEqual(jobs["offline-tests"]["strategy"]["matrix"]["shard"], ["0", "1", "2"])
        result = jobs["offline-tests-result"]
        self.assertEqual(result["needs"], "offline-tests")
        self.assertEqual(result["name"], "offline-tests (${{ matrix.python-version }})")
        self.assertEqual(result["strategy"]["matrix"]["python-version"],
                         jobs["offline-tests"]["strategy"]["matrix"]["python-version"])


class CodecovReportOnlyContract(unittest.TestCase):
    def test_only_python312_offline_legs_are_instrumented(self):
        job = load_workflow()["jobs"]["offline-tests"]
        runs = [step.get("run", "") for step in job["steps"]]
        offline = next(run for run in runs if "pytest" in run)
        self.assertIn("matrix.python-version == '3.12'", offline)
        self.assertIn("--cov=print_handy", offline)
        self.assertIn("--cov-report=xml:coverage.xml", offline)

    def test_instrumented_legs_use_the_sysmon_coverage_core(self):
        # The default C tracer more than tripled the million-triangle audit
        # test locally and pushed it past its 120 s budget in CI; sysmon
        # (sys.monitoring) measured within ~10% of uninstrumented.
        job = load_workflow()["jobs"]["offline-tests"]
        step = next(step for step in job["steps"]
                    if "pytest" in step.get("run", ""))
        self.assertIn("sysmon", step["env"]["COVERAGE_CORE"])
        self.assertIn("matrix.python-version == '3.12'", step["env"]["COVERAGE_CORE"])

    def test_upload_dependencies_are_installed_for_python312_legs(self):
        # The Codecov action's pre-flight dependency check (curl, gpg) is not
        # covered by fail_ci_if_error, and the slim container ships neither.
        job = load_workflow()["jobs"]["offline-tests"]
        step = steps_by_name(job)["Install Codecov upload dependencies (3.12 legs only)"]
        self.assertIn("3.12", step["if"])
        self.assertIn("curl", step["run"])
        self.assertIn("gnupg", step["run"])

    def test_upload_step_is_report_only(self):
        job = load_workflow()["jobs"]["offline-tests"]
        step = steps_by_name(job)["Upload coverage to Codecov (report-only)"]
        self.assertTrue(step["uses"].startswith("codecov/codecov-action@"))
        self.assertIn("3.12", step["if"])
        self.assertEqual(step["with"]["fail_ci_if_error"], "false")
        self.assertEqual(step["with"]["flags"], "offline-tests")

    def test_codecov_config_never_gates(self):
        config = yaml.load((ROOT / "codecov.yml").read_text(), Loader=yaml.BaseLoader)
        status = config["coverage"]["status"]
        self.assertEqual(status["project"], "off")
        self.assertEqual(status["patch"]["default"]["informational"], "true")


if __name__ == "__main__":
    unittest.main()
