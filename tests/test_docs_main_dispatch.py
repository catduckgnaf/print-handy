"""Contracts for the main-to-documentation dispatch workflow."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/docs-main-dispatch.yml"


def test_docs_changes_on_main_dispatch_sync_and_fail_closed():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    triggers = workflow.get("on", workflow.get(True))
    push = triggers["push"]
    assert push["branches"] == ["main"]
    assert "docs/**" in push["paths"]
    assert ".github/workflows/docs-main-dispatch.yml" in push["paths"]
    assert workflow["permissions"] == {}
    step = workflow["jobs"]["dispatch"]["steps"][0]
    assert step["env"]["GH_TOKEN"] == "${{ secrets.DOCS_SYNC_TOKEN }}"
    script = step["run"]
    assert "set -euo pipefail" in script
    assert "exit 1" in script
    assert 'event_type:"print-handy-main"' in script
    assert "repos/catduckgnaf/print-handy-docs/dispatches" in script
