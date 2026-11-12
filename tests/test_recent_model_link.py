"""Offline tests for "print it" recent-link recall (#524).

Print Handy's own per-channel recent-links file is the single source in
every mode: the Hermes state.db messages scan is gone, so a link sitting in
state.db — even one the old fallback would have matched — is never recalled.
No Discord, no network.
"""
import importlib.util
import sqlite3
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from print_handy.bot import recent_links
from print_handy.runtime import _config

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "print_feedback_recent_links", ROOT / "plugins/print-feedback/__init__.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)

OWN_URL = "https://www.printables.com/model/111-own-file-widget"
DB_URL = "https://makerworld.com/en/models/222222-state-db-widget"


@pytest.fixture()
def home(tmp_path):
    previous = _config.cfg()
    _config.set_override({"paths": {"state_home": tmp_path}})
    yield tmp_path
    _config.set_override(previous)


def _source(chat_id="chan", thread_id=None):
    return S(chat_id=chat_id, thread_id=thread_id)


def _seed_state_db(home):
    conn = sqlite3.connect(home / "state.db")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, role TEXT, content TEXT)")
    conn.execute("INSERT INTO messages (role, content) VALUES ('user', ?)",
                 (f"print this later: {DB_URL}",))
    conn.execute("INSERT INTO messages (role, content) VALUES ('assistant', ?)",
                 (f"here it is: {DB_URL}",))
    conn.commit()
    conn.close()


def test_own_file_hit_resolves(home):
    recent_links.remember("chan", OWN_URL)
    assert plugin._recent_model_link(_source()) == OWN_URL


def test_own_file_hit_is_scoped_to_the_channel(home):
    recent_links.remember("other-chan", OWN_URL)
    assert plugin._recent_model_link(_source()) is None


def test_own_file_miss_returns_none_even_when_state_db_has_a_link(home):
    _seed_state_db(home)
    assert (home / "state.db").exists()
    assert plugin._recent_model_link(_source()) is None


def test_own_file_wins_and_state_db_is_not_needed(home):
    _seed_state_db(home)
    recent_links.remember("chan", OWN_URL)
    assert plugin._recent_model_link(_source()) == OWN_URL


def test_state_db_helper_is_removed(home):
    assert not hasattr(plugin, "_recent_model_link_from_db")
