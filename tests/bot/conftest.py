"""Bot tests: a fake discord.py client, no token, no network.

The bot switches a process global on (the plugin's Stop sender). Each test gets
it back off.
"""
import copy

import pytest

from print_handy.core import prepared_intake
from print_handy.runtime import _config


@pytest.fixture(autouse=True)
def _restore_bot_globals(monkeypatch):
    monkeypatch.setattr(prepared_intake, "STOP_SENDER", None)
    yield


@pytest.fixture
def cfg(tmp_path):
    data = copy.deepcopy(_config.cfg())
    data["bot"]["mode"] = "standalone"
    data["discord"].update(chat_id="100000000000000001", allowed_user_ids="200000000000000002",
                           approval_user_id="")
    data["paths"]["cache"] = tmp_path / "cache"
    data["llm"].update(chat_url="", model="")
    return data
