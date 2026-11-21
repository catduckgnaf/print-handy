"""#1428: the suite never inherits a credential from the machine running it."""
import ast
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "print_handy"
CONFTEST = Path(__file__).resolve().parent / "conftest.py"
_CREDENTIAL = re.compile(r'"([A-Z][A-Z0-9_]*(?:API_KEY|_KEY|TOKEN|PASSWORD|SECRET))"')


def _credential_env():
    """CREDENTIAL_ENV read from tests/conftest.py's source.

    Never ``import`` the conftest: pytest has already loaded it as ``conftest``,
    and a second import runs it again, which makes a new temp state folder and
    swaps the process settings mid-run (anything built from the first, such as
    print_control's control lock, then points at the old folder).
    """
    for node in ast.parse(CONFTEST.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "CREDENTIAL_ENV" for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    raise AssertionError("tests/conftest.py defines no CREDENTIAL_ENV")


def test_no_credential_reaches_the_tests():
    leaked = [name for name in _credential_env() if os.environ.get(name)]
    assert leaked == [], f"inherited credentials: {leaked}"


def test_the_scrub_list_covers_every_credential_the_code_reads():
    named = set()
    for path in ROOT.rglob("*.py"):
        named.update(_CREDENTIAL.findall(path.read_text(encoding="utf-8")))
    missing = sorted(named - set(_credential_env()))
    assert missing == [], f"add to tests/conftest.py CREDENTIAL_ENV: {missing}"


def test_local_detector_answers_although_the_host_exports_a_vision_key():
    """The #1428 failure, end to end: run the two streak tests in a child pytest
    whose environment carries LLM_API_KEY (the one endpoint's key, #1731), as
    the failing host's did."""
    import subprocess
    import sys
    env = dict(os.environ, LLM_API_KEY="host-exported-key")
    # The child runs exactly these two tests: drop CI's shard spec, which would
    # otherwise deal them to another shard and deselect them both (exit 5).
    env.pop("PRINT_HANDY_TEST_SHARD", None)
    tests = ["tests/runtime/test_local_vision.py::test_listener_verdict_threads_its_streak",
             "tests/runtime/test_local_vision.py::test_bambu_monitor_verdict_threads_its_streak"]
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
                         cwd=ROOT.parent, env=env, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-2000:]
