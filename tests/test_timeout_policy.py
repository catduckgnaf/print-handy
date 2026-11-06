"""The per-test timeout policy: the pyproject.toml limit applies everywhere except tests/corpus (#687)."""
import pytest


def test_non_corpus_item_keeps_the_ini_timeout(request):
    # The corpus conftest hook sees every collected item; it must not mark this one.
    assert request.node.get_closest_marker("timeout") is None
    assert float(request.config.getini("timeout")) == 120


def test_ini_timeout_func_only_is_on(request):
    assert request.config.getini("timeout_func_only") is True
