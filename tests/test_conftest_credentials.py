"""#1428: the suite never inherits a credential from the machine running it."""
import os
import re
from pathlib import Path

from tests import conftest

ROOT = Path(__file__).resolve().parents[1] / "print_handy"
_CREDENTIAL = re.compile(r'"([A-Z][A-Z0-9_]*(?:API_KEY|_KEY|TOKEN|PASSWORD|SECRET))"')


def test_no_credential_reaches_the_tests():
    leaked = [name for name in conftest.CREDENTIAL_ENV if os.environ.get(name)]
    assert leaked == [], f"inherited credentials: {leaked}"


def test_the_scrub_list_covers_every_credential_the_code_reads():
    named = set()
    for path in ROOT.rglob("*.py"):
        named.update(_CREDENTIAL.findall(path.read_text(encoding="utf-8")))
    missing = sorted(named - set(conftest.CREDENTIAL_ENV))
    assert missing == [], f"add to tests/conftest.py CREDENTIAL_ENV: {missing}"


def test_local_detector_answers_although_the_host_exports_a_vision_key():
    """The #1428 failure, end to end: run the two streak tests in a child pytest
    whose environment carries CLIPROXY_API_KEY, as the failing host's did."""
    import subprocess
    import sys
    env = dict(os.environ, CLIPROXY_API_KEY="host-exported-key")
    tests = ["tests/runtime/test_local_vision.py::test_listener_verdict_threads_its_streak",
             "tests/runtime/test_local_vision.py::test_bambu_monitor_verdict_threads_its_streak"]
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
                         cwd=ROOT.parent, env=env, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout[-2000:]
