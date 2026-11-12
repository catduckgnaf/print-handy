"""The #908 agent-lane identity check (#1332, from #1146).

The pre-release review could not find the "agent lane identity layer"
#908 describes. It is gone by decision: #908 rescoped #902 (owner
approval, 2026-11-05) and removed ``print_handy/bot/agent_identity.py``,
the per-user endpoints and keys and the Hermes profile parsing, because
the container cannot see Hermes profile configs. What shipped instead is
the AI lane gate: ``ai.allowed_users`` and ``ai.edit_requests_per_hour``
(tests/test_edit_dispatch.py covers it through ``edit_chat``). These
tests keep the removed layer from coming back unnoticed.
"""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANE_MODULES = ("print_handy/core/edit_plan.py", "print_handy/bot/edit_chat.py",
                "print_handy/core/ai_fix.py")


def test_the_identity_module_stays_removed():
    assert importlib.util.find_spec("print_handy.bot.agent_identity") is None


def test_the_ai_lane_reads_no_hermes_profile_state():
    pattern = re.compile(r"hermes_home|platform_toolsets|profiles/|/p/|agent_identity")
    hits = [f"{rel}:{n}" for rel in LANE_MODULES
            for n, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1)
            if pattern.search(line)]
    assert hits == []


def test_the_shipped_gate_is_what_the_lane_calls():
    from print_handy.core import edit_plan

    calls = {}
    assert edit_plan.ai_lane_allowed("42", {"allowed_users": ["7"]}, calls, 0) == (False, "not on the AI allowlist")
    ai = {"allowed_users": [], "edit_requests_per_hour": 1}
    assert edit_plan.ai_lane_allowed("42", ai, calls, 0) == (True, "")
    assert edit_plan.ai_lane_allowed("42", ai, calls, 1) == (False, "AI edit rate limit reached")
    source = (ROOT / "print_handy/bot/edit_chat.py").read_text(encoding="utf-8")
    assert "edit_plan.ai_lane_allowed(" in source
