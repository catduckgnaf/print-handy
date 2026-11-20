"""The reference Cloudflare Workers in examples/cloudflare stay tested.

The Workers' own tests use Node's built-in runner with a mock AI binding
(no npm install, no network). This test runs them when `node` is on PATH
and skips otherwise.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "cloudflare"


def test_workers_have_wrangler_config_without_account_id():
    for worker in ("vision-verdict",):
        config = (EXAMPLES / worker / "wrangler.toml").read_text()
        assert 'main = "src/index.js"' in config
        assert "[ai]" in config and 'binding = "AI"' in config
        assert not re.search(r"^\s*account_id\s*=", config, re.M)
        assert re.search(r'^compatibility_date = "\d{4}-\d{2}-\d{2}"$', config, re.M)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_worker_node_tests_pass():
    files = sorted(str(p.relative_to(ROOT)) for p in EXAMPLES.glob("*/test/*.test.mjs"))
    assert len(files) == 1, files
    result = subprocess.run(["node", "--test", *files], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    # The summary line's format depends on the host's Node version: TAP
    # ("# fail 0") on older Node, the spec reporter ("ℹ fail 0") on newer
    # Node (#971: a local run outside the container must not fail on the
    # host's Node version when the worker tests themselves pass).
    assert re.search(r"^(?:#|ℹ) fail 0$", result.stdout, re.M), result.stdout[-2000:]
