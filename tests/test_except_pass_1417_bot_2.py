import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "print_handy/bot/health.py",
    "print_handy/bot/llm.py",
    "print_handy/bot/outage_alert.py",
    "print_handy/bot/recent_links.py",
    "print_handy/bot/rest.py",
    "print_handy/bot/runtime.py",
    "print_handy/bot/telegram.py",
    "print_handy/bot/web_feed.py",
    "print_handy/live_activity.py",
    "print_handy/live_stream.py",
    "print_handy/listeners.py",
)


@pytest.mark.parametrize("module", MODULES)
def test_assigned_module_has_no_silent_except_pass(module):
    tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
    silent = [node.lineno for node in ast.walk(tree)
              if isinstance(node, ast.ExceptHandler)
              and len(node.body) == 1
              and isinstance(node.body[0], ast.Pass)]
    assert not silent, f"{module} contains silent except-pass at {silent}"


def test_unparseable_live_stream_url_has_no_parser_exception_context():
    from print_handy.live_stream import LiveStreamError, _split_url

    with pytest.raises(LiveStreamError) as caught:
        _split_url("http://[invalid")

    assert caught.value.__cause__ is None
    assert "secret" not in str(caught.value.__context__)
