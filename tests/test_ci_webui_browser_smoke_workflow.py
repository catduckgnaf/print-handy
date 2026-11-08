"""CI contract: the browser tests must run, not silently skip (72-hour audit)."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_real_browser_job_is_a_blocking_pr_check():
    workflow = yaml.load((ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["webui-browser-smoke"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job.get("continue-on-error") not in (True, "true")
    steps = {step.get("name"): step for step in job["steps"]}
    assert "playwright install --with-deps chromium" in steps["Install the headless Chromium browser"]["run"]
    test = steps["Run real-browser tests and record machine-readable results"]
    assert test["env"]["PRINT_HANDY_BROWSER_SMOKE"] == "1"
    assert "tests/test_webui_browser_smoke.py" in test["run"]
    assert "--junitxml=browser-smoke.xml" in test["run"]
    refusal = steps["Refuse missing or skipped browser tests"]["run"]
    assert "assert tests >= 5 and not skipped and not failed" in refusal
